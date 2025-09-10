import os
import time
import logging
import asyncio
import re
import io
import requests
from datetime import datetime, timedelta
import praw
from telegram import Bot, InputMediaPhoto, InputMediaVideo, constants
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
# Set up logging for better visibility
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Load credentials from environment variables
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = os.getenv("REDDIT_USERNAME")
REDDIT_PASSWORD = os.getenv("REDDIT_PASSWORD")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID")

# Global list to prevent sending the same post multiple times
sent_posts = []
CHECK_INTERVAL_SECONDS = 1800  # 30 minutes

# --- PRAW & Telegram Bot Initialization ---
reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent="script:Reddit to Telegram Bot by u/YOUR_REDDIT_USERNAME",
    username=REDDIT_USERNAME,
    password=REDDIT_PASSWORD
)

# Initialize the Telegram bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# --- Helper Functions ---

def escape_markdown_v2_text(text):
    reserved_chars = r'_*[]()~`>#+-=|{}.!'
    escaped_text = re.sub(f'([{re.escape(reserved_chars)}])', r'\\\1', text)
    return escaped_text

def get_post_media_url(submission):
    if submission.is_self:
        return None
    
    if submission.is_video:
        if hasattr(submission.media, 'get') and submission.media.get('reddit_video'):
            return submission.media['reddit_video']['fallback_url'].split('?')[0]

    if hasattr(submission, 'url'):
        if submission.url.endswith(('.jpg', '.jpeg', '.png', '.gif', '.gifv', '.mp4')):
            return submission.url.split('?')[0]
        
    return None

def get_post_comments(submission):
    comments_str = ""
    try:
        submission.comments.replace_more(limit=0)
        comments = submission.comments.list()
        if comments:
            for i in range(min(3, len(comments))):
                comment = comments[i]
                if comment and hasattr(comment, 'author') and comment.author:
                    author_text = f"u/{comment.author.name}"
                    body_text = comment.body.strip()
                    escaped_author = escape_markdown_v2_text(author_text)
                    escaped_body = escape_markdown_v2_text(body_text)
                    comments_str += f"**{escaped_author}**: {escaped_body}\n"
    except Exception as e:
        logging.error(f"Failed to retrieve comments for post {submission.id}: {e}")
    return comments_str

def get_post_caption(submission, comments_str):
    title = submission.title
    author = submission.author.name if submission.author else "Unknown"
    post_link = f"https://www.reddit.com{submission.permalink}"
    
    escaped_title = escape_markdown_v2_text(title)
    escaped_author = escape_markdown_v2_text(f"u/{author}")
    escaped_link = escape_markdown_v2_text(post_link)
    
    caption_parts = [
        f"**New Post from r/{submission.subreddit.display_name}**",
        f"**Title**: {escaped_title}",
        f"**Author**: {escaped_author}",
        f"**Link**: [Click to view post]({escaped_link})",
    ]
    if comments_str:
        caption_parts.append("\n**Top Comments:**")
        caption_parts.append(comments_str)
        
    caption = "\n".join(caption_parts)
    return caption

async def send_media(media_url, telegram_method, chat_id, caption, topic_id):
    try:
        response = requests.get(media_url, stream=True)
        response.raise_for_status()
        media_stream = io.BytesIO(response.content)
        await telegram_method(
            chat_id=chat_id,
            media=media_stream,
            caption=caption,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            message_thread_id=topic_id
        )
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to download media from {media_url}: {e}")
        return False
    except Exception as e:
        logging.error(f"Failed to send media via {telegram_method.__name__}: {e}")
        return False

async def get_gallery_media(submission, caption):
    media_group = []
    if not hasattr(submission, 'gallery_data'):
        return media_group

    for item in submission.gallery_data['items']:
        media_id = item['media_id']
        if media_id in submission.media_metadata:
            meta = submission.media_metadata[media_id]
            file_type = meta['m']
            
            if 'p' in meta and 'u' in meta['p'][-1]:
                url = meta['p'][-1]['u'].split('?')[0]
                try:
                    response = requests.get(url, stream=True)
                    response.raise_for_status()
                    media_stream = io.BytesIO(response.content)
                    
                    if file_type.startswith('image'):
                        media_group.append(InputMediaPhoto(media=media_stream))
                    elif file_type.startswith('video'):
                        media_group.append(InputMediaVideo(media=media_stream))
                except requests.exceptions.RequestException as e:
                    logging.error(f"Failed to download gallery media from {url}: {e}")
    
    if media_group:
        media_group[0].caption = caption
        media_group[0].parse_mode = constants.ParseMode.MARKDOWN_V2

    return media_group


