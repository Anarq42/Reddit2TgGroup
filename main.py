#!/usr/bin/env python3
import os
import logging
import asyncio
import aiohttp
import json
import re
import html
from io import BytesIO
from typing import Optional, Callable, Awaitable

from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError, BadRequest, TimedOut

import asyncpraw
from bs4 import BeautifulSoup

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Reduce noisy library logging that may include tokens/URLs (avoid leaking token in logs)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# ---------- ENV VARS ----------
def get_env_var(name: str, cast=str):
    val = os.getenv(name)
    if val is None:
        raise ValueError(f"Missing required environment variable: {name}")
    try:
        return cast(val)
    except Exception:
        raise ValueError(f"Invalid value for {name}: {val}")

TELEGRAM_TOKEN = get_env_var("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = get_env_var("TELEGRAM_GROUP_ID", int)
TELEGRAM_ERROR_TOPIC_ID = get_env_var("TELEGRAM_ERROR_TOPIC_ID", int)
REDDIT_CLIENT_ID = get_env_var("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = get_env_var("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = get_env_var("REDDIT_USERNAME")
REDDIT_PASSWORD = get_env_var("REDDIT_PASSWORD")

SUBREDDITS_DB_PATH = "subreddits.db"
POSTED_IDS_PATH = "posted_ids.json"  # For tracking duplicates

# ---------- LOAD SUBREDDITS ----------
def load_subreddits_mapping(file_path):
    mapping = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) != 2:
                    continue
                subreddit_name, topic_id = parts
                mapping[subreddit_name.strip().lower()] = int(topic_id.strip())
    except FileNotFoundError:
        logging.warning(f"{file_path} not found. Starting with empty mapping.")
    except Exception:
        logging.exception("Failed to load subreddit mapping")
    return mapping

subreddit_map = load_subreddits_mapping(SUBREDDITS_DB_PATH)

# ---------- TRACK POSTED IDS ----------
def load_posted_ids():
    try:
        with open(POSTED_IDS_PATH, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
            logging.warning("posted_ids file has unexpected format, starting fresh.")
            return set()
    except FileNotFoundError:
        return set()
    except Exception as e:
        logging.warning(f"Failed loading posted ids: {e}")
        return set()

def save_posted_ids(posted_ids):
    try:
        with open(POSTED_IDS_PATH, "w") as f:
            json.dump(list(posted_ids), f)
    except Exception as e:
        logging.error(f"Failed to save posted ids: {e}")

posted_ids = load_posted_ids()

# ---------- UTILITIES ----------
def prepare_caption(submission):
    author = submission.author.name if getattr(submission, "author", None) else "[deleted]"
    safe_title = html.escape(getattr(submission, "title", ""))
    safe_author = html.escape(author)
    safe_url = html.escape(getattr(submission, "url", ""))
    safe_subreddit = html.escape(submission.subreddit.display_name)
    return (
        f"<b>{safe_title}</b>\n"
        f"Posted by u/{safe_author} in r/{safe_subreddit}\n"
        f"<a href='{safe_url}'>Reddit Link</a>"
    )

async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> Optional[BytesIO]:
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            bio = BytesIO(data)
            bio.seek(0)
            bio.name = os.path.basename(url.split("?")[0]) or "file"
            return bio
    except Exception as e:
        logging.warning(f"Failed to fetch {url}: {e}")
        return None

# ---------- Gfycat/Redgifs MP4 ----------
async def get_gfy_redgifs_mp4(url):
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                text = await resp.text()
                soup = BeautifulSoup(text, "html.parser")
                mp4_tag = soup.find("source", {"type": "video/mp4"})
                if mp4_tag and mp4_tag.get("src"):
                    mp4_url = mp4_tag["src"]
                    if mp4_url.startswith("//"):
                        mp4_url = "https:" + mp4_url
                    return mp4_url
                match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', text)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        return data.get("gfyItem", {}).get("mp4Url")
                    except Exception:
                        return None
    except Exception as e:
        logging.warning(f"Failed to get mp4 from {url}: {e}")
    return None

# ---------- MEDIA HANDLING ----------
async def get_media_urls(submission):
    media_list = []
    try:
        # Reddit gallery
        if getattr(submission, "is_gallery", False):
            meta = getattr(submission, "media_metadata", None) or {}
            for m in meta.values():
                if m.get("e") == "Image" and isinstance(m.get("s"), dict) and m["s"].get("u"):
                    url = m["s"]["u"].replace("&amp;", "&")
                    media_list.append({"url": url, "type": "photo"})

        # Reddit video (check attribute types)
        elif getattr(submission, "media", None) and isinstance(submission.media, dict) and "reddit_video" in submission.media:
            reddit_video = submission.media.get("reddit_video", {})
            media_url = reddit_video.get("fallback_url")
            if media_url:
                media_list.append({"url": media_url, "type": "video"})

        # Direct links
        elif getattr(submission, "url", "").lower().endswith((".jpg", ".jpeg", ".png")):
            media_list.append({"url": submission.url, "type": "photo"})
        elif getattr(submission, "url", "").lower().endswith((".gif", ".mp4")):
            media_type = "gif" if submission.url.lower().endswith(".gif") else "video"
            media_list.append({"url": submission.url, "type": media_type})

        else:
            url_lower = getattr(submission, "url", "").lower()
            if "imgur.com" in url_lower and url_lower.endswith((".jpg", ".png", ".gif")):
                media_list.append({"url": submission.url, "type": "photo"})
            elif "gfycat.com" in url_lower or "redgifs.com" in url_lower:
                mp4_url = await get_gfy_redgifs_mp4(submission.url)
                if mp4_url:
                    media_list.append({"url": mp4_url, "type": "video"})
    except Exception as e:
        logging.warning(f"Failed to get media URLs for {getattr(submission, 'id', '?')}: {e}")
    return media_list

# ---------- SAFE SEND helpers ----------
async def _safe_send(primary_fn: Callable[[], Awaitable], fallback_fn: Optional[Callable[[], Awaitable]] = None, retries: int = 3):
    """
    primary_fn: callable that returns coroutine to perform primary send (not called until used)
    fallback_fn: optional callable to perform fallback send (called only if needed)
    retries: number of retries for transient timeouts
    """
    last_exc = None
    for attempt in range(retries):
        try:
            return await primary_fn()
        except BadRequest as e:
            msg = str(e)
            # Topic closed handling (explicit)
            if "Topic_closed" in msg or "topic is closed" in msg or "topic_closed" in msg.lower():
                logging.warning("Topic closed when sending message; retrying without thread id")
                if fallback_fn is not None:
                    try:
                        return await fallback_fn()
                    except Exception:
                        logging.exception("Retry without thread id failed")
                        raise
            # Chat not found handling: bubble up so caller can mark chat invalid if needed
            if "Chat not found" in msg:
                logging.warning("Chat not found while sending message")
                raise
            # Other BadRequest re-raise
            raise
        except TimedOut as e:
            last_exc = e
            logging.warning("TimedOut while sending message, attempt %s/%s", attempt + 1, retries)
            await asyncio.sleep(1 * (2 ** attempt))
            continue
        except Exception as e:
            # For non-transient errors, propagate
            logging.exception("Unexpected error while sending message")
            raise
    # If we exit loop due to retries, raise the last exception
    if last_exc:
        raise last_exc

# ---------- SEND MEDIA ----------
async def send_media(submission, media_list, topic_id, bot):
    caption = prepare_caption(submission)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            if media_list:
                if len(media_list) > 1:
                    tg_media = []
                    # fetch bytes for top N media concurrently
                    media_bytes = await asyncio.gather(*(fetch_bytes(session, m["url"]) for m in media_list), return_exceptions=True)
                    for i, media in enumerate(media_list[:10]):
                        bio = media_bytes[i]
                        if isinstance(bio, Exception) or bio is None:
                            continue
                        kwargs = {"caption": caption if i == 0 else None, "parse_mode": ParseMode.HTML}
                        if media["type"] == "photo":
                            tg_media.append(InputMediaPhoto(media=bio, **kwargs))
                        elif media["type"] in ["video", "gif"]:
                            tg_media.append(InputMediaVideo(media=bio, **kwargs))
                    if tg_media:
                        await _safe_send(
                            primary_fn=lambda: bot.send_media_group(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, media=tg_media),
                            fallback_fn=lambda: bot.send_media_group(chat_id=TELEGRAM_GROUP_ID, media=tg_media),
                        )
                else:
                    media = media_list[0]
                    bio = await fetch_bytes(session, media["url"])
                    if bio is None:
                        # fallback to text-only post
                        await _safe_send(
                            primary_fn=lambda: bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, text=caption, parse_mode=ParseMode.HTML),
                            fallback_fn=lambda: bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=caption, parse_mode=ParseMode.HTML),
                        )
                        return True
                    if media["type"] == "photo":
                        await _safe_send(
                            primary_fn=lambda: bot.send_photo(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, photo=bio, caption=caption, parse_mode=ParseMode.HTML),
                            fallback_fn=lambda: bot.send_photo(chat_id=TELEGRAM_GROUP_ID, photo=bio, caption=caption, parse_mode=ParseMode.HTML),
                        )
                    elif media["type"] == "video":
                        await _safe_send(
                            primary_fn=lambda: bot.send_video(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, video=bio, caption=caption, parse_mode=ParseMode.HTML),
                            fallback_fn=lambda: bot.send_video(chat_id=TELEGRAM_GROUP_ID, video=bio, caption=caption, parse_mode=ParseMode.HTML),
                        )
                    elif media["type"] == "gif":
                        await _safe_send(
                            primary_fn=lambda: bot.send_animation(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, animation=bio, caption=caption, parse_mode=ParseMode.HTML),
                            fallback_fn=lambda: bot.send_animation(chat_id=TELEGRAM_GROUP_ID, animation=bio, caption=caption, parse_mode=ParseMode.HTML),
                        )
            else:
                await _safe_send(
                    primary_fn=lambda: bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, text=caption, parse_mode=ParseMode.HTML),
                    fallback_fn=lambda: bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=caption, parse_mode=ParseMode.HTML),
                )

            logging.info("Post sent: %s to topic %s", submission.title, topic_id)
            return True
        except TelegramError as e:
            logging.error("Error sending post: %s - %s", submission.title, e)
            # Try to notify error topic; if that fails log and continue
            try:
                await _safe_send(
                    primary_fn=lambda: bot.send_message(chat_id=TELEGRAM_ERROR_TOPIC_ID, text=f"Error sending post: {submission.title}\n{str(e)[:400]}"),
                    fallback_fn=lambda: bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=f"Error sending post: {submission.title}\n{str(e)[:400]}"),
                )
            except Exception:
                logging.exception("Failed to send error message to error topic")
            return False
        except Exception:
            logging.exception("Unexpected error while sending media")
            return False

