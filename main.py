# -*- coding: utf-8 -*-
import asyncio
import hashlib
import html
import io
import json # For storing lists/dicts in Redis
import math
import os
import random
import tempfile
import traceback
import urllib.parse
import time
from typing import List, Optional, Literal, Dict, Any
from contextlib import asynccontextmanager

# --- Redis Dependency ---
import redis.asyncio as redis # Import the async redis client

import aiofiles
import httpx # Needed for direct API calls to CapSolver
from bs4 import BeautifulSoup
from cachetools import TTLCache # For PDF->HTML cache, not related to session/links
from fastapi import FastAPI, Body, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from markitdown import MarkItDown # For PDF conversion utility
# Import Playwright
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page, BrowserContext

from pydantic import BaseModel, Field

# --- Configuration ---
# Redis settings
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
COOKIES_REDIS_KEY = "dergipark_scraper:session:last_cookies" # Key for storing session cookies
COOKIES_TTL = 1800 # TTL for cookies in Redis (30 minutes) - adjust as needed
ARTICLE_LINKS_CACHE_PREFIX = "dergipark_scraper:links:" # Prefix for article link cache keys
ARTICLE_LINKS_TTL = 600 # TTL for article links cache (10 minutes)

# CapSolver settings
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "CAP-1E1D6F5F97285F22927DFC04FA04116A4A5FCC9211E28F36195D8372CC7D6739") # Use env var or replace default
CAPSOLVER_CREATE_TASK_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"

# Playwright settings
USER_AGENTS = [ # Add more diverse and recent user agents
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]
HEADLESS_MODE = os.getenv("HEADLESS_MODE", "true").lower() == "true"

# Other settings
PDF_CACHE_TTL = int(os.getenv("PDF_CACHE_TTL", 86400)) # Cache TTL for PDF->HTML results

# --- Application State ---
app_state = {} # Holds Redis client during application lifespan

# --- FastAPI Lifespan for Redis Connection ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles Redis connection pool creation and closing."""
    print(f"Connecting to Redis at {REDIS_URL}...")
    try:
        redis_client = redis.Redis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True
        )
        await redis_client.ping()
        print("Successfully connected to Redis.")
        app_state["redis_client"] = redis_client
    except Exception as e:
        print(f"FATAL: Could not connect to Redis: {e}")
        app_state["redis_client"] = None
    yield # Application runs
    print("Shutting down...")
    redis_client = app_state.get("redis_client")
    if redis_client:
        print("Closing Redis connection...")
        await redis_client.aclose()
        print("Redis connection closed.")

# --- FastAPI App Initialization ---
app = FastAPI(
    title="DergiPark Scraper API (Cookie Injection Strategy)",
    version="1.10.2", # Version bumped for syntax fix
    description="API to search DergiPark articles with pagination. Attempts to maintain sessions using cookie injection.",
    lifespan=lifespan
)

# --- API Key Check ---
if CAPSOLVER_API_KEY == "YOUR_CAPSOLVER_API_KEY_HERE" or not CAPSOLVER_API_KEY:
    print("\n" + "="*60 + "\nUYARI: CAPSOLVER_API_KEY ortam değişkeni ayarlanmamış veya varsayılan değerde!\nCAPTCHA çözme denemeleri başarısız olacaktır.\n" + "="*60 + "\n")

# --- HTTP Client for PDF Downloads ---
pdf_http_client = httpx.AsyncClient( timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True )

# --- Cache for PDF-to-HTML ---
pdf_cache = TTLCache(maxsize=500, ttl=PDF_CACHE_TTL)

