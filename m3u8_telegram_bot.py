import os
import sys
import logging
import asyncio
import traceback
from datetime import datetime
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import NetworkError, TelegramError
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Global Constants
CURRENT_TIME = "2025-06-13 06:01:38"  # Exact timestamp provided
CURRENT_USER = "harshMrDev"           # Exact user login provided
BOT_VERSION = "2.0.0"
SUPPORT_GROUP = "@m3u8bot_support"
UPDATES_CHANNEL = "@m3u8bot_updates"

# Stream Format Support
STREAM_FORMATS = {
    '.m3u8': {
        'name': 'HTTP Live Streaming (HLS)',
        'options': {'hls_prefer_native': True}
    },
    '.mpd': {
        'name': 'MPEG-DASH',
        'options': {'format': 'bestvideo+bestaudio/best'}
    },
    '.ts': {
        'name': 'Transport Stream',
        'options': {'hls_use_mpegts': True}
    },
    '.f4m': {
        'name': 'Adobe HDS',
        'options': {'f4m_prefer_native': True}
    }
}

# Quality Options
QUALITY_OPTIONS = [
    ('auto', 'Best Quality'),
    ('1080p', 'Full HD (1080p)'),
    ('720p', 'HD (720p)'),
    ('480p', 'SD (480p)'),
    ('360p', 'Low (360p)'),
    ('audio', 'Audio Only')
]

