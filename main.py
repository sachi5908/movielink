import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
import json
import base64
import time
from playwright.async_api import async_playwright
import asyncio
import re
import os
import logging
from aiohttp import web

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# THIS IS THE NEW LINE TO HIDE THE NOISY HTTP POLLING LOGS
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("FATAL: BOT_TOKEN environment variable is not set!")
    exit(1)

PORT = int(os.environ.get("PORT", 8080))
DOMAIN_FILE = "domain.txt"
DEFAULT_DOMAIN = "https://ndjsbda.com"

# --- CACHE ---
RESULTS_CACHE = {}

# --- HEALTH CHECK SERVER ---
async def health_check(request):
    """A simple health check endpoint."""
    return web.Response(text="Telegram Bot is Running")

def setup_http_server():
    """Sets up the aiohttp application and returns the runner for lifecycle management."""
    app = web.Application()
    app.router.add_get('/', health_check)
    return web.AppRunner(app)


# --- DOMAIN MANAGEMENT ---
def get_domain():
    """Reads the domain from the domain file, or returns the default."""
    if os.path.exists(DOMAIN_FILE):
        with open(DOMAIN_FILE, "r") as f:
            domain = f.read().strip()
            if domain:
                return domain
    return DEFAULT_DOMAIN

def save_domain(new_domain: str):
    """Saves the new domain to the domain file."""
    with open(DOMAIN_FILE, "w") as f:
        f.write(new_domain)