# --- Pydantic Models ---
class SearchParams(BaseModel):
    """Model defining search parameters and pagination options."""
    title: Optional[str] = Field(None, description="Filter by article title.")
    running_title: Optional[str] = Field(None, description="Filter by article running title.")
    journal: Optional[str] = Field(None, description="Filter by journal name.")
    issn: Optional[str] = Field(None, description="Filter by journal ISSN.")
    eissn: Optional[str] = Field(None, description="Filter by journal E-ISSN.")
    abstract: Optional[str] = Field(None, description="Filter by article abstract.")
    keywords: Optional[str] = Field(None, description="Filter by article keywords.")
    doi: Optional[str] = Field(None, description="Filter by exact DOI.")
    doi_url: Optional[str] = Field(None, description="Filter by DOI URL.", examples=["https://doi.org/10.xxxx/yyyyy"])
    doi_prefix: Optional[str] = Field(None, description="Filter by DOI prefix (e.g., 10.xxxx).")
    author: Optional[str] = Field(None, description="Filter by author name.")
    orcid: Optional[str] = Field(None, description="Filter by author ORCID.")
    institution: Optional[str] = Field(None, description="Filter by author institution.")
    translator: Optional[str] = Field(None, description="Filter by translator name.")
    pubyear: Optional[str] = Field(None, description="Filter by publication year (e.g., '2023' or '2020-2023').")
    citation: Optional[str] = Field(None, description="Filter by citation text.")
    dergipark_page: int = Field(default=1, ge=1, description="DergiPark search results page number to query.")
    api_page: int = Field(default=1, ge=1, description="Which page of API results to return for the given DergiPark page.")
    page_size: int = Field(default=5, ge=1, le=20, description="Number of articles per API page response (max 20).")
    sort_by: Optional[Literal["newest", "oldest"]] = Field(None, description="Sort order for DergiPark results.")
    article_type: Optional[Literal[ # Add descriptions if known
        "54", "56", "58", "55", "60", "65", "57", "1", "5",
        "62", "73", "2", "10", "59", "66", "72"
    ]] = Field(None, description="Filter by DergiPark article type code.")
    index_filter: Optional[Literal["tr_dizin_icerenler", "bos_olmayanlar", "hepsi"]] = Field(
        default="hepsi", description="Filter results based on indexing status."
    )

# --- Utility Functions ---
def truncate_text(text: str, word_limit: int) -> str:
    """Truncates text to a specified word limit."""
    if not text: return ""; words = text.split(); return ' '.join(words[:word_limit]) + '...' if len(words) > word_limit else text

def generate_links_cache_key(params: SearchParams) -> str:
    """Generates a stable Redis cache key for article links based on search parameters."""
    key_data = params.model_dump(exclude={'api_page', 'page_size'}, exclude_unset=True, mode='python'); sorted_items = sorted(key_data.items()); key_string = json.dumps(sorted_items); hash_object = hashlib.sha256(key_string.encode('utf-8')); return f"{ARTICLE_LINKS_CACHE_PREFIX}{hash_object.hexdigest()}:dp_page_{params.dergipark_page}"

# --- Playwright Functions ---
async def get_playwright_page(p: async_playwright) -> tuple[Any, BrowserContext, Page]:
    """Initializes Playwright browser, context, and a new page."""
    try:
        print(f"Launching browser (Headless: {HEADLESS_MODE})...")
        browser = await p.chromium.launch(headless=HEADLESS_MODE, args=['--disable-dev-shm-usage', '--no-sandbox'])
        context = await browser.new_context(user_agent=random.choice(USER_AGENTS), locale='tr-TR', viewport={'width': 1920, 'height': 1080}, ignore_https_errors=True)
        page = await context.new_page()
        print("Browser page created successfully.")
        return browser, context, page
    except Exception as e:
        print(f"FATAL: Playwright initialization error: {e}")
        raise HTTPException(status_code=503, detail=f"Browser service unavailable: {e}")

