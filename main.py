import os
import sys
import time
import logging
import requests
import asyncio
import re
import threading
from dotenv import load_dotenv
import praw
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ApplicationHandler
from telegram.constants import ParseMode

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Helper Functions ---
def escape_html_text(text):
    """Escapes HTML special characters in a string."""
    if text is None:
        return ""
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
        submission.comments.replace_more(limit=None)  # Expand all comments
        comments_list = submission.comments.list()
        
        twelve_hours_ago = time.time() - (last_hours * 3600)
        recent_comments = [c for c in comments_list if c.created_utc >= twelve_hours_ago]
        
        # Combine top and recent comments
        combined_comments = sorted(comments_list, key=lambda c: c.score, reverse=True)[:num_comments]
        # Add recent comments not already in top list
        for comment in recent_comments:
            if comment not in combined_comments and comment.id not in comment_ids:
                combined_comments.append(comment)
                comment_ids.add(comment.id)
        
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

    # Direct Reddit video
    if hasattr(submission, 'is_video') and submission.is_video:
        media_data = submission.media.get('reddit_video', {})
        if 'fallback_url' not in media_data:
            logger.error(f"Missing fallback URL for video post {submission.id}")
            return media_list
        media_list.append((media_data['fallback_url'], 'video'))
        return media_list

    # Third-party video/GIF
    if 'gfycat.com' in url or 'redgifs.com' in url or url.endswith('.gifv'):
        media_list.append((url, 'video'))
    elif url.endswith('.gif'):
        media_list.append((url, 'gif'))
    # Reddit gallery
    elif hasattr(submission, 'media_metadata') and hasattr(submission, 'gallery_data'):
        gallery_data = submission.gallery_data
        if not gallery_data:
            logger.error(f"Empty gallery data for post {submission.id}")
            return media_list
        for item in gallery_data.get('items', []):
            media_id = item.get('media_id')
            if not media_id:
                logger.warning(f"Missing media_id in gallery item for {submission.id}")
                continue
            meta = submission.media_metadata.get(media_id)
            if not meta:
                logger.warning(f"Missing metadata for media_id {media_id} in {submission.id}")
                continue

            media_type = 'photo'
            s_data = meta.get('s')
            media_e = meta.get('e')
            if not s_data:
                logger.error(f"Missing 's' data in metadata for {media_id} in {submission.id}")
                continue
            media_url = s_data.get('u')
            if media_e == 'RedditVideo':
                media_type = 'video'
            elif media_e == 'AnimatedImage':
                media_type = 'gif'
                media_url = s_data.get('gif')
                if not media_url:
                    logger.error(f"Missing 'gif' URL in AnimatedImage metadata for {media_id}")
                    continue
            else:
                if media_e and media_e != 'photo':
                    logger.warning(f"Unknown media type '{media_e}' for {media_id} - defaulting to photo")
            
            if not media_url:
                logger.error(f"Missing media URL for {media_id} in {submission.id}")
                continue
            media_list.append((media_url, media_type))
    # Direct image
    elif re.match(r'^https://(i.redd.it|preview.redd.it)/.*\.(jpg|jpeg|png)$', url):
        media_list.append((url, 'photo'))
    
    return media_list

async def download_media(reddit, url, post_link):
    """Non-blocking media download using threads."""
    if "redd.it" in url:
        try:
            def reddit_download():
                response = reddit.session.get(url, stream=True)
                response.raise_for_status()
                return response.raw.read()
            return await asyncio.to_thread(reddit_download)
        except Exception as e:
            logger.error(f"Reddit media download failed ({url}): {e}")
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
            logger.error(f"Media download failed ({url}): {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected download error ({url}): {e}")
            return None

