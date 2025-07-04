import asyncio
import json
import base64
import time
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
import nest_asyncio # 👈 Import the library

BOT_TOKEN = ""

async def extract_link_from_page(page):
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
        except:
            continue
    return None

async def resolve_ozolink_once(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        for _ in range(15):
            await page.wait_for_timeout(1000)
            if "ozolinks.art" not in page.url:
                break
        final = await extract_link_from_page(page)
        await browser.close()
        return final

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me an Ozolink URL and I'll decode it.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    await update.message.reply_text("🔍 Decoding the Ozolink...")
    try:
        result = await resolve_ozolink_once(url)
        if result:
            await update.message.reply_text(f"✅ Final Link:\n{result}")
        else:
            await update.message.reply_text("❌ Could not decode the link.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Bot running...")
    await app.run_polling()

if __name__ == "__main__":
    nest_asyncio.apply() # 👈 Apply the patch
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped gracefully")
