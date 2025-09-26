#!/usr/bin/env python3
import os
import logging
import asyncio
import aiohttp
import json
import re
import html
from io import BytesIO
from typing import Optional, Callable, Awaitable
import uuid

from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import TelegramError, BadRequest, TimedOut

import asyncpraw
from bs4 import BeautifulSoup
from moviepy.editor import VideoFileClip, AudioFileClip

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------- ENV VARS ----------
def get_env_var(name: str, cast=str, default=None):
    val = os.getenv(name)
    if val is None:
        if default is not None: return default
        raise ValueError(f"Missing required environment variable: {name}")
    try:
        return cast(val)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid value for {name}: {val}")

TELEGRAM_TOKEN = get_env_var("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = get_env_var("TELEGRAM_GROUP_ID", int)
TELEGRAM_ERROR_TOPIC_ID = get_env_var("TELEGRAM_ERROR_TOPIC_ID", int)
TELEGRAM_ADMIN_ID = get_env_var("TELEGRAM_ADMIN_ID", int)
REDDIT_CLIENT_ID = get_env_var("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = get_env_var("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = get_env_var("REDDIT_USERNAME")
REDDIT_PASSWORD = get_env_var("REDDIT_PASSWORD")

SUBREDDITS_DB_PATH = "subreddits.db"
POSTED_IDS_PATH = "posted_ids.json"

# ---------- SUBREDDITS MAPPING ----------
def load_subreddits_mapping(file_path):
    mapping = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = line.split(",")
                if len(parts) != 2:
                    logging.warning(f"Skipping malformed line in {file_path}: {line}")
                    continue
                subreddit_name, topic_id = parts
                mapping[subreddit_name.strip().lower()] = int(topic_id.strip())
    except FileNotFoundError:
        logging.warning(f"{file_path} not found. Starting with empty mapping.")
    except Exception:
        logging.exception("Failed to load subreddit mapping")
    return mapping

# ---------- TRACK POSTED IDS ----------
def load_posted_ids():
    try:
        with open(POSTED_IDS_PATH, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_posted_ids(posted_ids):
    try:
        with open(POSTED_IDS_PATH, "w") as f: json.dump(list(posted_ids), f)
    except Exception as e:
        logging.error(f"Failed to save posted ids: {e}")

# ---------- UTILITIES ----------
def prepare_caption(submission):
    author = submission.author.name if getattr(submission, "author", None) else "[deleted]"
    return (
        f"<b>{html.escape(getattr(submission, 'title', ''))}</b>\n\n"
        f"Posted by u/{html.escape(author)} in r/{html.escape(submission.subreddit.display_name)}\n"
        f"<a href='https://www.reddit.com{submission.permalink}'>Comments</a> | <a href='{html.escape(getattr(submission, 'url', ''))}'>Source</a>"
    )

async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> Optional[BytesIO]:
    try:
        async with session.get(url, timeout=45) as resp:
            resp.raise_for_status()
            data = await resp.read()
            bio = BytesIO(data)
            bio.name = os.path.basename(url.split("?")[0]) or "file.dat"
            return bio
    except Exception as e:
        logging.warning(f"Failed to fetch {url}: {e}")
        return None

# ---------- MEDIA HANDLING ----------
async def get_media_urls(submission, session):
    media_list = []
    url_lower = getattr(submission, "url", "").lower()
    try:
        if getattr(submission, "is_gallery", False) and hasattr(submission, "media_metadata"):
            for item in submission.gallery_data['items']:
                media_id = item['media_id']
                if media_id in submission.media_metadata and submission.media_metadata[media_id]['e'] == 'Image':
                    url = submission.media_metadata[media_id]['s']['u'].replace("&amp;", "&")
                    media_list.append({"url": url, "type": "photo"})
        elif getattr(submission, "is_video", False) and hasattr(submission, "media") and submission.media.get("reddit_video"):
            dash_url = submission.media["reddit_video"]["dash_url"]
            async with session.get(dash_url) as resp:
                manifest_text = await resp.text()
            
            soup = BeautifulSoup(manifest_text, "lxml")
            base_url = dash_url.rsplit('/', 1)[0] + '/'
            video_url = None
            audio_url = None
            
            for adaptation_set in soup.find_all("adaptationset"):
                mime_type = adaptation_set.get("mimetype")
                if "video" in mime_type:
                    video_url = base_url + adaptation_set.find("baseurl").text
                elif "audio" in mime_type:
                    audio_url = base_url + adaptation_set.find("baseurl").text
            
            if video_url:
                 media_list.append({"url": video_url, "audio_url": audio_url, "type": "video"})
        elif any(url_lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png"]):
            media_list.append({"url": submission.url, "type": "photo"})
        elif any(url_lower.endswith(ext) for ext in [".gif", ".mp4"]):
            media_list.append({"url": submission.url, "type": "gif" if url_lower.endswith(".gif") else "video"})
        elif "gfycat.com" in url_lower or "redgifs.com" in url_lower:
            async with session.get(submission.url) as resp: text = await resp.text()
            soup = BeautifulSoup(text, "html.parser")
            if (mp4_tag := soup.find("source", {"type": "video/mp4", "src": True})):
                media_list.append({"url": mp4_tag["src"], "type": "video"})
    except Exception as e:
        logging.warning(f"Failed to get media URLs for post {getattr(submission, 'id', '?')}: {e}")
    return media_list

# ---------- SAFE SEND HELPER ----------
async def _safe_send(primary_fn: Callable[[], Awaitable], fallback_fn: Optional[Callable[[], Awaitable]] = None):
    try:
        return await primary_fn()
    except (BadRequest, TimedOut) as e:
        msg = str(e).lower()
        if "topic_closed" in msg or "topic is closed" in msg:
            logging.warning("Topic closed, attempting to send to main group.")
            if fallback_fn: return await fallback_fn()
        raise e

# ---------- SEND MEDIA & ERROR REPORTING ----------
async def report_error(bot, submission, error):
    logging.error(f"Error processing post {submission.id}: {error}")
    error_text = (
        f"⚠️ <b>Error Processing Post</b> ⚠️\n\n"
        f"<b>Title:</b> {html.escape(submission.title)}\n"
        f"<b>Subreddit:</b> r/{submission.subreddit.display_name}\n"
        f"<b>Link:</b> https://www.reddit.com{submission.permalink}\n\n"
        f"<b>Reason:</b>\n<pre>{html.escape(str(error))}</pre>"
    )
    try:
        await bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=TELEGRAM_ERROR_TOPIC_ID, text=error_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.exception(f"CRITICAL: Could not send failure notice to error topic: {e}")

async def send_media(submission, topic_id, bot):
    caption = prepare_caption(submission)
    send_params = {"chat_id": TELEGRAM_GROUP_ID, "message_thread_id": topic_id, "caption": caption, "parse_mode": ParseMode.HTML}
    fallback_params = {k: v for k, v in send_params.items() if k != "message_thread_id"}
    text_params = {**{k: v for k, v in send_params.items() if k != "caption"}, "text": caption}

    async with aiohttp.ClientSession() as session:
        media_list = await get_media_urls(submission, session)

        if not media_list:
            await _safe_send(lambda: bot.send_message(**text_params))
        elif len(media_list) > 1:
            media_bytes = await asyncio.gather(*(fetch_bytes(session, m["url"]) for m in media_list[:10]))
            tg_media = [InputMediaPhoto(media=bio, caption=caption if i == 0 else None, parse_mode=ParseMode.HTML) for i, bio in enumerate(media_bytes) if bio]
            if tg_media:
                await _safe_send(
                    lambda: bot.send_media_group(chat_id=TELEGRAM_GROUP_ID, message_thread_id=topic_id, media=tg_media),
                    lambda: bot.send_media_group(chat_id=TELEGRAM_GROUP_ID, media=tg_media)
                )
        else:
            media = media_list[0]
            if media["type"] == "video" and media.get("audio_url"):
                video_bio = await fetch_bytes(session, media["url"])
                audio_bio = await fetch_bytes(session, media["audio_url"])

                if video_bio and audio_bio:
                    tmp_id = uuid.uuid4()
                    video_path = f"/tmp/{tmp_id}_video.mp4"
                    audio_path = f"/tmp/{tmp_id}_audio.mp4"
                    output_path = f"/tmp/{tmp_id}_final.mp4"

                    with open(video_path, "wb") as f: f.write(video_bio.getvalue())
                    with open(audio_path, "wb") as f: f.write(audio_bio.getvalue())

                    try:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None,
                            lambda: VideoFileClip(video_path).set_audio(AudioFileClip(audio_path)).write_videofile(
                                output_path,
                                codec='libx264',
                                audio_codec='aac',
                                temp_audiofile=f'/tmp/{tmp_id}_temp_audio.m4a',
                                remove_temp=True,
                                logger=None
                            )
                        )
                        
                        with open(output_path, "rb") as f:
                             await _safe_send(
                                lambda: bot.send_video(video=f, **send_params),
                                lambda: bot.send_video(video=f, **fallback_params)
                            )
                    finally:
                        if os.path.exists(video_path): os.remove(video_path)
                        if os.path.exists(audio_path): os.remove(audio_path)
                        if os.path.exists(output_path): os.remove(output_path)
                        
                elif video_bio:
                    bio = video_bio
                    await _safe_send(
                        lambda: bot.send_video(video=bio, **send_params),
                        lambda: bot.send_video(video=bio, **fallback_params)
                    )
                else:
                    raise ValueError("Video download failed.")

            else:
                bio = await fetch_bytes(session, media["url"])
                if not bio: raise ValueError("Media download failed or returned empty.")
                
                send_map = {"photo": bot.send_photo, "video": bot.send_video, "gif": bot.send_animation}
                send_func = send_map.get(media["type"])
                if send_func:
                    media_kwarg = "animation" if media["type"] == "gif" else media["type"]
                    await _safe_send(
                        lambda: send_func(**{media_kwarg: bio}, **send_params),
                        lambda: send_func(**{media_kwarg: bio}, **fallback_params)
                    )

    logging.info("Post sent: %s to topic %s", submission.title, topic_id)
    return True

# ---------- CORE SUBMISSION PROCESSING ----------
async def process_submission(submission, context: ContextTypes.DEFAULT_TYPE):
    app_data = context.application.bot_data
    async with app_data["posted_ids_lock"]:
        if submission.id in app_data["posted_ids"]: return

    topic_id = app_data["subreddit_map"].get(submission.subreddit.display_name.lower(), TELEGRAM_ERROR_TOPIC_ID)
    
    try:
        if await send_media(submission, topic_id, context.bot):
            async with app_data["posted_ids_lock"]:
                app_data["posted_ids"].add(submission.id)
                save_posted_ids(app_data["posted_ids"])
    except Exception as e:
        await report_error(context.bot, submission, e)

# ---------- TELEGRAM COMMANDS ----------
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args: return await msg.reply_text("Usage: /post <reddit_url>")
    if not (reddit := context.application.bot_data.get("reddit_client")):
        return await msg.reply_text("Reddit client not ready.")
    try:
        submission = await reddit.submission(url=context.args[0])
        await process_submission(submission, context)
        await msg.reply_text(f"Attempted to process post: {submission.title}")
    except Exception as e:
        await msg.reply_text(f"Error fetching Reddit URL: {e}")

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Reloading subreddits and restarting stream...")
    context.application.bot_data["subreddit_map"] = load_subreddits_mapping(SUBREDDITS_DB_PATH)
    await stop_and_restart_stream(context.application)
    new_subs = ", ".join(context.application.bot_data["subreddit_map"].keys())
    await update.effective_message.reply_text(f"Reload complete. Now monitoring: {new_subs or 'None'}")

# ---------- STREAMING LOGIC ----------
async def stream_subreddits_task(app: Application):
    subreddit_map = app.bot_data["subreddit_map"]
    if not subreddit_map:
        logging.warning("No subreddits configured. Stream will not start.")
        return

    subreddit_names = "+".join(subreddit_map.keys())
    logging.info(f"Starting stream for subreddits: {subreddit_names}")
    try:
        subreddit = await app.bot_data["reddit_client"].subreddit(subreddit_names)
        async for submission in subreddit.stream.submissions(skip_existing=True):
            context = ContextTypes.DEFAULT_TYPE(application=app)
            asyncio.create_task(process_submission(submission, context))
    except asyncio.CancelledError:
        logging.info("Subreddit stream task was cancelled.")
    except Exception as e:
        logging.exception(f"Subreddit stream failed: {e}")
        error_text = f"🚨 **CRITICAL: Reddit Stream Failure** 🚨\n\nThe bot's Reddit stream has crashed and will not automatically restart.\n\n**Error:**\n`{html.escape(str(e))}`"
        await app.bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=TELEGRAM_ERROR_TOPIC_ID, text=error_text, parse_mode=ParseMode.HTML)

async def stop_and_restart_stream(app: Application):
    if (task := app.bot_data.get("stream_task")) and not task.done():
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    app.bot_data["stream_task"] = asyncio.create_task(stream_subreddits_task(app))

# ---------- STARTUP / SHUTDOWN / ERROR HANDLER ----------
async def on_startup(app: Application):
    logging.info("Bot starting up...")
    app.bot_data["reddit_client"] = asyncpraw.Reddit(
        client_id=REDDIT_CLIENT_ID, client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME, password=REDDIT_PASSWORD,
        user_agent="TelegramRedditBot/2.2 by anarq42",
    )
    app.bot_data.update({
        "posted_ids": load_posted_ids(), "posted_ids_lock": asyncio.Lock(),
        "subreddit_map": load_subreddits_mapping(SUBREDDITS_DB_PATH)
    })
    await stop_and_restart_stream(app)
    logging.info("Startup complete.")

async def on_shutdown(app: Application):
    logging.info("Bot shutting down...")
    if (task := app.bot_data.get("stream_task")) and not task.done():
        task.cancel()
        try: await task
        except asyncio.CancelledError: logging.info("Subreddit stream task successfully cancelled.")
    if (reddit := app.bot_data.get("reddit_client")): await reddit.close()
    logging.info("Shutdown complete.")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled exception: %s", context.error, exc_info=context.error)
    error_text = (
        f"🆘 <b>CRITICAL: Unhandled Bot Exception</b> 🆘\n\n"
        f"The bot encountered an error it could not recover from.\n\n"
        f"<b>Error:</b>\n<pre>{html.escape(str(context.error))}</pre>"
    )
    try:
        await context.bot.send_message(chat_id=TELEGRAM_GROUP_ID, message_thread_id=TELEGRAM_ERROR_TOPIC_ID, text=error_text, parse_mode=ParseMode.HTML)
    except Exception:
        logging.exception("FATAL: Could not send unhandled exception notice to error topic.")

# ---------- MAIN ----------
def main():
    app = (
        Application.builder().token(TELEGRAM_TOKEN)
        .post_init(on_startup).post_shutdown(on_shutdown).build()
    )
    admin_filter = filters.User(user_id=TELEGRAM_ADMIN_ID)
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("reload", reload_command, filters=admin_filter))
    app.add_error_handler(global_error_handler)
    
    logging.info("Bot application configured. Starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
