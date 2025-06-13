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

class M3U8Downloader:
    def __init__(self, max_size_mb=49):
        self.max_bytes = max_size_mb * 1024 * 1024
        self.temp_dir = "/tmp/downloads"
        os.makedirs(self.temp_dir, exist_ok=True)
        self.downloads = {}  # Track ongoing downloads

    async def download(self, m3u8_url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Download M3U8 stream and send to Telegram"""
        chat = update.effective_chat
        message_id = update.message.message_id
        user_id = update.effective_user.id

        if user_id in self.downloads:
            await chat.send_message(
                "⚠️ You already have an active download. Please wait for it to complete."
            )
            return

        self.downloads[user_id] = message_id

        try:
            # Send initial status message with cancel button
            keyboard = [[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{message_id}")]]
            status_msg = await chat.send_message(
                "⏳ Starting download...",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

            # Configure yt-dlp options
            ydl_opts = {
                'format': 'best',
                'outtmpl': f"{self.temp_dir}/%(title)s.%(ext)s",
                'merge_output_format': 'mp4',
                'retries': 10,
                'fragment_retries': 10,
                'http_chunk_size': 10485760,  # 10MB chunks
                'quiet': True,
                'no_warnings': True,
            }

            def progress_hook(d):
                if d['status'] == 'downloading':
                    try:
                        if 'total_bytes' in d:
                            percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
                            progress_text = f"⏳ Downloaded: {percent:.1f}%"
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
                # Extract info first
                await self.update_status(status_msg, "🔍 Analyzing stream...")
                info = ydl.extract_info(m3u8_url, download=False)
                file_path = ydl.prepare_filename(info)
                
                # Start download
                await self.update_status(status_msg, "📥 Downloading stream...")
                ydl.download([m3u8_url])

                if not os.path.exists(file_path):
                    raise Exception("Download failed - file not found")

                size = os.path.getsize(file_path)
                if size == 0:
                    raise Exception("Downloaded file is empty")

                # Handle large files
                if size > self.max_bytes:
                    await self.update_status(
                        status_msg,
                        f"⚠️ File is large ({size/1024/1024:.1f}MB). Splitting..."
                    )
                    await self.split_and_send(file_path, chat, status_msg)
                else:
                    await self.update_status(status_msg, "📤 Uploading to Telegram...")
                    caption = (
                        f"🎥 Title: {info.get('title', 'Video')}\n"
                        f"💾 Size: {size/1024/1024:.1f}MB\n"
                        f"🔄 Format: {info.get('format', 'Unknown')}\n"
                        f"⚡ Downloaded by @{update.effective_user.username or 'user'}"
                    )
                    
                    with open(file_path, 'rb') as f:
                        await chat.send_document(
                            document=f,
                            filename=os.path.basename(file_path),
                            caption=caption
                        )

                # Cleanup
                if os.path.exists(file_path):
                    os.remove(file_path)
                await self.update_status(status_msg, "✅ Download completed!")

        except asyncio.CancelledError:
            await self.update_status(status_msg, "❌ Download cancelled by user")
            if 'file_path' in locals() and os.path.exists(file_path):
                os.remove(file_path)

        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            logger.error(f"Download error: {error_msg}")
            logger.error(traceback.format_exc())
            await self.update_status(status_msg, error_msg)
            if 'file_path' in locals() and os.path.exists(file_path):
                os.remove(file_path)

        finally:
            if user_id in self.downloads:
                del self.downloads[user_id]

    async def split_and_send(self, file_path: str, chat, status_msg):
        """Split large files and send in parts"""
        try:
            file_size = os.path.getsize(file_path)
            base_name = os.path.basename(file_path)
            file_name, ext = os.path.splitext(base_name)
            
            total_parts = -(-file_size // self.max_bytes)  # Ceiling division
            
            with open(file_path, 'rb') as f:
                for part in range(total_parts):
                    content = f.read(self.max_bytes)
                    if not content:
                        break
                        
                    part_name = f"{file_name}_part{part+1}of{total_parts}{ext}"
                    part_path = os.path.join(self.temp_dir, part_name)
                    
                    with open(part_path, 'wb') as part_file:
                        part_file.write(content)
                    
                    await self.update_status(
                        status_msg,
                        f"📤 Sending part {part+1} of {total_parts}..."
                    )
                    
                    with open(part_path, 'rb') as part_file:
                        await chat.send_document(
                            document=part_file,
                            filename=part_name,
                            caption=f"📦 Part {part+1}/{total_parts}"
                        )
                    
                    os.remove(part_path)
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Split and send error: {e}")
            raise

    @staticmethod
    async def update_status(message, text: str):
        """Update status message with retry logic"""
        for _ in range(3):  # Retry up to 3 times
            try:
                await message.edit_text(text)
                break
            except Exception as e:
                logger.error(f"Status update error: {e}")
                await asyncio.sleep(1)

class TelegramBot:
    def __init__(self):
        self.downloader = M3U8Downloader()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        await update.effective_chat.send_message(
            f"👋 Welcome {user.mention_html()}!\n\n"
            "🎥 *M3U8 Stream Downloader*\n\n"
            "Send me an M3U8 URL and I'll download it for you!\n\n"
            "*Features:*\n"
            "• Best quality downloads\n"
            "• Large file support (auto-split)\n"
            "• Progress updates\n"
            "• Download cancellation\n\n"
            "*Usage:*\n"
            "Just paste the M3U8 URL and I'll handle the rest!\n\n"
            "Type /help for more information.",
            parse_mode="HTML"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await update.effective_chat.send_message(
            "*How to use this bot:*\n\n"
            "1. Find an M3U8 stream URL\n"
            "2. Copy the URL (must end with .m3u8)\n"
            "3. Paste it in this chat\n"
            "4. Wait for download and upload\n\n"
            "*Notes:*\n"
            "• Large files will be split into parts\n"
            "• You can cancel downloads anytime\n"
            "• One download at a time per user\n\n"
            "*Commands:*\n"
            "/start - Start the bot\n"
            "/help - Show this help message\n"
            "/status - Check bot status",
            parse_mode="Markdown"
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        active_downloads = len(self.downloader.downloads)
        await update.effective_chat.send_message(
            f"🤖 *Bot Status*\n\n"
            f"• Active Downloads: {active_downloads}\n"
            f"• System Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"• Bot Version: 2.0.0\n"
            f"• Created by: @harshMrDev",
            parse_mode="Markdown"
        )

    async def handle_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle M3U8 URLs"""
        text = update.message.text
        if not text:
            return
            
        if not text.strip().lower().endswith('.m3u8'):
            await update.effective_chat.send_message(
                "❌ Please send a valid M3U8 URL (should end with .m3u8)"
            )
            return

        await self.downloader.download(text, update, context)

    async def handle_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle download cancellation"""
        query = update.callback_query
        await query.answer()
        
        # Extract message_id from callback data
        message_id = int(query.data.split('_')[1])
        user_id = update.effective_user.id
        
        if user_id in self.downloader.downloads and self.downloader.downloads[user_id] == message_id:
            del self.downloader.downloads[user_id]
            await query.edit_message_text("❌ Download cancelled by user")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    error_message = "❌ An error occurred. Please try again later."
    if isinstance(context.error, NetworkError):
        error_message = "❌ Network error. Please check your URL and try again."
    elif isinstance(context.error, TelegramError):
        error_message = "❌ Telegram API error. Please try again later."
    
    if update and isinstance(update, Update) and update.effective_chat:
        await update.effective_chat.send_message(error_message)

def main():
    """Main function"""
    try:
        # Get bot token
        BOT_TOKEN = os.getenv("BOT_TOKEN")
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN environment variable not set!")
            sys.exit(1)
        
        # Initialize bot
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
        
        # Add handlers
        application.add_handler(CommandHandler("start", bot.start_command))
        application.add_handler(CommandHandler("help", bot.help_command))
        application.add_handler(CommandHandler("status", bot.status_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_url))
        application.add_handler(CallbackQueryHandler(bot.handle_cancel, pattern="^cancel_"))
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        # Start bot
        logger.info("🤖 Bot is starting... Created by @harshMrDev")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)
