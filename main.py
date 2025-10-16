import requests
from bs4 import BeautifulSoup
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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
from urllib.parse import urlparse, parse_qs, urljoin
from datetime import datetime, timedelta

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

LOG_BOT_TOKEN = os.environ.get("LOG_BOT_TOKEN")
if not LOG_BOT_TOKEN:
    logger.error("FATAL: LOG_BOT_TOKEN environment variable is not set!")
    exit(1)

# Note: The following lines related to FORCE_JOIN_CHANNEL have been removed.
# FORCE_JOIN_CHANNEL = os.environ.get("FORCE_JOIN_CHANNEL")
# if not FORCE_JOIN_CHANNEL:
#     logger.error("FATAL: FORCE_JOIN_CHANNEL environment variable is not set!")
#     exit(1)

LOG_GROUP_CHAT_ID_STR = os.environ.get("LOG_GROUP_CHAT_ID")
LOG_GROUP_CHAT_ID = None
if LOG_GROUP_CHAT_ID_STR:
    try:
        LOG_GROUP_CHAT_ID = int(LOG_GROUP_CHAT_ID_STR)
        if not LOG_BOT_TOKEN:
            logger.error("FATAL: LOG_GROUP_CHAT_ID is set, but LOG_BOT_TOKEN is not!")
            exit(1)
    except ValueError:
        logger.error("FATAL: LOG_GROUP_CHAT_ID environment variable is not a valid integer!")
        exit(1)
else:
    logger.warning("LOG_GROUP_CHAT_ID is not set. Search logging will be disabled.")

PORT = int(os.environ.get("PORT", 8080))
DOMAIN_FILE = "domain.txt"
DEFAULT_DOMAIN =  os.environ.get("DEFAULT_DOMAIN")
if not DEFAULT_DOMAIN:
    logger.error("FATAL: DEFAULT_DOMAIN environment variable is not set!")
    exit(1)

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

async def async_retry_step(async_func, retries=3, delay=2, desc="Step"):
    """Asynchronous retry wrapper for playwright operations."""
    for attempt in range(1, retries + 1):
        try:
            return await async_func()
        except Exception as e:
            logger.warning(f"[!] {desc} failed (Attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                await asyncio.sleep(delay)
            else:
                raise

# --- SCRAPING & RESOLVER FUNCTIONS ---

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
            response = requests.get(page_url, timeout=15)
            response.raise_for_status()
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

def get_final_redirected_url(url: str):
    """Follows redirects from a URL and returns the final destination."""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return response.url  # .url contains the final URL after all redirects
    except requests.exceptions.RequestException as e:
        logger.error(f"Error getting final redirected URL for {url}: {e}")
    return None

async def resolve_gdflix_link(url: str) -> list or None:
    """
    (UPDATED)
    Uses Playwright to render a GDFlix page and extract ALL final download links.
    Returns a list of dictionaries: [{'text': 'Button Text', 'url': '...'}, ...]
    """
    logger.info(f"Attempting to resolve all links from GDFlix page: {url}")
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)

            # Wait for at least one button to be present to ensure page has loaded
            await page.wait_for_selector("a.btn", timeout=15000)

            extracted_links = []
            all_buttons = await page.query_selector_all("a.btn")

            for button in all_buttons:
                href = await button.get_attribute("href")
                # Use inner_text() to get the visible text, then clean it
                text = (await button.inner_text()).strip().replace('\n', ' ')
                
                # Filter out login buttons and ensure href and text exist
                if href and text and "/login" not in href:
                    final_link = urljoin(url, href)
                    extracted_links.append({'text': text, 'url': final_link})

            if extracted_links:
                logger.info(f"Successfully extracted {len(extracted_links)} links from GDFlix.")
                return extracted_links
            else:
                logger.warning("No valid download links found on the GDFlix page.")
                return None
            
    except Exception as e:
        logger.error(f"Error resolving GDFlix links from {url}: {e}")
        return None
    finally:
        if browser and browser.is_connected():
            await browser.close()

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

