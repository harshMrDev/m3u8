import os
import asyncio
import traceback
from datetime import datetime
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class M3U8Downloader:
    def __init__(self, max_size_mb=49):
        self.max_bytes = max_size_mb * 1024 * 1024
        self.temp_dir = "/tmp/downloads"
        os.makedirs(self.temp_dir, exist_ok=True)
        self.output_template = os.path.join(self.temp_dir, "%(title)s.%(ext)s")

    async def download(self, m3u8_url, update, context):
        """Download M3U8 stream and send to Telegram"""
        chat = update.effective_chat
        try:
            status_msg = await chat.send_message("⏳ Starting download...")
            
            # Configure yt-dlp options
            ydl_opts = {
                'format': 'best',
                'outtmpl': self.output_template,
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
                'progress': False,
                'retries': 10,
            }

            def download_progress_hook(d):
                if d['status'] == 'downloading':
                    if 'total_bytes' in d:
                        percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
                        context.job_queue.run_once(
                            lambda _: self.update_status(status_msg, f"⏳ Downloaded: {percent:.1f}%"),
                            0
                        )
                    elif 'downloaded_bytes' in d:
                        mb = d['downloaded_bytes'] / 1024 / 1024
                        context.job_queue.run_once(
                            lambda _: self.update_status(status_msg, f"⏳ Downloaded: {mb:.1f}MB"),
                            0
                        )

            ydl_opts['progress_hooks'] = [download_progress_hook]

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await chat.send_message("🔍 Checking stream...")
                info = ydl.extract_info(m3u8_url, download=False)
                file_path = ydl.prepare_filename(info)
                
                await chat.send_message("📥 Starting download...")
                ydl.download([m3u8_url])

                if not os.path.exists(file_path):
                    await chat.send_message("❌ Download failed!")
                    return

                size = os.path.getsize(file_path)
                if size == 0:
                    await chat.send_message("❌ Downloaded file is empty!")
                    os.remove(file_path)
                    return

                if size > self.max_bytes:
                    await chat.send_message(
                        f"⚠️ File is too large ({size/1024/1024:.1f}MB). Splitting..."
                    )
                    await self.split_and_send(file_path, update, context)
                else:
                    await chat.send_message("📤 Uploading to Telegram...")
                    with open(file_path, 'rb') as f:
                        await chat.send_document(
                            document=f,
                            filename=os.path.basename(file_path),
                            caption=f"🎥 {info.get('title', 'Video')}\n💾 {size/1024/1024:.1f}MB"
                        )

                os.remove(file_path)
                await status_msg.edit_text("✅ Download completed!")

        except Exception as e:
            traceback.print_exc()
            await chat.send_message(f"❌ Error: {str(e)}")
            if 'file_path' in locals() and os.path.exists(file_path):
                os.remove(file_path)

    @staticmethod
    async def update_status(message, text):
        try:
            await message.edit_text(text)
        except Exception:
            pass

    async def split_and_send(self, file_path, update, context):
        """Split large files and send in parts"""
        chat = update.effective_chat
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
                
                await chat.send_message(f"📤 Sending part {part+1} of {total_parts}...")
                
                with open(part_path, 'rb') as part_file:
                    await chat.send_document(
                        document=part_file,
                        filename=part_name,
                        caption=f"📦 Part {part+1}/{total_parts}"
                    )
                
                os.remove(part_path)
                await asyncio.sleep(1)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    try:
        await update.effective_chat.send_message(
            "🎥 *M3U8 Stream Downloader*\n\n"
            "Send me an M3U8 URL and I'll download it for you!\n\n"
            "*Features:*\n"
            "• Downloads in best quality\n"
            "• Supports large files (auto-split)\n"
            "• Progress updates\n\n"
            "*Usage:*\n"
            "Just paste the M3U8 URL and I'll handle the rest!",
            parse_mode="Markdown"
        )
    except Exception:
        traceback.print_exc()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    try:
        await update.effective_chat.send_message(
            "*How to use:*\n\n"
            "1. Find an M3U8 stream URL\n"
            "2. Copy the URL (should end with .m3u8)\n"
            "3. Paste it here\n"
            "4. Wait for download and upload\n\n"
            "*Note:* Large files will be split into parts",
            parse_mode="Markdown"
        )
    except Exception:
        traceback.print_exc()

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """URL handler"""
    try:
        text = update.message.text
        if not text:
            return
            
        if not text.strip().lower().endswith('.m3u8'):
            await update.effective_chat.send_message(
                "❌ Please send a valid M3U8 URL (should end with .m3u8)"
            )
            return

        downloader = M3U8Downloader()
        await downloader.download(text, update, context)
    except Exception:
        traceback.print_exc()
        await update.effective_chat.send_message(
            "❌ An error occurred while processing your request."
        )

def main():
    """Main function"""
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable not set!")
        
    # Initialize bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    
    # Start bot
    print("🤖 Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()