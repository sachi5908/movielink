import asyncio
import json
import base64
import time
import os
import logging
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
import nest_asyncio

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get bot token from environment variable
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set!")
    exit(1)

async def extract_link_from_page(page):
    try:
        await page.wait_for_timeout(2000)
        current_time = int(time.time())

        for frame in page.frames:
            try:
                raw = await frame.evaluate("window.localStorage.getItem('soralinklite')")
                if not raw:
                    continue
                
                obj = json.loads(raw)
                for value in obj.values():
                    if value.get("new") and current_time - value.get("time", 0) < 600:
                        b64_link = value.get("link")
                        if b64_link:
                            decoded = base64.b64decode(b64_link).decode()
                            return decoded
            except Exception as e:
                logger.debug(f"Error checking frame: {e}")
                continue
        return None
    except Exception as e:
        logger.error(f"Error in extract_link_from_page: {e}")
        return None

async def resolve_ozolink_once(url):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-gpu',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--no-sandbox'
                ],
                timeout=30000
            )
            context = await browser.new_context()
            page = await context.new_page()
            
            try:
                await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    if "ozolinks.art" not in page.url:
                        break
                
                final = await extract_link_from_page(page)
                return final
            except Exception as e:
                logger.error(f"Error during page operations: {e}")
                return None
            finally:
                await browser.close()
    except Exception as e:
        logger.error(f"Error in resolve_ozolink_once: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("Send me an Ozolink URL and I'll decode it.")
    except Exception as e:
        logger.error(f"Error in start command: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    url = update.message.text.strip()
    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ Please send a valid URL starting with http:// or https://")
        return

    try:
        await update.message.reply_text("🔍 Decoding the Ozolink...")
        
        result = await resolve_ozolink_once(url)
        if result:
            await update.message.reply_text(f"✅ Final Link:\n{result}")
        else:
            await update.message.reply_text("❌ Could not decode the link.")
    except asyncio.TimeoutError:
        await update.message.reply_text("⚠️ The operation timed out. Please try again.")
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

async def main():
    try:
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        logger.info("✅ Bot starting...")
        await application.run_polling()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        raise

if __name__ == "__main__":
    nest_asyncio.apply()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped gracefully")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