# ---------- PROCESS REDDIT LINK ----------
async def send_reddit_link(url, bot, reddit_client, posted_ids_lock):
    try:
        submission = await reddit_client.submission(url=url)
        topic_id = subreddit_map.get(submission.subreddit.display_name.lower(), TELEGRAM_ERROR_TOPIC_ID)
        media_list = await get_media_urls(submission)
        ok = await send_media(submission, media_list, topic_id, bot)
        if ok:
            async with posted_ids_lock:
                if submission.id not in posted_ids:
                    posted_ids.add(submission.id)
                    save_posted_ids(posted_ids)
        return f"Processed Reddit post: {submission.title}"
    except Exception as e:
        logging.warning("Failed to process Reddit URL %s: %s", url, e)
        return f"Error processing Reddit URL: {e}"

# ---------- TELEGRAM COMMAND ----------
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_obj = update.effective_message
    if msg_obj is None:
        logging.warning("Received /post with no effective message")
        return
    if not context.args:
        try:
            await msg_obj.reply_text("Usage: /post <reddit_url>")
        except Exception:
            logging.exception("Failed to send usage message")
        return
    url = context.args[0]
    reddit_client = context.application.bot_data.get("reddit_client")
    posted_ids_lock = context.application.bot_data.get("posted_ids_lock")
    if reddit_client is None or posted_ids_lock is None:
        try:
            await msg_obj.reply_text("Reddit client not ready; try again in a moment.")
        except Exception:
            logging.exception("Failed to notify user reddit client not ready")
        return
    result_msg = await send_reddit_link(url, context.bot, reddit_client, posted_ids_lock)
    try:
        await msg_obj.reply_text(result_msg)
    except BadRequest as e:
        logging.warning("Failed to send reply to user: %s", e)
    except Exception:
        logging.exception("Unexpected error replying to user")

