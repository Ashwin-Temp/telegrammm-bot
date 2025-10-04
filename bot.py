import os
import re
import json
import shutil
import asyncio
import logging
from pathlib import Path
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from telegram.error import TelegramError

# ============================ CONFIGURATION ============================
# --- Load from Environment Variables ---
# Your Telegram bot token from @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN")
# The ID of the channel you want to post to (e.g., "@mychannel" or -100123456789)
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")
# The numeric Telegram user ID of the person allowed to use this bot
try:
    ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))
except (ValueError, TypeError):
    ALLOWED_USER_ID = None

# --- Optional ---
# Telegram's API limit for video captions
TELEGRAM_CAPTION_LIMIT = 1024
# Telegram's file size limit for bots in MB
MAX_FILE_SIZE_MB = 50
# =======================================================================

# --- Setup Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- THIS IS THE NEW PART TO REDUCE LOG NOISE ---
# Set higher logging levels for httpx and telegram.ext to avoid spamming the console
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
# ===============================================

# --- Constants ---
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
SHORTCODE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?"
)

def extract_shortcode(url: str):
    """Extracts the shortcode from an Instagram URL."""
    match = SHORTCODE_RE.search(url)
    return match.group(1) if match else None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized user {update.effective_user.id} tried to use /start.")
        return
    await update.message.reply_text(
        "Hello! Send me a public Instagram post or reel URL, and I will repost it to the target channel."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming text messages containing Instagram links."""
    user = update.effective_user
    text = (update.message.text or "").strip()
    
    if user.id != ALLOWED_USER_ID:
        logger.warning(f"Ignoring message from unauthorized user {user.id} ({user.username}).")
        return

    shortcode = extract_shortcode(text)
    if not shortcode:
        await update.message.reply_text(
            "That doesn't look like a valid Instagram post/reel URL. "
            "Please send a link like `https://instagram.com/p/SHORTCODE`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    url = f"https://www.instagram.com/p/{shortcode}/"
    processing_message = await update.message.reply_text("🔗 Got it. Starting download...")
    
    # Create a temporary directory in the script's location
    temp_dir = Path(f"./temp_download_{shortcode}")
    temp_dir.mkdir(exist_ok=True)

    try:
        video_path_template = temp_dir / f"{shortcode}.%(ext)s"

        # --- Run yt-dlp asynchronously to not block the bot ---
        cmd = [
            "yt-dlp",
            "--no-check-certificate",
            "--write-info-json",
            # Select best video/audio under 720p and remux to MP4 for compatibility
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "--remux-video", "mp4",
            "-o", str(video_path_template),
            url,
        ]
        
        logger.info(f"Running yt-dlp for shortcode {shortcode}: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_message = stderr.decode('utf-8', errors='ignore').strip()
            logger.error(f"yt-dlp failed for {shortcode}: {error_message}")
            await processing_message.edit_text(f"❌ Download failed.\n\nError: `{error_message}`")
            return

        # --- Find the downloaded video and metadata files ---
        info_json_path = next(temp_dir.glob("*.info.json"), None)
        # Find the remuxed MP4 file
        video_path = next(temp_dir.glob("*.mp4"), None)

        if not video_path or not info_json_path:
            logger.error(f"Could not find downloaded video or JSON for {shortcode}.")
            await processing_message.edit_text("❌ Download failed: Could not find media files after download.")
            return
            
        # --- Check file size before uploading ---
        file_size = video_path.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            logger.warning(f"Video {shortcode} is too large: {file_size / 1e6:.2f} MB")
            await processing_message.edit_text(
                f"❌ Video is too large ({file_size / 1e6:.2f} MB). "
                f"Telegram's limit is {MAX_FILE_SIZE_MB} MB."
            )
            return
            
        await processing_message.edit_text("✅ Download complete. Preparing to post...")

        # --- Load metadata and create the caption ---
        with open(info_json_path, 'r', encoding='utf-8') as f:
            info = json.load(f)
        
        uploader = info.get("uploader", "unknown")
        description = info.get("description", "")
        post_url = info.get("webpage_url", url)
        
        # Build the caption
        caption = f"{description}\n\n"
        caption += f"Source: @{uploader}\n"
        caption += f"Link: {post_url}"

        # Truncate caption if it's too long
        if len(caption) > TELEGRAM_CAPTION_LIMIT:
            caption = caption[:TELEGRAM_CAPTION_LIMIT - 4] + "..."
            logger.info(f"Caption for {shortcode} was truncated.")

        # --- Send video to the channel ---
        await context.bot.send_video(
            chat_id=TARGET_CHANNEL_ID,
            video=video_path.read_bytes(),
            caption=caption,
        )
        logger.info(f"Successfully posted video for {shortcode} to {TARGET_CHANNEL_ID}")
        await processing_message.edit_text("✅ Successfully posted to your channel!")

    except TelegramError as e:
        logger.error(f"Failed to post video {shortcode}: {e}")
        await processing_message.edit_text(f"❌ Failed to post to Telegram: {e.message}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during processing of {shortcode}: {e}", exc_info=True)
        await processing_message.edit_text(f"❌ An unexpected error occurred: {e}")
    finally:
        # Clean up the temporary directory in the script's location
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"Cleaned up temporary files for {shortcode}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Logs errors caused by Updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)


def main():
    """Starts the bot."""
    if not all([BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID]):
        raise ValueError("One or more required environment variables (BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID) are not set.")
    
    # Set custom timeouts using the new method for python-telegram-bot v20+
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

