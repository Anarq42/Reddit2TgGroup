import os
import logging
import asyncio
import aiohttp
from io import BytesIO
from telegram import Bot, InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from telegram.constants import ParseMode
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
telegram_group_id = int(os.getenv("TELEGRAM_GROUP_ID"))  # Main group ID
telegram_error_topic_id = int(os.getenv("TELEGRAM_ERROR_TOPIC_ID"))  # Fallback topic
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

# ---------- MEDIA HANDLING ----------
async def fetch_bytes(session, url):
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.read()
        return BytesIO(data)

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
        if "imgur.com" in host_url:
            if host_url.endswith((".jpg", ".png", ".gif")):
                media_list.append({"url": submission.url, "type": "photo"})
            elif "/a/" in host_url or "/gallery/" in host_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(submission.url) as resp:
                            text = await resp.text()
                            soup = BeautifulSoup(text, "lxml")
                            images = [img['src'] for img in soup.find_all('img') if 'i.imgur.com' in img['src']]
                            for img_url in images:
                                if img_url.startswith("//"):
                                    img_url = "https:" + img_url
                                media_list.append({"url": img_url, "type": "photo"})
                except Exception as e:
                    logging.warning("Failed to parse Imgur album: %s", submission.url)
        elif "gfycat.com" in host_url or "redgifs.com" in host_url:
            media_list.append({"url": submission.url, "type": "video"})

    return media_list

# ---------- UTILS ----------
def prepare_caption(submission: Submission):
    return f"<b>{submission.title}</b>\nPosted by u/{submission.author}\n<a href='{submission.url}'>Reddit Link</a>"

async def send_media(submission: Submission, media_list, topic_id):
    """Send media or fallback Reddit link"""
    caption = prepare_caption(submission)
    try:
        if media_list:
            if len(media_list) > 1:
                tg_media = []
                async with aiohttp.ClientSession() as session:
                    for media in media_list:
                        bio = await fetch_bytes(session, media["url"])
                        if media["type"] == "photo":
                            tg_media.append(InputMediaPhoto(media=bio))
                        elif media["type"] in ["video", "gif"]:
                            tg_media.append(InputMediaVideo(media=bio))
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
            # No media, just send Reddit link
            await bot.send_message(chat_id=telegram_group_id, message_thread_id=topic_id,
                                   text=caption, parse_mode=ParseMode.HTML)

        logging.info(f"Post sent: {submission.title} to topic {topic_id}")
    except TelegramError as e:
        logging.error(f"Error sending post: {submission.title} - {e}")
        await bot.send_message(chat_id=telegram_error_topic_id,
                               text=f"Error sending post: {submission.title}\n{e}")

# ---------- MAIN LOOP ----------
async def main():
    subreddit_map = load_subreddits_mapping(subreddits_db_path)
    logging.info("Bot started, monitoring subreddits: %s", ", ".join(subreddit_map.keys()))
    while True:
        try:
            for submission in reddit.subreddit("+".join(subreddit_map.keys())).stream.submissions(skip_existing=True):
                media_list = await get_media_urls(submission)
                topic_id = subreddit_map.get(submission.subreddit.display_name, telegram_error_topic_id)
                await send_media(submission, media_list, topic_id)
        except Exception as e:
            logging.error(f"Stream error: {e}. Restarting in 10 seconds...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