class StreamDownloader:
    def __init__(self, max_size_mb=49):
        self.max_bytes = max_size_mb * 1024 * 1024
        self.temp_dir = "/tmp/downloads"
        os.makedirs(self.temp_dir, exist_ok=True)
        self.downloads = {}

    async def download(self, url: str, quality: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Download stream and send to Telegram"""
        chat = update.effective_chat
        message_id = update.message.message_id
        user_id = update.effective_user.id

        if user_id in self.downloads:
            await chat.send_message(
                "⚠️ You already have an active download. Please wait."
            )
            return

        self.downloads[user_id] = message_id

        try:
            # Send initial status message
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{message_id}")]]
            status_msg = await chat.send_message(
                f"📥 Starting download...\n"
                f"Quality: {quality}\n"
                f"Time: {CURRENT_TIME}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

            # Configure download options
            ydl_opts = {
                'format': f'bestvideo[height<={quality[:-1]}]+bestaudio/best' if quality != 'auto' else 'best',
                'outtmpl': f"{self.temp_dir}/%(title)s_{quality}.%(ext)s",
                'merge_output_format': 'mp4',
                'retries': 10,
                'fragment_retries': 10,
                'http_chunk_size': 10485760,
                'quiet': True,
                'no_warnings': True,
            }

            def progress_hook(d):
                if d['status'] == 'downloading':
                    try:
                        if 'total_bytes' in d:
                            percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
                            speed = d.get('speed', 0)
                            speed_str = f"{speed/1024/1024:.1f} MB/s" if speed else "N/A"
                            
                            progress_text = (
                                f"⏳ Downloading: {percent:.1f}%\n"
                                f"⚡ Speed: {speed_str}\n"
                                f"📊 Quality: {quality}\n"
                                f"🕒 Time: {CURRENT_TIME}\n"
                                f"👤 User: @{CURRENT_USER}"
                            )
                        else:
                            mb = d['downloaded_bytes'] / 1024 / 1024
                            progress_text = f"⏳ Downloaded: {mb:.1f}MB"
                        
                        context.job_queue.run_once(
                            lambda _: self.update_status(status_msg, progress_text),
                            0
                        )
                    except Exception as e:
                        logger.error(f"Progress hook error: {e}")

            ydl_opts['progress_hooks'] = [progress_hook]

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await self.update_status(status_msg, "🔍 Analyzing stream...")
                info = ydl.extract_info(url, download=False)
                file_path = ydl.prepare_filename(info)
                
                await self.update_status(status_msg, "📥 Downloading...")
                ydl.download([url])

                if not os.path.exists(file_path):
                    raise Exception("Download failed - file not found")

                size = os.path.getsize(file_path)
                if size == 0:
                    raise Exception("Downloaded file is empty")

                if size > self.max_bytes:
                    await self.update_status(
                        status_msg,
                        f"⚠️ File is large ({size/1024/1024:.1f}MB). Splitting..."
                    )
                    await self.split_and_send(file_path, chat, status_msg)
                else:
                    await self.update_status(status_msg, "📤 Uploading to Telegram...")
                    caption = (
                        f"🎥 Download Complete\n\n"
                        f"📊 Quality: {quality}\n"
                        f"💾 Size: {size/1024/1024:.1f}MB\n"
                        f"🕒 Time: {CURRENT_TIME}\n"
                        f"👤 User: @{CURRENT_USER}"
                    )
                    
                    with open(file_path, 'rb') as f:
                        await chat.send_document(
                            document=f,
                            filename=os.path.basename(file_path),
                            caption=caption
                        )

                if os.path.exists(file_path):
                    os.remove(file_path)
                await self.update_status(status_msg, "✅ Download completed!")

        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            logger.error(f"Download error: {error_msg}")
            await self.update_status(status_msg, error_msg)
        finally:
            if user_id in self.downloads:
                del self.downloads[user_id]

    async def split_and_send(self, file_path: str, chat, status_msg):
        """Split large files and send in parts"""
        try:
            file_size = os.path.getsize(file_path)
            base_name = os.path.basename(file_path)
            name, ext = os.path.splitext(base_name)
            
            total_parts = -(-file_size // self.max_bytes)
            
            with open(file_path, 'rb') as f:
                for part in range(total_parts):
                    content = f.read(self.max_bytes)
                    if not content:
                        break
                        
                    part_name = f"{name}_part{part+1}of{total_parts}{ext}"
                    part_path = os.path.join(self.temp_dir, part_name)
                    
                    with open(part_path, 'wb') as part_file:
                        part_file.write(content)
                    
                    await self.update_status(
                        status_msg,
                        f"📤 Sending part {part+1}/{total_parts}"
                    )
                    
                    with open(part_path, 'rb') as part_file:
                        await chat.send_document(
                            document=part_file,
                            filename=part_name,
                            caption=f"📦 Part {part+1}/{total_parts}\n"
                                   f"🕒 Time: {CURRENT_TIME}\n"
                                   f"👤 User: @{CURRENT_USER}"
                        )
                    
                    os.remove(part_path)
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Split and send error: {e}")
            raise

    @staticmethod
    async def update_status(message, text: str):
        """Update status message with retry logic"""
        for _ in range(3):
            try:
                await message.edit_text(text)
                break
            except Exception as e:
                logger.error(f"Status update error: {e}")
                await asyncio.sleep(1)

class TelegramBot:
    def __init__(self):
        self.downloader = StreamDownloader()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        await update.effective_chat.send_message(
            f"👋 Welcome {user.mention_html()}!\n\n"
            f"🕒 Bot Time: {CURRENT_TIME}\n"
            f"👤 Developer: @{CURRENT_USER}\n\n"
            "📝 *How to use:*\n"
            "1. Send any M3U8 or stream URL\n"
            "2. Select quality\n"
            "3. Wait for download\n\n"
            "⚡ Features:\n"
            "• Multiple formats support\n"
            "• Quality selection\n"
            "• Progress tracking\n"
            "• Auto-split large files\n\n"
            "Type /help for more information",
            parse_mode="HTML"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        formats_list = "\n".join([f"• {info['name']}" for info in STREAM_FORMATS.values()])
        qualities_list = "\n".join([f"• {name}" for _, name in QUALITY_OPTIONS])
        
        await update.effective_chat.send_message(
            "*Help Guide*\n\n"
            "*Supported Formats:*\n"
            f"{formats_list}\n\n"
            "*Available Qualities:*\n"
            f"{qualities_list}\n\n"
            "*Commands:*\n"
            "/start - Start bot\n"
            "/help - Show this message\n"
            "/status - Check bot status\n\n"
            "*Notes:*\n"
            "• Large files are split automatically\n"
            "• One download at a time per user\n"
            "• You can cancel downloads anytime",
            parse_mode="Markdown"
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        active_downloads = len(self.downloader.downloads)
        await update.effective_chat.send_message(
            f"🤖 *Bot Status*\n\n"
            f"• Active: ✅\n"
            f"• Time: {CURRENT_TIME}\n"
            f"• Downloads: {active_downloads}\n"
            f"• Developer: @{CURRENT_USER}\n"
            f"• Version: {BOT_VERSION}",
            parse_mode="Markdown"
        )

    async def handle_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle URLs"""
        url = update.message.text
        if not url:
            return

        # Show quality selection keyboard
        keyboard = []
        for quality, name in QUALITY_OPTIONS:
            keyboard.append([InlineKeyboardButton(
                name, 
                callback_data=f"quality_{quality}_{update.message.message_id}"
            )])
        keyboard.append([InlineKeyboardButton(
            "❌ Cancel", 
            callback_data=f"cancel_{update.message.message_id}"
        )])

        await update.effective_chat.send_message(
            "📊 Select Quality:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback queries"""
        query = update.callback_query
        await query.answer()

        if query.data.startswith("cancel_"):
            await query.edit_message_text("❌ Download cancelled")
            return

        if query.data.startswith("quality_"):
            quality = query.data.split("_")[1]
            message_id = query.data.split("_")[2]
            url = update.effective_message.text
            await self.downloader.download(url, quality, update, context)

def main():
    """Main function"""
    try:
        print(f"""
╔══════════════════════════════════════╗
║      Stream Downloader Bot           ║
║──────────────────────────────────────║
║  Time: {CURRENT_TIME}     ║
║  Dev: @{CURRENT_USER}              ║
║  Version: {BOT_VERSION}                    ║
╚══════════════════════════════════════╝
        """)
        
        BOT_TOKEN = os.getenv("BOT_TOKEN")
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN environment variable not set!")
            sys.exit(1)
        
        bot = TelegramBot()
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .read_timeout(30)
            .write_timeout(30)
            .connect_timeout(30)
            .pool_timeout(30)
            .build()
        )
        
        application.add_handler(CommandHandler("start", bot.start_command))
        application.add_handler(CommandHandler("help", bot.help_command))
        application.add_handler(CommandHandler("status", bot.status_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_url))
        application.add_handler(CallbackQueryHandler(bot.handle_callback))
        
        logger.info(f"🤖 Bot starting... Time: {CURRENT_TIME} Developer: @{CURRENT_USER}")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        sys.exit(1)
