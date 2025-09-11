import os
import sys
import time
import logging
import requests
import asyncio
import re
from dotenv import load_dotenv
import telegram
import praw
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# Configure logging to provide detailed output for debugging.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global variables
start_time = time.time()
processed_posts = set()
running_stream = None

# --- Helper Functions ---

def escape_html_text(text):
    """Escapes HTML special characters in a string."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def get_top_comments(submission, num_comments=5, last_hours=12):
    """
    Fetches the top upvoted comments and recent comments for a submission.
    Returns a formatted string of the comments.
    """
    comments_str = ""
    comment_ids = set()
    try:
        submission.comments.replace_more(limit=0)
        
        # Get top 5 comments
        top_upvoted = sorted(submission.comments.list, key=lambda c: c.score, reverse=True)[:num_comments]
        
        # Get most recent comments from the last 12 hours
        twelve_hours_ago = time.time() - (last_hours * 3600)
        recent = [c for c in submission.comments.list if c.created_utc >= twelve_hours_ago]
        
        # Combine and format comments, avoiding duplicates
        for comment in top_upvoted:
            if comment.id not in comment_ids:
                author = escape_html_text(str(comment.author))
                body = escape_html_text(comment.body)
                comments_str += f"<b>{author}</b>: <code>{body}</code>\n\n"
                comment_ids.add(comment.id)
        
        for comment in recent:
            if comment.id not in comment_ids:
                author = escape_html_text(str(comment.author))
                body = escape_html_text(comment.body)
                comments_str += f"<b>{author}</b>: <code>{body}</code>\n\n"
                comment_ids.add(comment.id)
                
    except Exception as e:
        logger.error(f"Error fetching comments for post {submission.id}: {e}")
    return comments_str

def get_media_urls(submission):
    """
    Extracts media URLs from a submission, handling different post types.
    Returns a list of tuples: (media_url, media_type).
    """
    media_list = []
    
    # Handle direct video posts from Reddit
    if hasattr(submission, 'is_video') and submission.is_video:
        url = submission.media['reddit_video']['fallback_url'].split("?")[0]
        media_list.append((url, 'video'))
        return media_list

    url = submission.url
    
    # Check for third-party videos and animated GIFs first
    if 'gfycat.com' in url or 'redgifs.com' in url or url.endswith('.gifv'):
        media_list.append((url, 'video'))
    elif url.endswith('.gif'):
        media_list.append((url, 'gif'))
    
    # Check for Reddit's native galleries
    elif hasattr(submission, 'media_metadata') and 'gallery_data' in submission.__dict__:
        for item in submission.gallery_data['items']:
            media_id = item['media_id']
            meta = submission.media_metadata[media_id]
            
            media_type = 'photo'
            best_url = meta['s']['u'] # Fallback to the 'u' key for a high-quality preview

            if meta['e'] == 'RedditVideo':
                media_type = 'video'
            elif meta['e'] == 'AnimatedImage':
                media_type = 'gif'
                best_url = meta['s']['gif']
            
            # Clean URL to prevent issues with Telegram's API
            clean_url = best_url.split("?")[0]
            media_list.append((clean_url, media_type))
    
    # Check for a single, direct image hosted on Reddit
    elif re.match(r'^https://(i.redd.it|preview.redd.it)/.*\.(jpg|jpeg|png)$', url):
        media_list.append((url, 'photo'))

    return media_list

def download_media(reddit, url, post_link):
    """Downloads media from a URL, using PRAW's session for Reddit-hosted media."""
    # Use PRAW's session for Reddit-hosted media to avoid 403 errors
    if "redd.it" in url:
        try:
            # Use the PRAW session directly for authenticated download
            response = reddit.s.get(url, stream=True)
            response.raise_for_status()
            return response.raw.read()
        except Exception as e:
            logger.error(f"Failed to download Reddit media with PRAW's session for {url}: {e}")
            return None
    else:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': post_link
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download media from {url}: {e}")
            return None

async def send_error_to_telegram(bot, chat_id, topic_id, error_message):
    """Sends a formatted error message to Telegram."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=f"<b>An error occurred</b>: <code>{error_message}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send error message to Telegram: {e}")

async def send_to_telegram(bot, reddit, chat_id, topic_id, submission, media_list, error_topic_id):
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
                media_content = download_media(reddit, url, post_link)
                if not media_content:
                    await send_error_to_telegram(bot, chat_id, error_topic_id, f"Failed to download media for gallery post {submission.id} from {url}")
                    return

                if media_type == 'video':
                    media_group.append(InputMediaVideo(media=media_content, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML))
                else:
                    media_group.append(InputMediaPhoto(media=media_content, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML))

            if media_group:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    message_thread_id=topic_id
                )
        elif media_list:
            url, media_type = media_list[0]
            media_content = download_media(reddit, url, post_link)
            if not media_content:
                await send_error_to_telegram(bot, chat_id, error_topic_id, f"Failed to download media for single post {submission.id} from {url}")
                return

            if media_type == 'video':
                await bot.send_video(
                    chat_id=chat_id,
                    video=media_content,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=topic_id
                )
            elif media_type == 'gif':
                await bot.send_animation(
                    chat_id=chat_id,
                    animation=media_content,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=topic_id
                )
            else:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=media_content,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=topic_id
                )
        else:
            logger.info(f"Skipping post {submission.id}: No supported media found.")

    except telegram.error.TelegramError as e:
        logger.error(f"Failed to send post {submission.id} to Telegram: {e}")
        await send_error_to_telegram(bot, chat_id, error_topic_id, f"Telegram API error for {submission.id}: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred for post {submission.id}: {e}")
        await send_error_to_telegram(bot, chat_id, error_topic_id, f"Unexpected error for {submission.id}: {e}")

# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a short description of the bot."""
    await update.message.reply_text(
        "Hello! I am a bot that forwards new media posts from Reddit to this Telegram group. "
        "I'll automatically send photos, videos, and GIFs from the subreddits you've configured."
    )