async def close_playwright(browser, context, page):
    """Safely closes Playwright resources."""
    print("--- Initiating Playwright Cleanup ---")
    closed_components = []
    try:
        if page and not page.is_closed(): await page.close(); closed_components.append("Page")
        else: closed_components.append("Page (closed/none)")
        if context: await context.close(); closed_components.append("Context")
        else: closed_components.append("Context (none)")
        if browser and browser.is_connected(): await browser.close(); closed_components.append("Browser")
        else: closed_components.append("Browser (closed/none)")
        print(f"Playwright close attempted for: {', '.join(closed_components)}")
    except Exception as e:
        if "Target page, context or browser has been closed" not in str(e): print(f"Warning: Error closing Playwright resource: {e}")
    print("--- Playwright Cleanup Finished ---")


async def get_article_details_pw(page: Page, article_url: str, referer_url: Optional[str] = None) -> dict:
    """Fetches metadata and index info for a single article URL."""
    print(f"Fetching details: {article_url}")
    details = {'error': None}; pdf_url = None; indices = ''; max_retries = 1; attempt = 0
    while attempt <= max_retries:
        attempt += 1
        try:
            await page.set_extra_http_headers({'Referer': referer_url if referer_url else page.url})
            await page.goto(article_url, wait_until='domcontentloaded', timeout=30000)
            html_content = await page.content()
            if any(s in html_content.lower() for s in ["cloudflare", "captcha", "blocked", "erişim engellendi"]): details['error'] = "Blocked on details page"; break
            soup = BeautifulSoup(html_content, 'html5lib'); meta_tags = soup.find_all('meta')
            if not meta_tags:
                if attempt <= max_retries: await asyncio.sleep(1.5 * attempt); continue
                else: details['error'] = "No meta tags found"; break
            raw_details = {tag.get('name'): tag.get('content','').strip() for tag in meta_tags if tag.get('name')}
            journal_url_base = raw_details.get('DC.Source.URI')
            pdf_url = raw_details.get('citation_pdf_url')
            details = {k: raw_details.get(k) for k in ['citation_title', 'DC.Creator.PersonalName', 'citation_journal_title', 'citation_publication_date', 'citation_keywords', 'citation_doi', 'citation_issn']}
            details['citation_abstract'] = truncate_text(raw_details.get('citation_abstract', ''), 100)
            details['citation_author'] = details.pop('DC.Creator.PersonalName') # Rename key

            if journal_url_base:
                 try:
                     index_url = f"{journal_url_base.rstrip('/')}/indexes"
                     await page.goto(index_url, wait_until='domcontentloaded', timeout=12000)
                     index_soup = BeautifulSoup(await page.content(), 'html5lib')
                     index_elements = index_soup.select('table.journal-index-listing h5.j-index-listing-index-title')
                     indices_list = [idx.text.strip() for idx in index_elements if idx.text]
                     indices = ', '.join(indices_list)
                     print(f"Found indexes: {indices if indices else 'None'}")
                 except Exception as e_idx: print(f"Index page error/timeout for {journal_url_base}: {e_idx}")
                 finally:
                    try: # Ensure back on article page
                        if page.url != article_url: await page.goto(article_url, wait_until='domcontentloaded', timeout=10000)
                    except Exception as e_back: print(f"Warning: Failed to navigate back to article page: {e_back}")

            details['error'] = None; break # Success
        except PlaywrightTimeoutError:
            if attempt > max_retries: details['error'] = "Detail page timed out"; break
            else: await asyncio.sleep(2 * attempt)
        except Exception as e:
             if attempt > max_retries: details['error'] = f"Detail fetch error: {e}"; break
             else: await asyncio.sleep(2 * attempt)
    return {'details': details, 'pdf_url': pdf_url, 'indices': indices}


