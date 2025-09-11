# main.py
import os
import sys
import time
import logging
import requests
import asyncio
import re
import io
from dotenv import load_dotenv
import praw
import telegram
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def escape_html_text(text):
    """Escapes HTML special characters in a string."""
    if text is None:
        return ""
    return (
        str(text).replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def get_top_comments(submission, num_comments=5, last_hours=12):
    """
    Fetches top upvoted comments and recent comments for a submission.
    Returns a formatted string of the comments.
    """
    comments_str = ""
    comment_ids = set()
    try:
        submission.comments.replace_more(limit=0)
        comment_list = submission.comments.list()  # <- call the method
        top_upvoted = sorted(comment_list, key=lambda c: getattr(c, "score", 0), reverse=True)[:num_comments]

        twelve_hours_ago = time.time() - (last_hours * 3600)
        recent = [c for c in comment_list if getattr(c, "created_utc", 0) >= twelve_hours_ago]

        for comment in top_upvoted:
            cid = getattr(comment, "id", None)
            if cid and cid not in comment_ids:
                author = escape_html_text(comment.author)
                body = escape_html_text(comment.body)
                comments_str += f"<b>{author}</b>: <code>{body}</code>\n\n"
                comment_ids.add(cid)

        for comment in recent:
            cid = getattr(comment, "id", None)
            if cid and cid not in comment_ids:
                author = escape_html_text(comment.author)
                body = escape_html_text(comment.body)
                comments_str += f"<b>{author}</b>: <code>{body}</code>\n\n"
                comment_ids.add(cid)

    except Exception:
        logger.exception(f"Error fetching comments for post {getattr(submission, 'id', 'unknown')}:")
    return comments_str

def get_media_urls(submission):
    """
    Extracts media URLs from a submission, handling different post types.
    Returns a list of tuples: (media_url, media_type) where media_type in {'video','gif','photo'}.
    """
    media_list = []
    try:
        # native reddit video
        if getattr(submission, "is_video", False):
            try:
                url = submission.media['reddit_video']['fallback_url'].split("?")[0]
                media_list.append((url, 'video'))
                return media_list
            except Exception:
                logger.debug("Could not parse submission.media for reddit_video", exc_info=True)

        url = getattr(submission, "url", "") or ""

        # third-party hosts or gifv
        if any(h in url for h in ("gfycat.com", "redgifs.com")) or url.endswith('.gifv'):
            media_list.append((url, 'video'))
        elif url.endswith('.gif'):
            media_list.append((url, 'gif'))
        # gallery
        elif hasattr(submission, 'media_metadata') and hasattr(submission, 'gallery_data') and submission.gallery_data:
            for item in submission.gallery_data.get('items', []):
                media_id = item.get('media_id')
                meta = submission.media_metadata.get(media_id, {})
                s = meta.get('s', {}) or {}
                best_url = s.get('u') or s.get('gif') or s.get('mp4') or ''
                clean_url = best_url.split("?")[0]
                mtype = meta.get('e', '').lower()
                if mtype == 'redditvideo':
                    media_type = 'video'
                elif mtype == 'animatedimage':
                    media_type = 'gif'
                else:
                    media_type = 'photo'
                if clean_url:
                    media_list.append((clean_url, media_type))
        # direct reddit images
        elif re.match(r'^https?://(i\.redd\.it|preview\.redd\.it)/.*\.(jpg|jpeg|png)$', url):
            media_list.append((url, 'photo'))
    except Exception:
        logger.exception(f"Error extracting media URLs for submission {getattr(submission, 'id', 'unknown')}:")
    return media_list

def download_media_sync(url, post_link):
    """
    Synchronous media downloader. Returns bytes or None.
    Use referer header to reduce the chance of 403.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': post_link
    }
    try:
        r = requests.get(url, headers=headers, timeout=20, stream=True)
        r.raise_for_status()
        return r.content
    except Exception:
        logger.exception(f"Failed to download media from {url}:")
        return None

async def send_error_to_telegram(bot: telegram.Bot, chat_id: int, topic_id: int, error_message: str):
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=f"<b>An error occurred</b>: <code>{escape_html_text(error_message)}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        logger.exception("Failed to send error message to Telegram:")

async def send_to_telegram(bot: telegram.Bot, reddit, chat_id, topic_id, submission, media_list, error_topic_id):
    """
    Sends a post to Telegram.
    Downloads media with asyncio.to_thread(download_media_sync,...).
    """
    title = escape_html_text(submission.title)
    author = escape_html_text(str(submission.author))
    post_link = f"https://reddit.com{submission.permalink}"
    comments_text = get_top_comments(submission)

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
                media_bytes = await asyncio.to_thread(download_media_sync, url, post_link)
                if not media_bytes:
                    await send_error_to_telegram(bot, chat_id, error_topic_id, f"Failed to download media for gallery post {submission.id} from {url}")
                    return

                bio = io.BytesIO(media_bytes)
                if media_type == 'video':
                    bio.name = f"{submission.id}_{i}.mp4"
                    media_group.append(InputMediaVideo(media=bio, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML))
                else:
                    bio.name = f"{submission.id}_{i}.jpg"
                    media_group.append(InputMediaPhoto(media=bio, caption=caption if i == 0 else "", parse_mode=ParseMode.HTML))

            # rewind and send
            for m in media_group:
                try:
                    if hasattr(m.media, "seek"):
                        m.media.seek(0)
                except Exception:
                    pass
            await bot.send_media_group(chat_id=chat_id, media=media_group, message_thread_id=topic_id)

        elif media_list:
            url, media_type = media_list[0]
            media_bytes = await asyncio.to_thread(download_media_sync, url, post_link)
            if not media_bytes:
                await send_error_to_telegram(bot, chat_id, error_topic_id, f"Failed to download media for single post {submission.id} from {url}")
                return

            bio = io.BytesIO(media_bytes)
            if media_type == 'video':
                bio.name = f"{submission.id}.mp4"
                bio.seek(0)
                await bot.send_video(chat_id=chat_id, video=bio, caption=caption, parse_mode=ParseMode.HTML, message_thread_id=topic_id)
            elif media_type == 'gif':
                bio.name = f"{submission.id}.gif"
                bio.seek(0)
                await bot.send_animation(chat_id=chat_id, animation=bio, caption=caption, parse_mode=ParseMode.HTML, message_thread_id=topic_id)
            else:
                bio.name = f"{submission.id}.jpg"
                bio.seek(0)
                await bot.send_photo(chat_id=chat_id, photo=bio, caption=caption, parse_mode=ParseMode.HTML, message_thread_id=topic_id)
        else:
            logger.info(f"Skipping post {submission.id}: no supported media found.")
    except telegram.error.TelegramError:
        logger.exception(f"Telegram error when sending post {getattr(submission, 'id', 'unknown')}:")
        await send_error_to_telegram(bot, chat_id, error_topic_id, f"Telegram API error for {getattr(submission, 'id', 'unknown')}")
    except Exception:
        logger.exception(f"Unexpected error sending post {getattr(submission, 'id', 'unknown')}:")
        await send_error_to_telegram(bot, chat_id, error_topic_id, f"Unexpected error for {getattr(submission, 'id', 'unknown')}")

# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "I'm a bot that forwards new media posts from Reddit to this Telegram group. "
        "I'll automatically send photos, videos, and GIFs from configured subreddits."
    )

async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("This command is for the bot owner.")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    start_time = context.application.bot_data.get('start_time')
    if str(update.effective_user.id) == admin_id:
        if start_time:
            uptime_seconds = time.time() - start_time
            days, remainder = divmod(uptime_seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"
            await update.message.reply_text(f"The bot has been running for: {uptime_str}")
        else:
            await update.message.reply_text("Uptime not available.")
    else:
        await update.message.reply_text("You are not authorized to use this command.")

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Usage: /add <subreddit_name> <topic_id>")
            return

        subreddit_name, topic_id_str = args
        topic_id = int(topic_id_str)

        reddit = context.application.bot_data['reddit']
        try:
            subreddit = await asyncio.to_thread(reddit.subreddit, subreddit_name)
            _ = await asyncio.to_thread(lambda: subreddit.fullname)
        except Exception:
            await update.message.reply_text(f"Subreddit r/{subreddit_name} does not exist or is inaccessible.")
            return

        if not update.effective_chat.is_forum:
            await update.message.reply_text("This command must be used in a Telegram group with topics enabled.")
            return

        with open("subreddits.db", "a") as f:
            f.write(f"\n{subreddit_name.lower()},{topic_id}")

        bot_data = context.application.bot_data
        bot_data['subreddits_config'][subreddit_name.lower()] = topic_id
        bot_data['restart_flag'] = True

        await update.message.reply_text(f"Added r/{subreddit_name} to the watchlist. The bot will restart its stream to apply changes.")

    except ValueError:
        await update.message.reply_text("Invalid Topic ID. Please provide a valid integer.")
    except Exception:
        logger.exception("Error in /add command:")
        await update.message.reply_text("An error occurred while adding the subreddit.")

async def comments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_link = None
    if context.args:
        post_link = context.args[0]
    elif update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
        match = re.search(r'https?://(?:www\.)?reddit\.com/r/[^/]+/comments/([^/]+)/', text)
        if match:
            post_link = update.message.reply_to_message.text

    if not post_link:
        await update.message.reply_text("Please provide a Reddit post link or reply to a message containing one.")
        return

    try:
        reddit = context.application.bot_data['reddit']
        submission = await asyncio.to_thread(reddit.submission, url=post_link)
        comments_text = await asyncio.to_thread(get_top_comments, submission, 5, 12)

        if comments_text:
            await update.message.reply_text(
                f"<b>Top Comments for r/{submission.subreddit.display_name}</b>:\n\n{comments_text}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text("No comments found for this post.")
    except Exception:
        logger.exception(f"Failed to get comments for {post_link}:")
        await update.message.reply_text("An error occurred while fetching comments.")

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a message containing a Reddit post link to use this command.")
        return

    text = update.message.reply_to_message.text or ""
    match = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd.it/)([^/]+)', text)

    if match:
        submission_id = match.group(1)
        try:
            reddit = context.application.bot_data['reddit']
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
        except Exception:
            logger.exception("Failed to reload Reddit post from message:")
            await update.message.reply_text("An error occurred while trying to reload the post.")
    else:
        await update.message.reply_text("Could not find a Reddit post link in the replied message.")

async def handle_reddit_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = re.search(r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd.it/)([^/]+)', text)

    if match:
        submission_id = match.group(1)
        try:
            reddit = context.application.bot_data['reddit']
            submission = await asyncio.to_thread(reddit.submission, id=submission_id)

            media_list = get_media_urls(submission)
            if not media_list:
                await update.message.reply_text("This post does not contain supported media.")
                return

            processed_posts = context.application.bot_data.setdefault('processed_posts', set())
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
        except Exception:
            logger.exception("Failed to process Reddit link from message:")
            await update.message.reply_text("An error occurred while trying to send the post.")

# --- Blocking streaming worker (runs in separate thread) ---

def blocking_streaming_loop(application: Application):
    """
    Blocking PRAW streaming loop. Runs inside a background thread via asyncio.to_thread.
    It schedules coroutine tasks on the main loop via loop.call_soon_threadsafe.
    """
    try:
        bot = application.bot
        reddit = application.bot_data['reddit']
        processed_posts = application.bot_data.setdefault('processed_posts', set())
        loop = application.bot_data.get('loop')
        if loop is None:
            logger.error("No asyncio loop in bot_data['loop']; aborting streaming loop.")
            return

        while True:
            subreddits_config = application.bot_data.get('subreddits_config', {})
            if not subreddits_config:
                logger.info("No subreddits configured. Sleeping 10s.")
                time.sleep(10)
                continue

            sub_list = '+'.join(subreddits_config.keys())
            try:
                logger.info("Starting to monitor subreddits in real-time...")
                for submission in reddit.subreddit(sub_list).stream.submissions(skip_existing=True):
                    try:
                        if application.bot_data.get('restart_flag'):
                            application.bot_data['restart_flag'] = False
                            logger.info("Restart flag: rebuilding subreddit list.")
                            break

                        if submission.id in processed_posts:
                            continue

                        topic_id = subreddits_config.get(submission.subreddit.display_name.lower())
                        if topic_id is None:
                            continue

                        media_list = get_media_urls(submission)
                        if not media_list:
                            continue

                        coro = send_to_telegram(bot, reddit, application.bot_data['telegram_group_id'], topic_id, submission, media_list, application.bot_data.get('telegram_error_topic_id'))
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                        processed_posts.add(submission.id)
                        logger.info(f"Scheduled submission {submission.id} to be sent.")
                    except Exception:
                        logger.exception("Error processing submission in streaming loop:")
            except Exception:
                logger.exception("PRAW streaming error; sleeping 10s then retrying...")
                time.sleep(10)
    except Exception:
        logger.exception("Fatal error in blocking_streaming_loop:")

# --- Main ---

async def main() -> None:
    load_dotenv()

    try:
        reddit_client_id = os.getenv("REDDIT_CLIENT_ID")
        reddit_client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        reddit_username = os.getenv("REDDIT_USERNAME")
        reddit_password = os.getenv("REDDIT_PASSWORD")
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_group_id = os.getenv("TELEGRAM_GROUP_ID")
        telegram_error_topic_id = os.getenv("TELEGRAM_ERROR_TOPIC_ID", telegram_group_id)

        if not all([reddit_client_id, reddit_client_secret, reddit_username, reddit_password, telegram_token, telegram_group_id]):
            raise ValueError("Missing required environment variables.")

        telegram_group_id = int(telegram_group_id)
        telegram_error_topic_id = int(telegram_error_topic_id) if telegram_error_topic_id else None
    except (ValueError, TypeError) as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    try:
        reddit = praw.Reddit(
            client_id=reddit_client_id,
            client_secret=reddit_client_secret,
            username=reddit_username,
            password=reddit_password,
            user_agent="Reddit to Telegram Bot v1.0"
        )
        logger.info("Connected to Reddit.")
    except Exception:
        logger.exception("Failed to connect to Reddit:")
        sys.exit(1)

    try:
        application = Application.builder().token(telegram_token).read_timeout(10).write_timeout(10).build()
        logger.info("Telegram Application created.")
    except Exception:
        logger.exception("Failed to create Telegram Application:")
        sys.exit(1)

    # Load subreddits.db
    subreddits_config = {}
    try:
        if not os.path.exists("subreddits.db"):
            logger.error("subreddits.db not found.")
            sys.exit(1)
        with open("subreddits.db", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',')
                    if len(parts) >= 2:
                        subreddit = parts[0].strip().lower()
                        topic_id = int(parts[1].strip())
                        subreddits_config[subreddit] = topic_id
        logger.info(f"Loaded {len(subreddits_config)} subreddits.")
    except Exception:
        logger.exception("Error reading subreddits.db:")
        sys.exit(1)

    # store in bot_data
    application.bot_data['reddit'] = reddit
    application.bot_data['subreddits_config'] = subreddits_config
    application.bot_data['telegram_group_id'] = telegram_group_id
    application.bot_data['telegram_error_topic_id'] = telegram_error_topic_id
    application.bot_data['restart_flag'] = False
    application.bot_data['processed_posts'] = set()
    application.bot_data['start_time'] = time.time()

    # capture event loop for background thread scheduling
    loop = asyncio.get_running_loop()
    application.bot_data['loop'] = loop

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("comments", comments_command))
    application.add_handler(CommandHandler("owner", owner_command))
    application.add_handler(CommandHandler("reload", reload_command))

    link_regex = r'https?://(?:www\.)?(?:reddit\.com/r/[^/]+/comments/|redd.it/)([^/]+)'
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(link_regex), handle_reddit_link))

    # Start blocking streaming worker in a separate thread
    asyncio.create_task(asyncio.to_thread(blocking_streaming_loop, application))
    logger.info("Started Reddit streaming background thread.")

    # Run the bot
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except Exception:
        logger.exception("Unhandled exception in __main__:")
