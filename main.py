import praw
import telegram
import time
import requests
import os
import datetime
import logging
import asyncio
from telegram.constants import ParseMode
from telegram import InputMediaPhoto, InputMediaVideo

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration Section ---
# This bot uses environment variables for security.
# You MUST set these environment variables before running the script:
# export REDDIT_CLIENT_ID='your_reddit_client_id'
# export REDDIT_CLIENT_SECRET='your_reddit_client_secret'
# export REDDIT_USERNAME='your_reddit_username'
# export REDDIT_PASSWORD='your_reddit_password'
# export TELEGRAM_BOT_TOKEN='your_telegram_bot_token'
# export TELEGRAM_GROUP_ID='-100xxxxxxxxxx'

# A note on Telegram Group IDs: They start with "-100" and are followed by a series of numbers.
# To find your group's ID, add a bot to the group, send a message, and check the getUpdates API endpoint:
# https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/getUpdates

# Load credentials from environment variables
try:
    REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
    REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
    REDDIT_USERNAME = os.environ['REDDIT_USERNAME']
    REDDIT_PASSWORD = os.environ['REDDIT_PASSWORD']
    TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
    TELEGRAM_GROUP_ID = os.environ['TELEGRAM_GROUP_ID']
except KeyError as e:
    logging.error(f"Missing environment variable: {e}. Please set all required variables.")
    exit()

# Set a descriptive user agent for Reddit, as required by their API rules.
# This helps them identify your bot and contact you if needed.
REDDIT_USER_AGENT = "script:reddit-to-telegram-bot:v1.0 (by /u/YOUR_REDDIT_USERNAME)"

# Initialize Reddit API client
try:
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        password=REDDIT_PASSWORD,
        user_agent=REDDIT_USER_AGENT,
        username=REDDIT_USERNAME,
    )
    logging.info("Successfully connected to Reddit.")
except Exception as e:
    logging.error(f"Failed to connect to Reddit: {e}")
    exit()

# Initialize Telegram bot API client
try:
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    logging.info("Successfully connected to Telegram.")
except Exception as e:
    logging.error(f"Failed to connect to Telegram: {e}")
    exit()

# Path to the subreddit configuration file
SUBREDDIT_DB_FILE = 'subreddits.db'
# Time in seconds to check for new posts (30 minutes)
CHECK_INTERVAL_SECONDS = 30 * 60

# Keep a set of previously processed posts to avoid duplicates.
# This simple cache will be cleared upon script restart. For long-term
# storage, a database like SQLite or a simple file could be used.
processed_posts = set()