async def _inject_and_submit_captcha(page: Page, token: str, verification_submit_selector: str) -> bool:
    """Helper: Injects token (with events), clicks submit, checks result."""
    injection_target_selector = '#g-recaptcha-response'
    try:
        print(f"Injecting token via JS: {token[:15]}...")
        js_func = """(t)=>{let e=document.getElementById('g-recaptcha-response');if(e){console.log('Injecting token...');e.value=t;e.dispatchEvent(new Event('input',{bubbles:!0}));e.dispatchEvent(new Event('change',{bubbles:!0}));console.log('Injected/dispatched.');return!0}return console.error('#g-recaptcha-response missing!'),!1}"""
        if not await page.evaluate(js_func, token): return False
        print("Token injection script succeeded."); await asyncio.sleep(random.uniform(0.5, 1.2))
        submit_button = page.locator(verification_submit_selector)
        print(f"Clicking submit button ('{verification_submit_selector}')...")
        try:
            await submit_button.wait_for(state="visible", timeout=7000)
            async with page.expect_navigation(wait_until='load', timeout=35000): await submit_button.click()
            print("Submit clicked, navigation finished ('load').")
            current_url = page.url; print(f"URL after submit: {current_url}")
            return "/search-verification" not in current_url
        except Exception as e_sub: print(f"Submit/Nav Error: {e_sub}"); return False
    except Exception as e_js: print(f"JS Injection Error: {e_js}"); return False


async def solve_recaptcha_v2_capsolver_direct_async(page: Page, redis_client: Optional[redis.Redis]) -> bool:
    """
    Solves reCAPTCHA v2 when encountered by fetching a *new* token from CapSolver.
    Waits for necessary elements before interacting.
    """
    # This function no longer uses redis_client parameter, but kept for signature compatibility
    print("CAPTCHA detected. Fetching NEW token from CapSolver...")
    site_key_element_selector = '.g-recaptcha[data-sitekey]'
    injection_target_selector = '#g-recaptcha-response'
    verification_submit_selector = 'form[name="search_verification"] button[type="submit"]:has-text("Devam Et")'

    if not CAPSOLVER_API_KEY or CAPSOLVER_API_KEY == "YOUR_CAPSOLVER_API_KEY_HERE":
        print("Error: CAPSOLVER_API_KEY is not configured."); return False

    try:
        # --- Fetch Site Key ---
        print(f"Waiting for sitekey element ('{site_key_element_selector}')...")
        try:
            site_key_element = await page.wait_for_selector(site_key_element_selector, state="attached", timeout=15000)
            site_key = await site_key_element.get_attribute('data-sitekey')
            if not site_key: raise ValueError("Sitekey attribute is empty.")
            print("Sitekey element found.")
        except (PlaywrightTimeoutError, ValueError) as e:
             print(f"Error finding/getting sitekey: {e}"); return False
        page_url = page.url; print(f"Sitekey: {site_key}, URL: {page_url}")

        # --- Call CapSolver API for NEW token ---
        task_payload = { "clientKey": CAPSOLVER_API_KEY, "task": { "type": "ReCaptchaV2TaskProxyless", "websiteURL": page_url, "websiteKey": site_key } }
        captcha_token = None
        async with httpx.AsyncClient(timeout=20.0) as client:
            print("Sending task to CapSolver..."); task_id = None
            try:
                create_response = await client.post(CAPSOLVER_CREATE_TASK_URL, json=task_payload); create_response.raise_for_status()
                create_result = create_response.json()
                if create_result.get("errorId", 0) != 0: raise ValueError(f"API Error Create: {create_result}")
                task_id = create_result.get("taskId");
                if not task_id: raise ValueError("No Task ID received.")
                print(f"CapSolver Task created: {task_id}")
            except Exception as e: print(f"Error Creating CapSolver Task: {e}"); return False

            # --- Poll for result ---
            start_time = time.time(); timeout_seconds = 180
            while time.time() - start_time < timeout_seconds:
                await asyncio.sleep(6); print(f"Polling CapSolver (ID: {task_id})...")
                result_payload = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
                try:
                    get_response = await client.post(CAPSOLVER_GET_RESULT_URL, json=result_payload, timeout=15); get_response.raise_for_status()
                    get_result = get_response.json()
                    if get_result.get("errorId", 0) != 0: raise ValueError(f"API Error Poll: {get_result}")
                    status = get_result.get("status"); print(f"Task status: {status}")
                    if status == "ready":
                        solution = get_result.get("solution"); captcha_token = solution.get("gRecaptchaResponse") if solution else None
                        if captcha_token: print("CapSolver solution received!"); break
                        else: raise ValueError("Task ready but no token in solution.")
                    elif status in ["failed", "error"]: raise ValueError(f"CapSolver task failed/errored: {get_result.get('errorDescription')}")
                except Exception as e: print(f"Error Polling CapSolver Task: {e}"); await asyncio.sleep(5); # Continue polling

            if not captcha_token: print("Polling timeout or error getting token."); return False

        # --- Submit with the new token ---
        print("New token received. Attempting submission...")
        try: # Wait for injection target before submitting
            print(f"Waiting for injection target ('{injection_target_selector}')...")
            await page.wait_for_selector(injection_target_selector, state="attached", timeout=10000)
            print("Injection target found.")
        except PlaywrightTimeoutError:
            print(f"Timeout waiting for injection target ('{injection_target_selector}') before submission."); return False

        submission_successful = await _inject_and_submit_captcha(page, captcha_token, verification_submit_selector)

        # Return success status (no need to interact with Redis for token here)
        return submission_successful

    # --- General Error Handling ---
    except Exception as e:
        print(f"Unexpected error during CAPTCHA solving: {e}")
        # print(traceback.format_exc()) # Uncomment for full traceback if needed
        return False