# ---------- STREAM SUBREDDITS ----------
async def stream_subreddits(reddit_client, bot, posted_ids_lock):
    if not subreddit_map:
        logging.warning("No subreddits configured. Skipping stream.")
        return

    subreddit_names = "+".join(subreddit_map.keys())
    try:
        maybe_subreddit = reddit_client.subreddit(subreddit_names)
        if asyncio.iscoroutine(maybe_subreddit):
            subreddit = await maybe_subreddit
        else:
            subreddit = maybe_subreddit
    except Exception as e:
        logging.error("Failed to access subreddits: %s", e)
        return

    logging.info("Starting subreddit stream: %s", subreddit_names)

    backoff = 1
    while True:
        try:
            async for submission in subreddit.stream.submissions(skip_existing=True):
                logging.info("New submission: %s (r/%s)", submission.title, submission.subreddit.display_name)
                topic_id = subreddit_map.get(submission.subreddit.display_name.lower(), TELEGRAM_ERROR_TOPIC_ID)
                try:
                    media_list = await get_media_urls(submission)
                    ok = await send_media(submission, media_list, topic_id, bot)
                    if ok:
                        async with posted_ids_lock:
                            if submission.id not in posted_ids:
                                posted_ids.add(submission.id)
                                save_posted_ids(posted_ids)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("Failed to send submission %s", submission.title)
                    try:
                        await _safe_send(
                            primary_fn=lambda: bot.send_message(chat_id=TELEGRAM_ERROR_TOPIC_ID, text=f"Error sending submission: {submission.title}"),
                            fallback_fn=lambda: bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=f"Error sending submission: {submission.title}"),
                        )
                    except Exception:
                        logging.exception("Failed to report send error")
            break
        except asyncio.CancelledError:
            logging.info("stream_subreddits cancelled, exiting")
            break
        except Exception as e:
            logging.exception("Stream error: %s; backing off for %s s", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# ---------- STARTUP / SHUTDOWN HOOKS ----------
async def _on_startup(context: ContextTypes.DEFAULT_TYPE):
    """
    Create the reddit client and start stream as a background task.
    """
    logging.info("Running startup job: creating reddit client and starting stream")
    reddit_client = asyncpraw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent="TelegramRedditBot/1.0",
    )
    context.application.bot_data["reddit_client"] = reddit_client
    context.application.bot_data["posted_ids_lock"] = asyncio.Lock()
    # attach background task to application so it shuts down with the app
    context.application.create_task(stream_subreddits(reddit_client, context.application.bot, context.application.bot_data["posted_ids_lock"]))

