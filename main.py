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

from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import TelegramError, BadRequest, TimedOut

import asyncpraw
from bs4 import BeautifulSoup

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# ---------- ENV VARS ----------
def get_env_var(name: str, cast=str, default=None):
    val = os.getenv(name)
    if val is None:
        if default is not None:
            return default
        raise ValueError(f"Missing required environment variable: {name}")
    try:
        return cast(val)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid value for {name}: {val}")

TELEGRAM_TOKEN = get_env_var("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = get_env_var("TELEGRAM_GROUP_ID", int)
TELEGRAM_ERROR_TOPIC_ID = get_env_var("TELEGRAM_ERROR_TOPIC_ID", int)
TELEGRAM_ADMIN_ID = get_env_var("TELEGRAM_ADMIN_ID", int)
REDDIT_CLIENT_ID = get_env_var("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = get_env_var("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = get_env_var("REDDIT_USERNAME")
REDDIT_PASSWORD = get_env_var("REDDIT_PASSWORD")

SUBREDDITS_DB_PATH = "subreddits.db"
POSTED_IDS_PATH = "posted_ids.json"

# ---------- SUBREDDITS MAPPING ----------
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
                    logging.warning(f"Skipping malformed line in {file_path}: {line}")
                    continue
                subreddit_name, topic_id = parts
                mapping[subreddit_name.strip().lower()] = int(topic_id.strip())
    except FileNotFoundError:
        logging.warning(f"{file_path} not found. Starting with empty mapping.")
    except Exception:
        logging.exception("Failed to load subreddit mapping")
    return mapping

# ---------- TRACK POSTED IDS ----------
def load_posted_ids():
    try:
        with open(POSTED_IDS_PATH, "r") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_posted_ids(posted_ids):
    try:
        with open(POSTED_IDS_PATH, "w") as f:
            json.dump(list(posted_ids), f)
    except Exception as e:
        logging.error(f"Failed to save posted ids: {e}")

# ---------- UTILITIES ----------
def prepare_caption(submission):
    author = submission.author.name if getattr(submission, "author", None) else "[deleted]"
    safe_title = html.escape(getattr(submission, "title", ""))
    safe_author = html.escape(author)
    safe_url = f"https://www.reddit.com{submission.permalink}"
    post_url = html.escape(getattr(submission, "url", ""))
    safe_subreddit = html.escape(submission.subreddit.display_name)
    return (
        f"<b>{safe_title}</b>\n\n"
        f"Posted by u/{safe_author} in r/{safe_subreddit}\n"
        f"<a href='{safe_url}'>Comments</a> | <a href='{post_url}'>Source</a>"
    )

async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> Optional[BytesIO]:
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            bio = BytesIO(data)
            bio.seek(0)
            bio.name = os.path.basename(url.split("?")[0]) or "file.dat"
            return bio
    except Exception as e:
        logging.warning(f"Failed to fetch {url}: {e}")
        return None

# ---------- Gfycat/Redgifs MP4 ----------
async def get_gfy_redgifs_mp4(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        async with session.get(url) as resp:
            text = await resp.text()
            soup = BeautifulSoup(text, "html.parser")
            # First try the standard video source tag
            mp4_tag = soup.find("source", {"type": "video/mp4", "src": True})
            if mp4_tag:
                return mp4_tag["src"]
            # Fallback for Redgifs' newer script-based data embedding
            script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
            if script_tag:
                data = json.loads(script_tag.string)
                return data.get("props", {}).get("pageProps", {}).get("gfy", {}).get("urls", {}).get("sd")
    except Exception as e:
        logging.warning(f"Failed to get mp4 from {url}: {e}")
    return None

# ---------- MEDIA HANDLING ----------
async def get_media_urls(submission, session):
    media_list = []
    try:
        # Reddit gallery
        if getattr(submission, "is_gallery", False) and hasattr(submission, "media_metadata"):
            for item in submission.gallery_data['items']:
                media_id = item['media_id']
                meta = submission.media_metadata[media_id]
                if meta['e'] == 'Image':
                    url = meta['s']['u'].replace("&amp;", "&")
                    media_list.append({"url": url, "type": "photo"})

        # Reddit video
        elif getattr(submission, "is_video", False) and hasattr(submission, "media") and submission.media and "reddit_video" in submission.media:
            media_url = submission.media["reddit_video"]["fallback_url"]
            media_list.append({"url": media_url, "type": "video"})

        else: # Direct links and external sites
            url_lower = getattr(submission, "url", "").lower()
            if any(url_lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png"]):
                media_list.append({"url": submission.url, "type": "photo"})
            elif any(url_lower.endswith(ext) for ext in [".gif", ".mp4"]):
                media_type = "gif" if url_lower.endswith(".gif") else "video"
                media_list.append({"url": submission.url, "type": media_type})
            elif "gfycat.com" in url_lower or "redgifs.com" in url_lower:
                mp4_url = await get_gfy_redgifs_mp4(submission.url, session)
                if mp4_url:
                    media_list.append({"url": mp4_url, "type": "video"})

    except Exception as e:
        logging.warning(f"Failed to get media URLs for post {getattr(submission, 'id', '?')}: {e}")
    return media_list

# ---------- SAFE SEND HELPER ----------
async def _safe_send(primary_fn: Callable[[], Awaitable], fallback_fn: Optional[Callable[[], Awaitable]] = None, retries: int = 3):
    last_exc = None
    for attempt in range(retries):
        try:
            return await primary_fn()
        except BadRequest as e:
            msg = str(e).lower()
            if "topic_closed" in msg or "topic is closed" in msg:
                logging.warning("Topic closed, attempting to send to main group.")
                if fallback_fn:
                    return await fallback_fn()
                raise  # Re-raise if no fallback
            elif "chat not found" in msg:
                logging.error("Chat not found. Cannot send message.")
                raise # Propagate this critical error
            else:
                logging.error(f"BadRequest on send: {e}")
                raise # Re-raise other BadRequests
        except TimedOut:
            logging.warning(f"TimedOut sending message, attempt {attempt + 1}/{retries}. Retrying...")
            await asyncio.sleep(2 ** attempt)
            continue
        except Exception:
            logging.exception("Unexpected error in _safe_send")
            raise
    raise last_exc or Exception("Failed to send message after multiple retries.")

# ---------- SEND MEDIA (OPTIMIZED) ----------
async def send_media(submission, media_list, topic_id, bot):
    caption = prepare_caption(submission)
    send_params = {
        "chat_id": TELEGRAM_GROUP_ID,
        "message_thread_id": topic_id,
        "caption": caption,
        "parse_mode": ParseMode.HTML
    }
    fallback_params = {k: v for k, v in send_params.items() if k != "message_thread_id"}

    try:
        if not media_list:
            await _safe_send(
                primary_fn=lambda: bot.send_message(text=caption, **send_params),
                fallback_fn=lambda: bot.send_message(text=caption, **fallback_params)
            )
        elif len(media_list) > 1:
            async with aiohttp.ClientSession() as session:
                media_bytes_tasks = [fetch_bytes(session, m["url"]) for m in media_list[:10]]
                media_bytes_results = await asyncio.gather(*media_bytes_tasks)
                
                tg_media = []
                for i, bio in enumerate(media_bytes_results):
                    if bio:
                        media_type = media_list[i]["type"]
                        InputMediaClass = InputMediaPhoto if media_type == "photo" else InputMediaVideo
                        tg_media.append(InputMediaClass(media=bio, caption=caption if i == 0 else None, parse_mode=ParseMode.HTML))

                if tg_media:
                    await _safe_send(
                        primary_fn=lambda: bot.send_media_group(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, media=tg_media),
                        fallback_fn=lambda: bot.send_media_group(chat_id=TELEGRAM_GROUP_ID, media=tg_media)
                    )
        else: # Single media
            media = media_list[0]
            async with aiohttp.ClientSession() as session:
                bio = await fetch_bytes(session, media["url"])

            if bio is None: # Fallback to text if download fails
                await send_media(submission, [], topic_id, bot)
                return True

            send_map = {"photo": bot.send_photo, "video": bot.send_video, "gif": bot.send_animation}
            send_func = send_map.get(media["type"])

            if send_func:
                media_kwarg = "animation" if media["type"] == "gif" else media["type"]
                await _safe_send(
                    primary_fn=lambda: send_func(**{media_kwarg: bio}, **send_params),
                    fallback_fn=lambda: send_func(**{media_kwarg: bio}, **fallback_params)
                )

        logging.info("Post sent: %s to topic %s", submission.title, topic_id)
        return True
    except Exception as e:
        logging.error("Failed to send post %s: %s", submission.id, e)
        # Attempt to notify admin in error topic
        try:
            error_text = f"Failed to send post:\n<b>Title:</b> {html.escape(submission.title)}\n<b>Error:</b> {html.escape(str(e))}"
            await bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=TELEGRAM_ERROR_TOPIC_ID, text=error_text, parse_mode=ParseMode.HTML)
        except Exception:
            logging.exception("Could not send failure notice to error topic.")
        return False

# ---------- PROCESS REDDIT LINK ----------
async def process_submission(submission, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    app_data = context.application.bot_data
    
    async with app_data["posted_ids_lock"]:
        if submission.id in app_data["posted_ids"]:
            return # Skip duplicate

    topic_id = app_data["subreddit_map"].get(submission.subreddit.display_name.lower(), TELEGRAM_ERROR_TOPIC_ID)
    
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        media_list = await get_media_urls(submission, session)

    if await send_media(submission, media_list, topic_id, bot):
        async with app_data["posted_ids_lock"]:
            app_data["posted_ids"].add(submission.id)
            save_posted_ids(app_data["posted_ids"])

# ---------- TELEGRAM COMMANDS ----------
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: /post <reddit_url>")
        return
    
    url = context.args[0]
    reddit_client = context.application.bot_data.get("reddit_client")
    if not reddit_client:
        await update.effective_message.reply_text("Reddit client not ready. Please wait a moment.")
        return
        
    try:
        submission = await reddit_client.submission(url=url)
        await process_submission(submission, context)
        await update.effective_message.reply_text(f"Processed post: {submission.title}")
    except Exception as e:
        logging.error(f"Failed to process /post command for URL {url}: {e}")
        await update.effective_message.reply_text(f"Error processing URL: {e}")

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != TELEGRAM_ADMIN_ID:
        await update.effective_message.reply_text("You are not authorized to use this command.")
        return

    logging.info("Admin triggered /reload command.")
    await update.effective_message.reply_text("Reloading subreddits and restarting stream...")
    
    # Reload subreddit mapping
    context.application.bot_data["subreddit_map"] = load_subreddits_mapping(SUBREDDITS_DB_PATH)
    
    # Restart the background stream task
    await stop_and_restart_stream(context.application)
    
    new_subreddits = ", ".join(context.application.bot_data["subreddit_map"].keys())
    await update.effective_message.reply_text(f"Reload complete. Now monitoring: {new_subreddits}")


# ---------- STREAM SUBREDDITS ----------
async def stream_subreddits_task(app: Application):
    reddit = app.bot_data["reddit_client"]
    subreddit_map = app.bot_data["subreddit_map"]

    if not subreddit_map:
        logging.warning("No subreddits configured. Stream will not start.")
        return

    subreddit_names = "+".join(subreddit_map.keys())
    logging.info(f"Starting stream for subreddits: {subreddit_names}")
    
    try:
        subreddit = await reddit.subreddit(subreddit_names)
        async for submission in subreddit.stream.submissions(skip_existing=True):
            # Use run_coroutine_on_thread to process each submission
            # This ensures that one slow post doesn't block new ones from the stream
            context = ContextTypes.DEFAULT_TYPE(application=app)
            asyncio.create_task(process_submission(submission, context))
    except asyncio.CancelledError:
        logging.info("Subreddit stream task was cancelled.")
    except Exception:
        logging.exception("Subreddit stream encountered a critical error. It will not restart automatically.")

async def stop_and_restart_stream(app: Application):
    # Cancel the old task if it exists
    current_task = app.bot_data.get("stream_task")
    if current_task and not current_task.done():
        current_task.cancel()
        try:
            await current_task
        except asyncio.CancelledError:
            pass # Expected
    
    # Start the new task
    app.bot_data["stream_task"] = asyncio.create_task(stream_subreddits_task(app))

# ---------- STARTUP / SHUTDOWN HOOKS ----------
async def on_startup(app: Application):
    logging.info("Bot starting up...")
    app.bot_data["reddit_client"] = asyncpraw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent="TelegramRedditBot/2.0 by anarq42",
    )
    app.bot_data["posted_ids"] = load_posted_ids()
    app.bot_data["posted_ids_lock"] = asyncio.Lock()
    app.bot_data["subreddit_map"] = load_subreddits_mapping(SUBREDDITS_DB_PATH)
    
    await stop_and_restart_stream(app) # Initial start
    logging.info("Startup complete.")

async def on_shutdown(app: Application):
    logging.info("Bot shutting down...")
    # Cleanly cancel the stream task
    stream_task = app.bot_data.get("stream_task")
    if stream_task and not stream_task.done():
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            logging.info("Subreddit stream task successfully cancelled.")
    
    # Close Reddit client
    reddit_client = app.bot_data.get("reddit_client")
    if reddit_client:
        await reddit_client.close()
    logging.info("Shutdown complete.")

# ---------- GLOBAL ERROR HANDLER ----------
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled exception: %s", context.error, exc_info=context.error)

# ---------- MAIN ----------
def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("reload", reload_command, filters=filters.User(user_id=TELEGRAM_ADMIN_ID)))
    app.add_error_handler(global_error_handler)

    logging.info("Bot application configured. Starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
