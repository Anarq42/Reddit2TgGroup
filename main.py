import os
import sys
import time
import logging
import requests
import asyncio
import re
from dotenv import load_dotenv
import praw
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Helper Functions ---
def escape_html_text(text):
    """Escapes HTML special characters in a string."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def get_top_comments(submission, num_comments=5, last_hours=12):
    """Fetches and formats top/recent comments."""
    comments_str = ""
    comment_ids = set()
    try:
        submission.comments.replace_more(limit=None)
        comments_list = submission.comments.list()
        
        twelve_hours_ago = time.time() - (last_hours * 3600)
        recent_comments = [c for c in comments_list if c.created_utc >= twelve_hours_ago]
        
        # Combine top and recent comments
        combined_comments = sorted(comments_list, key=lambda c: c.score, reverse=True)[:num_comments + len(recent_comments)]
        for comment in combined_comments:
            if comment.id not in comment_ids:
                author = escape_html_text(str(comment.author))
                body = escape_html_text(comment.body)
                comments_str += f"<b>{author}</b>: <code>{body}</code>\n\n"
                comment_ids.add(comment.id)
    except Exception as e:
        logger.error(f"Error fetching comments for post {submission.id}: {e}")
    return comments_str

def get_media_urls(submission):
    """Extracts media URLs with proper validation."""
    media_list = []
    url = submission.url

    if hasattr(submission, 'is_video') and submission.is_video:
        media_data = submission.media.get('reddit_video', {})
        if 'fallback_url' not in media_data:
            logger.error(f"Missing fallback URL for video post {submission.id}")
            return media_list
        media_list.append((media_data['fallback_url'], 'video'))
        return media_list

    # Check for external video URLs
    if 'gfycat.com' in url or 'redgifs.com' in url or url.endswith('.gifv'):
        media_list.append((url, 'video'))

    # Check for GIFs
    elif url.endswith('.gif'):
        media_list.append((url, 'gif'))

    # Check for Reddit gallery posts
    elif hasattr(submission, 'media_metadata') and hasattr(submission, 'gallery_data'):
        gallery_data = submission.gallery_data
        if gallery_data:
            for item in gallery_data.get('items', []):
                media_id = item.get('media_id')
                if not media_id:
                    continue
                meta = submission.media_metadata.get(media_id)
                if not meta:
                    continue

                media_type = 'photo'
                s_data = meta.get('s')
                if s_data:
                    media_url = s_data.get('u')
                    if media_url:
                        if meta.get('e') == 'RedditVideo':
                            media_type = 'video'
                        elif meta.get('e') == 'AnimatedImage':
                            media_type = 'gif'
                            media_url = s_data.get('gif')
                        media_list.append((media_url, media_type))

    # Check for direct images from Reddit
    elif re.match(r'^https://(i.redd.it|preview.redd.it)/.*\.(jpg|jpeg|png)$', url):
        media_list.append((url, 'photo'))

    return media_list

async def download_media(reddit, url, post_link):
    """Downloads media from URL using PRAW's session for Reddit content."""
    if "redd.it" in url:
        try:
            def reddit_download():
                response = reddit.session.get(url, stream=True)
                response.raise_for_status()
                return response.raw.read()
            return await asyncio.to_thread(reddit_download)
        except Exception as e:
            logger.error(f"Failed to download Reddit media: {e}")
            return None
    else:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': post_link
        }
        try:
            response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download media: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error downloading media: {e}")
            return None

