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
try:
    ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))
except (ValueError, TypeError):
    ALLOWED_USER_ID = None

TELEGRAM_CAPTION_LIMIT = 1024
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
# =======================================================================

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# --- Regex for Instagram shortcode ---
SHORTCODE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?"
)

def extract_shortcode(url: str):
    match = SHORTCODE_RE.search(url)
    return match.group(1) if match else None

# --- Escape function for MarkdownV2 ---
def escape_markdown_v2(text: str) -> str:
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in escape_chars else c for c in text)

# --- /start command ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized user {update.effective_user.id} tried to use /start.")
        return
    await update.message.reply_text(
        "Hello! Send me a public Instagram post or reel URL, and I will repost it to the target channel."
    )

# --- Handle incoming messages ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message:
        return  # ignore non-message updates
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

    temp_dir = Path(f"./temp_download_{shortcode}")
    temp_dir.mkdir(exist_ok=True)

    try:
        video_path_template = temp_dir / f"{shortcode}.%(ext)s"

        # Run yt-dlp
        cmd = [
            "yt-dlp",
            "--no-check-certificate",
            "--write-info-json",
            "-f", "best[ext=mp4][height<=720]/best",
            "--merge-output-format", "mp4",
            "--remux-video", "mp4",
            "-o", str(video_path_template),
            url,
        ]
        logger.info(f"Running yt-dlp: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            error_message = stderr.decode('utf-8', errors='ignore').strip()
            logger.error(f"yt-dlp failed: {error_message}")
            await processing_message.edit_text(f"❌ Download failed.\n\nError: `{error_message}`")
            return

        # Find files
        info_json_path = next(temp_dir.glob("*.info.json"), None)
        video_path = next(temp_dir.glob("*.mp4"), None)
        if not video_path or not info_json_path:
            logger.error("Could not find downloaded video or JSON.")
            await processing_message.edit_text("❌ Download failed: Could not find media files.")
            return

        # File size check
        if video_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            await processing_message.edit_text(
                f"❌ Video too large ({video_path.stat().st_size / 1e6:.2f} MB). Telegram limit is {MAX_FILE_SIZE_MB} MB."
            )
            return

        await processing_message.edit_text("✅ Download complete. Preparing to post...")

        # Load metadata
        with open(info_json_path, 'r', encoding='utf-8') as f:
            info = json.load(f)
        uploader = info.get("uploader", "unknown")
        description = info.get("description", "")
        post_url = info.get("webpage_url", url)

        # Escape for MarkdownV2
        escaped_description = escape_markdown_v2(description)
        escaped_username = escape_markdown_v2(uploader)

        # Build caption with clickable username and "Click here" link
        caption = f"{escaped_description}\n\n"
        caption += f"🎥 Credit: [@{escaped_username}](https://instagram.com/{escaped_username})\n"
        caption += f"🔗 Reel: [Click here]({post_url})"

        # Truncate if too long
        if len(caption) > TELEGRAM_CAPTION_LIMIT:
            caption = caption[:TELEGRAM_CAPTION_LIMIT - 4] + "..."

        # Send video
        await context.bot.send_video(
            chat_id=TARGET_CHANNEL_ID,
            video=video_path.read_bytes(),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        await processing_message.edit_text("✅ Successfully posted to your channel!")
        logger.info(f"Posted video for {shortcode} successfully.")

    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        await processing_message.edit_text(f"❌ Telegram error: {e.message}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        await processing_message.edit_text(f"❌ Unexpected error: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"🧹 Cleaned up temp files for {shortcode}")

# --- Error handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

# --- Main ---
def main():
    if not all([BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID]):
        raise ValueError("Environment variables BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID not set.")

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

    logger.info("🤖 Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()


