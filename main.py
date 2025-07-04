import asyncio
import json
import base64
import time
import os
import logging
from aiohttp import web
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

# Health check server setup
async def health_check(request):
    return web.Response(text="Telegram Bot is Running")

async def start_http_server():
    """Start a minimal HTTP server for Render's port requirement"""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("HTTP server started on port 8080")

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
                logger.debug(f"Frame check error: {e}")
                continue
        return None
    except Exception as e:
        logger.error(f"Extraction error: {e}")
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
                
                # Wait for redirects
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    if "ozolinks.art" not in page.url:
                        break
                
                return await extract_link_from_page(page)
            except Exception as e:
                logger.error(f"Page error: {e}")
                return None
            finally:
                await browser.close()
    except Exception as e:
        logger.error(f"Browser error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("Send me an Ozolink URL and I'll decode it.")
    except Exception as e:
        logger.error(f"Start command error: {e}")

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
        logger.error(f"Message handling error: {e}")
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

async def main():
    # Start HTTP server in background
    asyncio.create_task(start_http_server())
    
    # Start Telegram bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("✅ Bot starting...")
    await application.run_polling()

if __name__ == "__main__":
    nest_asyncio.apply()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped gracefully")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
