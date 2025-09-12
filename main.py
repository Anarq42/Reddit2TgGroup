import os
import logging
import asyncio
import aiohttp
import re
from io import BytesIO
from telegram import Bot, Update, InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError
from praw import Reddit
from praw.models import Submission
from bs4 import BeautifulSoup

# ---------- CREDENTIALS ----------
reddit_client_id = os.getenv("REDDIT_CLIENT_ID")
reddit_client_secret = os.getenv("REDDIT_CLIENT_SECRET")
reddit_username = os.getenv("REDDIT_USERNAME")
reddit_password = os.getenv("REDDIT_PASSWORD")
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_group_id = int(os.getenv("TELEGRAM_GROUP_ID"))
telegram_error_topic_id = int(os.getenv("TELEGRAM_ERROR_TOPIC_ID"))
subreddits_db_path = "subreddits.db"

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ---------- INIT ----------
bot = Bot(token=telegram_token)
reddit = Reddit(
    client_id=reddit_client_id,
    client_secret=reddit_client_secret,
    username=reddit_username,
    password=reddit_password,
    user_agent="TelegramRedditBot/1.0"
)

# ---------- LOAD SUBREDDITS ----------
def load_subreddits_mapping(file_path):
    mapping = {}
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
    return mapping

subreddit_map = load_subreddits_mapping(subreddits_db_path)

# ---------- UTILS ----------
def prepare_caption(submission: Submission):
    return f"<b>{submission.title}</b>\nPosted by u/{submission.author}\n<a href='{submission.url}'>Reddit Link</a>"

async def fetch_bytes(session, url):
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.read()
        return BytesIO(data)

# ---------- MEDIA HANDLING ----------
async def get_media_urls(submission: Submission):
    media_list = []

    # Reddit gallery
    if getattr(submission, 'is_gallery', False):
        for key, meta in submission.media_metadata.items():
            if meta['e'] == 'Image':
                url = meta['s']['u'].replace("&amp;", "&")
                media_list.append({"url": url, "type": "photo"})

    # Reddit video
    elif hasattr(submission, 'media') and submission.media and 'reddit_video' in submission.media:
        media_url = submission.media['reddit_video']['fallback_url']
        media_list.append({"url": media_url, "type": "video"})

    # Direct image/video links
    elif submission.url.endswith((".jpg", ".jpeg", ".png")):
        media_list.append({"url": submission.url, "type": "photo"})
    elif submission.url.endswith((".gif", ".mp4")):
        media_type = "gif" if submission.url.endswith(".gif") else "video"
        media_list.append({"url": submission.url, "type": media_type})

    # External hosts
    else:
        host_url = submission.url.lower()
        if "imgur.com" in host_url and host_url.endswith((".jpg", ".png", ".gif")):
            media_list.append({"url": submission.url, "type": "photo"})
        elif "gfycat.com" in host_url or "redgifs.com" in host_url:
            mp4_url = await get_gfy_redgifs_mp4(submission.url)
            if mp4_url:
                media_list.append({"url": mp4_url, "type": "video"})

    return media_list