# --- WEB SCRAPING ---
def search_bollyflix(movie_title: str):
    """Searches for content on the currently configured domain."""
    domain = get_domain()
    search_url = f"{domain}/?s={movie_title.replace(' ', '+')}"
    try:
        response = requests.get(search_url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.find_all('article', class_='latestPost')
        if not articles:
            return None
        return [{'title': a.find('h2', class_='title').a.get_text(strip=True), 'link': a.find('h2', class_='title').a['href']} for a in articles]
    except requests.exceptions.RequestException as e:
        logger.error(f"Domain connection error: {e}")
        return "domain_error"

def get_download_links(page_url: str):
    """Scrapes download links from a movie or series page."""
    try:
        response = requests.get(page_url, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        final_links = []

        movie_headings = soup.find_all('h5', style="text-align: center;")
        for heading in movie_headings:
            if heading.find('span', style="color: #13BF3C;"):
                quality_title = heading.get_text(strip=True)
                links_p = heading.find_next_sibling('p')
                if links_p:
                    download_links = links_p.find_all('a', class_='dl')
                    links_data = [{'text': link.get_text(strip=True), 'url': link['href']} for link in download_links]
                    if links_data:
                        final_links.append({'quality': quality_title, 'links': links_data})
        if final_links:
            return final_links

        series_headings = soup.find_all('h4', style="text-align: center;")
        for heading in series_headings:
            quality_title = heading.get_text(strip=True)
            links_p = heading.find_next_sibling('p')
            if links_p:
                download_button = links_p.find('a', class_=re.compile(r'(maxbutton|btnn)'))
                if download_button and download_button.has_attr('href'):
                    links_data = [{'text': 'Download Links', 'url': download_button['href']}]
                    final_links.append({'quality': quality_title, 'links': links_data})

        return final_links if final_links else None

    except requests.exceptions.RequestException as e:
        logger.error(f"Web scraping error on page: {e}")
        return "error"

# --- LINK EXTRACTION ---
async def parse_page_for_link(page):
    """Scans the given Playwright page object for the Base64-encoded link."""
    try:
        await page.wait_for_timeout(2000)
        current_time = int(time.time())
        for frame in page.frames:
            try:
                if frame.is_detached(): continue
                raw = await frame.evaluate("window.localStorage.getItem('soralinklite')")
                if not raw: continue
                obj = json.loads(raw)
                for value in obj.values():
                    if value.get("new") and current_time - value.get("time", 0) < 600:
                        b64_link = value.get("link")
                        if b64_link:
                            return base64.b64decode(b64_link).decode()
            except Exception as e:
                logger.warning(f"Frame check error (continuing): {e}")
                continue
        return None
    except Exception as e:
        logger.error(f"Error during page parsing: {e}")
        return None

async def extract_final_link(url: str):
    """Manages the browser lifecycle to resolve the final link."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-gpu',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--no-sandbox'
                ]
            )
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)
                found_link = await parse_page_for_link(page)
                return found_link
            except Exception as e:
                logger.error(f"Page navigation error: {e}")
                return None
            finally:
                await browser.close()
    except Exception as e:
        logger.critical(f"A critical browser error occurred: {e}")
        return None

# --- TELEGRAM BOT HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a movie/series name or a direct link to process.")

async def set_domain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_domain = context.args[0]
        if not (new_domain.startswith("http://") or new_domain.startswith("https://")):
            await update.message.reply_text("Invalid format. Please provide a full URL like `https://newdomain.com`")
            return
        save_domain(new_domain)
        await update.message.reply_text(f"✅ Domain successfully updated to: `{new_domain}`", parse_mode=ParseMode.MARKDOWN)
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/setdomain https://new-domain.com`")

async def handle_direct_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    msg = await update.message.reply_text("Processing your direct link... This may take a moment. ⏳")
    
    final_url = await extract_final_link(url)

    if final_url:
        keyboard = [[InlineKeyboardButton("✅ Open Download Link in Browser", url=final_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.edit_text("Your direct download link is ready!", reply_markup=reply_markup)
    else:
        await msg.edit_text("❌ Sorry, I could not extract the final download link from the provided URL.")

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text
    msg = await update.message.reply_text(f"Searching for '{title}'...")
    search_results = search_bollyflix(title)

    if search_results == "domain_error":
        domain = get_domain()
        await msg.edit_text(
            f"⚠️ The current domain `{domain}` seems to be down.\n\n"
            "Please set a new domain with the command:\n"
            "`/setdomain https://new-domain.com`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if not search_results:
        await msg.edit_text(f"Sorry, no results found for '{title}'.")
        return

    search_key = f"search_{update.effective_chat.id}-{update.message.message_id}"
    RESULTS_CACHE[search_key] = search_results
    keyboard = []
    for i, result in enumerate(search_results[:10]):
        callback_data = f"movie:{search_key}:{i}"
        keyboard.append([InlineKeyboardButton(result['title'], callback_data=callback_data)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.edit_text('Please select an item:', reply_markup=reply_markup)

async def movie_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles content selection and displays available quality buttons."""
    query = update.callback_query
    await query.answer()
    _p, search_key, index_str = query.data.split(':', 2)
    index = int(index_str)

    search_results = RESULTS_CACHE.get(search_key)
    if not search_results or index >= len(search_results):
        await query.edit_message_text("Error: Your search result has expired. Please search again.")
        return
        
    selected_item = search_results[index]
    page_url = selected_item['link']
    item_title_display = selected_item['title']
    
    await query.edit_message_text(text=f"Fetching qualities for '{item_title_display}'...")
    download_links_data = get_download_links(page_url)

    if not download_links_data or download_links_data == "error":
        await query.edit_message_text(f"Sorry, couldn't find any download links for '{item_title_display}'.")
        return

    links_key = f"links_{query.id}"
    RESULTS_CACHE[links_key] = download_links_data
    keyboard = []
    for i, quality_group in enumerate(download_links_data):
        full_title = quality_group['quality']
        season_match = re.search(r'(Season\s*\d+)', full_title, re.IGNORECASE)
        season_text = season_match.group(1).strip() if season_match else ""
        quality_match = re.search(r'(\d+p\s*\[.*?\])', full_title)
        quality_text = quality_match.group(1).strip() if quality_match else ""
        if season_text and quality_text:
            button_text = f"{season_text} - {quality_text}"
        elif quality_text:
            button_text = quality_text
        else:
            button_text = full_title
        callback_data = f"quality:{links_key}:{i}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"✅ *Select a quality for {item_title_display}*",
                                  reply_markup=reply_markup,
                                  parse_mode=ParseMode.MARKDOWN)
    if search_key in RESULTS_CACHE:
        del RESULTS_CACHE[search_key]

async def quality_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _p, links_key, quality_index_str = query.data.split(':', 2)
    quality_index = int(quality_index_str)

    all_links_data = RESULTS_CACHE.get(links_key)
    if not all_links_data or quality_index >= len(all_links_data):
        await query.edit_message_text("Error: These links have expired. Please start a new search.")
        return

    selected_quality_group = all_links_data[quality_index]
    keyboard = []
    for i, link_info in enumerate(selected_quality_group['links']):
        callback_data = f"process:{links_key}:{quality_index}:{i}"
        keyboard.append([InlineKeyboardButton(link_info['text'], callback_data=callback_data)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"✅ *Select a download source for {selected_quality_group['quality']}*",
                                  reply_markup=reply_markup,
                                  parse_mode=ParseMode.MARKDOWN)

async def process_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _p, links_key, quality_index_str, link_index_str = query.data.split(':', 3)
    quality_index = int(quality_index_str)
    link_index = int(link_index_str)

    all_links_data = RESULTS_CACHE.get(links_key)
    if not all_links_data or quality_index >= len(all_links_data) or link_index >= len(all_links_data[quality_index]['links']):
        await query.edit_message_text("Error: This link has expired. Please search again.")
        return

    selected_link = all_links_data[quality_index]['links'][link_index]
    link_url = selected_link['url']
    link_text = selected_link['text']
    
    await query.edit_message_text(f"Processing '{link_text}'... This may take a moment. ⏳")
    final_url = await extract_final_link(link_url)

    if final_url:
        keyboard = [[InlineKeyboardButton("✅ Open Download Link in Browser", url=final_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Your direct download link is ready!", reply_markup=reply_markup)
    else:
        await query.edit_message_text("❌ Sorry, I could not extract the final download link. The link might be broken or changed.")
    if links_key in RESULTS_CACHE:
        del RESULTS_CACHE[links_key]

# --- MAIN EXECUTION ---
async def main():
    """Configures and runs the bot and web server concurrently."""
    
    # Set up the Telegram application
    application = Application.builder().token(BOT_TOKEN).build()
    
    direct_link_regex = re.compile(r'https?://[a-zA-Z0-9.-]+\/\?id=[\w/+=.-]+')
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setdomain", set_domain_command))
    application.add_handler(MessageHandler(filters.Regex(direct_link_regex), handle_direct_link))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(movie_selection_handler, pattern="^movie:.*"))
    application.add_handler(CallbackQueryHandler(quality_selection_handler, pattern="^quality:.*"))
    application.add_handler(CallbackQueryHandler(process_link_handler, pattern="^process:.*"))
    
    # Set up the aiohttp web server
    http_runner = setup_http_server()
    await http_runner.setup()
    site = web.TCPSite(http_runner, "0.0.0.0", PORT)

    # Run application and web server together
    async with application:
        bot_info = await application.bot.get_me()
        logger.info(f"Successfully connected as bot: {bot_info.username}")
        
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot has started polling for updates.")
        
        await site.start()
        logger.info(f"HTTP health check server started on port {PORT}")
        
        # Keep the script running until interrupted
        await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped manually.")
