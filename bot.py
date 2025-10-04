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
from dotenv import load_dotenv  # <-- added to load .env

# ============================ CONFIGURATION ============================
# Load .env file
load_dotenv()

# Environment variables
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

# --- Logging setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# --- Regex for Instagram ---
SHORTCODE_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?")

def extract_shortcode(url: str):
    match = SHORTCODE_RE.search(url)
    return match.group(1) if match else None

# =======================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 Send me a public Instagram post or reel link and I’ll repost it to your channel."
    )

# =======================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages containing Instagram links."""
    user = update.effective_user
    text = (update.message.text or "").strip()

    if user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized user {user.id} tried to use the bot.")
        return

    shortcode = extract_shortcode(text)
    if not shortcode:
        await update.message.reply_text(
            "❌ Invalid link. Please send a valid Instagram reel/post URL like:\n"
            "`https://www.instagram.com/reel/XXXXXXXX/`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    url = f"https://www.instagram.com/p/{shortcode}/"
    processing_message = await update.message.reply_text("🔗 Downloading reel... please wait...")

    temp_dir = Path(f"./temp_download_{shortcode}")
    temp_dir.mkdir(exist_ok=True)

    try:
        video_path_template = temp_dir / f"{shortcode}.%(ext)s"

        cmd = [
            "yt-dlp",
            "--no-check-certificate",
            "--write-info-json",
            "-f", "bestvideo+bestaudio/best",  # ensure audio + video
            "--merge-output-format", "mp4",
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
            error_message = stderr.decode("utf-8", errors="ignore")
            logger.error(f"yt-dlp failed for {shortcode}: {error_message}")
            await processing_message.edit_text(f"❌ Download failed.\n\n`{error_message[:800]}`", parse_mode=ParseMode.MARKDOWN)
            return

        video_path = next(temp_dir.glob("*.mp4"), None)
        info_json_path = next(temp_dir.glob("*.info.json"), None)

        if not video_path or not info_json_path:
            await processing_message.edit_text("❌ Could not find the downloaded video.")
            return

        if video_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            await processing_message.edit_text(f"❌ File too large (> {MAX_FILE_SIZE_MB} MB).")
            return

        await processing_message.edit_text("✅ Download complete. Uploading to channel...")

        # --- Build caption with credit ---
        with open(info_json_path, "r", encoding="utf-8") as f:
            info = json.load(f)

        uploader = info.get("uploader_id") or info.get("uploader") or "unknown"
        description = info.get("description", "").strip()
        post_url = info.get("webpage_url", url)

        caption = f"{description}\n\n"
        if uploader != "unknown":
            caption += f"🎥 Credit: @{uploader}\n"
        caption += f"🔗 Link: {post_url}"

        if len(caption) > TELEGRAM_CAPTION_LIMIT:
            caption = caption[:TELEGRAM_CAPTION_LIMIT - 3] + "..."

        await context.bot.send_video(
            chat_id=TARGET_CHANNEL_ID,
            video=video_path.read_bytes(),
            caption=caption,
        )

        logger.info(f"✅ Posted {shortcode} to {TARGET_CHANNEL_ID}")
        await processing_message.edit_text("✅ Successfully posted to your channel!")

    except TelegramError as e:
        logger.error(f"Telegram error while posting: {e}")
        await processing_message.edit_text(f"❌ Failed to post to Telegram: {e.message}")
    except Exception as e:
        logger.error(f"Unexpected error while processing {shortcode}: {e}", exc_info=True)
        await processing_message.edit_text(f"❌ Unexpected error: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"🧹 Cleaned up temp files for {shortcode}")

# =======================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

def main():
    """Start the bot."""
    if not all([BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID]):
        raise ValueError("Missing environment variables (BOT_TOKEN, TARGET_CHANNEL_ID, ALLOWED_USER_ID)")

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