async def send_to_telegram(bot, reddit, chat_id, topic_id, submission, media_list, error_topic_id):
    """Sends post to Telegram with media handling."""
    title = escape_html_text(submission.title)
    author = escape_html_text(str(submission.author))
    post_link = f"https://reddit.com{submission.permalink}"
    comments_text = get_top_comments(submission)

    caption = (
        f"<b>New Post from r/{submission.subreddit.display_name}</b>\n"
        f"<b>Title</b>: {title}\n"
        f"<b>Author</b>: u/{author}\n\n"
        f"<b>Link</b>: <a href='{post_link}'>Click to view</a>\n\n"
    )
    if comments_text:
        caption += f"<b>Top Comments:</b>\n{comments_text}"

    if len(media_list) > 10:
        await bot.send_message(chat_id=chat_id, message_thread_id=error_topic_id,
                              text=f"Too many media items ({len(media_list)}) for post {submission.id}")
        logger.warning(f"Media limit exceeded for post {submission.id}")
        return

    try:
        media_content = []
        for i, (url, media_type) in enumerate(media_list):
            content = await download_media(reddit, url, post_link)
            if not content:
                await bot.send_message(chat_id=chat_id, message_thread_id=error_topic_id,
                                      text=f"Failed to download media for post {submission.id}")
                return
            media_content.append(content)

        if len(media_list) > 1:
            media_group = []
            for i, (url, media_type) in enumerate(media_list):
                media = InputMediaVideo(media_content[i], caption=caption if i == 0 else "") if media_type == 'video' \
                    else InputMediaPhoto(media_content[i], caption=caption if i == 0 else "")
                media_group.append(media)
            await bot.send_media_group(chat_id=chat_id, media=media_group, message_thread_id=topic_id)
        else:
            url, media_type = media_list[0]
            if media_type == 'video':
                await bot.send_video(chat_id=chat_id, video=media_content[0], caption=caption, message_thread_id=topic_id)
            elif media_type == 'gif':
                await bot.send_animation(chat_id=chat_id, animation=media_content[0], caption=caption, message_thread_id=topic_id)
            else:
                await bot.send_photo(chat_id=chat_id, photo=media_content[0], caption=caption, message_thread_id=topic_id)
    except telegram.error.TelegramError as e:
        logger.error(f"Telegram API Error: {e}")
    except Exception as e:
        logger.error(f"General Error: {e}")

# --- Persistence ---
def load_processed_posts():
    processed_posts = set()
    try:
        with open("processed_posts.db", "r") as f:
            for line in f:
                pid = line.strip()
                if pid:
                    processed_posts.add(pid)
        logger.info(f"Loaded {len(processed_posts)} processed posts.")
    except FileNotFoundError:
        logger.info("Processed posts file not found, starting with empty set.")
    except Exception as e:
        logger.error(f"Error loading processed_posts.db: {e}")
    return processed_posts

async def save_processed_posts(context):
    processed_posts = context.application.bot_data['processed_posts']
    try:
        with open("processed_posts.db", "w") as f:
            for pid in processed_posts:
                f.write(f"{pid}\n")
        logger.info(f"Saved {len(processed_posts)} processed posts.")
    except Exception as e:
        logger.error(f"Error saving processed_posts.db: {e}")

# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "I forward new media posts from Reddit to Telegram. "
        "Configure subreddits with /add <subreddit> <topic_id>."
    )

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /add <subreddit_name> <topic_id>")
        return
    subreddit_name, topic_id = context.args
    try:
        topic_id = int(topic_id)
    except ValueError:
        await update.message.reply_text("Topic ID must be an integer.")
        return

    reddit = context.application.bot_data['reddit']
    try:
        await asyncio.to_thread(reddit.subreddit(subreddit_name).about)
    except Exception:
        await update.message.reply_text(f"Subreddit r/{subreddit_name} not found.")
        return

    with open("subreddits.db", "a") as f:
        f.write(f"\n{subreddit_name.lower()},{topic_id}")

    context.application.bot_data['restart_flag'] = True
    await update.message.reply_text(f"Added r/{subreddit_name} to watchlist.")

async def comments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_link = context.args[0] if context.args else None
    if not post_link:
        await update.message.reply_text("Provide a Reddit post link.")
        return

    try:
        reddit = context.application.bot_data['reddit']
        submission = await asyncio.to_thread(reddit.submission, url=post_link)
        comments_text = get_top_comments(submission)
        await update.message.reply_text(comments_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Comment error: {e}")
        await update.message.reply_text("Error fetching comments.")

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a post link to reload.")
        return
    text = update.message.reply_to_message.text
    submission_id = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd\.it/)([^/]+)', text).group(1)

    try:
        reddit = context.application.bot_data['reddit']
        submission = await asyncio.to_thread(reddit.submission, id=submission_id)
        media_list = get_media_urls(submission)
        if media_list:
            await send_to_telegram(context.bot, reddit, update.effective_chat.id, update.message.message_thread_id,
                                  submission, media_list, context.application.bot_data['telegram_error_topic_id'])
    except Exception as e:
        logger.error(f"Reload error: {e}")
        await update.message.reply_text("Error reloading post.")

