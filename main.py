# simplified and corrected main.py (only the startup-related fix applied)
import os
import logging
import asyncio
import aiohttp
import json
import re
import html
from io import BytesIO
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError
import asyncpraw
from bs4 import BeautifulSoup
from asyncpraw.models import Submission

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# (the rest of the module is unchanged from your last version up to main())

# ---------- ENV VARS ----------
def get_env_var(name, cast=str):
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
posted_ids_lock = asyncio.Lock()

# ---------- UTILITIES ----------
def prepare_caption(submission):
    author = submission.author.name if submission.author else "[deleted]"
    # HTML-escape title/author/url
    safe_title = html.escape(submission.title)
    safe_author = html.escape(author)
    safe_url = html.escape(submission.url)
    return f"<b>{safe_title}</b>\nPosted by u/{safe_author}\n<a href='{safe_url}'>Reddit Link</a>"

async def fetch_bytes(session, url):
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            bio = BytesIO(data)
            bio.seek(0)
            # give a name so telegram can infer content-type if needed
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
                        return data.get('gfyItem', {}).get('mp4Url')
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
        if getattr(submission, 'is_gallery', False):
            meta = getattr(submission, 'media_metadata', {}) or {}
            for m in meta.values():
                if m.get('e') == 'Image' and 's' in m and 'u' in m['s']:
                    url = m['s']['u'].replace("&amp;", "&")
                    media_list.append({"url": url, "type": "photo"})

        # Reddit video
        elif getattr(submission, 'media', None) and isinstance(submission.media, dict) and 'reddit_video' in submission.media:
            reddit_video = submission.media['reddit_video']
            media_url = reddit_video.get('fallback_url')
            if media_url:
                media_list.append({"url": media_url, "type": "video"})

        # Direct links
        elif submission.url.lower().endswith((".jpg", ".jpeg", ".png")):
            media_list.append({"url": submission.url, "type": "photo"})
        elif submission.url.lower().endswith((".gif", ".mp4")):
            media_type = "gif" if submission.url.lower().endswith(".gif") else "video"
            media_list.append({"url": submission.url, "type": media_type})

        # External hosts
        else:
            url_lower = submission.url.lower()
            if "imgur.com" in url_lower and url_lower.endswith((".jpg", ".png", ".gif")):
                media_list.append({"url": submission.url, "type": "photo"})
            elif "gfycat.com" in url_lower or "redgifs.com" in url_lower:
                mp4_url = await get_gfy_redgifs_mp4(submission.url)
                if mp4_url:
                    media_list.append({"url": mp4_url, "type": "video"})
    except Exception as e:
        logging.warning(f"Failed to get media URLs for {getattr(submission, 'id', '?')}: {e}")

    return media_list

# ---------- SEND MEDIA ----------
async def send_media(submission, media_list, topic_id, bot):
    # prepare caption first
    caption = prepare_caption(submission)

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            if media_list:
                if len(media_list) > 1:
                    # limit to 10 for groups
                    tg_media = []
                    # fetch bytes concurrently
                    media_bytes = await asyncio.gather(*(fetch_bytes(session, m['url']) for m in media_list))
                    for i, media in enumerate(media_list[:10]):
                        bio = media_bytes[i]
                        if bio is None:
                            continue
                        kwargs = {"caption": caption if i == 0 else None, "parse_mode": ParseMode.HTML}
                        if media["type"] == "photo":
                            tg_media.append(InputMediaPhoto(media=bio, **kwargs))
                        elif media["type"] in ["video", "gif"]:
                            tg_media.append(InputMediaVideo(media=bio, **kwargs))
                    if tg_media:
                        await bot.send_media_group(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, media=tg_media)
                else:
                    media = media_list[0]
                    bio = await fetch_bytes(session, media["url"])
                    if bio is None:
                        await bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, text=caption, parse_mode=ParseMode.HTML)
                        return True
                    if media["type"] == "photo":
                        await bot.send_photo(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, photo=bio, caption=caption, parse_mode=ParseMode.HTML)
                    elif media["type"] == "video":
                        await bot.send_video(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, video=bio, caption=caption, parse_mode=ParseMode.HTML)
                    elif media["type"] == "gif":
                        await bot.send_animation(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, animation=bio, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, text=caption, parse_mode=ParseMode.HTML)

            logging.info(f"Post sent: {submission.title} to topic {topic_id}")
            return True
        except TelegramError as e:
            logging.error(f"Error sending post: {submission.title} - {e}")
            try:
                await bot.send_message(chat_id=TELEGRAM_ERROR_TOPIC_ID, text=f"Error sending post: {submission.title}\n{str(e)[:400]}")
            except Exception:
                logging.exception("Failed to send error message to error topic")
            return False
        except Exception:
            logging.exception("Unexpected error while sending media")
            return False