async def get_article_links_with_cache(
    page: Page,
    search_url: str,
    cache_key: str,
    redis_client: Optional[redis.Redis]
) -> List[Dict[str, str]]:
    """
    Gets article links. Uses Redis cache. Fetches if cache miss.
    Handles CAPTCHA (always fetches new token). Saves cookies if CAPTCHA solved.
    """
    # 1. Check Cache
    if redis_client:
        try:
            cached_data = await redis_client.get(cache_key)
            if cached_data: print(f"Cache HIT: Links {cache_key}"); return json.loads(cached_data)
        except Exception as e: print(f"Warning: Redis GET links error {cache_key}: {e}")

    print(f"Cache MISS: Links {cache_key}. Fetching from DergiPark...")
    article_links = []; article_card_selector = 'div.card.article-card.dp-card-outline'; captcha_was_solved = False

    try:
        # 2. Navigate
        print(f"Navigating to: {search_url}"); await page.goto(search_url, wait_until='load', timeout=40000); print(f"Nav complete. URL: {page.url}")

        # 3. Handle CAPTCHA
        if "/search-verification" in page.url:
            print("CAPTCHA page detected."); captcha_passed = await solve_recaptcha_v2_capsolver_direct_async(page, redis_client)
            if not captcha_passed: raise HTTPException(429, "CAPTCHA solving failed.")
            print("CAPTCHA passed. Checking results page..."); captcha_was_solved = True
            try: await page.wait_for_selector(article_card_selector, state="visible", timeout=15000); print("Results page elements confirmed.")
            except PlaywrightTimeoutError: raise HTTPException(500, f"Failed to find results elements after CAPTCHA. URL: {page.url}")
        else: print("No CAPTCHA detected.")

        # 4. Extract Links
        try:
            await page.wait_for_selector(article_card_selector, state="attached", timeout=10000)
            article_cards = await page.query_selector_all(article_card_selector); print(f"{len(article_cards)} article cards found.")
        except PlaywrightTimeoutError:
            if "sonuç bulunamadı" in (await page.content()).lower(): print("No results msg found."); article_links = []
            else: raise HTTPException(500, "Link extraction failed (timeout finding cards).")

        if article_cards:
            base_page_url = page.url
            for card in article_cards:
                a_tag = await card.query_selector('h5.card-title > a[href]')
                if a_tag: url = await a_tag.get_attribute('href'); title = await a_tag.text_content(); article_links.append({'url': urllib.parse.urljoin(base_page_url, url.strip()), 'title': title.strip() or "N/A"})

        # 5. Cache Links (even if empty)
        if redis_client:
            try: await redis_client.set(cache_key, json.dumps(article_links), ex=ARTICLE_LINKS_TTL); print(f"Stored {len(article_links)} links in link cache: {cache_key}")
            except Exception as e: print(f"Warning: Redis SET links error {cache_key}: {e}")

        # 6. Save Cookies if CAPTCHA was solved this time
        if captcha_was_solved and redis_client:
            try:
                print("Saving cookies post-CAPTCHA..."); browser_context = page.context
                current_cookies = await browser_context.cookies(urls=[page.url]) # Cookies for current context
                if current_cookies:
                    # Filter potentially problematic cookies if needed (e.g., based on name or domain)
                    # Ensure expires is int if present for JSON compatibility with add_cookies
                    for c in current_cookies:
                        if 'expires' in c and isinstance(c['expires'], float): c['expires'] = int(c['expires'])
                    await redis_client.set(COOKIES_REDIS_KEY, json.dumps(current_cookies), ex=COOKIES_TTL)
                    print(f"Saved {len(current_cookies)} cookies to '{COOKIES_REDIS_KEY}' (TTL: {COOKIES_TTL}s).")
                else: print("No cookies found to save.")
            except Exception as e: print(f"Warning: Failed to save cookies: {e}")

        return article_links

    except Exception as e: # Catch-all
        if isinstance(e, HTTPException): raise e
        print(f"Error in get_article_links_with_cache: {e}\n{traceback.format_exc()}");
        raise HTTPException(500, f"Link fetching failed: {e}")