async def handle_reddit_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    submission_id = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd\.it/)([^/]+)', update.message.text).group(1)
    if submission_id not in context.application.bot_data['processed_posts']:
        try:
            reddit = context.application.bot_data['reddit']
            submission = await asyncio.to_thread(reddit.submission, id=submission_id)
            media_list = get_media_urls(submission)
            if media_list:
                await send_to_telegram(context.bot, reddit, update.effective_chat.id, update.message.message_thread_id,
                                      submission, media_list, context.application.bot_data['telegram_error_topic_id'])
                context.application.bot_data['processed_posts'].add(submission_id)
                await save_processed_posts(context)
        except Exception as e:
            logger.error(f"Link processing error: {e}")
            await update.message.reply_text("Error processing post.")

# --- Stream Function ---
async def stream_submissions(context: ContextTypes.DEFAULT_TYPE):
    reddit = context.application.bot_data['reddit']
    bot = context.application.bot
    subreddit_stream = reddit.subreddit('+'.join(context.application.bot_data['subreddits_config'].keys())).stream.submissions(skip_existing=True)
    
    async for submission in subreddit_stream:
        if context.application.bot_data.get('restart_flag'):
            context.application.bot_data['restart_flag'] = False
            logger.info("Stream restarted.")
            break
        subreddit_lower = submission.subreddit.display_name.lower()
        topic_id = context.application.bot_data['subreddits_config'].get(subreddit_lower)
        if topic_id and submission.id not in context.application.bot_data['processed_posts']:
            media_list = get_media_urls(submission)
            if media_list:
                await send_to_telegram(bot, reddit, context.application.bot_data['telegram_group_id'], topic_id, submission, media_list, context.application.bot_data['telegram_error_topic_id'])
                context.application.bot_data['processed_posts'].add(submission.id)
                await save_processed_posts(context)

# --- Main Function ---
def main() -> None:
    load_dotenv()
    # Environment variables
    reddit_client_id = os.getenv("REDDIT_CLIENT_ID")
    reddit_client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    reddit_username = os.getenv("REDDIT_USERNAME")
    reddit_password = os.getenv("REDDIT_PASSWORD")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_group_id = os.getenv("TELEGRAM_GROUP_ID")
    telegram_error_topic_id = os.getenv("TELEGRAM_ERROR_TOPIC_ID", telegram_group_id)
    
    # Validate variables
    if not all([reddit_client_id, reddit_client_secret, reddit_username, reddit_password, telegram_token, telegram_group_id]):
        logger.error("Missing environment variables.")
        sys.exit(1)
    
    try:
        telegram_group_id = int(telegram_group_id)
        telegram_error_topic_id = int(telegram_error_topic_id)
    except ValueError:
        logger.error("Invalid group or error topic ID.")
        sys.exit(1)

    # Initialize Reddit and Telegram clients
    try:
        reddit = praw.Reddit(
            client_id=reddit_client_id,
            client_secret=reddit_client_secret,
            username=reddit_username,
            password=reddit_password,
            user_agent="Reddit to Telegram Bot"
        )
        application = Application.builder().token(telegram_token).build()
    except Exception as e:
        logger.error(f"Client initialization failed: {e}")
        sys.exit(1)

    # Load subreddit configuration
    subreddits_config = {}
    try:
        with open("subreddits.db", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    subreddit, topic_id = line.split(',')
                    subreddits_config[subreddit.strip().lower()] = int(topic_id.strip())
        logger.info(f"Loaded {len(subreddits_config)} subreddits.")
    except FileNotFoundError:
        with open("subreddits.db", "w") as f:
            f.write("# Subreddit,Topic_ID\n")
        logger.warning("Created new subreddits.db file.")
    except Exception as e:
        logger.error(f"Configuration load failed: {e}")
        sys.exit(1)

    # Initialize bot data
    application.bot_data['reddit'] = reddit
    application.bot_data['subreddits_config'] = subreddits_config
    application.bot_data['telegram_group_id'] = telegram_group_id
    application.bot_data['telegram_error_topic_id'] = telegram_error_topic_id
    application.bot_data['processed_posts'] = load_processed_posts()
    application.bot_data['restart_flag'] = False
    application.bot_data['start_time'] = time.time()

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("comments", comments_command))
    application.add_handler(CommandHandler("reload", reload_command))
    
    # Reddit link handler
    link_regex = r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd\.it/)([^/]+)'
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(link_regex), handle_reddit_link))

    # Start streaming job
    application.job_queue.run_once(stream_submissions, 1)

    # Run the bot
    try:
        application.run_polling()
    finally:
        # Save processed posts on exit
        asyncio.run(save_processed_posts(ContextTypes.DEFAULT_TYPE()))  # Note: This line is illustrative; proper context handling needed
        logger.info("Bot stopped.")

if __name__ == "__main__":
    main()
