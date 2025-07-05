import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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

# --- SETUP 1: LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- SETUP 2: CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("FATAL: BOT_TOKEN environment variable is not set!")
    exit(1)

PORT = int(os.environ.get("PORT", 8080))
DOMAIN_FILE = "domain.txt"
DEFAULT_DOMAIN = "https://ndjsbda.com"

# --- CACHE ---
RESULTS_CACHE = {}

# --- SETUP 3: HEALTH CHECK SERVER ---
async def health_check(request):
    """A simple health check endpoint."""
    return web.Response(text="Telegram Bot is Running")

def setup_http_server():
    """Sets up the aiohttp application."""
    app = web.Application()
    app.router.add_get('/', health_check)
    return web.AppRunner(app)

# --- UTILITY FUNCTIONS ---
def get_domain():
    if os.path.exists(DOMAIN_FILE):
        with open(DOMAIN_FILE, "r") as f:
            domain = f.read().strip()
            if domain:
                return domain
    return DEFAULT_DOMAIN

def save_domain(new_domain: str):
    with open(DOMAIN_FILE, "w") as f:
        f.write(new_domain)

# --- SCRAPING FUNCTIONS ---
def search_bollyflix(movie_title: str):
    """Searches for content, retrying once on failure."""
    for attempt in range(2):
        try:
            domain = get_domain()
            search_url = f"{domain}/?s={movie_title.replace(' ', '+')}"
            response = requests.get(search_url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            articles = soup.find_all('article', class_='latestPost')
            if not articles:
                return None
            return [{'title': a.find('h2', class_='title').a.get_text(strip=True), 'link': a.find('h2', class_='title').a['href']} for a in articles]
        except requests.exceptions.RequestException as e:
            logger.error(f"search_bollyflix attempt {attempt + 1} failed: {e}")
            if attempt < 1:
                time.sleep(1)
                continue
    return "domain_error"

def get_page_details(page_url: str):
    """
    Scrapes page details, retrying once on failure. 
    The "loading" message is handled by the calling function.
    """
    for attempt in range(2):
        try:
            # This is the potentially long-running process
            response = requests.get(page_url, timeout=15)
            response.raise_for_status()
            
            # --- Start of processing ---
            soup = BeautifulSoup(response.text, 'html.parser')

            poster_tag = soup.find('meta', property='og:image')
            poster_url = poster_tag['content'] if poster_tag else None

            screenshot_urls = []
            screenshots_heading = soup.find(lambda tag: tag.name in ['h2', 'h3'] and 'ScreenShots' in tag.get_text())
            if screenshots_heading:
                p_tag = screenshots_heading.find_next_sibling('p')
                if p_tag:
                    img_tags = p_tag.find_all('img')
                    for img in img_tags:
                        if img.has_attr('src'):
                            screenshot_urls.append(img['src'])

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
            
            if not final_links:
                series_headings = soup.find_all('h4', style="text-align: center;")
                for heading in series_headings:
                    quality_title = heading.get_text(strip=True)
                    links_p = heading.find_next_sibling('p')
                    if links_p:
                        download_button = links_p.find('a', class_=re.compile(r'(maxbutton|btnn)'))
                        if download_button and download_button.has_attr('href'):
                            button_text = 'Download Links'
                            if 'season' in quality_title.lower():
                                button_text = 'Episode Links'
                            links_data = [{'text': button_text, 'url': download_button['href']}]
                            final_links.append({'quality': quality_title, 'links': links_data})
            
            # --- End of processing ---
            return {
                'poster': poster_url,
                'screenshots': screenshot_urls,
                'download_links': final_links if final_links else None
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"get_page_details attempt {attempt + 1} failed: {e}")
            if attempt < 1:
                time.sleep(1)
                continue
    return "error"

async def parse_page_for_link(page):
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
    """Manages the browser lifecycle to resolve the final link, with extra retries."""
    max_attempts = 4
    for attempt in range(max_attempts):
        logger.info(f"--- Link extraction attempt {attempt + 1} of {max_attempts} ---")
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
                context = await browser.new_context()
                page = await context.new_page()
                try:
                    await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(10000)
                    found_link = await parse_page_for_link(page)
                    if found_link:
                        await browser.close()
                        return found_link
                except Exception as e:
                    logger.error(f"Page navigation error on attempt {attempt + 1}: {e}")
        except Exception as e:
            logger.critical(f"A critical browser error occurred on attempt {attempt + 1}: {e}")
        finally:
            if browser and browser.is_connected():
                await browser.close()
        if attempt < max_attempts - 1:
            logger.warning(f"Attempt {attempt + 1} failed. Retrying in 1 seconds...")
            await asyncio.sleep(1)
    logger.error("All retry attempts failed to extract the link.")
    return None

def scrape_fxlinks_page(url: str):
    """Scrapes fxlinks page, retrying once on failure."""
    for attempt in range(2):
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            scraped_data = []
            for a_tag in soup.find_all('a', href=re.compile(r'fastdlserver\.life')):
                href = a_tag['href']
                button_text = a_tag.get_text(strip=True)
                if button_text and href:
                    scraped_data.append({'text': button_text, 'url': href})
            logger.info(f"Found {len(scraped_data)} links on fxlinks page.")
            return scraped_data
        except requests.exceptions.RequestException as e:
            logger.error(f"scrape_fxlinks_page attempt {attempt + 1} failed: {e}")
            if attempt < 1:
                time.sleep(1)
                continue
    return "An error occurred while fetching the URL."

def scrape_fastdl_links(url: str):
    """Scrapes fastdlserver page, retrying once on failure."""
    for attempt in range(2):
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            scraped_data = []
            avoid_keywords = ['login', 'mirror']
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                link_text_lower = a_tag.get_text().lower()
                if not any(keyword in link_text_lower for keyword in avoid_keywords) and \
                   not any(keyword in href for keyword in avoid_keywords):
                    if any(site in href for site in ['busycdn', 'pixeldrain', 'filesgram', 'drivebots']):
                        button_text = a_tag.get_text(strip=True)
                        scraped_data.append({'text': button_text, 'url': href})
            return scraped_data
        except requests.exceptions.RequestException as e:
            logger.error(f"scrape_fastdl_links attempt {attempt + 1} failed: {e}")
            if attempt < 1:
                time.sleep(1)
                continue
    return "An error occurred while fetching the URL."


# --- TELEGRAM HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a movie/series name.")

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
            f"⚠️ The current domain `{domain}` seems to be down.\n\nPlease set a new domain with the command:\n`/setdomain https://new-domain.com`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if not search_results:
        await msg.edit_text(f"Sorry, no results found for '{title}'.")
        return
    search_key = f"search_{update.effective_chat.id}-{int(time.time())}"
    RESULTS_CACHE[search_key] = search_results
    keyboard = []
    for i, result in enumerate(search_results[:10]):
        callback_data = f"movie:{search_key}:{i}"
        keyboard.append([InlineKeyboardButton(result['title'], callback_data=callback_data)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.edit_text('Please select an item:', reply_markup=reply_markup)

# --- UPDATED: MOVIE SELECTION HANDLER WITH LOADING MESSAGE ---
async def movie_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _p, search_key, index_str = query.data.split(':', 2)
        index = int(index_str)
    except ValueError:
        await query.edit_message_text("Error: Invalid callback data. Please search again.")
        return
        
    search_results = RESULTS_CACHE.get(search_key)
    if not search_results or index >= len(search_results):
        await query.edit_message_text("Error: Your search result has expired. Please search again.")
        return
    
    # --- MODIFIED: Show loading message before calling get_page_details ---
    is_back_navigation = "Select a download source" in query.message.text if query.message else False
    loading_text = "⏳ Loading qualities..." if is_back_navigation else "⏳ Loading details, please wait..."
    await query.edit_message_text(text=loading_text)

    selected_item = search_results[index]
    page_url = selected_item['link']
    item_title_display = selected_item['title']

    # This is the blocking call, the "Loading..." message is displayed during this time
    page_details = get_page_details(page_url)
    
    if not page_details or page_details == "error":
        await query.edit_message_text(f"Sorry, couldn't find any details for '{item_title_display}'.")
        return
    
    download_links_data = page_details.get('download_links')
    if not download_links_data:
        await query.edit_message_text(f"Sorry, couldn't find any download links for '{item_title_display}'.")
        return
    
    if not is_back_navigation:
        # On forward navigation, delete the loading message and send images
        await query.delete_message()
        
        poster_id = None
        screenshot_ids = []
        
        poster_url = page_details.get('poster')
        if poster_url:
            try:
                poster_msg = await context.bot.send_photo(chat_id=query.message.chat_id, photo=poster_url)
                poster_id = poster_msg.message_id
            except Exception as e: logger.warning(f"Could not send poster: {e}")
                
        screenshot_urls = page_details.get('screenshots')
        if screenshot_urls:
            media_group = [InputMediaPhoto(media=url) for url in screenshot_urls[:10]]
            if media_group:
                try:
                    screenshot_msgs = await context.bot.send_media_group(chat_id=query.message.chat_id, media=media_group)
                    screenshot_ids = [msg.message_id for msg in screenshot_msgs]
                except Exception as e: logger.warning(f"Could not send media group: {e}")

        active_images_key = f"active_images_{query.message.chat_id}"
        all_image_ids = screenshot_ids[:]
        if poster_id: all_image_ids.insert(0, poster_id)
        RESULTS_CACHE[active_images_key] = all_image_ids
        
        links_key = f"links_{query.id}"
        RESULTS_CACHE[links_key] = { 'links': download_links_data, 'back_data_movie': query.data, 'message_ids_screenshots': screenshot_ids }
    else:
        # On back navigation, just prepare the key
        links_key = f"links_{query.id}"
        if not RESULTS_CACHE.get(links_key):
             RESULTS_CACHE[links_key] = { 'links': download_links_data, 'back_data_movie': query.data, 'message_ids_screenshots': [] }

    # Build Keyboard for Quality/Season List
    keyboard = []
    for i, quality_group in enumerate(download_links_data):
        full_title = quality_group['quality']
        season_match = re.search(r'(Season\s*\d+)', full_title, re.IGNORECASE)
        season_text = season_match.group(1).strip() if season_match else ""
        quality_match = re.search(r'(\d{3,4}p.*)', full_title, re.IGNORECASE)
        quality_text = quality_match.group(1).strip() if quality_match else ""
        button_text = f"{season_text} - {quality_text}" if season_text and quality_text else (quality_text or full_title)
        
        callback_data = f"quality:{links_key}:{i}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("« Back to Search Results", callback_data=f"back_search:{search_key}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = f"✅ *Select a quality for {item_title_display}*"
    if is_back_navigation:
        # Edit the "Loading..." message
        await query.edit_message_text(text=message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        # Send a new message after images
        await context.bot.send_message(chat_id=query.message.chat_id, text=message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def quality_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _p, links_key, quality_index_str = query.data.split(':', 2)
    quality_index = int(quality_index_str)
    
    cached_data = RESULTS_CACHE.get(links_key)
    if not cached_data:
        await query.edit_message_text("Error: These links have expired. Please start a new search.")
        return

    selected_quality_group = cached_data['links'][quality_index]
    keyboard = []
    for i, link_info in enumerate(selected_quality_group['links']):
        callback_data = f"process:{links_key}:{quality_index}:{i}"
        keyboard.append([InlineKeyboardButton(link_info['text'], callback_data=callback_data)])

    back_data_to_movie = cached_data.get('back_data_movie')
    if back_data_to_movie:
        keyboard.append([InlineKeyboardButton("« Back to Qualities", callback_data=back_data_to_movie)])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"✅ *Select a download source for {selected_quality_group['quality']}*", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def process_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _p, links_key, quality_index_str, link_index_str = query.data.split(':', 3)
    quality_index = int(quality_index_str)
    link_index = int(link_index_str)

    cached_data = RESULTS_CACHE.get(links_key)
    if not cached_data:
        await query.edit_message_text("Error: This link has expired. Please search again.")
        return

    screenshot_ids_to_delete = cached_data.get('message_ids_screenshots', [])
    for msg_id in screenshot_ids_to_delete:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception as e:
            logger.warning(f"Could not delete screenshot message {msg_id}: {e}")
    cached_data['message_ids_screenshots'] = []


    selected_link = cached_data['links'][quality_index]['links'][link_index]
    link_url = selected_link['url']
    link_text = selected_link['text']
    
    await query.edit_message_text(f"Processing '{link_text}'... This may take a moment. ⏳")
    
    final_url = await extract_final_link(link_url)

    if final_url:
        if "fxlinks" in final_url:
            await query.edit_message_text("Found an Episode page, scraping for episode links... 🕵️‍♂️")
            
            scraped_data = await asyncio.to_thread(scrape_fxlinks_page, final_url)

            if isinstance(scraped_data, list) and scraped_data:
                fxlinks_key = f"fxlinks_{query.id}"
                RESULTS_CACHE[fxlinks_key] = scraped_data

                keyboard = []
                for i, link_info in enumerate(scraped_data):
                    callback_data = f"process_fx:{fxlinks_key}:{i}"
                    keyboard.append([InlineKeyboardButton(link_info['text'], callback_data=callback_data)])
                
                back_to_source_callback = f"quality:{links_key}:{quality_index}"
                keyboard.append([InlineKeyboardButton("« Back to Sources", callback_data=back_to_source_callback)])

                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("✅ Found episode links! Please select one to proceed:", reply_markup=reply_markup)
            else:
                error_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open episode Page Manually", url=final_url)]])
                await query.edit_message_text("❌ Could not scrape any episode links.", reply_markup=error_markup)
        
        elif "fastdlserver" in final_url:
            await query.edit_message_text("Found a download link page, scraping for direct links... 🕵️‍♂️")
            
            scraped_data = await asyncio.to_thread(scrape_fastdl_links, final_url)

            if isinstance(scraped_data, list) and scraped_data:
                keyboard = []
                for link_info in scraped_data:
                    keyboard.append([InlineKeyboardButton(link_info['text'], url=link_info['url'])])
                
                keyboard.append([InlineKeyboardButton("🔗 Open download link Page Manually", url=final_url)])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("✅ Your direct download links are ready!", reply_markup=reply_markup)
            else:
                error_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open download link Manually", url=final_url)]])
                await query.edit_message_text("❌ Could not scrape valid links. Try opening the page manually.", reply_markup=error_markup)
        
        else:
            keyboard = [[InlineKeyboardButton("✅ Open Download Link in Browser", url=final_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Your direct download link is ready!", reply_markup=reply_markup)
    else:
        await query.edit_message_text("❌ Sorry, I could not extract the final download link.")

async def process_fx_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        _p, fxlinks_key, link_index_str = query.data.split(':', 2)
        link_index = int(link_index_str)
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid callback data format for process_fx: {query.data} - Error: {e}")
        await query.edit_message_text("Error: Invalid link data. Please start a new search.")
        return

    links_list = RESULTS_CACHE.get(fxlinks_key)
    if not links_list or link_index >= len(links_list):
        await query.edit_message_text("Error: These links have expired or are invalid. Please start a new search.")
        return

    selected_link_info = links_list[link_index]
    final_url = selected_link_info['url']
    
    await query.edit_message_text(f"Processing '{selected_link_info['text']}'...\nScraping for direct links... 🕵️‍♂️")
    
    scraped_data = await asyncio.to_thread(scrape_fastdl_links, final_url)

    if isinstance(scraped_data, list) and scraped_data:
        keyboard = []
        for link_info in scraped_data:
            keyboard.append([InlineKeyboardButton(link_info['text'], url=link_info['url'])]) 
        keyboard.append([InlineKeyboardButton("🔗 Open Download link Page Manually", url=final_url)])
        keyboard.append([InlineKeyboardButton("« Back to Episode List", callback_data=f"back_episodes:{fxlinks_key}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"✅ Your direct download links for '{selected_link_info['text']}'", reply_markup=reply_markup)
    else:
        keyboard = [
            [InlineKeyboardButton("Open Page Manually", url=final_url)],
            [InlineKeyboardButton("« Back to Episode List", callback_data=f"back_episodes:{fxlinks_key}")]
        ]
        error_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("❌ Could not scrape valid links from the server page.", reply_markup=error_markup)

async def back_to_episodes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _p, fxlinks_key = query.data.split(':', 1)
    except (ValueError, IndexError):
        await query.edit_message_text("Error: Invalid back link. Please search again.")
        return
        
    episodes_list = RESULTS_CACHE.get(fxlinks_key)
    if not episodes_list:
        await query.edit_message_text("Error: The episode list has expired. Please search again.")
        return

    keyboard = []
    for i, link_info in enumerate(episodes_list):
        callback_data = f"process_fx:{fxlinks_key}:{i}"
        keyboard.append([InlineKeyboardButton(link_info['text'], callback_data=callback_data)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("✅ Found episode links! Please select one to proceed:", reply_markup=reply_markup)

async def back_to_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        _p, search_key = query.data.split(':', 1)
    except (ValueError, IndexError):
        await query.edit_message_text("Error: Invalid back link. Please start a new search.")
        return
    
    search_results = RESULTS_CACHE.get(search_key)
    if not search_results:
        await query.edit_message_text("Error: The search results have expired. Please start a new search.")
        return
    
    active_images_key = f"active_images_{query.message.chat_id}"
    image_ids_to_delete = RESULTS_CACHE.pop(active_images_key, [])
    for msg_id in image_ids_to_delete:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception as e:
            logger.warning(f"Could not delete active image {msg_id}: {e}")

    await query.delete_message()
    
    keyboard = []
    for i, result in enumerate(search_results[:10]):
        callback_data = f"movie:{search_key}:{i}"
        keyboard.append([InlineKeyboardButton(result['title'], callback_data=callback_data)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=query.message.chat_id, text='Please select an item:', reply_markup=reply_markup)

# --- MAIN EXECUTION ---
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setdomain", set_domain_command))
    application.add_handler(MessageHandler(filters.Regex(r'https?://[a-zA-Z0-9.-]+\/\?id=[\w/+=.-]+'), handle_direct_link))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(movie_selection_handler, pattern="^movie:.*"))
    application.add_handler(CallbackQueryHandler(quality_selection_handler, pattern="^quality:.*"))
    application.add_handler(CallbackQueryHandler(process_link_handler, pattern="^process:.*"))
    application.add_handler(CallbackQueryHandler(process_fx_link_handler, pattern="^process_fx:.*"))
    application.add_handler(CallbackQueryHandler(back_to_episodes_handler, pattern="^back_episodes:.*"))
    application.add_handler(CallbackQueryHandler(back_to_search_handler, pattern="^back_search:.*"))

    http_runner = setup_http_server()
    await http_runner.setup()
    site = web.TCPSite(http_runner, "0.0.0.0", PORT)

    async with application:
        bot_info = await application.bot.get_me()
        logger.info(f"Successfully connected as bot: {bot_info.username}")
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot has started polling for updates.")
        await site.start()
        logger.info(f"HTTP health check server started on port {PORT}")
        await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped manually.")