# --- DRIVEBOTS RESOLVER FUNCTIONS ---
def get_intermediate_links(start_url: str) -> list:
    """Visits the initial page to extract the two intermediate URLs."""
    try:
        response = requests.get(start_url)
        response.raise_for_status()
        parsed_url = urlparse(start_url)
        query_params = parse_qs(parsed_url.query)
        id_value = query_params.get('id', [None])[0]
        do_value = query_params.get('do', [None])[0]

        if not id_value or not do_value:
            logger.error("❌ DriveBot Error: Could not find 'id' or 'do' parameters in the start URL.")
            return []
        base_urls = re.findall(r"onclick=\"downloadFile\('([^']+)'", response.text)
        if not base_urls:
            logger.error("❌ DriveBot Error: No intermediate base URLs found on the page.")
            return []
        intermediate_links = [f"{base}?id={id_value}&do={do_value}" for base in base_urls]
        return intermediate_links
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ DriveBot Error fetching the start URL: {e}")
        return []

def get_final_link_from_page(page_url: str, session: requests.Session) -> str or None:
    """Visits an intermediate page and tries to extract the final download link."""
    try:
        response = session.get(page_url)
        response.raise_for_status()
        html_content = response.text
        post_path_match = re.search(r"fetch\('([^']+)'", html_content)
        token_match = re.search(r"formData\.append\('token', '([^']+)'\)", html_content)
        if not post_path_match or not token_match:
            return None
        relative_path = post_path_match.group(1)
        token = token_match.group(1)
        api_url = urljoin(page_url, relative_path)
        post_data = {'token': token}
        headers = {'Referer': page_url, 'X-Requested-With': 'XMLHttpRequest'}
        api_response = session.post(api_url, data=post_data, headers=headers)
        api_response.raise_for_status()
        json_data = api_response.json()
        final_url = json_data.get('url')
        if final_url:
            return final_url
        else:
            logger.error(f"❌ DriveBot Error: API call succeeded but response did not contain a URL. Response: {json_data}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ DriveBot Error visiting or processing this page: {e}")
        return None
    except ValueError:
        logger.error("❌ DriveBot Error: Failed to decode JSON from API response.")
        return None

async def resolve_drivebot_link(start_url: str) -> str or None:
    """Async wrapper to resolve a drivebots link."""
    links_to_check = await asyncio.to_thread(get_intermediate_links, start_url)
    final_download_link = None
    if links_to_check:
        links_to_check.reverse()
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'
        })
        for link in links_to_check:
            final_download_link = await asyncio.to_thread(get_final_link_from_page, link, session)
            if final_download_link:
                break
    return final_download_link

# --- ENVATO RESOLVER FUNCTION ---
async def resolve_envato_link(envato_direct_url: str) -> dict or str or None:
    """
    Asynchronously bypasses the Envato link protection funnel.
    Returns a URL string on success, a dict on limit error, or None on failure.
    """
    initial_url = f"https://envato.isyyy.com/go?url={envato_direct_url}"
    funnel_entry_url = "https://applelatest.com/en/"
    final_url = None
    browser = None
    
    logger.info(f"Starting Envato bypass for: {envato_direct_url}")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
            page = await context.new_page()

            await async_retry_step(lambda: page.goto(initial_url, timeout=15000), desc="Goto initial URL")
            
            # --- NEW: Check for download limit message ---
            page_content = await page.content()
            if "download limit" in page_content.lower():
                logger.warning("Envato download limit reached.")
                match = re.search(r"come back in\s+(\d+)\s+hours?\s+and\s+(\d+)\s+minutes?", page_content, re.IGNORECASE)
                limit_message = "Download limit reached. Please try again later."
                if match:
                    hours = int(match.group(1))
                    minutes = int(match.group(2))
                    retry_time = datetime.now() + timedelta(hours=hours, minutes=minutes)
                    limit_message = f"Download limit reached. Please try again in {hours}h {minutes}m (around {retry_time.strftime('%I:%M %p')})."
                
                await browser.close()
                return {'status': 'limit_reached', 'message': limit_message}
            # --- END NEW ---

            if "privacy-error" in page.url or "cert-error" in page.url:
                logger.info("Privacy error page detected, attempting to proceed.")
                await async_retry_step(lambda: page.click("#proceed-link"), desc="Bypass Chrome security warning")

            await async_retry_step(lambda: page.goto(funnel_entry_url, timeout=15000), desc="Goto funnel entry URL")

            js_script = """
                var btn = document.getElementById('tp98');
                var link = document.getElementById('link');
                if (link) link.style.display = 'none';
                if (btn) { btn.style.display = 'block'; btn.disabled = false; btn.style.opacity = '1'; }
                var t = document.getElementById('tp-time');
                if (t) t.innerHTML = '0';
            """
            await async_retry_step(lambda: page.evaluate(js_script), desc="Run skip timer JS")
            
            await async_retry_step(lambda: page.click("#tp98"), desc="Click Stage 2 button")
            await async_retry_step(lambda: page.wait_for_selector("#btn6", timeout=10000), desc="Wait for final button")
            await async_retry_step(lambda: page.click("#btn6"), desc="Click final button")

            await page.wait_for_load_state('networkidle', timeout=15000)

            final_url = page.url
            logger.info(f"Successfully bypassed Envato, final URL: {final_url}")
            
    except Exception as e:
        logger.error(f"A critical error occurred during Envato bypass: {e}")
        return None
    finally:
        if browser and browser.is_connected():
            await browser.close()
            
    return final_url