def load_subreddits():
    """Loads subreddits and their corresponding Telegram topic IDs from the database file."""
    subreddits = {}
    if not os.path.exists(SUBREDDIT_DB_FILE):
        logging.warning(f"Configuration file '{SUBREDDIT_DB_FILE}' not found. Please create it.")
        return subreddits

    with open(SUBREDDIT_DB_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                subreddit_name, topic_id = line.split(',')
                subreddits[subreddit_name.strip()] = int(topic_id.strip())
            except ValueError:
                logging.error(f"Invalid format in {SUBREDDIT_DB_FILE}: '{line}'. Expected format: subreddit_name,topic_id")
    logging.info(f"Loaded {len(subreddits)} subreddits from configuration.")
    return subreddits

def escape_markdown_v2_text(text):
    """
    Escapes special MarkdownV2 characters.
    Note: Link URLs and code blocks should NOT be escaped.
    """
    reserved_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    escaped_text = text
    for char in reserved_chars:
        escaped_text = escaped_text.replace(char, f'\\{char}')
    return escaped_text

def get_submission_media(submission):
    """Checks for and returns a list of media objects (urls and types) from a Reddit submission."""
    media_list = []
    if hasattr(submission, 'is_gallery') and submission.is_gallery:
        # It's a gallery post, so get all media items.
        for item in submission.gallery_data['items']:
            media_id = item['media_id']
            # Get the media type from the metadata to determine if it's an image or video.
            media_type = submission.media_metadata[media_id]['e']
            if media_type == 'Image':
                url = submission.media_metadata[media_id]['s']['u'].split('?')[0].replace('preview', 'i')
                media_list.append({'url': url, 'type': 'photo'})
            elif media_type == 'Video':
                # Galleries can contain videos, though this is less common.
                url = submission.media_metadata[media_id]['s']['fallback_url']
                media_list.append({'url': url, 'type': 'video'})
    elif hasattr(submission, 'post_hint'):
        if submission.post_hint == 'image':
            media_list.append({'url': submission.url, 'type': 'photo'})
        elif submission.post_hint == 'video' and hasattr(submission.media, 'reddit_video'):
            media_list.append({'url': submission.media['reddit_video']['fallback_url'], 'type': 'video'})
    elif hasattr(submission, 'preview') and 'images' in submission.preview:
        # A fallback for posts that might have a preview image but aren't a direct link
        url = submission.preview['images'][0]['source']['url']
        media_list.append({'url': url, 'type': 'photo'})
        
    return media_list

def get_top_comments(submission):
    """Fetches the top 3 comments from a submission."""
    comments_text = []
    try:
        submission.comments.replace_more(limit=0)  # Flatten the comment tree
        top_comments = sorted(submission.comments, key=lambda c: c.score, reverse=True)[:3]
        for comment in top_comments:
            if not comment.author: continue # Skip deleted comments
            author_name = escape_markdown_v2_text(comment.author.name)
            comment_body = escape_markdown_v2_text(comment.body.strip())
            comments_text.append(f"\\*\\*u/{author_name}\\*\\*: {comment_body}")
    except Exception as e:
        logging.warning(f"Could not retrieve comments for post {submission.id}: {e}")
    return "\n\n".join(comments_text)

async def main():
    """Main function to run the bot loop."""
    logging.info("Starting bot.")
    while True:
        subreddits = load_subreddits()
        if not subreddits:
            logging.error("No subreddits found in configuration. Please add some.")
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        start_time = time.time()
        for subreddit_name, topic_id in subreddits.items():
            logging.info(f"Checking r/{subreddit_name} for new posts...")
            subreddit = reddit.subreddit(subreddit_name)

            # Iterate through the newest submissions. We'll check the creation time.
            for submission in subreddit.new(limit=25):  # Limiting to 25 to stay within rate limits
                # Check if the post is recent (last 30 minutes) and not already processed
                if submission.created_utc > (time.time() - CHECK_INTERVAL_SECONDS) and submission.id not in processed_posts:
                    media_list = get_submission_media(submission)
                    if media_list:
                        logging.info(f"Found new media post in r/{subreddit_name}: {submission.title}")
                        
                        # Add to processed set immediately to avoid duplicates in this run.
                        processed_posts.add(submission.id)
                        
                        # Prepare the message caption with escaped characters
                        escaped_title = escape_markdown_v2_text(submission.title)
                        author_name = submission.author.name if submission.author else '[deleted]'
                        escaped_author = escape_markdown_v2_text(author_name)
                        
                        caption = (
                            f"\\*\\*New Post from r/{escape_markdown_v2_text(submission.subreddit.display_name)}\\*\\*\n"
                            f"\\*\\*Title\\*\\*: {escaped_title}\n"
                            f"\\*\\*Author\\*\\*: u/{escaped_author}\n\n"
                            f"\\*\\*Link\\*\\*: [Click to view post]({submission.url})\n\n"
                        )
                        
                        # Get top comments
                        comments = get_top_comments(submission)
                        if comments:
                            caption += f"\\*\\*Top Comments:\\*\\*\n{comments}"
                        
                        try:
                            if len(media_list) > 1:
                                # This is a gallery/album. Send as a media group.
                                # The caption is attached to the first item in the group.
                                media_group = []
                                for i, media_item in enumerate(media_list):
                                    if media_item['type'] == 'photo':
                                        if i == 0:
                                            media_group.append(InputMediaPhoto(media=media_item['url'], caption=caption, parse_mode=ParseMode.MARKDOWN_V2))
                                        else:
                                            media_group.append(InputMediaPhoto(media=media_item['url']))
                                    elif media_item['type'] == 'video':
                                        if i == 0:
                                            media_group.append(InputMediaVideo(media=media_item['url'], caption=caption, parse_mode=ParseMode.MARKDOWN_V2))
                                        else:
                                            media_group.append(InputMediaVideo(media=media_item['url']))

                                await bot.send_media_group(
                                    chat_id=TELEGRAM_GROUP_ID,
                                    media=media_group,
                                    message_thread_id=topic_id
                                )
                            else:
                                # It's a single photo or video.
                                media_item = media_list[0]
                                if media_item['type'] == 'photo':
                                    await bot.send_photo(
                                        chat_id=TELEGRAM_GROUP_ID,
                                        photo=media_item['url'],
                                        caption=caption,
                                        parse_mode=ParseMode.MARKDOWN_V2,
                                        message_thread_id=topic_id,
                                    )
                                elif media_item['type'] == 'video':
                                    await bot.send_video(
                                        chat_id=TELEGRAM_GROUP_ID,
                                        video=media_item['url'],
                                        caption=caption,
                                        parse_mode=ParseMode.MARKDOWN_V2,
                                        message_thread_id=topic_id,
                                    )
                            logging.info(f"Successfully sent post {submission.id} to Telegram.")
                        except Exception as e:
                            logging.error(f"Failed to send post {submission.id} to Telegram: {e}")
            
        end_time = time.time()
        duration = end_time - start_time
        sleep_duration = max(0, CHECK_INTERVAL_SECONDS - duration)
        
        logging.info(f"Loop finished in {duration:.2f} seconds. Sleeping for {sleep_duration:.2f} seconds.")
        time.sleep(sleep_duration)

if __name__ == "__main__":
    asyncio.run(main())
