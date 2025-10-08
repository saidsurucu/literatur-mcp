# -*- coding: utf-8 -*-
import asyncio
import hashlib
import html
import io
import json
import math
import os # OS modülü import edildi
import pickle
import random
import tempfile
import traceback
import urllib.parse
import time
from typing import List, Optional, Literal, Dict, Any

# --- Gerekli Kütüphaneler ---
import aiofiles
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Body, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
import fitz 
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page, BrowserContext
from pydantic import BaseModel, Field

# --- Configuration ---
# Hafıza İçi Önbellek Ayarları
COOKIES_TTL = 1800; MAX_COOKIE_SETS = 10
ARTICLE_LINKS_TTL = 600; MAX_LINK_LISTS = 100
cookie_cache = TTLCache(maxsize=MAX_COOKIE_SETS, ttl=COOKIES_TTL)
links_cache = TTLCache(maxsize=MAX_LINK_LISTS, ttl=ARTICLE_LINKS_TTL)
COOKIES_CACHE_KEY = "dergipark_scraper:session:last_cookies"
COOKIES_FILE_PATH = "cookies_persistent.pkl"

# Helper functions for persistent cookie storage
def save_cookies_to_disk(cookies):
    """Save cookies to disk using pickle"""
    try:
        with open(COOKIES_FILE_PATH, 'wb') as f:
            pickle.dump({'cookies': cookies, 'timestamp': time.time()}, f)
        print(f"Cookies saved to disk: {COOKIES_FILE_PATH}")
    except Exception as e:
        print(f"Failed to save cookies to disk: {e}")

def load_cookies_from_disk():
    """Load cookies from disk if they exist and are fresh"""
    try:
        if not os.path.exists(COOKIES_FILE_PATH):
            return None
        with open(COOKIES_FILE_PATH, 'rb') as f:
            data = pickle.load(f)
        # Check if cookies are still valid (within TTL)
        age = time.time() - data['timestamp']
        if age > COOKIES_TTL:
            print(f"Disk cookies expired (age: {age:.0f}s > {COOKIES_TTL}s)")
            os.remove(COOKIES_FILE_PATH)
            return None
        print(f"Loaded {len(data['cookies'])} cookies from disk (age: {age:.0f}s)")
        return data['cookies']
    except Exception as e:
        print(f"Failed to load cookies from disk: {e}")
        return None

# CapSolver Ayarları
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "CAP-1E1D6F5F97285F22927DFC04FA04116A4A5FCC9211E28F36195D8372CC7D6739")
CAPSOLVER_CREATE_TASK_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"

# Playwright Ayarları
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]
HEADLESS_MODE = os.getenv("HEADLESS_MODE", "true").lower() == "true"

# Diğer Ayarlar
PDF_CACHE_TTL = int(os.getenv("PDF_CACHE_TTL", 86400))
pdf_cache = TTLCache(maxsize=500, ttl=PDF_CACHE_TTL)

# --- Browser Pool Configuration ---
BROWSER_POOL_SIZE = 2
browser_pool = []
playwright_instance = None
pool_lock = asyncio.Lock()

# --- FastAPI App Initialization ---
app = FastAPI(
    title="DergiPark Scraper API (Browser Pool + In-Memory Cache)",
    version="1.12.0", # Browser pooling için versiyon güncellendi
    description="API to search DergiPark articles with browser pooling for better performance and reduced CAPTCHA challenges.",
)

# --- API Key Check ---
if CAPSOLVER_API_KEY == "YOUR_CAPSOLVER_API_KEY_HERE" or not CAPSOLVER_API_KEY:
    print("\n" + "="*60 + "\nUYARI: CAPSOLVER_API_KEY ortam değişkeni ayarlanmamış...\n" + "="*60 + "\n")


# --- Pydantic Models ---
class SearchParams(BaseModel):
    q: Optional[str] = Field(None)  # Simple direct search query
    title: Optional[str] = Field(None); running_title: Optional[str] = Field(None); journal: Optional[str] = Field(None); issn: Optional[str] = Field(None); eissn: Optional[str] = Field(None); abstract: Optional[str] = Field(None); keywords: Optional[str] = Field(None); doi: Optional[str] = Field(None); doi_url: Optional[str] = Field(None); doi_prefix: Optional[str] = Field(None); author: Optional[str] = Field(None); orcid: Optional[str] = Field(None); institution: Optional[str] = Field(None); translator: Optional[str] = Field(None); pubyear: Optional[str] = Field(None); citation: Optional[str] = Field(None)
    dergipark_page: int = Field(default=1, ge=1); api_page: int = Field(default=1, ge=1)
    sort_by: Optional[Literal["newest", "oldest"]] = Field(None)
    article_type: Optional[Literal["54", "56", "58", "55", "60", "65", "57", "1", "5", "62", "73", "2", "10", "59", "66", "72"]] = Field(None)
    index_filter: Optional[Literal["tr_dizin_icerenler", "bos_olmayanlar", "hepsi"]] = Field(default="hepsi")
    publication_year: Optional[str] = Field(None)  # Year filter (e.g., "2022")

# --- Utility Functions ---
def truncate_text(text: str, word_limit: int) -> str:
    """Truncates text to a specified word limit."""
    if not text:
        return ""
    words = text.split()
    if len(words) > word_limit:
        return ' '.join(words[:word_limit]) + '...'
    return text