# --- FastAPI Endpoints ---

@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """API health and Redis connection check."""
    redis_status = "disconnected"; redis_client = app_state.get("redis_client")
    if redis_client:
        try: await redis_client.ping(); redis_status = "ok"
        except: redis_status = "error"
    return {"status": "ok", "redis_status": redis_status}

@app.get("/gizlilik", response_class=HTMLResponse)
async def get_gizlilik():
    """Serves the privacy policy HTML file."""
    file_path = os.path.join("gizlilik", "index.html")
    if not os.path.exists(file_path):
         print(f"Error: Gizlilik file not found at {os.path.abspath(file_path)}")
         raise HTTPException(status_code=404, detail="Privacy policy file not found.")
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()
        # Return *after* the file is closed by the 'with' block
        return HTMLResponse(content=content, status_code=200)
    except Exception as e:
         print(f"Error reading gizlilik file '{file_path}': {e}")
         raise HTTPException(status_code=500, detail=f"Error reading privacy policy file: {e}")


@app.post("/api/search", response_class=JSONResponse)
async def search_articles(request: Request, search_params: SearchParams = Body(...)):
    """
    Search DergiPark articles with pagination. Attempts cookie injection for session persistence.
    """
    redis_client = app_state.get("redis_client")
    if not redis_client: print("Warning: Redis client not available.")

    # --- Construct DergiPark Search URL ---
    base_url = "https://dergipark.org.tr/tr/search"; query_params = {}
    search_q = " ".join(f"{f}:{v}" for f, v in search_params.model_dump(exclude={'dergipark_page', 'api_page', 'page_size', 'sort_by', 'article_type', 'index_filter'}, exclude_unset=True).items())
    if search_q: query_params['q'] = search_q
    query_params['section'] = 'articles'
    if search_params.dergipark_page > 1: query_params['page'] = search_params.dergipark_page
    if search_params.article_type: query_params['aggs[articleType.id][0]'] = search_params.article_type
    if search_params.sort_by: query_params['sortBy'] = search_params.sort_by
    target_search_url = f"{base_url}?{urllib.parse.urlencode(query_params, quote_via=urllib.parse.quote)}"
    print(f"Target DP URL: {target_search_url} | API Page: {search_params.api_page} | Size: {search_params.page_size}")

    host = str(request.base_url).rstrip('/')
    browser = context = page = playwright_instance = None
    total_items_on_page = 0

    try:
        # --- Start Playwright ---
        playwright_instance = await async_playwright().start()
        browser, context, page = await get_playwright_page(playwright_instance)

        # --- Attempt to Inject Cookies ---
        if redis_client:
            try:
                print(f"Checking Redis for cookies: {COOKIES_REDIS_KEY}")
                cookies_json = await redis_client.get(COOKIES_REDIS_KEY)
                if cookies_json:
                    saved_cookies = json.loads(cookies_json)
                    if saved_cookies:
                        # Basic validation and cleaning for add_cookies
                        required_keys = {'name', 'value', 'domain', 'path'}
                        valid_cookies = []
                        for c in saved_cookies:
                            if required_keys.issubset(c.keys()):
                                if 'expires' in c and isinstance(c['expires'], float): c['expires'] = int(c['expires'])
                                if 'sameSite' in c and c['sameSite'] not in ['Strict', 'Lax', 'None']: del c['sameSite'] # Remove invalid sameSite
                                valid_cookies.append(c)
                        if valid_cookies:
                            print(f"Injecting {len(valid_cookies)} cookies...")
                            await context.add_cookies(valid_cookies); print("Cookies injected.")
                        else: print("No valid cookies found in saved data.")
                else: print("No saved cookies found in Redis.")
            except Exception as e: print(f"Warning: Cookie load/injection error: {e}")

        # --- Get Article Links ---
        links_cache_key = generate_links_cache_key(search_params)
        full_link_list = await get_article_links_with_cache(page, target_search_url, links_cache_key, redis_client)

        # --- Process Results & Pagination ---
        total_items_on_page = len(full_link_list)
        total_api_pages = math.ceil(total_items_on_page / search_params.page_size) if total_items_on_page > 0 else 0
        pagination_info = {"api_page": search_params.api_page, "page_size": search_params.page_size, "total_items_on_dergipark_page": total_items_on_page, "total_api_pages_for_dergipark_page": total_api_pages}
        if total_items_on_page == 0: return JSONResponse({"pagination": pagination_info, "articles": []})

        offset = (search_params.api_page - 1) * search_params.page_size; limit = search_params.page_size
        links_to_process = full_link_list[offset : offset + limit]
        print(f"Links: Total={total_items_on_page}, Slice={len(links_to_process)} (API Page {search_params.api_page}/{total_api_pages})")
        if not links_to_process: return JSONResponse({"pagination": pagination_info, "articles": []}) # Page out of bounds

        # --- Fetch Details for Slice ---
        articles_details = []
        print(f"Fetching details for {len(links_to_process)} articles...")
        referer_url = page.url
        for i, link_info in enumerate(links_to_process):
            print(f"  Processing {offset + i + 1}/{total_items_on_page}: {link_info['url']}")
            details_result = await get_article_details_pw(page, link_info['url'], referer_url=referer_url)
            # Combine details
            pdf_url = details_result.get('pdf_url'); article_details = details_result.get('details', {}); indices_str = details_result.get('indices', '')
            if article_details.get('error'): article_data = {'title': link_info['title'], 'url': link_info['url'], 'error': f"Detail error: {article_details['error']}", 'details': None, 'indices': '', 'readable_pdf': None}
            else: article_data = {'title': link_info['title'], 'url': link_info['url'], 'error': None, 'details': article_details, 'indices': indices_str, 'readable_pdf': f"{host}/api/pdf-to-html?pdf_url={urllib.parse.quote(pdf_url)}" if pdf_url else None}
            # Apply filter
            passes = not ((search_params.index_filter == "tr_dizin_icerenler" and "TR Dizin" not in indices_str) or \
                          (search_params.index_filter == "bos_olmayanlar" and not indices_str))
            if passes: articles_details.append(article_data)
            else: print(f"  Filtered out: {link_info['url']}")
            await asyncio.sleep(random.uniform(0.8, 1.8)) # Delay

        return JSONResponse({"pagination": pagination_info, "articles": articles_details})

    except HTTPException as e: return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    except Exception as e: print(f"General search error: {e}\n{traceback.format_exc()}"); return JSONResponse(status_code=500, content={"detail": f"Unexpected search error: {e}"})
    finally: await close_playwright(browser, context, page); # Playwright cleanup