async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder for the owner command."""
    await update.message.reply_text("This command is for the bot owner.")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replies with the bot's uptime if the user is the admin."""
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    if str(update.effective_user.id) == admin_id:
        uptime_seconds = time.time() - start_time
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"
        await update.message.reply_text(f"The bot has been running for: {uptime_str}")
    else:
        await update.message.reply_text("You are not authorized to use this command.")

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Adds a new subreddit to the bot's watchlist."""
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Usage: /add <subreddit_name> <topic_id>")
            return

        subreddit_name, topic_id_str = args
        topic_id = int(topic_id_str)

        # Check if subreddit exists
        try:
            context.job_queue.context['reddit'].subreddit(subreddit_name).id
        except Exception:
            await update.message.reply_text(f"Subreddit r/{subreddit_name} does not exist or is inaccessible.")
            return

        # Check if topic ID is valid (optional, but good practice)
        if not update.effective_chat.is_forum:
             await update.message.reply_text("This command must be used in a Telegram group with topics enabled.")
             return
        
        # Add to subreddits.db file
        with open("subreddits.db", "a") as f:
            f.write(f"\n{subreddit_name.lower()},{topic_id}")
        
        await update.message.reply_text(f"Added r/{subreddit_name} to the watchlist. The bot will restart its stream to apply changes.")
        
        # Restart the stream
        context.job_queue.context['restart_flag'] = True
    
    except ValueError:
        await update.message.reply_text("Invalid Topic ID. Please provide a valid integer.")
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")

async def comments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replies with the top comments of a Reddit post."""
    post_link = None
    if context.args:
        post_link = context.args[0]
    elif update.message.reply_to_message:
        text = update.message.reply_to_message.text
        match = re.search(r'https?://(?:www\.)?reddit\.com/r/[^/]+/comments/([^/]+)/', text)
        if match:
            post_link = update.message.reply_to_message.text
    
    if not post_link:
        await update.message.reply_text("Please provide a Reddit post link or reply to a message containing one.")
        return
    
    try:
        reddit = context.job_queue.context['reddit']
        submission_id = reddit.submission(url=post_link).id
        submission = await asyncio.to_thread(reddit.submission, id=submission_id)
        
        comments_text = get_top_comments(submission, num_comments=5, last_hours=12)
        
        if comments_text:
            await update.message.reply_text(
                f"<b>Top Comments for r/{submission.subreddit.display_name}</b>:\n\n{comments_text}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text("No comments found for this post.")
            
    except Exception as e:
        logger.error(f"Failed to get comments for {post_link}: {e}")
        await update.message.reply_text("An error occurred while fetching comments.")

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-sends a specified Reddit post to the Telegram group for debugging."""
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a message containing a Reddit post link to use this command.")
        return

    text = update.message.reply_to_message.text
    match = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd.it/)([^/]+)', text)

    if match:
        submission_id = match.group(1)
        try:
            reddit = context.job_queue.context['reddit']
            await update.message.reply_text(f"Reloading post with ID: {submission_id}...", reply_to_message_id=update.message.message_id)

            submission = await asyncio.to_thread(reddit.submission, id=submission_id)
            media_list = get_media_urls(submission)

            if not media_list:
                await update.message.reply_text("This post does not contain supported media.")
                return

            await send_to_telegram(
                context.bot,
                reddit,
                update.effective_chat.id,
                update.message.message_thread_id,
                submission,
                media_list,
                int(os.getenv("TELEGRAM_ERROR_TOPIC_ID", update.effective_chat.id))
            )
            logger.info(f"Successfully reloaded and sent post {submission.id} for debugging.")
            await update.message.reply_text("Post reloaded successfully!", reply_to_message_id=update.message.message_id)
        except Exception as e:
            logger.error(f"Failed to reload Reddit post from message: {e}")
            await update.message.reply_text("An error occurred while trying to reload the post.")
    else:
        await update.message.reply_text("Could not find a Reddit post link in the replied message.")

