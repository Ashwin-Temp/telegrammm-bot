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
# These should be set as Environment Variables in your hosting service (e.g., Railway)
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
# =======================================================================

# --- Basic Sanity Check ---
if not all([BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID]):
    raise ValueError(
        "FATAL: One or more environment variables (BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID) are missing."
    )

try:
    # Convert ALLOWED_USER_ID to integer for comparison
    ALLOWED_USER_ID = int(ALLOWED_USER_ID)
except (ValueError, TypeError):
    raise ValueError("FATAL: ALLOWED_USER_ID environment variable is not a valid integer.")


# --- Logging Configuration (less verbose) ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Silence the overly verbose libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# --- Constants ---
TELEGRAM_CAPTION_LIMIT = 1024
SHORTCODE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?"
)


def extract_shortcode(url: str):
    """Extracts the shortcode from an Instagram URL."""
    match = SHORTCODE_RE.search(url)
    return match.group(1) if match else None

def escape_markdown_v2(text: str) -> str:
    """Escapes text for Telegram's MarkdownV2 parse mode."""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in escape_chars else c for c in text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    if update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized user {update.effective_user.id} tried to use /start.")
        return
    await update.message.reply_text(
        "Hello! Send me a public Instagram post or reel URL, and I will repost it."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler to process Instagram URLs."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    text = update.message.text.strip()

    if user.id != ALLOWED_USER_ID:
        logger.warning(f"Ignoring message from unauthorized user {user.id}.")
        return

    shortcode = extract_shortcode(text)
    if not shortcode:
        await update.message.reply_text("This doesn't look like a valid Instagram URL.")
        return

    url = f"https://www.instagram.com/p/{shortcode}/"
    processing_message = await update.message.reply_text("⏳ Processing link...")
    temp_dir = Path(f"./temp_download_{shortcode}")
    temp_dir.mkdir(exist_ok=True)

    try:
        # --- Download using yt-dlp ---
        await processing_message.edit_text("📥 Downloading video...")
        cmd = [
            "yt-dlp",
            "--no-check-certificate",
            "--write-info-json",
            # This format string is key:
            # 1. Tries to get the best pre-merged mp4 up to 720p.
            # 2. Falls back to getting best video-only and best audio-only streams and merging them.
            # This ensures you always get sound.
            "-f", "bestvideo[ext=mp4][height<=720]+bestaudio/best[ext=mp4][height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", str(temp_dir / f"{shortcode}.%(ext)s"),
            url,
        ]
        logger.info(f"Running yt-dlp for shortcode {shortcode}: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            error_message = stderr.decode('utf-8', errors='ignore').strip()
            logger.error(f"yt-dlp failed: {error_message}")
            await processing_message.edit_text(f"❌ Download failed.\n\n`{error_message}`")
            return

        # --- Find downloaded files ---
        info_json_path = next(temp_dir.glob("*.info.json"), None)
        video_path = next(temp_dir.glob("*.mp4"), None)

        if not video_path or not info_json_path:
            logger.error(f"Could not find downloaded files for {shortcode}.")
            await processing_message.edit_text("❌ Download successful, but couldn't find media files.")
            return

        await processing_message.edit_text("✅ Download complete. Preparing caption...")

        # --- Process metadata and create caption ---
        with open(info_json_path, 'r', encoding='utf-8') as f:
            info = json.load(f)

        # ** THE FIX IS HERE: Use 'uploader' not 'uploader_id' **
        username = info.get("uploader", "unknown_user")
        description = info.get("description", "")
        post_url = info.get("webpage_url", url)

        escaped_username = escape_markdown_v2(username)
        escaped_description = escape_markdown_v2(description)

        caption = (
            f"Original post by [@{escaped_username}](https://instagram.com/{username})\n\n"
            f"{escaped_description}"
        )

        if len(caption) > TELEGRAM_CAPTION_LIMIT:
            caption = caption[:TELEGRAM_CAPTION_LIMIT - 4] + "\\.\\.\\."

        # --- Upload to Telegram ---
        await processing_message.edit_text("⬆️ Uploading to channel...")
        with open(video_path, "rb") as video_file:
            await context.bot.send_video(
                chat_id=TARGET_CHANNEL_ID,
                video=video_file,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        logger.info(f"Successfully posted video for {shortcode} to {TARGET_CHANNEL_ID}")
        await processing_message.edit_text("✅ Posted successfully!")

    except TelegramError as e:
        logger.error(f"Telegram API error while posting {shortcode}: {e}")
        await processing_message.edit_text(f"❌ Failed to post to Telegram: {e.message}")
    except Exception as e:
        logger.error(f"An unexpected error occurred for {shortcode}: {e}", exc_info=True)
        await processing_message.edit_text(f"❌ An unexpected error occurred: {e}")
    finally:
        # --- Cleanup ---
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"Cleaned up temporary files for {shortcode}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors caused by updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)


def main():
    """Start the bot."""
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