# --- TELEGRAM HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a movie/series name, or a supported direct link.")

async def set_domain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    AUTHORIZED_USER = os.environ.get("AUTHORIZED_USER")
    user = update.effective_user

    if user.username != AUTHORIZED_USER:
        await update.message.reply_text(f"❌ Sorry, you can't use this command.")
        return

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
        keyboard = [[InlineKeyboardButton("📄 Open Original Link Manually", url=url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.edit_text("❌ Sorry, I could not extract the final link. You can try opening it manually.", reply_markup=reply_markup)

async def handle_envato_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    msg = await update.message.reply_text("Processing your Envato link... This can take up to a minute. ⏳")
    result = await resolve_envato_link(url)
    
    if isinstance(result, str):
        keyboard = [[InlineKeyboardButton("✅ Open Final Envato Link", url=result)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.edit_text("Your Envato link is ready!", reply_markup=reply_markup)
    elif isinstance(result, dict) and result.get('status') == 'limit_reached':
        await msg.edit_text(f"❌ {result.get('message')}")
    else:
        await msg.edit_text("❌ Sorry, I failed to bypass the Envato link. The site structure may have changed or another error occurred.")


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text
    user = update.effective_user
    user_identifier = f"@{user.username}" if user.username else user.full_name

    # Send an initial "verifying" message that will be edited later
    msg = await update.message.reply_text("Searching... ⏳")

    # --- START: MEMBERSHIP CHECK & LOGGING ---
    if LOG_BOT_TOKEN:
        log_bot = Bot(token=LOG_BOT_TOKEN)

        # The section for Force Join Check has been removed.
        
        # 2. Log the search query
        if LOG_GROUP_CHAT_ID:
            try:
                log_message = (
                    f"👤 <b>New Search Alert</b> 🎬\n\n"
                    f"<b>User:</b> {user_identifier}\n"
                    f"<b>ID:</b> <code>{user.id}</code>\n"
                    f"<b>Query:</b> {title}"
                )
                await log_bot.send_message(
                    chat_id=LOG_GROUP_CHAT_ID,
                    text=log_message,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Could not send search notification to group {LOG_GROUP_CHAT_ID}: {e}")
    # --- END: MEMBERSHIP CHECK & LOGGING ---

    await msg.edit_text(f"Searching for '{title}'...")
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

    is_back_navigation = "Select a download source" in query.message.text if query.message else False
    loading_text = "⏳ Loading qualities..." if is_back_navigation else "⏳ Loading details, please wait..."
    await query.edit_message_text(text=loading_text)

    selected_item = search_results[index]
    page_url = selected_item['link']
    item_title_display = selected_item['title']

    page_details = get_page_details(page_url)

    if not page_details or page_details == "error":
        await query.edit_message_text(f"Sorry, couldn't find any details for '{item_title_display}'.")
        return

    download_links_data = page_details.get('download_links')
    if not download_links_data:
        await query.edit_message_text(f"Sorry, couldn't find any download links for '{item_title_display}'.")
        return

    if not is_back_navigation:
        await query.delete_message()
        poster_id, screenshot_ids = None, []
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
        links_key = f"links_{query.id}"
        if not RESULTS_CACHE.get(links_key):
             RESULTS_CACHE[links_key] = { 'links': download_links_data, 'back_data_movie': query.data, 'message_ids_screenshots': [] }

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
        await query.edit_message_text(text=message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
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
        button_text = link_info['text']
        if "google drive" in button_text.lower():
            button_text = f"{button_text} (Recommended)"

        callback_data = f"process:{links_key}:{quality_index}:{i}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    back_data_to_movie = cached_data.get('back_data_movie')
    if back_data_to_movie:
        keyboard.append([InlineKeyboardButton("« Back to Qualities", callback_data=back_data_to_movie)])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"✅ *Select a download source for {selected_quality_group['quality']}*", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def process_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data_key = query.data
    
    cached_links = RESULTS_CACHE.get(callback_data_key)
    if cached_links:
        scraped_data = cached_links
        _p, links_key, quality_index_str, _l = callback_data_key.split(':', 3)
    else:
        _p, links_key, quality_index_str, link_index_str = callback_data_key.split(':', 3)
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

        if not final_url:
            logger.warning(f"extract_final_link failed for {link_url}. Providing manual link.")
            keyboard = [[InlineKeyboardButton("📄 Open Page Manually", url=link_url)]]
            back_to_source_callback = f"quality:{links_key}:{quality_index_str}"
            keyboard.append([InlineKeyboardButton("« Back to Sources", callback_data=back_to_source_callback)])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Failed to resolve the link automatically. You can try opening the original link manually.",
                reply_markup=reply_markup
            )
            return

        if "fastdlserver" in final_url:
            await query.edit_message_text("Found fastdlserver link, checking for GDFlix redirect... 🕵️‍♂️")
            redirected_url = await asyncio.to_thread(get_final_redirected_url, final_url)
            
            if redirected_url and "gdflix.me" in redirected_url:
                await query.edit_message_text("Redirected to GDFlix. Rendering page to find links... ⏳")
                gdflix_links = await resolve_gdflix_link(redirected_url)
                
                if gdflix_links:
                    keyboard = []
                    for link_info in gdflix_links:
                        keyboard.append([InlineKeyboardButton(f"🔗 {link_info['text']}", url=link_info['url'])])
                    
                    keyboard.append([InlineKeyboardButton("📄 Open Page Manually", url=redirected_url)])
                    
                    back_to_source_callback = f"quality:{links_key}:{quality_index_str}"
                    keyboard.append([InlineKeyboardButton("« Back to Sources", callback_data=back_to_source_callback)])
                    
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text("✅ Found GDFlix Links! Select one:", reply_markup=reply_markup)
                    return
                else:
                    await query.edit_message_text("❌ Failed to resolve GDFlix link. Falling back to scraping...")
            
            await query.edit_message_text("Scraping fastdlserver page for direct links... 🕵️‍♂️")
            scraped_data = await asyncio.to_thread(scrape_fastdl_links, final_url)
            if isinstance(scraped_data, list) and scraped_data:
                RESULTS_CACHE[callback_data_key] = scraped_data
            else:
                logger.warning(f"Failed to scrape fastdlserver page. Providing manual link: {final_url}")
                keyboard = [[InlineKeyboardButton("📄 Open Page Manually", url=final_url)]]
                back_to_sources_callback = f"quality:{links_key}:{quality_index_str}"
                keyboard.append([InlineKeyboardButton("« Back to Sources", callback_data=back_to_sources_callback)])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    "❌ Could not automatically find links on this page. You can try opening it manually.",
                    reply_markup=reply_markup
                )
                return

        elif "fxlinks" in final_url:
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
                back_to_source_callback = f"quality:{links_key}:{quality_index}"
                error_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📄 Open Episode Page Manually", url=final_url)],
                    [InlineKeyboardButton("« Back to Sources", callback_data=back_to_source_callback)]
                ])
                await query.edit_message_text("❌ Could not scrape any episode links.", reply_markup=error_markup)
            return
        
        else:
            keyboard = [[InlineKeyboardButton("✅ Open Download Link in Browser", url=final_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Your direct download link is ready!", reply_markup=reply_markup)
            return

    if isinstance(scraped_data, list) and scraped_data:
        keyboard = []
        for i, link_info in enumerate(scraped_data):
            if 'drivebots' in link_info['url']:
                drivebot_key = f"db_{query.id}_{i}"
                back_callback = callback_data_key
                RESULTS_CACHE[drivebot_key] = {'url': link_info['url'], 'back': back_callback}
                callback_data = f"drivebot:{drivebot_key}"
                keyboard.append([InlineKeyboardButton(f"🤖 {link_info['text']}", callback_data=callback_data)])
            else:
                keyboard.append([InlineKeyboardButton(link_info['text'], url=link_info['url'])])
        
        back_to_sources_callback = f"quality:{links_key}:{quality_index_str}"
        keyboard.append([InlineKeyboardButton("« Back", callback_data=back_to_sources_callback)])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("✅ Your direct download links are ready!", reply_markup=reply_markup)
    else:
        logger.warning(f"Scraped data was not a list or was empty. Fallback required. Data: {scraped_data}")
        keyboard = [
            [InlineKeyboardButton("« Back", callback_data=f"quality:{links_key}:{quality_index_str}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("❌ Could not scrape valid links. Please go back and try another source.", reply_markup=reply_markup)


async def process_fx_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data_key = query.data

    final_url_from_cache = None
    try:
        _p, fxlinks_key, link_index_str = callback_data_key.split(':', 2)
        links_list = RESULTS_CACHE.get(fxlinks_key)
        if links_list:
            link_index = int(link_index_str)
            if link_index < len(links_list):
                 final_url_from_cache = links_list[link_index].get('url')
    except (ValueError, IndexError):
        pass

    cached_links = RESULTS_CACHE.get(callback_data_key)
    if cached_links:
        scraped_data = cached_links
        _p, fxlinks_key, _l = callback_data_key.split(':', 2)
    else:
        try:
            _p, fxlinks_key, link_index_str = callback_data_key.split(':', 2)
            link_index = int(link_index_str)
        except (ValueError, IndexError) as e:
            logger.error(f"Invalid callback data format for process_fx: {callback_data_key} - Error: {e}")
            await query.edit_message_text("Error: Invalid link data. Please start a new search.")
            return

        links_list = RESULTS_CACHE.get(fxlinks_key)
        if not links_list or link_index >= len(links_list):
            await query.edit_message_text("Error: These links have expired or are invalid. Please start a new search.")
            return

        selected_link_info = links_list[link_index]
        final_url = selected_link_info['url']
        await query.edit_message_text(f"Processing '{selected_link_info['text']}'...\nChecking for GDFlix redirect... 🕵️‍♂️")
        
        redirected_url = await asyncio.to_thread(get_final_redirected_url, final_url)
        if redirected_url and "gdflix.me" in redirected_url:
            await query.edit_message_text("Redirected to GDFlix. Rendering page to find links... ⏳")
            gdflix_links = await resolve_gdflix_link(redirected_url)
            if gdflix_links:
                keyboard = []
                for link_info in gdflix_links:
                    keyboard.append([InlineKeyboardButton(f"🔗 {link_info['text']}", url=link_info['url'])])
                
                keyboard.append([InlineKeyboardButton("📄 Open Page Manually", url=redirected_url)])
                keyboard.append([InlineKeyboardButton("« Back to Episode List", callback_data=f"back_episodes:{fxlinks_key}")])

                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("✅ Found GDFlix Episode Links! Select one:", reply_markup=reply_markup)
                return
            else:
                await query.edit_message_text("❌ Failed to resolve GDFlix link. Falling back to scraping...")

        await query.edit_message_text(f"Scraping for direct links... 🕵️‍♂️")
        scraped_data = await asyncio.to_thread(scrape_fastdl_links, final_url)
        if isinstance(scraped_data, list) and scraped_data:
            RESULTS_CACHE[callback_data_key] = scraped_data
        else:
            logger.warning(f"Failed to scrape from FX link's final URL. Providing manual link: {final_url}")
            keyboard = [
                [InlineKeyboardButton("📄 Open Page Manually", url=final_url)],
                [InlineKeyboardButton("« Back to Episode List", callback_data=f"back_episodes:{fxlinks_key}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Could not automatically find links on this page. You can try opening it manually.",
                reply_markup=reply_markup
            )
            return

    if isinstance(scraped_data, list) and scraped_data:
        keyboard = []
        for i, link_info in enumerate(scraped_data):
            if 'drivebots' in link_info['url']:
                drivebot_key = f"db_{query.id}_{i}"
                back_callback = callback_data_key
                RESULTS_CACHE[drivebot_key] = {'url': link_info['url'], 'back': back_callback}
                callback_data = f"drivebot:{drivebot_key}"
                keyboard.append([InlineKeyboardButton(f"🤖 {link_info['text']}", callback_data=callback_data)])
            else:
                keyboard.append([InlineKeyboardButton(link_info['text'], url=link_info['url'])])
        
        keyboard.append([InlineKeyboardButton("« Back to Episode List", callback_data=f"back_episodes:{fxlinks_key}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"✅ Your direct download links for the selected episode:", reply_markup=reply_markup)
    else:
        keyboard = [
            [InlineKeyboardButton("📄 Open Page Manually", url=final_url_from_cache or "#")],
            [InlineKeyboardButton("« Back to Episode List", callback_data=f"back_episodes:{fxlinks_key}")]
        ]
        error_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("❌ Could not scrape valid links from the server page.", reply_markup=error_markup)


async def drivebot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, drivebot_key = query.data.split(':', 1)
        cached_info = RESULTS_CACHE.get(drivebot_key)
        if not cached_info:
            await query.edit_message_text("Error: This link has expired. Please try the search again.")
            return
        start_url = cached_info['url']
        back_callback = cached_info['back']
    except (ValueError, IndexError, TypeError):
        await query.edit_message_text("Error: Invalid drivebot link data. Please try again.")
        return

    await query.edit_message_text(f"🤖 Resolving DriveBot link...")

    final_link = await resolve_drivebot_link(start_url)
    if not final_link:
        logger.warning("First DriveBot attempt failed. Retrying in 1 second...")
        await asyncio.sleep(1)
        final_link = await resolve_drivebot_link(start_url)

    if final_link:
        keyboard = [
            [InlineKeyboardButton("🚀 Open Final Download Link", url=final_link)],
            [InlineKeyboardButton("« Back", callback_data=back_callback)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("✅ Success! Your DriveBot link is resolved:", reply_markup=reply_markup)
    else:
        keyboard = [
            [InlineKeyboardButton("📄 Manual Fallback Link", url=start_url)],
            [InlineKeyboardButton("« Back", callback_data=back_callback)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("❌ Failed to automatically resolve the DriveBot link. You can try opening it manually.", reply_markup=reply_markup)


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
    await query.edit_message_text("✅ Please select an episode to proceed:", reply_markup=reply_markup)


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

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setdomain", set_domain_command))

    # Message Handlers - Order is important! Specific link handlers go first.
    application.add_handler(MessageHandler(filters.Regex(r'https?://elements\.envato\.com/'), handle_envato_link))
    application.add_handler(MessageHandler(filters.Regex(r'https?://[a-zA-Z0-9.-]+\/\?id=[\w/+=.-]+'), handle_direct_link))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    
    # Callback Query Handlers
    application.add_handler(CallbackQueryHandler(movie_selection_handler, pattern="^movie:.*"))
    application.add_handler(CallbackQueryHandler(quality_selection_handler, pattern="^quality:.*"))
    application.add_handler(CallbackQueryHandler(process_link_handler, pattern="^process:.*"))
    application.add_handler(CallbackQueryHandler(process_fx_link_handler, pattern="^process_fx:.*"))
    application.add_handler(CallbackQueryHandler(drivebot_handler, pattern="^drivebot:.*"))
    application.add_handler(CallbackQueryHandler(back_to_episodes_handler, pattern="^back_episodes:.*"))
    application.add_handler(CallbackQueryHandler(back_to_search_handler, pattern="^back_search:.*"))

    # Setup and run Bot and Web Server
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