async def handle_reddit_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles messages with a Reddit post link and sends the post."""
    text = update.message.text
    match = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd.it/)([^/]+)', text)
    
    if match:
        submission_id = match.group(1)
        try:
            reddit = context.job_queue.context['reddit']
            submission = await asyncio.to_thread(reddit.submission, id=submission_id)
            
            # Check if post has media
            media_list = get_media_urls(submission)
            if not media_list:
                await update.message.reply_text("This post does not contain supported media.")
                return

            # Check if post is already processed to avoid duplicates
            if submission_id in processed_posts:
                await update.message.reply_text("This post has already been sent.")
                return
            
            await update.message.reply_text("Sending post to the group...", reply_to_message_id=update.message.message_id)
            await send_to_telegram(
                context.bot,
                reddit,
                update.effective_chat.id,
                update.message.message_thread_id,
                submission,
                media_list,
                int(os.getenv("TELEGRAM_ERROR_TOPIC_ID", update.effective_chat.id))
            )
            processed_posts.add(submission.id)
            await update.message.reply_text("Post sent!", reply_to_message_id=update.message.message_id)

        except Exception as e:
            logger.error(f"Failed to process Reddit link from message: {e}")
            await update.message.reply_text("An error occurred while trying to send the post.")

async def stream_submissions(app):
    """Monitors Reddit for new submissions and sends them to Telegram."""
    reddit = app.job_queue.context['reddit']
    bot = app.bot
    subreddits_config = app.job_queue.context['subreddits_config']
    telegram_group_id = app.job_queue.context['telegram_group_id']
    telegram_error_topic_id = app.job_queue.context['telegram_error_topic_id']
    
    while True:
        try:
            subreddit_stream = reddit.subreddit('+'.join(subreddits_config.keys())).stream.submissions(skip_existing=True)
            logger.info("Starting to monitor subreddits in real-time...")
            
            for submission in subreddit_stream:
                if 'restart_flag' in app.job_queue.context and app.job_queue.context['restart_flag']:
                    app.job_queue.context['restart_flag'] = False
                    logger.info("Restarting stream to load new subreddits...")
                    break
                
                logger.info(f"Found new submission: {submission.id} in r/{submission.subreddit.display_name}")
                
                if submission.id in processed_posts:
                    logger.info(f"Skipping post {submission.id}: already processed.")
                    continue
                
                topic_id = subreddits_config.get(submission.subreddit.display_name)
                
                if topic_id is not None:
                    media_list = get_media_urls(submission)
                    if media_list:
                        logger.info(f"Found new media post in r/{submission.subreddit.display_name}: {submission.title}")
                        await send_to_telegram(bot, reddit, telegram_group_id, topic_id, submission, media_list, telegram_error_topic_id)
                        processed_posts.add(submission.id)
                        logger.info(f"Successfully sent post {submission.id} to Telegram.")
                    else:
                        logger.info(f"Skipping post {submission.id} (no supported media).")
            
        except Exception as e:
            logger.error(f"An error occurred in the submission stream: {e}. Restarting stream in 10 seconds...")
            await asyncio.sleep(10)

async def main() -> None:
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
        telegram_error_topic_id = os.getenv("TELEGRAM_ERROR_TOPIC_ID", telegram_group_id)

        if not all([reddit_client_id, reddit_client_secret, reddit_username, reddit_password, telegram_token, telegram_group_id]):
            raise ValueError("Missing one or more required environment variables.")

        telegram_group_id = int(telegram_group_id)
        if telegram_error_topic_id:
            telegram_error_topic_id = int(telegram_error_topic_id)
        else:
            telegram_error_topic_id = None

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
        application = Application.builder().token(telegram_token).build()
        logger.info("Successfully connected to Telegram.")
    except Exception as e:
        logger.error(f"Failed to connect to Telegram: {e}")
        sys.exit(1)

    logger.info("Starting bot.")

    subreddits_config = {}

    try:
        with open("subreddits.db", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    subreddit, topic_id = line.split(',')
                    subreddits_config[subreddit.strip().lower()] = int(topic_id.strip())

        logger.info(f"Loaded {len(subreddits_config)} subreddits from configuration.")
    except FileNotFoundError:
        logger.error("subreddits.db file not found. Please create it.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error reading subreddits.db: {e}")
        sys.exit(1)

    # Store global state in the application context
    application.job_queue.context['reddit'] = reddit
    application.job_queue.context['subreddits_config'] = subreddits_config
    application.job_queue.context['telegram_group_id'] = telegram_group_id
    application.job_queue.context['telegram_error_topic_id'] = telegram_error_topic_id
    application.job_queue.context['restart_flag'] = False

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("comments", comments_command))
    application.add_handler(CommandHandler("owner", owner_command))
    application.add_handler(CommandHandler("reload", reload_command))
    
    # Add handler for messages containing a Reddit link
    link_regex = r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd.it/)([^/]+)'
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(link_regex), handle_reddit_link))

    # Add a job to run the streaming function
    application.job_queue.run_once(stream_submissions, 1)

    # Run the bot
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