def generate_links_cache_key(params: SearchParams) -> Any:
    """Generates a hashable cache key for TTLCache based on search parameters."""
    key_data = params.model_dump(exclude={'api_page'}, exclude_unset=True, mode='python')
    # Use tuple of sorted items as dicts are not hashable
    sorted_items = tuple(sorted(key_data.items()))
    cache_key = (sorted_items, params.dergipark_page)
    return cache_key


# --- Fitz için Yardımcı Senkron Fonksiyon ---
def _extract_text_with_fitz_sync(pdf_path: str) -> str:
    """Synchronous helper to extract text using PyMuPDF."""
    extracted_text = ""
    try:
        doc = fitz.open(pdf_path)
        for page in doc: # Sayfalar üzerinde döngü
            extracted_text += page.get_text("text") # Sayfanın metnini al ve ekle
        doc.close()
        return extracted_text
    except Exception as e:
        print(f"PyMuPDF (fitz) extraction failed in helper for '{pdf_path}': {e}")
        raise # Hatanın ana try/except bloğunda yakalanmasını sağla



# --- Browser Pool Management ---
class BrowserPool:
    def __init__(self):
        self.browsers = []
        self.authenticated_browsers = set()  # Track CAPTCHA-solved browsers
        self.lock = asyncio.Lock()
    
    async def initialize(self):
        """Initialize browser pool on startup."""
        global playwright_instance
        try:
            print(f"Initializing browser pool with {BROWSER_POOL_SIZE} browsers...")
            playwright_instance = await async_playwright().start()
            
            for i in range(BROWSER_POOL_SIZE):
                browser = await self.create_browser()
                self.browsers.append(browser)
                print(f"Browser {i+1}/{BROWSER_POOL_SIZE} created")
            
            print("Browser pool initialization complete!")
        except Exception as e:
            print(f"Failed to initialize browser pool: {e}")
            raise
    
    async def create_browser(self):
        """Create a single browser instance."""
        browser = await playwright_instance.chromium.launch(
            headless=HEADLESS_MODE,
            args=['--disable-dev-shm-usage', '--no-sandbox']
        )
        return browser
    
    async def get_browser_and_context(self) -> tuple[Any, BrowserContext, Page]:
        """Get browser from pool and create new context."""
        async with self.lock:
            if not self.browsers:
                raise HTTPException(503, "No browsers available in pool")
            
            # Prefer authenticated browsers first
            browser = None
            for b in self.browsers:
                if b in self.authenticated_browsers and b.is_connected():
                    browser = b
                    break
            
            # Fallback to any available browser
            if not browser:
                for b in self.browsers:
                    if b.is_connected():
                        browser = b
                        break
            
            if not browser:
                print("No healthy browser found, creating new one...")
                browser = await self.create_browser()
                self.browsers[0] = browser  # Replace first browser
            
            # Create fresh context
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale='tr-TR',
                viewport={'width': 1920, 'height': 1080},
                ignore_https_errors=True
            )
            page = await context.new_page()
            
            print(f"Using browser from pool (authenticated: {browser in self.authenticated_browsers})")
            return browser, context, page
    
    async def mark_authenticated(self, browser):
        """Mark browser as CAPTCHA-solved."""
        async with self.lock:
            self.authenticated_browsers.add(browser)
            print("Browser marked as authenticated")
    
    async def cleanup(self):
        """Close all browsers in pool."""
        print("Cleaning up browser pool...")
        async with self.lock:
            for browser in self.browsers:
                try:
                    if browser.is_connected():
                        await browser.close()
                except Exception as e:
                    print(f"Error closing browser: {e}")
            self.browsers.clear()
            self.authenticated_browsers.clear()
        
        if playwright_instance:
            try:
                await playwright_instance.stop()
                print("Playwright instance stopped")
            except Exception as e:
                print(f"Error stopping playwright: {e}")

# Global browser pool instance
browser_pool_manager = BrowserPool()

async def close_context_and_page(context, page):
    """Safely close context and page (keep browser in pool)."""
    try:
        if page and not page.is_closed():
            await page.close()
        if context:
            await context.close()
    except Exception as e:
        if "closed" not in str(e).lower():
            print(f"Warning: Error closing context/page: {e}")