# ---------- Gfycat/Redgifs MP4 DOWNLOAD ----------
async def get_gfy_redgifs_mp4(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                text = await resp.text()
                soup = BeautifulSoup(text, "lxml")
                mp4_tag = soup.find("source", {"type": "video/mp4"})
                if mp4_tag and mp4_tag.get("src"):
                    mp4_url = mp4_tag["src"]
                    if mp4_url.startswith("//"):
                        mp4_url = "https:" + mp4_url
                    return mp4_url
                json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', text)
                if json_match:
                    import json
                    data = json.loads(json_match.group(1))
                    try:
                        return data['gfyItem']['mp4Url']
                    except:
                        return None
    except Exception as e:
        logging.warning(f"Failed to get mp4 from {url}: {e}")
    return None

# ---------- SEND MEDIA ----------
async def send_media(submission: Submission, media_list, topic_id):
    caption = prepare_caption(submission)
    try:
        if media_list:
            if len(media_list) > 1:
                tg_media = []
                async with aiohttp.ClientSession() as session:
                    for i, media in enumerate(media_list):
                        bio = await fetch_bytes(session, media["url"])
                        kwargs = {"caption": caption if i == 0 else None, "parse_mode": ParseMode.HTML}
                        if media["type"] == "photo":
                            tg_media.append(InputMediaPhoto(media=bio, **kwargs))
                        elif media["type"] in ["video", "gif"]:
                            tg_media.append(InputMediaVideo(media=bio, **kwargs))
                await bot.send_media_group(chat_id=telegram_group_id, message_thread_id=topic_id, media=tg_media)
            else:
                media = media_list[0]
                async with aiohttp.ClientSession() as session:
                    bio = await fetch_bytes(session, media["url"])
                if media["type"] == "photo":
                    await bot.send_photo(chat_id=telegram_group_id, message_thread_id=topic_id,
                                         photo=bio, caption=caption, parse_mode=ParseMode.HTML)
                elif media["type"] == "video":
                    await bot.send_video(chat_id=telegram_group_id, message_thread_id=topic_id,
                                         video=bio, caption=caption, parse_mode=ParseMode.HTML)
                elif media["type"] == "gif":
                    await bot.send_animation(chat_id=telegram_group_id, message_thread_id=topic_id,
                                             animation=bio, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=telegram_group_id, message_thread_id=topic_id,
                                   text=caption, parse_mode=ParseMode.HTML)

        logging.info(f"Post sent: {submission.title} to topic {topic_id}")
    except TelegramError as e:
        logging.error(f"Error sending post: {submission.title} - {e}")
        await bot.send_message(chat_id=telegram_error_topic_id,
                               text=f"Error sending post: {submission.title}\n{e}")

# ---------- PROCESS REDDIT LINK ----------
async def send_reddit_link(url: str):
    match = re.search(r"comments/([a-z0-9]+)/", url)
    if not match:
        logging.warning(f"Invalid Reddit URL: {url}")
        return "Invalid Reddit URL"
    submission_id = match.group(1)
    submission = reddit.submission(id=submission_id)
    topic_id = subreddit_map.get(submission.subreddit.display_name, telegram_error_topic_id)
    media_list = await get_media_urls(submission)
    await send_media(submission, media_list, topic_id)
    return f"Processed Reddit post: {submission.title}"

# ---------- TELEGRAM COMMAND ----------
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /post <reddit_url>")
        return
    url = context.args[0]
    msg = await send_reddit_link(url)
    await update.message.reply_text(msg)

# ---------- MAIN STREAM WITH DETAILED LOGGING ----------
async def stream_subreddits():
    subreddit_names = "+".join(subreddit_map.keys())
    subreddit = reddit.subreddit(subreddit_names)

    logging.info(f"Starting subreddit stream: {subreddit_names}")

    while True:
        try:
            for submission in subreddit.stream.submissions(skip_existing=True, pause_after=0):
                if submission is None:
                    # No new submission, yield control
                    await asyncio.sleep(2)
                    continue

                logging.info(f"New submission detected: {submission.title} (r/{submission.subreddit.display_name})")

                topic_id = subreddit_map.get(submission.subreddit.display_name, telegram_error_topic_id)

                try:
                    media_list = await get_media_urls(submission)
                    if not media_list:
                        logging.info(f"No media detected, sending as text post: {submission.title}")

                    await send_media(submission, media_list, topic_id)
                    logging.info(f"Post sent successfully: {submission.title} to topic {topic_id}")
                except Exception as media_err:
                    logging.error(f"Failed to send submission {submission.title}: {media_err}")
                    await bot.send_message(
                        chat_id=telegram_error_topic_id,
                        text=f"Error sending submission: {submission.title}\n{media_err}"
                    )

        except Exception as e:
            logging.error(f"Stream error: {e}. Restarting in 10s...")
            await asyncio.sleep(10)

# ---------- MAIN LOOP ----------
async def main():
    app = Application.builder().token(telegram_token).build()
    app.add_handler(CommandHandler("post", post_command))
    logging.info("Bot started, monitoring subreddits: %s", ", ".join(subreddit_map.keys()))
    asyncio.create_task(stream_subreddits())
    await app.initialize()
    await app.updater.start_polling()
    await app.updater.idle()

# ---------- START ----------
if __name__ == "__main__":
    async def runner():
        await main()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        asyncio.create_task(runner())
    else:
        asyncio.run(runner())
