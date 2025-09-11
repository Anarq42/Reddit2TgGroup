import os
import sys
import time
import logging
import requests
import asyncio
from dotenv import load_dotenv
import telegram
import praw
from telegram import InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode

# Configure logging to provide detailed output for debugging.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def escape_html_text(text):
    """Escapes HTML special characters in a string."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def get_top_comments(submission, num_comments=3):
    """
    Fetches the top comments for a given submission.
    Returns a formatted string of the comments.
    """
    comments_str = ""
    try:
        submission.comments.replace_more(limit=0)
        top_comments = list(submission.comments.list)[:num_comments]
        if top_comments:
            for comment in top_comments:
                author = escape_html_text(str(comment.author))
                body = escape_html_text(comment.body)
                comments_str += f"<b>{author}</b>: {body}\n\n"
    except Exception as e:
        logger.error(f"Error fetching comments for post {submission.id}: {e}")
    return comments_str

def get_media_urls(submission):
    """
    Extracts media URLs from a submission, handling different post types.
    Returns a list of tuples: (media_url, media_type).
    """
    media_list = []
    is_gallery = hasattr(submission, 'media_metadata') and 'gallery_data' in submission.__dict__

    if is_gallery:
        for item in submission.gallery_data['items']:
            media_id = item['media_id']
            meta = submission.media_metadata[media_id]
            url = meta['s']['u']

            media_type = 'photo'
            if meta['e'] == 'RedditVideo':
                media_type = 'video'
                url = submission.media['reddit_video']['fallback_url'].split("?")[0]
            elif meta['e'] == 'AnimatedImage':
                media_type = 'gif'
                url = meta['s']['gif']

            # Clean URL to prevent issues with Telegram's API
            clean_url = url.split("?")[0]
            media_list.append((clean_url, media_type))
    elif submission.is_video:
        url = submission.media['reddit_video']['fallback_url'].split("?")[0]
        media_list.append((url, 'video'))
    elif submission.url.endswith(('jpg', 'jpeg', 'png')):
        media_list.append((submission.url, 'photo'))
    elif submission.url.endswith(('gif', 'gifv')):
        media_list.append((submission.url, 'gif'))

    return media_list

async def send_to_telegram(bot, chat_id, topic_id, submission, media_list):
    """
    Sends a post to Telegram, handling different media types.
    """
    title = escape_html_text(submission.title)
    author = escape_html_text(str(submission.author))
    post_link = f"https://reddit.com{submission.permalink}"
    comments_text = get_top_comments(submission)

    # Base caption for all media types
    caption = (
        f"<b>New Post from r/{submission.subreddit.display_name}</b>\n"
        f"<b>Title</b>: {title}\n"
        f"<b>Author</b>: u/{author}\n\n"
        f"<b>Link</b>: <a href='{post_link}'>Click to view post</a>\n\n"
    )

    if comments_text:
        caption += f"<b>Top Comments:</b>\n{comments_text}"

    try:
        if len(media_list) > 1:
            media_group = []
            for i, (url, media_type) in enumerate(media_list):
                try:
                    # Download media into memory
                    response = requests.get(url, timeout=10)
                    response.raise_for_status()

                    if media_type == 'video':
                        media_group.append(InputMediaVideo(media=response.content, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML))
                    else:
                        media_group.append(InputMediaPhoto(media=response.content, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML))

                except requests.exceptions.RequestException as e:
                    logger.error(f"Failed to download media from {url}: {e}")
                    return

            if media_group:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    message_thread_id=topic_id
                )
        elif media_list:
            url, media_type = media_list[0]
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            if media_type == 'video':
                await bot.send_video(
                    chat_id=chat_id,
                    video=response.content,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=topic_id
                )
            elif media_type == 'gif':
                await bot.send_animation(
                    chat_id=chat_id,
                    animation=response.content,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=topic_id
                )
            else:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=response.content,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=topic_id
                )
        else:
            logger.info(f"Skipping post {submission.id}: No supported media found.")

    except telegram.error.TelegramError as e:
        logger.error(f"Failed to send post {submission.id} to Telegram: {e}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download media for post {submission.id}: {e}")

# --- Main Bot Logic ---

async def main():
    """Main function to run the bot."""
    load_dotenv()

    # Retrieve credentials from environment variables
    try:
        reddit_client_id = os.getenv("REDDIT_CLIENT_ID")
        reddit_client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        reddit_username = os.getenv("REDDIT_USERNAME")
        reddit_password = os.getenv("REDDIT_PASSWORD")
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_group_id = os.getenv("TELEGRAM_GROUP_ID")

        if not all([reddit_client_id, reddit_client_secret, reddit_username, reddit_password, telegram_token, telegram_group_id]):
            raise ValueError("Missing one or more required environment variables.")

        telegram_group_id = int(telegram_group_id)

    except (ValueError, TypeError) as e:
        logger.error(f"Configuration error: {e}. Please check your .env file or environment variables.")
        sys.exit(1)

    # Connect to Reddit
    try:
        reddit = praw.Reddit(
            client_id=reddit_client_id,
            client_secret=reddit_client_secret,
            username=reddit_username,
            password=reddit_password,
            user_agent="Reddit to Telegram Bot v1.0"
        )
        logger.info("Successfully connected to Reddit.")
    except Exception as e:
        logger.error(f"Failed to connect to Reddit: {e}")
        sys.exit(1)

    # Connect to Telegram
    try:
        bot = telegram.Bot(token=telegram_token)
        logger.info("Successfully connected to Telegram.")
    except Exception as e:
        logger.error(f"Failed to connect to Telegram: {e}")
        sys.exit(1)

    logger.info("Starting bot.")

    processed_posts = set()
    subreddits_config = {}

    try:
        with open("subreddits.db", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    subreddit, topic_id = line.split(',')
                    subreddits_config[subreddit.strip()] = int(topic_id.strip())

        logger.info(f"Loaded {len(subreddits_config)} subreddits from configuration.")
    except FileNotFoundError:
        logger.error("subreddits.db file not found. Please create it.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error reading subreddits.db: {e}")
        sys.exit(1)

    while True:
        try:
            # Use non-blocking stream iteration
            subreddit_stream = reddit.subreddit('+'.join(subreddits_config.keys())).stream.submissions(skip_existing=True)
            
            logger.info("Starting to monitor subreddits in real-time...")
            
            for submission in subreddit_stream:
                if submission.id not in processed_posts:
                    logger.info(f"Found new submission: {submission.id} in r/{submission.subreddit.display_name}")
                    
                    subreddit_name = submission.subreddit.display_name
                    topic_id = subreddits_config.get(subreddit_name)
                    
                    if topic_id is not None:
                        try:
                            media_list = get_media_urls(submission)
                            if media_list:
                                logger.info(f"Found new media post in r/{subreddit_name}: {submission.title}")
                                await send_to_telegram(bot, telegram_group_id, topic_id, submission, media_list)
                                processed_posts.add(submission.id)
                                logger.info(f"Successfully sent post {submission.id} to Telegram.")
                            else:
                                processed_posts.add(submission.id)
                                logger.info(f"Skipping post {submission.id} (no supported media).")
                            
                        except Exception as e:
                            logger.error(f"An error occurred while processing post {submission.id}: {e}")
            
        except Exception as e:
            logger.error(f"An error occurred in the submission stream: {e}. Restarting stream in 10 seconds...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