async def get_article_details_pw(page: Page, article_url: str, referer_url: Optional[str] = None) -> dict:
    """Fetches metadata and index info for a single article URL with retries."""
    print(f"Fetching details: {article_url}")
    details = {'error': None}; pdf_url = None; indices = ''; retries = 0
    max_retries = 1 # Allow one retry

    while retries <= max_retries:
        try:
            # --- Attempt Fetch ---
            print(f"Attempt {retries + 1} for {article_url}")
            await page.set_extra_http_headers({'Referer': referer_url or page.url})
            await page.goto(article_url, wait_until='domcontentloaded', timeout=30000)
            html_content = await page.content()

            # --- Check for Blocking ---
            if any(s in html_content.lower() for s in ["cloudflare", "captcha", "blocked", "erişim engellendi"]):
                print(f"Blocking pattern detected on details page: {article_url}")
                details['error'] = "Blocked"
                break # Exit loop immediately if blocked

            # --- Check Meta Tags ---
            soup = BeautifulSoup(html_content, 'html5lib')
            meta_tags = soup.find_all('meta')
            if not meta_tags:
                print(f"No meta tags found (Attempt {retries + 1}).")
                if retries < max_retries:
                    await asyncio.sleep(1.5 * (retries + 1)); retries += 1; continue # Retry
                else:
                    details['error'] = "No meta tags found after retries"; break # Exit loop

            # --- Extract Meta Details ---
            raw_details = {tag.get('name'): tag.get('content','').strip() for tag in meta_tags if tag.get('name')}
            pdf_url = raw_details.get('citation_pdf_url')
            journal_url_base = raw_details.get('DC.Source.URI') # Needed for index URL
            # Populate details dictionary carefully
            details = {
                'citation_title': raw_details.get('citation_title'),
                'citation_author': raw_details.get('DC.Creator.PersonalName'), # Correct meta name for author
                'citation_journal_title': raw_details.get('citation_journal_title'),
                'citation_publication_date': raw_details.get('citation_publication_date'),
                'citation_keywords': raw_details.get('citation_keywords'),
                'citation_doi': raw_details.get('citation_doi'),
                'citation_issn': raw_details.get('citation_issn'),
                'citation_abstract': truncate_text(raw_details.get('citation_abstract', ''), 100)
            }

            # --- Fetch Indexes (Optional) ---
            if journal_url_base:
                try:
                    index_url = f"{journal_url_base.rstrip('/')}/indexes"
                    print(f"Fetching indexes from: {index_url}")
                    await page.goto(index_url, wait_until='domcontentloaded', timeout=12000)
                    index_soup = BeautifulSoup(await page.content(), 'html5lib')
                    indices_list = [
                        i.text.strip() for i in index_soup.select('h5.j-index-listing-index-title') if i.text # Ensure text exists
                    ]
                    indices = ', '.join(indices_list)
                    print(f"Found indexes: {indices or 'None'}")
                except Exception as e_idx:
                    # Log index error but don't fail the whole detail fetch
                    print(f"Warning: Index page error/timeout for {journal_url_base}: {e_idx}")
                finally:
                    # Always try to navigate back to the article page
                    try:
                        if page.url != article_url:
                            print("Navigating back to article page after index check...")
                            await page.goto(article_url, wait_until='domcontentloaded', timeout=10000)
                    except Exception as e_back:
                        print(f"Warning: Failed to navigate back to article page: {e_back}")

            # --- Success ---
            details['error'] = None
            print(f"Successfully fetched details for {article_url}")
            break # Exit loop on success

        # --- Exception Handling for the Attempt ---
        except PlaywrightTimeoutError:
            print(f"Timeout fetching details (Attempt {retries + 1})")
            if retries < max_retries:
                await asyncio.sleep(2 * (retries + 1)); retries += 1; continue # Retry
            else:
                details['error'] = "Timeout after retries"; break # Exit loop

        except Exception as e:
            print(f"Error fetching details (Attempt {retries + 1}): {e}")
            # print(traceback.format_exc()) # Optional for debugging
            if retries < max_retries:
                await asyncio.sleep(2 * (retries + 1)); retries += 1; continue # Retry
            else:
                details['error'] = f"Error after retries: {e}"; break # Exit loop

    # --- End of While Loop ---
    return {'details': details, 'pdf_url': pdf_url, 'indices': indices}


async def _inject_and_submit_captcha(page: Page, token: str, verification_submit_selector: str, captcha_type: str = "recaptcha") -> bool:
    """Helper: Injects token (with events), clicks submit, checks result."""
    # Select injection target based on CAPTCHA type
    if captcha_type == "turnstile":
        injection_target_selector = '[name="cf-turnstile-response"]'
        js_func = """(t)=>{let e=document.querySelector('[name="cf-turnstile-response"]');if(e){console.log('Injecting Turnstile token...');e.value=t;e.dispatchEvent(new Event('input',{bubbles:!0}));e.dispatchEvent(new Event('change',{bubbles:!0}));console.log('Injected/dispatched.');return!0}return console.error('cf-turnstile-response missing!'),!1}"""
    else:  # recaptcha
        injection_target_selector = '#g-recaptcha-response'
        js_func = """(t)=>{let e=document.getElementById('g-recaptcha-response');if(e){console.log('Injecting token...');e.value=t;e.dispatchEvent(new Event('input',{bubbles:!0}));e.dispatchEvent(new Event('change',{bubbles:!0}));console.log('Injected/dispatched.');return!0}return console.error('#g-recaptcha-response missing!'),!1}"""

    try:
        print(f"Injecting {captcha_type} token via JS: {token[:15]}...")
        injection_success = await page.evaluate(js_func, token)
        if not injection_success:
            print(f"Error: Injection JS failed, target '{injection_target_selector}' not found?.")
            return False

        print("Token injection script executed successfully.")

        # For Turnstile, wait for the widget to process the token
        if captcha_type == "turnstile":
            print("Waiting for Turnstile to process token...")
            await asyncio.sleep(random.uniform(2.0, 3.5))
            # Try to unhide the submit button by removing kt-hidden class
            try:
                await page.evaluate("""
                    () => {
                        const submitBtn = document.querySelector('form[name="search_verification"] button[type="submit"]');
                        if (submitBtn && submitBtn.classList.contains('kt-hidden')) {
                            console.log('Removing kt-hidden class from submit button...');
                            submitBtn.classList.remove('kt-hidden');
                            return true;
                        }
                        return false;
                    }
                """)
                print("Attempted to unhide submit button.")
            except Exception as e:
                print(f"Could not unhide button via JS: {e}")
        else:
            await asyncio.sleep(random.uniform(0.5, 1.2)) # Brief pause

        # Locate and click submit button
        submit_button = page.locator(verification_submit_selector)
        print(f"Looking for submit button ('{verification_submit_selector}')...")
        try:
            # Wait for button to be attached first, then try to click even if hidden
            await submit_button.wait_for(state="attached", timeout=7000)
            # Try to force visibility and click
            await page.evaluate("""
                () => {
                    const submitBtn = document.querySelector('form[name="search_verification"] button[type="submit"]');
                    if (submitBtn) {
                        submitBtn.classList.remove('kt-hidden');
                        submitBtn.style.display = 'block';
                        submitBtn.style.visibility = 'visible';
                    }
                }
            """)
            await asyncio.sleep(0.5)
            # Now try to wait for visible state
            await submit_button.wait_for(state="visible", timeout=5000)
            print("Clicking 'Devam Et' button...")
            # Wait for navigation to complete after click, using 'load' state
            async with page.expect_navigation(wait_until='load', timeout=35000):
                await submit_button.click()
            print("Submit clicked, navigation finished ('load' event).")

            # Verify navigation was successful (not still on verification page)
            current_url = page.url
            print(f"URL after submit: {current_url}")
            if "verification" in current_url:
                print("Submission failed: Still on verification page.")
                return False
            else:
                print("Submission seems successful: Navigated away from verification page.")
                return True # Success

        except Exception as e_sub:
            # Handle errors during submit click or navigation wait
            print(f"Error during submit/navigation: {e_sub}")
            return False

    except Exception as e_js:
        # Handle errors during JavaScript evaluation (injection)
        print(f"Error executing JS injection: {e_js}")
        return False


