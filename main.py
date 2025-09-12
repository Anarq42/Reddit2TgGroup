import os
import logging
import asyncio
import aiohttp
import json
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
                mapping[subreddit_name.strip()] = int(topic_id.strip())
    except FileNotFoundError:
        logging.warning(f"{file_path} not found. Starting with empty mapping.")
    return mapping

subreddit_map = load_subreddits_mapping(SUBREDDITS_DB_PATH)

# ---------- TRACK POSTED IDS ----------
def load_posted_ids():
    try:
        with open(POSTED_IDS_PATH, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_posted_ids(posted_ids):
    with open(POSTED_IDS_PATH, "w") as f:
        json.dump(list(posted_ids), f)

posted_ids = load_posted_ids()

# ---------- REDDIT INIT ----------
reddit = asyncpraw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    username=REDDIT_USERNAME,
    password=REDDIT_PASSWORD,
    user_agent="TelegramRedditBot/1.0"
)

# ---------- UTILITIES ----------
def prepare_caption(submission):
    author = submission.author.name if submission.author else "[deleted]"
    return f"<b>{submission.title}</b>\nPosted by u/{author}\n<a href='{submission.url}'>Reddit Link</a>"

async def fetch_bytes(session, url):
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            return BytesIO(data)
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
                soup = BeautifulSoup(text, "lxml")
                mp4_tag = soup.find("source", {"type": "video/mp4"})
                if mp4_tag and mp4_tag.get("src"):
                    mp4_url = mp4_tag["src"]
                    if mp4_url.startswith("//"):
                        mp4_url = "https:" + mp4_url
                    return mp4_url
                match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', text)
                if match:
                    data = json.loads(match.group(1))
                    return data.get('gfyItem', {}).get('mp4Url')
    except Exception as e:
        logging.warning(f"Failed to get mp4 from {url}: {e}")
    return None

# ---------- MEDIA HANDLING ----------
async def get_media_urls(submission):
    media_list = []

    try:
        # Reddit gallery
        if getattr(submission, 'is_gallery', False):
            for meta in submission.media_metadata.values():
                if meta['e'] == 'Image':
                    url = meta['s']['u'].replace("&amp;", "&")
                    media_list.append({"url": url, "type": "photo"})

        # Reddit video
        elif getattr(submission, 'media', None) and 'reddit_video' in submission.media:
            media_url = submission.media['reddit_video']['fallback_url']
            media_list.append({"url": media_url, "type": "video"})

        # Direct links
        elif submission.url.endswith((".jpg", ".jpeg", ".png")):
            media_list.append({"url": submission.url, "type": "photo"})
        elif submission.url.endswith((".gif", ".mp4")):
            media_type = "gif" if submission.url.endswith(".gif") else "video"
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
        logging.warning(f"Failed to get media URLs for {submission.id}: {e}")

    return media_list

# ---------- SEND MEDIA ----------
async def send_media(submission, media_list, topic_id, bot):
    if submission.id in posted_ids:
        logging.info(f"Skipping duplicate post: {submission.title}")
        return
    posted_ids.add(submission.id)
    save_posted_ids(posted_ids)

    caption = prepare_caption(submission)
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if media_list:
                if len(media_list) > 1:
                    tg_media = []
                    media_bytes = await asyncio.gather(*(fetch_bytes(session, m['url']) for m in media_list))
                    for i, media in enumerate(media_list):
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
                        return
                    if media["type"] == "photo":
                        await bot.send_photo(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, photo=bio, caption=caption, parse_mode=ParseMode.HTML)
                    elif media["type"] == "video":
                        await bot.send_video(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, video=bio, caption=caption, parse_mode=ParseMode.HTML)
                    elif media["type"] == "gif":
                        await bot.send_animation(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, animation=bio, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, text=caption, parse_mode=ParseMode.HTML)

        logging.info(f"Post sent: {submission.title} to topic {topic_id}")
    except TelegramError as e:
        logging.error(f"Error sending post: {submission.title} - {e}")
        await bot.send_message(chat_id=TELEGRAM_ERROR_TOPIC_ID, text=f"Error sending post: {submission.title}\n{e}")

# ---------- PROCESS REDDIT LINK ----------
async def send_reddit_link(url, bot):
    try:
        submission_id = Submission.id_from_url(url)
        submission = await reddit.submission(id=submission_id)
        topic_id = subreddit_map.get(submission.subreddit.display_name, TELEGRAM_ERROR_TOPIC_ID)
        media_list = await get_media_urls(submission)
        await send_media(submission, media_list, topic_id, bot)
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
    msg = await send_reddit_link(url, context.bot)
    await update.message.reply_text(msg)

# ---------- STREAM SUBREDDITS ----------
async def stream_subreddits(bot):
    if not subreddit_map:
        logging.warning("No subreddits configured. Skipping stream.")
        return

    subreddit_names = "+".join(subreddit_map.keys())
    try:
        subreddit = await reddit.subreddit(subreddit_names)
    except Exception as e:
        logging.error(f"Failed to access subreddits: {e}")
        return

    logging.info(f"Starting subreddit stream: {subreddit_names}")

    async for submission in subreddit.stream.submissions(skip_existing=True):
        logging.info(f"New submission: {submission.title} (r/{submission.subreddit.display_name})")
        topic_id = subreddit_map.get(submission.subreddit.display_name, TELEGRAM_ERROR_TOPIC_ID)
        try:
            media_list = await get_media_urls(submission)
            await send_media(submission, media_list, topic_id, bot)
        except Exception as e:
            logging.error(f"Failed to send submission {submission.title}: {e}")
            await bot.send_message(chat_id=TELEGRAM_ERROR_TOPIC_ID, text=f"Error sending submission: {submission.title}\n{e}")

# ---------- MAIN LOOP ----------
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("post", post_command))

    # Start subreddit stream in background
    asyncio.create_task(stream_subreddits(app.bot))

    logging.info("Bot started, monitoring subreddits: %s", ", ".join(subreddit_map.keys()))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.wait_closed()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