async def _on_shutdown(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Running shutdown job: closing reddit client if present")
    reddit_client = context.application.bot_data.get("reddit_client")
    if reddit_client:
        try:
            await reddit_client.close()
        except Exception:
            logging.exception("Error closing reddit client")

# ---------- GLOBAL ERROR HANDLER ----------
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Centralized error handler to log unhandled exceptions and notify admin/topic if configured.
    """
    logging.exception("Unhandled exception: %s", context.error)
    # Try to notify the error topic but avoid raising further
    try:
        # context.bot may not exist in some contexts, guard it
        bot = getattr(context, "bot", None) or context.application.bot
        await _safe_send(
            primary_fn=lambda: bot.send_message(chat_id=TELEGRAM_ERROR_TOPIC_ID, text=f"Unhandled exception: {context.error}"),
            fallback_fn=lambda: bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=f"Unhandled exception: {context.error}"),
        )
    except Exception:
        logging.exception("Failed to notify error topic about unhandled exception")

# ---------- MAIN (entrypoint) ----------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # register handlers
    app.add_handler(CommandHandler("post", post_command))

    # register global error handler (PTB v20+)
    try:
        app.add_error_handler(global_error_handler)
    except Exception:
        # older versions differ; attempt to set via dispatcher
        try:
            app.bot_data.setdefault("error_handler", global_error_handler)
        except Exception:
            logging.exception("Failed to register global error handler")

    # schedule startup job
    app.job_queue.run_once(_on_startup, when=0)

    # attempt to register shutdown hook if available
    post_shutdown_attr = getattr(app, "post_shutdown", None)
    if callable(post_shutdown_attr):
        try:
            post_shutdown_attr(_on_shutdown)
        except Exception:
            logging.debug("post_shutdown exists but couldn't be called; shutdown will be best-effort")

    logging.info("Bot started, monitoring subreddits: %s", ", ".join(subreddit_map.keys()) or "(none configured)")

    # Run polling by default. If you intend to use webhooks, change this to run_webhook and ensure only one instance handles webhooks.
    app.run_polling()

if __name__ == "__main__":
    main()