async def solve_recaptcha_v2_capsolver_direct_async(page: Page) -> bool:
    """Solves reCAPTCHA v2 by fetching a *new* token from CapSolver."""
    print("CAPTCHA detected. Fetching NEW token from CapSolver...")
    site_key_element_selector = '.g-recaptcha[data-sitekey]'
    injection_target_selector = '#g-recaptcha-response'
    verification_submit_selector = 'form[name="search_verification"] button[type="submit"]:has-text("Devam Et")'

    if not CAPSOLVER_API_KEY or CAPSOLVER_API_KEY == "YOUR_CAPSOLVER_API_KEY_HERE":
        print("Error: CAPSOLVER_API_KEY is not configured.")
        return False

    try:
        # --- Fetch Site Key ---
        site_key = None
        page_url = page.url
        print(f"Waiting for sitekey element on {page_url}...")
        try:
            site_key_element = await page.wait_for_selector(site_key_element_selector, state="attached", timeout=15000)
            site_key = await site_key_element.get_attribute('data-sitekey')
            if not site_key: raise ValueError("Sitekey attribute empty.")
            print("Sitekey element found.")
        except (PlaywrightTimeoutError, ValueError, Exception) as e:
            print(f"Error finding/getting sitekey: {e}")
            # Try fallback: extract sitekey from page source
            print("Trying fallback: extracting sitekey from page source...")
            page_content = await page.content()
            import re
            sitekey_match = re.search(r'data-sitekey=["\']([^"\']+)["\']', page_content)
            if sitekey_match:
                site_key = sitekey_match.group(1)
                print(f"Sitekey found via regex: {site_key}")
            else:
                print("Fallback failed: No sitekey found in page source.")
                return False # Cannot proceed

        print(f"Sitekey: {site_key}, URL: {page_url}")

        # --- Determine CAPTCHA Type ---
        # Cloudflare Turnstile keys start with 0x4, reCAPTCHA keys start with 6L
        if site_key.startswith("0x4"):
            task_type = "AntiTurnstileTaskProxyLess"
            captcha_type = "turnstile"
            injection_target_selector = '[name="cf-turnstile-response"]'
            print("Detected Cloudflare Turnstile CAPTCHA")
        else:
            task_type = "ReCaptchaV2TaskProxyless"
            captcha_type = "recaptcha"
            injection_target_selector = '#g-recaptcha-response'
            print("Detected reCAPTCHA v2")

        # --- Call CapSolver API ---
        task_payload = {"clientKey": CAPSOLVER_API_KEY, "task": {"type": task_type, "websiteURL": page_url, "websiteKey": site_key}}
        captcha_token = None
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Create Task
            print("Sending task to CapSolver...")
            task_id = None
            try:
                create_response = await client.post(CAPSOLVER_CREATE_TASK_URL, json=task_payload)
                create_response.raise_for_status()
                create_result = create_response.json()
                if create_result.get("errorId", 0) != 0: raise ValueError(f"API Error Create: {create_result}")
                task_id = create_result.get("taskId")
                if not task_id: raise ValueError("No Task ID received.")
                print(f"CapSolver Task created: {task_id}")
            except Exception as e:
                print(f"Error Creating CapSolver Task: {e}")
                return False

            # Poll for Result
            start_time = time.time(); timeout_seconds = 180
            while time.time() - start_time < timeout_seconds:
                await asyncio.sleep(6)
                print(f"Polling CapSolver (ID: {task_id})...")
                result_payload = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
                try:
                    get_response = await client.post(CAPSOLVER_GET_RESULT_URL, json=result_payload, timeout=15)
                    get_response.raise_for_status()
                    get_result = get_response.json()
                    if get_result.get("errorId", 0) != 0: raise ValueError(f"API Error Poll: {get_result}")
                    status = get_result.get("status")
                    print(f"Task status: {status}")
                    if status == "ready":
                        solution = get_result.get("solution")
                        if solution:
                            # Try both field names - Turnstile uses "token", reCAPTCHA uses "gRecaptchaResponse"
                            captcha_token = solution.get("token") or solution.get("gRecaptchaResponse")
                        if captcha_token: print("CapSolver solution received!"); break
                        else: raise ValueError("Task ready but no token.")
                    elif status in ["failed", "error"]:
                        raise ValueError(f"CapSolver task failed/errored: {get_result.get('errorDescription', 'N/A')}")
                    # Only continue loop if processing or unknown status
                except Exception as e:
                    print(f"Warning: Error Polling CapSolver Task (will retry): {e}")
                    await asyncio.sleep(5) # Wait before next poll attempt

            if not captcha_token:
                print("Polling timeout or final error getting token.")
                return False

        # --- Submit with the new token ---
        print("New token received. Attempting submission...")
        try:
            # Wait for injection target element
            print(f"Waiting for injection target ('{injection_target_selector}')...")
            await page.wait_for_selector(injection_target_selector, state="attached", timeout=10000)
            print("Injection target found.")
        except PlaywrightTimeoutError:
            print(f"Timeout waiting for injection target before submission.")
            return False

        # Inject and submit
        submission_successful = await _inject_and_submit_captcha(page, captcha_token, verification_submit_selector, captcha_type)

        if not submission_successful:
            print("Submission failed with the new token from CapSolver.")
        # Return status regardless of saving cookies (which happens elsewhere)
        return submission_successful

    # --- Outer Exception Handling ---
    except Exception as e:
        print(f"Unexpected error during CAPTCHA solving process: {e}")
        # print(traceback.format_exc()) # Uncomment for debugging
        return False