@app.get("/api/pdf-to-html", response_class=HTMLResponse)
async def pdf_to_html(pdf_url: str):
    """Downloads and converts PDF URL to readable HTML."""
    if not pdf_url or not pdf_url.startswith("http"): raise HTTPException(400,"Invalid/missing PDF URL.")
    cached_html = pdf_cache.get(pdf_url);
    if cached_html: print(f"PDF cache hit: {pdf_url}"); return HTMLResponse(cached_html, 200)
    print(f"PDF cache miss: {pdf_url}"); tmp_name = None
    try:
        async with pdf_http_client as client: response = await client.get(pdf_url, follow_redirects=True); response.raise_for_status(); pdf_content = response.content
        if not pdf_content: raise HTTPException(404,"Downloaded PDF content empty.")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp: tmp.write(pdf_content); tmp_name = tmp.name
        if not tmp_name or not await aiofiles.os.path.exists(tmp_name): raise FileNotFoundError("Temp PDF file failed.")
        print(f"Converting PDF {tmp_name}..."); md = MarkItDown()
        try: markdown_result = await asyncio.to_thread(md.convert, tmp_name); markdown_text = markdown_result.text_content if markdown_result else "Conversion failed."
        except Exception as convert_err: raise HTTPException(500, f"PDF conversion error: {convert_err}")
        escaped_pdf=html.escape(pdf_url); escaped_file=html.escape(pdf_url.split('/')[-1]); escaped_md=html.escape(markdown_text)
        html_content=f'<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8"><title>PDF-{escaped_file}</title><style>body{{font-family:sans-serif;line-height:1.6;padding:20px;max-width:900px;margin:auto;}}pre{{background:#f4f4f4;padding:15px;border-radius:5px;overflow-x:auto;white-space:pre-wrap;word-wrap:break-word;}}a button{{padding:10px 15px;}}</style></head><body><h1>PDF İçeriği</h1><p><a href="{escaped_pdf}" target="_blank"><button>Orijinal PDF</button></a></p><pre>{escaped_md}</pre></body></html>'
        pdf_cache[pdf_url] = html_content; return HTMLResponse(html_content, 200)
    except httpx.HTTPStatusError as e: status=e.response.status_code; detail=f"PDF download failed ({status})"; raise HTTPException(status if status<500 else 502, detail)
    except httpx.RequestError as e: raise HTTPException(504, f"Network error downloading PDF: {e}")
    except FileNotFoundError as e: raise HTTPException(500, "Internal error processing PDF file.")
    except Exception as e: print(f"PDF processing error: {e}"); raise HTTPException(500, f"PDF processing failed: {e}")
    finally: # Cleanup temp file
        if tmp_name and await aiofiles.os.path.exists(tmp_name):
            try: await aiofiles.os.remove(tmp_name); print(f"Temp PDF deleted: {tmp_name}")
            except OSError as e_rem: print(f"Error removing temp file {tmp_name}: {e_rem}")

# --- Local Development Runner ---
if __name__ == "__main__":
    import uvicorn
    print("--- Starting FastAPI Application (Cookie Injection Strategy) ---")
    print(f"Headless mode: {HEADLESS_MODE}")
    print(f"Redis URL: {REDIS_URL}")
    print("API available at: http://127.0.0.1:8000")
    print(f"CapSolver Key Provided: {'Yes' if CAPSOLVER_API_KEY != 'YOUR_CAPSOLVER_API_KEY_HERE' and CAPSOLVER_API_KEY else 'NO'}")
    # reload=True can cause issues with lifespan and state in development. Set to False for stable testing.
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)