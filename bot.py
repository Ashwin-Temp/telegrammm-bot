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
    ALLOWED_USER_ID = int(ALLOWED_USER_ID)
except (ValueError, TypeError):
    raise ValueError("FATAL: ALLOWED_USER_ID environment variable is not a valid integer.")

# --- Logging Configuration (less verbose) ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Constants ---
TELEGRAM_CAPTION_LIMIT = 1024
SHORTCODE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?"
)

def extract_shortcode(url: str):
    match = SHORTCODE_RE.search(url)
    return match.group(1) if match else None

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in escape_chars else c for c in text)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "Hello! Send me a public Instagram post or reel URL, and I will repost it."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if update.effective_user.id != ALLOWED_USER_ID:
        return

    shortcode = extract_shortcode(update.message.text.strip())
    if not shortcode:
        await update.message.reply_text("This doesn't look like a valid Instagram URL.")
        return

    url = f"https://www.instagram.com/p/{shortcode}/"
    processing_message = await update.message.reply_text("⏳ Processing link...")
    temp_dir = Path(f"./temp_download_{shortcode}")
    temp_dir.mkdir(exist_ok=True)

    try:
        await processing_message.edit_text("📥 Downloading video...")
        cmd = [
            "yt-dlp", "--no-check-certificate", "--write-info-json",
            "-f", "bestvideo[ext=mp4][height<=720]+bestaudio/best[ext=mp4][height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", str(temp_dir / f"{shortcode}.%(ext)s"), url,
        ]
        logger.info(f"Running yt-dlp: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            error_message = stderr.decode('utf-8', 'ignore').strip()
            await processing_message.edit_text(f"❌ Download failed.\n`{error_message}`")
            return

        info_json_path = next(temp_dir.glob("*.info.json"), None)
        video_path = next(temp_dir.glob("*.mp4"), None)

        if not video_path or not info_json_path:
            await processing_message.edit_text("❌ Download successful, but couldn't find media files.")
            return

        await processing_message.edit_text("✅ Download complete. Preparing caption...")

        with open(info_json_path, 'r', encoding='utf-8') as f:
            info = json.load(f)

        # ===== THIS IS THE CORRECTED LOGIC =====
        # Get BOTH the display name and the username
        display_name = info.get("uploader", "unknown_user") # e.g., "☠️Rostar☠️"
        username = info.get("uploader_id", display_name)    # e.g., "rostar_official"
        description = info.get("description", "")
        post_url = info.get("webpage_url", url)

        # Escape all parts for Markdown
        escaped_display_name = escape_markdown_v2(display_name)
        escaped_description = escape_markdown_v2(description)

        # Rebuild the caption in the format you want
        caption = (
            f"From: [{escaped_display_name}](https://instagram.com/{username})\n"
            f"Reel: [Click Here]({post_url})\n\n"
            f"{escaped_description}"
        )
        # =======================================

        if len(caption) > TELEGRAM_CAPTION_LIMIT:
            caption = caption[:TELEGRAM_CAPTION_LIMIT - 4] + "\\.\\.\\."

        await processing_message.edit_text("⬆️ Uploading to channel...")
        with open(video_path, "rb") as video_file:
            await context.bot.send_video(
                chat_id=TARGET_CHANNEL_ID,
                video=video_file,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        await processing_message.edit_text("✅ Posted successfully!")

    except TelegramError as e:
        await processing_message.edit_text(f"❌ Failed to post to Telegram: {e.message}")
    except Exception as e:
        await processing_message.edit_text(f"❌ An unexpected error occurred: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"Cleaned up temporary files for {shortcode}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