async def get_article_links_with_cache(
    page: Page, search_url: str, cache_key: Any
) -> List[Dict[str, str]]:
    """Gets links. Uses global TTLCache. Fetches if miss. Handles CAPTCHA. Saves cookies if solved."""
    # 1. Check Cache
    try:
        cached_data = links_cache.get(cache_key)
        # Check explicitly for None as empty list is a valid cached value
        if cached_data is not None:
            print(f"Cache HIT: Links {str(cache_key)[:100]}...")
            return cached_data
    except Exception as e:
        # Log error but treat as cache miss
        print(f"Warning: Links cache GET error for key {str(cache_key)[:100]}...: {e}")

    print(f"Cache MISS: Links {str(cache_key)[:100]}... Fetching from DergiPark...")
    article_links = []; article_card_selector = 'div.card.article-card.dp-card-outline'; captcha_was_solved = False

    # Main process: Navigation, CAPTCHA check, Link Extraction
    try:
        # 2. Navigate
        print(f"Navigating to: {search_url}")
        await page.goto(search_url, wait_until='load', timeout=40000)
        print(f"Nav complete. URL: {page.url}")

        # 3. Handle CAPTCHA if redirected
        if "/search/verification" in page.url or "verification" in page.url:
            print("CAPTCHA page detected.")
            captcha_passed = await solve_recaptcha_v2_capsolver_direct_async(page)
            if not captcha_passed:
                # If CAPTCHA fails, raise exception to stop processing this request
                raise HTTPException(429, "CAPTCHA solving failed.")
            print("CAPTCHA passed. Waiting for results page to load...")
            captcha_was_solved = True
            # Wait for page to fully load after CAPTCHA
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await page.wait_for_load_state("networkidle", timeout=20000)
                print("Results page loaded after CAPTCHA.")
            except Exception as e:
                print(f"Load state wait warning: {e}")

        else:
            print("No CAPTCHA detected.")

        # Click on "Makale" (articles) section ONLY if we're NOT already on article section
        # (clicking would lose our URL filters like publication_year, article_type)
        current_url = page.url
        if "section=article" not in current_url:
            try:
                print("Not on article section yet, looking for article section link to click...")
                article_section_link = await page.query_selector('a.search-section-link[href*="section=article"]')
                if article_section_link:
                    print("Clicking on article section...")
                    await article_section_link.click()
                    await page.wait_for_load_state("networkidle", timeout=20000)
                    print("Article section loaded.")
                else:
                    print("Article section link not found.")
            except Exception as e:
                print(f"Warning: Could not click article section: {e}")
        else:
            print("Already on article section (URL contains section=article), skipping click to preserve filters.")

        # Wait for client-side JavaScript filtering to complete
        print("Waiting for client-side JavaScript filtering...")
        await asyncio.sleep(3)  # Give JavaScript time to filter results
        await page.wait_for_load_state("networkidle", timeout=10000)
        print("JavaScript filtering should be complete.")

        # 4. Extract Article Links
        try:
            # Wait for cards to be attached to DOM
            print(f"Waiting for article cards with selector: {article_card_selector}")
            await page.wait_for_selector(article_card_selector, state="attached", timeout=15000)
            article_cards = await page.query_selector_all(article_card_selector)
            print(f"{len(article_cards)} article cards found.")
        except PlaywrightTimeoutError:
            # If cards timeout, check for "no results" message
            page_content = await page.content()
            if "sonuç bulunamadı" in page_content.lower():
                print("No results message detected.")
                article_links = [] # Explicitly set to empty list
            else:
                # Debug: try to find what's on the page
                print(f"DEBUG: Searching for alternative selectors...")
                alt_cards = await page.query_selector_all("div.card")
                print(f"DEBUG: Found {len(alt_cards)} elements with class 'card'")
                article_divs = await page.query_selector_all("div.article-card")
                print(f"DEBUG: Found {len(article_divs)} elements with class 'article-card'")

                # Take screenshot for debugging
                await page.screenshot(path="debug_search_results.png")
                print("DEBUG: Screenshot saved to debug_search_results.png")

                # Save page HTML for inspection
                with open("debug_page.html", "w", encoding="utf-8") as f:
                    f.write(page_content)
                print("DEBUG: Page HTML saved to debug_page.html")

                # If no results message and no cards, raise error
                raise HTTPException(500, "Link extraction failed (timeout finding cards).")

        # Process cards if found
        if article_cards:
            base_page_url = page.url
            for card in article_cards:
                a_tag = await card.query_selector('h5.card-title > a[href]')
                if a_tag:
                    url = await a_tag.get_attribute('href')
                    title = await a_tag.text_content()
                    # Ensure URL is absolute
                    absolute_url = urllib.parse.urljoin(base_page_url, url.strip())
                    article_links.append({'url': absolute_url, 'title': title.strip() or "N/A"})

        # 5. Cache Links (even if list is empty)
        try:
            links_cache[cache_key] = article_links
            print(f"Stored {len(article_links)} links in link cache: {str(cache_key)[:100]}...")
        except Exception as e:
            print(f"Warning: Links cache SET error for key {str(cache_key)[:100]}...: {e}")

        # 6. Save Cookies and Mark Browser as Authenticated if CAPTCHA was solved
        if captcha_was_solved:
            try:
                print("Saving cookies post-CAPTCHA to in-memory cache...")
                browser_context = page.context
                # Get cookies relevant to the current page's domain
                current_cookies = await browser_context.cookies(urls=[page.url])
                if current_cookies:
                    # Clean cookie data (expires format)
                    for c in current_cookies:
                        if 'expires' in c and isinstance(c['expires'], float): c['expires'] = int(c['expires'])
                    # Store using the constant key
                    cookie_cache[COOKIES_CACHE_KEY] = current_cookies
                    print(f"Saved {len(current_cookies)} cookies to cache '{COOKIES_CACHE_KEY}' (TTL: {COOKIES_TTL}s).")
                    # Also save to disk for persistence across restarts
                    save_cookies_to_disk(current_cookies)
                    
                    # Mark this browser as authenticated in the pool
                    browser = page.context.browser
                    await browser_pool_manager.mark_authenticated(browser)
                else:
                    print("No relevant cookies found to save.")
            except Exception as e:
                print(f"Warning: Failed to save cookies to cache: {e}")

        return article_links # Return the list (possibly empty)

    # Catch exceptions during the main fetching process
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e # Re-raise HTTP exceptions directly
        print(f"Error in get_article_links_with_cache: {e}\n{traceback.format_exc()}")
        # Wrap other exceptions in a standard 500 error
        raise HTTPException(500, f"Link fetching/processing failed: {e}")