async def send_post_to_telegram(submission, topic_id):
    global sent_posts
    
    comments_str = get_post_comments(submission)
    caption = get_post_caption(submission, comments_str)
    
    try:
        if hasattr(submission, 'is_gallery') and submission.is_gallery:
            media_group = await get_gallery_media(submission, caption)
            if media_group:
                await bot.send_media_group(
                    chat_id=TELEGRAM_GROUP_ID,
                    media=media_group,
                    message_thread_id=topic_id
                )
            else:
                logging.warning(f"Gallery post {submission.id} has no valid media.")

        elif submission.is_video:
            media_url = get_post_media_url(submission)
            if media_url:
                await bot.send_video(
                    chat_id=TELEGRAM_GROUP_ID,
                    video=media_url,
                    caption=caption,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    message_thread_id=topic_id
                )

        elif submission.url.endswith(('.gif', '.gifv')):
            media_url = get_post_media_url(submission)
            if media_url:
                await bot.send_animation(
                    chat_id=TELEGRAM_GROUP_ID,
                    animation=media_url,
                    caption=caption,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    message_thread_id=topic_id
                )

        elif submission.url.endswith(('.jpg', '.jpeg', '.png')):
            media_url = get_post_media_url(submission)
            if media_url:
                await bot.send_photo(
                    chat_id=TELEGRAM_GROUP_ID,
                    photo=media_url,
                    caption=caption,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    message_thread_id=topic_id
                )
        else:
            logging.info(f"Skipping post {submission.id} as it is not a supported media type.")
            return

        sent_posts.append(submission.id)
        logging.info(f"Successfully sent post {submission.id} to Telegram.")
    except Exception as e:
        logging.error(f"Failed to send post {submission.id} to Telegram: {e}")

async def check_new_posts(subreddit_name, topic_id):
    subreddit = reddit.subreddit(subreddit_name)
    now = datetime.utcnow()
    
    for submission in subreddit.new(limit=25):
        if submission.created_utc > (now - timedelta(minutes=30)).timestamp():
            if submission.id not in sent_posts:
                if (hasattr(submission, 'is_gallery') and submission.is_gallery) or \
                   (hasattr(submission, 'is_video') and submission.is_video) or \
                   (submission.url.endswith(('.jpg', '.jpeg', '.png', '.gif', '.gifv', '.mp4'))):
                    logging.info(f"Found new media post in r/{subreddit_name}: {submission.title}")
                    await send_post_to_telegram(submission, topic_id)

async def main():
    logging.info("Successfully connected to Reddit.")
    logging.info("Successfully connected to Telegram.")
    logging.info("Starting bot.")

    subreddits_to_check = {}
    try:
        with open("subreddits.db", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    subreddit, topic = line.split(',')
                    subreddits_to_check[subreddit.strip()] = int(topic.strip())
        logging.info(f"Loaded {len(subreddits_to_check)} subreddits from configuration.")
    except FileNotFoundError:
        logging.error("subreddits.db not found. Please create it.")
        return
    except Exception as e:
        logging.error(f"Error reading subreddits.db: {e}")
        return

    while True:
        start_time = time.time()
        for subreddit_name, topic_id in subreddits_to_check.items():
            logging.info(f"Checking r/{subreddit_name} for new posts...")
            await check_new_posts(subreddit_name, topic_id)
        
        loop_duration = time.time() - start_time
        sleep_time = CHECK_INTERVAL_SECONDS - loop_duration
        logging.info(f"Loop finished in {loop_duration:.2f} seconds. Sleeping for {sleep_time:.2f} seconds.")
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

if __name__ == "__main__":
    asyncio.run(main())