# ---------- PROCESS REDDIT LINK ----------
async def send_reddit_link(url, bot, reddit_client):
    try:
        # Use reddit.submission(url=...) to get by permalink or full URL
        submission = await reddit_client.submission(url=url)
        # ensure subreddit lookup in lowercase keys
        topic_id = subreddit_map.get(submission.subreddit.display_name.lower(), TELEGRAM_ERROR_TOPIC_ID)
        media_list = await get_media_urls(submission)
        ok = await send_media(submission, media_list, topic_id, bot)
        if ok:
            # record posted id, protected by lock
            async with posted_ids_lock:
                if submission.id not in posted_ids:
                    posted_ids.add(submission.id)
                    save_posted_ids(posted_ids)
        return f"Processed Reddit post: {submission.title}"
    except Exception as e:
        logging.warning(f"Failed to process Reddit URL {url}: {e}")
        return f"Error processing Reddit URL: {e}"

# ---------- TELEGRAM COMMAND ----------
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /post <reddit_url>")
        return
    url = context.args[0]
    # reddit_client attached to app via context.application.bot_data
    reddit_client = context.application.bot_data.get("reddit_client")
    if reddit_client is None:
        await update.message.reply_text("Reddit client not ready; try again in a moment.")
        return
    msg = await send_reddit_link(url, context.bot, reddit_client)
    await update.message.reply_text(msg)

# ---------- STREAM SUBREDDITS ----------
async def stream_subreddits(reddit_client, bot):
    if not subreddit_map:
        logging.warning("No subreddits configured. Skipping stream.")
        return

    subreddit_names = "+".join(subreddit_map.keys())
    try:
        subreddit = reddit_client.subreddit(subreddit_names)
    except Exception as e:
        logging.error(f"Failed to access subreddits: {e}")
        return

    logging.info(f"Starting subreddit stream: {subreddit_names}")

    # resilient loop with simple backoff on errors
    backoff = 1
    while True:
        try:
            async for submission in subreddit.stream.submissions(skip_existing=True):
                logging.info(f"New submission: {submission.title} (r/{submission.subreddit.display_name})")
                topic_id = subreddit_map.get(submission.subreddit.display_name.lower(), TELEGRAM_ERROR_TOPIC_ID)
                try:
                    media_list = await get_media_urls(submission)
                    ok = await send_media(submission, media_list, topic_id, bot)
                    if ok:
                        async with posted_ids_lock:
                            if submission.id not in posted_ids:
                                posted_ids.add(submission.id)
                                save_posted_ids(posted_ids)
                except Exception:
                    logging.exception(f"Failed to send submission {submission.title}")
                    try:
                        await bot.send_message(chat_id=TELEGRAM_ERROR_TOPIC_ID, text=f"Error sending submission: {submission.title}")
                    except Exception:
                        logging.exception("Failed to report error to error topic")
            # if the async for ends naturally, break
            break
        except asyncio.CancelledError:
            logging.info("stream_subreddits cancelled, exiting")
            break
        except Exception as e:
            logging.exception(f"Stream error: {e}; backing off for {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# ---------- STARTUP / SHUTDOWN HOOKS ----------
async def _on_startup(context):
    reddit_client = asyncpraw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent="TelegramRedditBot/1.0"
    )
    context.application.bot_data["reddit_client"] = reddit_client
    context.application.create_task(stream_subreddits(reddit_client, context.application.bot))

async def _on_shutdown(context):
    reddit_client = context.application.bot_data.get("reddit_client")
    if reddit_client:
        try:
            await reddit_client.close()
        except Exception:
            logging.exception("Error closing reddit client")

# ---------- MAIN (synchronous run) ----------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("post", post_command))

    # Safely call post_init only if it exists and is callable
    post_init_attr = getattr(app, "post_init", None)
    if callable(post_init_attr):
        post_init_attr(_on_startup)

    # Schedule startup via job_queue for versions that do not support post_init
    # pass the coroutine function itself (JobQueue will call it with a context)
    app.job_queue.run_once(_on_startup, when=0)

    # Run polling (manages lifecycle correctly for python-telegram-bot v20+)
    app.run_polling()

if __name__ == "__main__":
    main()