# --- FastAPI Endpoints ---

@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """API health check."""
    # Simple status endpoint
    return {"status": "ok"}

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
        return HTMLResponse(content=content, status_code=200)
    except Exception as e:
        print(f"Error reading gizlilik file '{file_path}': {e}")
        raise HTTPException(status_code=500, detail=f"Error reading privacy policy file: {e}")


@app.post("/api/search", response_class=JSONResponse)
async def search_articles(request: Request, search_params: SearchParams = Body(...)):
    """Search DergiPark articles. Uses in-memory cache. REQUIRES SINGLE WORKER."""
    # --- Construct DergiPark Search URL ---
    base_url = "https://dergipark.org.tr/tr/search"; query_params = {}

    # Handle backward compatibility: if pubyear is provided but publication_year is not, use pubyear for publication_year
    if search_params.pubyear and not search_params.publication_year:
        search_params.publication_year = search_params.pubyear

    # If direct 'q' param provided, use it. Otherwise build from specific fields
    if search_params.q:
        query_params['q'] = search_params.q
    else:
        # Build search query from all provided fields (excluding q, pagination, etc)
        search_q = " ".join(f"{f}:{v}" for f, v in search_params.model_dump(exclude={'q', 'dergipark_page', 'api_page', 'sort_by', 'article_type', 'index_filter', 'publication_year', 'pubyear'}, exclude_unset=True).items())
        if search_q:
            query_params['q'] = search_q
        else:
            # If no search params provided, search for everything
            query_params['q'] = '*'
    query_params['section'] = 'article'
    if search_params.dergipark_page > 1: query_params['page'] = search_params.dergipark_page
    if search_params.article_type: query_params['filter[article_type][]'] = search_params.article_type
    if search_params.sort_by: query_params['sortBy'] = search_params.sort_by
    if search_params.publication_year: query_params['filter[publication_year][]'] = search_params.publication_year
    target_search_url = f"{base_url}?{urllib.parse.urlencode(query_params, quote_via=urllib.parse.quote)}"
    page_size = 5  # Fixed page size
    print(f"Target DP URL: {target_search_url} | API Page: {search_params.api_page} | Size: {page_size}")

    host = str(request.base_url).rstrip('/')
    browser = context = page = playwright_instance = None
    total_items_on_page = 0

    try:
        # --- Get Browser from Pool ---
        browser, context, page = await browser_pool_manager.get_browser_and_context()

        # --- Attempt to Inject Cookies from Cache (Memory or Disk) ---
        try:
            print(f"Checking in-memory cache for cookies: {COOKIES_CACHE_KEY}")
            saved_cookies = cookie_cache.get(COOKIES_CACHE_KEY) # Use global cache

            # If not in memory, try loading from disk
            if not saved_cookies:
                print("Memory cache miss, checking disk...")
                saved_cookies = load_cookies_from_disk()
                if saved_cookies:
                    # Load into memory cache too
                    cookie_cache[COOKIES_CACHE_KEY] = saved_cookies

            if saved_cookies:
                 required_keys={'name','value','domain','path'}; valid_cookies=[]
                 for c in saved_cookies:
                     # Basic validation and cleaning
                     if required_keys.issubset(c.keys()):
                         if 'expires' in c and isinstance(c['expires'],float): c['expires']=int(c['expires'])
                         if 'sameSite' in c and c['sameSite'] not in ['Strict','Lax','None']: del c['sameSite']
                         valid_cookies.append(c)
                 if valid_cookies:
                     print(f"Injecting {len(valid_cookies)} cookies from cache...")
                     await context.add_cookies(valid_cookies)
                     print("Cookies injected.")
                 else: print("No valid cookies found in cache.")
            else: print("No saved cookies found in cache.")
        except Exception as e:
            # Log error but don't stop the request
            print(f"Warning: Cookie load/injection error from cache: {e}")

        # --- Get Article Links ---
        links_cache_key = generate_links_cache_key(search_params)
        full_link_list = await get_article_links_with_cache(page, target_search_url, links_cache_key) # Pass cache key

        # --- Process Results & Pagination ---
        total_items_on_page = len(full_link_list)
        total_api_pages = math.ceil(total_items_on_page / page_size) if total_items_on_page > 0 else 0
        pagination_info = {"api_page": search_params.api_page, "page_size": page_size, "total_items_on_dergipark_page": total_items_on_page, "total_api_pages_for_dergipark_page": total_api_pages}

        # Handle case where no articles found on DergiPark page
        if total_items_on_page == 0:
            return JSONResponse(content={"pagination": pagination_info, "articles": []})

        # Calculate slice - always process only 5 articles
        offset = (search_params.api_page - 1) * page_size
        limit = page_size
        links_to_process = full_link_list[offset : offset + limit]
        print(f"Links: Total={total_items_on_page}, Slice={len(links_to_process)} (API Page {search_params.api_page}/{total_api_pages})")

        # Handle case where API page is out of bounds
        if not links_to_process:
            return JSONResponse(content={"pagination": pagination_info, "articles": []})

        # --- Fetch Details for Slice ---
        articles_details = []
        print(f"Fetching details for {len(links_to_process)} articles...")
        referer_url = page.url # Use last known URL as referer
        for i, link_info in enumerate(links_to_process):
            print(f"  Processing {offset + i + 1}/{total_items_on_page}: {link_info['url']}")
            details_result = await get_article_details_pw(page, link_info['url'], referer_url=referer_url)

            # Combine details into final structure
            pdf_url = details_result.get('pdf_url')
            article_details = details_result.get('details', {}) # Default to empty dict
            indices_str = details_result.get('indices', '')

            if article_details.get('error'):
                article_data = {'title': link_info['title'], 'url': link_info['url'], 'error': f"Detail error: {article_details['error']}", 'details': None, 'indices': '', 'readable_pdf': None}
            else:
                article_data = {'title': link_info['title'], 'url': link_info['url'], 'error': None, 'details': article_details, 'indices': indices_str, 'readable_pdf': f"{host}/api/pdf-to-html?pdf_url={urllib.parse.quote(pdf_url)}" if pdf_url else None}

            # Apply index filter
            passes = not (
                (search_params.index_filter == "tr_dizin_icerenler" and "TR Dizin" not in indices_str) or
                (search_params.index_filter == "bos_olmayanlar" and not indices_str)
            )
            if passes:
                articles_details.append(article_data)
            else:
                print(f"  Filtered out by index_filter: {link_info['url']}")

            # Add delay between detail fetches
            await asyncio.sleep(random.uniform(0.8, 1.8))

        # Return final paginated result
        return JSONResponse(content={"pagination": pagination_info, "articles": articles_details})

    except HTTPException as e:
        # Handle known HTTP errors (like 429 from CAPTCHA)
        print(f"HTTP Exception: {e.status_code} - {e.detail}")
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    except Exception as e:
        # Handle unexpected errors
        print(f"General search error: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"detail": f"Unexpected search error: {e}"})
    finally:
        # Close only context and page (keep browser in pool)
        if context or page:
            await close_context_and_page(context, page)


@app.get("/api/pdf-to-html", response_class=HTMLResponse)
async def pdf_to_html(pdf_url: str):
    """Downloads and converts PDF URL to readable HTML."""
    if not pdf_url or not pdf_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid or missing PDF URL.")

    # Check cache first
    cached_html = pdf_cache.get(pdf_url)
    if cached_html:
        print(f"PDF cache hit: {pdf_url}")
        return HTMLResponse(content=cached_html, status_code=200)
    print(f"PDF cache miss: {pdf_url}")

    tmp_name = None
    try:
        # --- FIX: Create a new client instance for each request ---
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True) as client:
            print(f"Downloading PDF from: {pdf_url}")
            # Use the new 'client' instance directly
            response = await client.get(pdf_url) # follow_redirects client seviyesinde ayarlandı, burada tekrar gerekmez
            response.raise_for_status() # Raise errors for bad status codes
            content_type = response.headers.get('content-type', '').lower()
            if 'application/pdf' not in content_type:
                print(f"Warning: URL content type ('{content_type}') is not 'application/pdf'.")
            pdf_content = response.content
        # --- Client is automatically closed here by 'async with' ---

        if not pdf_content:
            raise HTTPException(status_code=404, detail="Downloaded PDF content is empty.")

        # Write to temporary file (using os.path for sync operations)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            # Use thread for potentially blocking disk I/O
            await asyncio.to_thread(tmp.write, pdf_content)
            tmp_name = tmp.name

        if not tmp_name or not os.path.exists(tmp_name):
            raise FileNotFoundError("Temporary PDF file not found after write.")

        # --- PDF Metin Çıkarma: MarkItDown yerine PyMuPDF (fitz) ---
        try:
            print(f"Converting PDF {tmp_name} to text using PyMuPDF (fitz)...")
            # Senkron fitz fonksiyonunu ayrı bir thread'de çalıştır
            markdown_text = await asyncio.to_thread(_extract_text_with_fitz_sync, tmp_name)
            if not markdown_text: # Başarısız veya boşsa
                print(f"Warning: PyMuPDF (fitz) produced empty text for {tmp_name}.")
                markdown_text = "PDF içeriği okunamadı veya boş." # Varsayılan mesaj
            print(f"Conversion result length: {len(markdown_text)}")
        except Exception as convert_err:
            print(f"PyMuPDF (fitz) conversion failed: {convert_err}")
            raise HTTPException(status_code=500, detail=f"PDF metin çıkarma hatası: {convert_err}")


        # Prepare HTML response safely
        escaped_pdf_url = html.escape(pdf_url)
        escaped_filename = html.escape(os.path.basename(urllib.parse.urlparse(pdf_url).path) or "document")
        escaped_markdown = html.escape(markdown_text)
        html_content = f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>PDF İçeriği - {escaped_filename}</title><style>body{{font-family:sans-serif;line-height:1.6;padding:20px;max-width:900px;margin:auto;background-color:#f8f9fa;}}pre{{background:#fff;padding:15px;border-radius:5px;overflow-x:auto;white-space:pre-wrap;word-wrap:break-word;border:1px solid #dee2e6;}}a button{{padding:10px 15px;cursor:pointer;}}h1{{text-align:center;}}</style></head><body><h1>Metne Dönüştürülmüş PDF İçeriği</h1><p style="text-align:center;"><a href="{escaped_pdf_url}" target="_blank"><button>Orijinal PDF'yi Görüntüle</button></a></p><pre>{escaped_markdown}</pre></body></html>"""

        # Cache the result
        pdf_cache[pdf_url] = html_content
        return HTMLResponse(content=html_content, status_code=200)

    # --- Exception Handling ---
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        detail = f"PDF download failed ({status_code}) for URL: {pdf_url}"
        print(detail)
        raise HTTPException(status_code=status_code if status_code < 500 else 502, detail=detail)
    except httpx.RequestError as e:
        print(f"Network error downloading PDF: {e}")
        raise HTTPException(status_code=504, detail=f"Network error downloading PDF: {e}")
    except FileNotFoundError as e:
         print(f"File system error during PDF processing: {e}")
         raise HTTPException(status_code=500, detail="Internal error processing PDF file.")
    except Exception as e:
        print(f"Unexpected PDF conversion/processing error: {e}")
        # print(traceback.format_exc()) # Optional
        raise HTTPException(status_code=500, detail=f"PDF processing failed unexpectedly: {e}")
    finally:
        # Clean up temporary file
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
                print(f"Temporary PDF file deleted: {tmp_name}")
            except OSError as e_remove:
                print(f"Error removing temporary file {tmp_name}: {e_remove}")


# --- FastAPI Lifecycle Events ---
@app.on_event("startup")
async def startup_event():
    """Initialize browser pool on startup."""
    print("=== APPLICATION STARTUP ===")
    await browser_pool_manager.initialize()
    print("=== STARTUP COMPLETE ===")

@app.on_event("shutdown")
async def shutdown_event():
    """Clean up browser pool on shutdown."""
    print("=== APPLICATION SHUTDOWN ===")
    await browser_pool_manager.cleanup()
    print("=== SHUTDOWN COMPLETE ===")

# --- Local Development Runner ---
if __name__ == "__main__":
    import uvicorn
    print("--- Starting FastAPI Application (Browser Pool + In-Memory Cache Strategy) ---")
    print("--- WARNING: Requires running with a SINGLE WORKER PROCESS (--workers 1) for cache effectiveness ---")
    print(f"Headless mode: {HEADLESS_MODE}")
    print(f"Browser pool size: {BROWSER_POOL_SIZE}")
    
    # Get port from environment (Fly.io sets this automatically)
    port = int(os.getenv("PORT", 8000))
    print(f"API available at: http://0.0.0.0:{port}")
    print(f"CapSolver Key Provided: {'Yes' if CAPSOLVER_API_KEY != 'YOUR_CAPSOLVER_API_KEY_HERE' and CAPSOLVER_API_KEY else 'NO'}")
    
    # Run with 1 worker explicitly, disable reload for stability if testing functionality
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, workers=1)