async def send_error_to_telegram(bot, chat_id, topic_id, error_message):
    """Sends error messages to Telegram."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=f"<b>Error</b>: <code>{error_message}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

async def send_to_telegram(bot, reddit, chat_id, topic_id, submission, media_list, error_topic_id):
    """Sends post to Telegram with media handling, returns success status."""
    title = escape_html_text(submission.title)
    author = escape_html_text(str(submission.author))
    post_link = f"https://reddit.com{submission.permalink}"
    comments_text = get_top_comments(submission)

    caption = (
        f"<b>New Post from r/{submission.subreddit.display_name}</b>\n"
        f"<b>Title</b>: {title}\n"
        f"<b>Author</b>: u/{author}\n\n"
        f"<b>Link</b>: <a href='{post_link}'>View Post</a>\n\n"
    )
    if comments_text:
        caption += f"<b>Top Comments:</b>\n{comments_text}"

    if len(media_list) > 10:
        await send_error_to_telegram(bot, chat_id, error_topic_id, 
                                    f"Too many media items ({len(media_list)}) for post {submission.id}")
        logger.warning(f"Media limit exceeded for {submission.id}")
        return False

    try:
        if not media_list:
            logger.info(f"Skipped post {submission.id} (no media)")
            return False

        # Download media content first to check for failures
        media_content = []
        for url, _ in media_list:
            content = await download_media(reddit, url, post_link)
            if not content:
                await send_error_to_telegram(bot, chat_id, error_topic_id, 
                                            f"Failed to download media for post {submission.id} (URL: {url})")
                return False
            media_content.append(content)

        # Prepare media group or single media
        if len(media_list) > 1:
            media_group = []
            for i, (url, media_type) in enumerate(media_list):
                content = media_content[i]
                if media_type == 'video':
                    media = InputMediaVideo(content, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML)
                else:
                    media = InputMediaPhoto(content, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML)
                media_group.append(media)
            await bot.send_media_group(chat_id=chat_id, media=media_group, message_thread_id=topic_id)
        else:
            url, media_type = media_list[0]
            content = media_content[0]
            if media_type == 'video':
                await bot.send_video(chat_id=chat_id, video=content, caption=caption, parse_mode=ParseMode.HTML, message_thread_id=topic_id)
            elif media_type == 'gif':
                await bot.send_animation(chat_id=chat_id, animation=content, caption=caption, parse_mode=ParseMode.HTML, message_thread_id=topic_id)
            else:
                await bot.send_photo(chat_id=chat_id, photo=content, caption=caption, parse_mode=ParseMode.HTML, message_thread_id=topic_id)
        logger.info(f"Successfully sent post {submission.id}")
        return True
    except telegram.error.TelegramError as e:
        logger.error(f"Telegram API Error for {submission.id}: {e}")
        await send_error_to_telegram(bot, chat_id, error_topic_id, f"Telegram API error: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error for {submission.id}: {e}", exc_info=True)
        await send_error_to_telegram(bot, chat_id, error_topic_id, f"Unexpected error: {str(e)}")
        return False

# --- Persistence ---
def load_processed_posts():
    """Loads processed post IDs from file."""
    processed_posts = set()
    try:
        with open("processed_posts.db", "r") as f:
            for line in f:
                pid = line.strip()
                if pid:
                    processed_posts.add(pid)
        logger.info(f"Loaded {len(processed_posts)} processed posts.")
    except FileNotFoundError:
        logger.info("Processed posts file not found - starting with empty set.")
    except Exception as e:
        logger.error(f"Error loading processed_posts.db: {e}")
    return processed_posts

async def save_processed_posts(context):
    """Saves processed post IDs to file."""
    processed_posts = context.application.bot_data.get('processed_posts', set())
    try:
        with open("processed_posts.db", "w") as f:
            for pid in processed_posts:
                f.write(f"{pid}\n")
        logger.info(f"Saved {len(processed_posts)} processed posts.")
    except Exception as e:
        logger.error(f"Error saving processed_posts.db: {e}")

# --- Error Handling ---
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles errors during update processing."""
    error = context.error
    logger.error(f"Error handling update {update}: {error}", exc_info=True)
    # Optionally send error notification to admin or error topic
    error_topic_id = context.application.bot_data.get('telegram_error_topic_id')
    if error_topic_id and update:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                message_thread_id=error_topic_id,
                text=f"Error occurred: {str(error)}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to notify error topic: {e}")

# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "I forward new media posts from Reddit to Telegram. "
        "Configure subreddits with /add <subreddit> <topic_id>"
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = context.application.bot_data.get('admin_id')
    user_id = update.effective_user.id
    if admin_id is None or user_id != admin_id:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    start_time = context.application.bot_data.get('start_time', time.time())
    uptime_seconds = time.time() - start_time
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds):.0f}s"
    await update.message.reply_text(f"The bot has been running for: {uptime_str}")

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat.is_forum:
        await update.message.reply_text("This command must be used in a Telegram group with topics enabled.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /add <subreddit_name> <topic_id>")
        return
    subreddit_name, topic_id_str = args
    try:
        topic_id = int(topic_id_str)
        if topic_id <= 0:
            raise ValueError("Topic ID must be a positive integer.")
    except ValueError:
        await update.message.reply_text("Invalid Topic ID. Please provide a valid positive integer.")
        return

    reddit = context.application.bot_data.get('reddit')
    if not reddit:
        await update.message.reply_text("Reddit client not initialized.")
        return

    # Verify subreddit exists
    try:
        await asyncio.to_thread(reddit.subreddit(subreddit_name).about)
    except Exception as e:
        logger.error(f"Subreddit check failed for {subreddit_name}: {e}")
        await update.message.reply_text(f"Subreddit r/{subreddit_name} does not exist or is inaccessible.")
        return

    # Check for existing entry
    subreddit_lower = subreddit_name.lower()
    existing_subreddits = set()
    try:
        with open("subreddits.db", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    sub = line.split(',', 1)[0].strip().lower()
                    existing_subreddits.add(sub)
        if subreddit_lower in existing_subreddits:
            await update.message.reply_text(f"r/{subreddit_name} is already in the watchlist.")
            return
    except Exception as e:
        logger.error(f"Error checking subreddits.db: {e}")
        await update.message.reply_text("An error occurred while checking the watchlist.")
        return

    # Add to config file
    try:
        with open("subreddits.db", "a") as f:
            f.write(f"\n{subreddit_lower},{topic_id}")
        await update.message.reply_text(f"Added r/{subreddit_name} to the watchlist. Stream will reload.")
        context.application.bot_data['restart_flag'] = True
    except Exception as e:
        logger.error(f"Error writing to subreddits.db: {e}")
        await update.message.reply_text("Failed to add subreddit to watchlist.")

async def comments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_link = None
    if context.args:
        post_link = context.args[0]
    elif update.message.reply_to_message:
        text = update.message.reply_to_message.text
        if not text:
            await update.message.reply_text("Replied message has no text.")
            return
        # Extract full post URL
        match_reddit = re.search(r'https?://(?:www\.)?reddit\.com/r/[^/]+/comments/([^/]+)/', text)
        match_redd_it = re.search(r'https?://redd\.it/([^/]+)', text)
        if match_reddit:
            post_id = match_reddit.group(1)
            post_link = f"https://reddit.com/r/{text.split('/r/')[-1].split('/')[0]}/comments/{post_id}"
        elif match_redd_it:
            post_id = match_redd_it.group(1)
            post_link = f"https://reddit.com/comments/{post_id}"
    
    if not post_link:
        await update.message.reply_text("Please provide a Reddit post link or reply to a message containing one.")
        return

    try:
        reddit = context.application.bot_data['reddit']
        submission = await asyncio.to_thread(reddit.submission, url=post_link)
        comments_text = get_top_comments(submission)
        if comments_text:
            await update.message.reply_text(
                f"<b>Top Comments for r/{submission.subreddit.display_name}</b>:\n\n{comments_text}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text("No comments found for this post.")
    except Exception as e:
        logger.error(f"Comments retrieval error: {e}")
        await update.message.reply_text("An error occurred while fetching comments.")

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-sends a specified Reddit post to the Telegram group for debugging (with topic validation)."""
    # Validate chat is forum
    if not update.effective_chat.is_forum:
        await update.message.reply_text("This command must be used in a Telegram group with topics enabled.")
        return

    # Check if user replied to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a message containing a Reddit post link to use this command.")
        return

    # Extract text from replied message
    text = update.message.reply_to_message.text
    if text is None:
        await update.message.reply_text("The replied message does not contain text.")
        return

    # Extract submission ID from text
    match = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd\.it/)([^/]+)', text)
    if not match:
        await update.message.reply_text("Could not find a Reddit post link in the replied message.")
        return
    submission_id = match.group(1)

    try:
        # Notify user
        await update.message.reply_text(f"Reloading post with ID: {submission_id}...", 
                                      reply_to_message_id=update.message.message_id)
        
        # Fetch submission
        reddit = context.application.bot_data['reddit']
        submission = await asyncio.to_thread(reddit.submission, id=submission_id)
        
        # Check media presence
        media_list = get_media_urls(submission)
        if not media_list:
            await update.message.reply_text("This post does not contain supported media.")
            return
        
        # Check if already processed (with lock)
        processed_posts = context.application.bot_data.get('processed_posts', set())
        processed_posts_lock = context.application.bot_data.get('processed_posts_lock', asyncio.Lock())
        async with processed_posts_lock:
            if submission.id in processed_posts:
                await update.message.reply_text("This post has already been processed.")
                return
        
        # Send media
        send_success = await send_to_telegram(
            context.bot,
            reddit,
            update.effective_chat.id,
            update.message.message_thread_id,
            submission,
            media_list,
            context.application.bot_data.get('telegram_error_topic_id', update.effective_chat.id)
        )
        
        # Update processed posts if successful (with lock)
        if send_success:
            async with processed_posts_lock:
                processed_posts.add(submission.id)
            await save_processed_posts(context)
            await update.message.reply_text("Post reloaded successfully!", 
                                          reply_to_message_id=update.message.message_id)
            logger.info(f"Successfully reloaded post {submission.id}")
        else:
            await update.message.reply_text("Failed to reload post.", 
                                          reply_to_message_id=update.message.message_id)
    except Exception as e:
        logger.error(f"Reload error for post {submission_id}: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while trying to reload the post.", 
                                      reply_to_message_id=update.message.message_id)

async def handle_reddit_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles messages with a Reddit post link and sends the post (with topic validation and proper ID check)."""
    if not update.effective_chat.is_forum:
        await update.message.reply_text("This command must be used in a Telegram group with topics enabled.")
        return

    text = update.message.text
    if not text:
        await update.message.reply_text("Message contains no text.")
        return

    # Extract submission ID from URL
    match = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd\.it/)([^/]+)', text)
    if not match:
        return  # No valid link found, do nothing
    
    submission_id = match.group(1)
    try:
        # Check if post already processed
        processed_posts = context.application.bot_data.get('processed_posts', set())
        processed_posts_lock = context.application.bot_data.get('processed_posts_lock', asyncio.Lock())
        async with processed_posts_lock:
            if submission_id in processed_posts:
                await update.message.reply_text("This post has already been sent.")
                return
        
        # Fetch submission
        reddit = context.application.bot_data['reddit']
        submission = await asyncio.to_thread(reddit.submission, id=submission_id)
        
        # Check media presence
        media_list = get_media_urls(submission)
        if not media_list:
            await update.message.reply_text("This post does not contain supported media.")
            return
        
        # Notify user and send
        await update.message.reply_text("Sending post to the group...", reply_to_message_id=update.message.message_id)
        send_success = await send_to_telegram(
            context.bot,
            reddit,
            update.effective_chat.id,
            update.message.message_thread_id,
            submission,
            media_list,
            context.application.bot_data.get('telegram_error_topic_id', update.effective_chat.id)
        )
        
        # Update processed posts if successful (with lock)
        if send_success:
            async with processed_posts_lock:
                processed_posts.add(submission_id)
            await save_processed_posts(context)
            await update.message.reply_text("Post sent!", reply_to_message_id=update.message.message_id)
            logger.info(f"Successfully sent post {submission_id}")
        else:
            await update.message.reply_text("Failed to send post.", reply_to_message_id=update.message.message_id)
    except Exception as e:
        logger.error(f"Link processing error for {submission_id}: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while trying to send the post.")

# --- Stream Function ---
async def stream_submissions(context: ContextTypes.DEFAULT_TYPE):
    """Monitors Reddit for new submissions and sends them to Telegram (with restart logic)."""
    reddit = context.application.bot_data.get('reddit')
    bot = context.application.bot
    subreddits_config = context.application.bot_data.get('subreddits_config', {})
    group_id = context.application.bot_data.get('telegram_group_id')
    error_topic_id = context.application.bot_data.get('telegram_error_topic_id', group_id)
    processed_posts = context.application.bot_data.get('processed_posts', set())
    processed_posts_lock = context.application.bot_data.get('processed_posts_lock', asyncio.Lock())

    if not reddit or not bot or not group_id:
        logger.error("Missing Reddit/Telegram configuration in bot data.")
        return

    while True:
        # Check and handle restart flag
        if context.application.bot_data.get('restart_flag'):
            context.application.bot_data['restart_flag'] = False
            logger.info("Reloading subreddits configuration...")
            try:
                new_subreddits_config = {}
                with open("subreddits.db", "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            parts = line.split(',', 1)
                            if len(parts) != 2:
                                continue
                            subreddit = parts[0].strip().lower()
                            topic_id = int(parts[1].strip())
                            new_subreddits_config[subreddit] = topic_id
                context.application.bot_data['subreddits_config'] = new_subreddits_config
                subreddits_config = new_subreddits_config
                logger.info(f"Reloaded {len(subreddits_config)} subreddits.")
            except Exception as e:
                logger.error(f"Error reloading subreddits.db: {e}")
            # Pause briefly before restarting the stream
            await asyncio.sleep(5)
            continue

        if not subreddits_config:
            logger.info("No subreddits configured. Pausing stream...")
            await asyncio.sleep(30)
            continue

        subreddit_names = '+'.join(subreddits_config.keys())
        logger.info(f"Monitoring subreddits: {subreddit_names}")

        try:
            # Get the subreddit stream (sync generator)
            subreddit_stream = reddit.subreddit(subreddit_names).stream.submissions(skip_existing=True)
            
            # Process each submission in the stream
            for submission in subreddit_stream:
                subreddit_lower = submission.subreddit.display_name.lower()
                if subreddit_lower not in subreddits_config:
                    continue  # Skip unconfigured subreddits
                
                topic_id = subreddits_config[subreddit_lower]
                submission_id = submission.id

                # Check if post is already processed (with lock)
                async with processed_posts_lock:
                    if submission_id in processed_posts:
                        logger.info(f"Skipping processed post {submission_id}")
                        continue

                # Check media presence
                media_list = get_media_urls(submission)
                if not media_list:
                    logger.info(f"Skipping post {submission_id}: No supported media")
                    continue

                # Attempt to send the post
                try:
                    logger.info(f"Processing new post {submission_id} from r/{subreddit_lower}")
                    send_success = await send_to_telegram(
                        bot,
                        reddit,
                        group_id,
                        topic_id,
                        submission,
                        media_list,
                        error_topic_id
                    )
                    if send_success:
                        async with processed_posts_lock:
                            processed_posts.add(submission_id)
                        await save_processed_posts(context)
                        logger.info(f"Successfully sent post {submission_id}")
                except Exception as e:
                    logger.error(f"Error sending post {submission_id}: {e}", exc_info=True)
                
                # Optional: Add a small delay between posts to avoid overwhelming the bot
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Stream error: {e}. Restarting stream in 10 seconds...")
            await asyncio.sleep(10)
            # Reset the stream after error
            subreddit_stream = reddit.subreddit(subreddit_names).stream.submissions(skip_existing=True)
            continue

# --- Main Function ---
def main() -> None:
    load_dotenv()

    # --- Environment Variable Validation ---
    required_vars = [
        ('REDDIT_CLIENT_ID', str),
        ('REDDIT_CLIENT_SECRET', str),
        ('REDDIT_USERNAME', str),
        ('REDDIT_PASSWORD', str),
        ('TELEGRAM_BOT_TOKEN', str),
        ('TELEGRAM_GROUP_ID', int),
        ('TELEGRAM_ADMIN_ID', int),
        ('TELEGRAM_ERROR_TOPIC_ID', int)
    ]
    env = {}
    for name, typ in required_vars:
        val = os.getenv(name)
        if val is None:
            logger.error(f"Missing required environment variable: {name}")
            sys.exit(1)
        if typ == int:
            try:
                env[name] = int(val)
            except ValueError:
                logger.error(f"Invalid {name}: must be an integer.")
                sys.exit(1)
        else:
            env[name] = val.strip()

    # --- Initialize Reddit ---
    try:
        reddit = praw.Reddit(
            client_id=env['REDDIT_CLIENT_ID'],
            client_secret=env['REDDIT_CLIENT_SECRET'],
            username=env['REDDIT_USERNAME'],
            password=env['REDDIT_PASSWORD'],
            user_agent="Reddit to Telegram Bot/1.0"
        )
        # Verify connection
        asyncio.run(asyncio.to_thread(reddit.user.me))
        logger.info("Successfully connected to Reddit.")
    except Exception as e:
        logger.error(f"Failed to connect to Reddit: {e}")
        sys.exit(1)

    # --- Initialize Telegram ---
    try:
        application = Application.builder().token(env['TELEGRAM_BOT_TOKEN']).build()
        logger.info("Successfully connected to Telegram.")
    except Exception as e:
        logger.error(f"Failed to connect to Telegram: {e}")
        sys.exit(1)

    # --- Load Configuration ---
    logger.info("Starting bot.")
    
    # Load subreddits config
    try:
        subreddits_config = {}
        with open("subreddits.db", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',', 1)
                    if len(parts) != 2:
                        logger.warning(f"Invalid line in subreddits.db: {line}")
                        continue
                    sub = parts[0].strip().lower()
                    tid = parts[1].strip()
                    try:
                        topic_id = int(tid)
                        subreddits_config[sub] = topic_id
                    except ValueError:
                        logger.warning(f"Invalid Topic ID in line: {line} (must be integer)")
        logger.info(f"Loaded {len(subreddits_config)} subreddits from configuration.")
    except FileNotFoundError:
        # Create empty file if not found
        with open("subreddits.db", "w") as f:
            f.write("# Format: subreddit,topic_id\n")
        subreddits_config = {}
        logger.warning("subreddits.db not found - created new file.")
    except Exception as e:
        logger.error(f"Error loading subreddits.db: {e}")
        sys.exit(1)

    # Load processed posts
    processed_posts = load_processed_posts()
    processed_posts_lock = asyncio.Lock()

    # --- Store Bot Data ---
    application.bot_data.update({
        'reddit': reddit,
        'subreddits_config': subreddits_config,
        'telegram_group_id': env['TELEGRAM_GROUP_ID'],
        'telegram_error_topic_id': env['TELEGRAM_ERROR_TOPIC_ID'],
        'admin_id': env['TELEGRAM_ADMIN_ID'],
        'processed_posts': processed_posts,
        'processed_posts_lock': processed_posts_lock,
        'start_time': time.time(),
        'restart_flag': False
    })

    # --- Register Command Handlers ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("comments", comments_command))
    application.add_handler(CommandHandler("reload", reload_command))

    # Add handler for Reddit links
    link_regex = r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd\.it/)([^/]+)'
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(link_regex), handle_reddit_link))

    # Register error handler
    application.add_error_handler(error_handler)

    # --- Start Streaming Job ---
    application.job_queue.run_once(stream_submissions, 1)

    # --- Run the Bot ---
    try:
        application.run_polling()
    finally:
        # Ensure processed posts are saved on shutdown
        asyncio.run(save_processed_posts(context=ContextTypes.DEFAULT_TYPE()))  # Note: Adjusted to pass a dummy context; ideally use the actual application context
        logger.info("Bot stopped - saved processed posts.")

if __name__ == "__main__":
    main()
