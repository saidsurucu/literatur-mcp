# -*- coding: utf-8 -*-
"""
DergiPark Scraper Core Module

Bu modül, DergiPark akademik makale arama ve PDF dönüştürme için
temel işlevselliği sağlar. Hem FastAPI hem de FastMCP sunucuları
tarafından kullanılabilir.
"""

import asyncio
import html
import math
import os
import pickle
import random
import sys
import tempfile
import traceback
import urllib.parse
import time
import re
from typing import List, Optional, Literal, Dict, Any, Tuple

# --- Gerekli Kütüphaneler ---
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
import fitz
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page, BrowserContext
from mistralai import Mistral

# --- Configuration ---
# Hafıza İçi Önbellek Ayarları
COOKIES_TTL = 1800
MAX_COOKIE_SETS = 10
ARTICLE_LINKS_TTL = 600
MAX_LINK_LISTS = 100
cookie_cache = TTLCache(maxsize=MAX_COOKIE_SETS, ttl=COOKIES_TTL)
links_cache = TTLCache(maxsize=MAX_LINK_LISTS, ttl=ARTICLE_LINKS_TTL)
COOKIES_CACHE_KEY = "dergipark_scraper:session:last_cookies"
COOKIES_FILE_PATH = "cookies_persistent.pkl"

# CapSolver Ayarları
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")
CAPSOLVER_CREATE_TASK_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"

# Mistral OCR Ayarları (PDF fallback)
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")

# Playwright Ayarları
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]
HEADLESS_MODE = os.getenv("HEADLESS_MODE", "true").lower() == "true"

# PDF Cache
PDF_CACHE_TTL = int(os.getenv("PDF_CACHE_TTL", 86400))
pdf_cache = TTLCache(maxsize=500, ttl=PDF_CACHE_TTL)

# Browser Pool Configuration
BROWSER_POOL_SIZE = 5  # Paralel işlem için artırıldı (eski: 2)


# --- Helper Functions ---
def save_cookies_to_disk(cookies):
    """Save cookies to disk using pickle"""
    try:
        with open(COOKIES_FILE_PATH, 'wb') as f:
            pickle.dump({'cookies': cookies, 'timestamp': time.time()}, f)
        print(f"Cookies saved to disk: {COOKIES_FILE_PATH}", file=sys.stderr)
    except Exception as e:
        print(f"Failed to save cookies to disk: {e}", file=sys.stderr)


def load_cookies_from_disk():
    """Load cookies from disk if they exist and are fresh"""
    try:
        if not os.path.exists(COOKIES_FILE_PATH):
            return None
        with open(COOKIES_FILE_PATH, 'rb') as f:
            data = pickle.load(f)
        age = time.time() - data['timestamp']
        if age > COOKIES_TTL:
            print(f"Disk cookies expired (age: {age:.0f}s > {COOKIES_TTL}s)", file=sys.stderr)
            os.remove(COOKIES_FILE_PATH)
            return None
        print(f"Loaded {len(data['cookies'])} cookies from disk (age: {age:.0f}s)", file=sys.stderr)
        return data['cookies']
    except Exception as e:
        print(f"Failed to load cookies from disk: {e}", file=sys.stderr)
        return None


def truncate_text(text: str, word_limit: int) -> str:
    """Truncates text to a specified word limit."""
    if not text:
        return ""
    words = text.split()
    if len(words) > word_limit:
        return ' '.join(words[:word_limit]) + '...'
    return text


def _extract_text_with_fitz_sync(pdf_path: str) -> str:
    """Synchronous helper to extract text using PyMuPDF."""
    extracted_text = ""
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            extracted_text += page.get_text("text")
        doc.close()
        return extracted_text
    except Exception as e:
        print(f"PyMuPDF (fitz) extraction failed in helper for '{pdf_path}': {e}", file=sys.stderr)
        raise


async def _ocr_with_mistral(pdf_url: str) -> str:
    """Mistral OCR API ile PDF'den metin çıkarır (fallback)."""
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY not configured")

    try:
        client = Mistral(api_key=MISTRAL_API_KEY)

        print(f"Mistral OCR processing: {pdf_url}", file=sys.stderr)
        ocr_response = await asyncio.to_thread(
            lambda: client.ocr.process(
                model="mistral-ocr-latest",
                document={
                    "type": "document_url",
                    "document_url": pdf_url
                },
                include_image_base64=False
            )
        )

        # Extract text from OCR response
        if hasattr(ocr_response, 'pages') and ocr_response.pages:
            text_parts = []
            for page in ocr_response.pages:
                if hasattr(page, 'markdown') and page.markdown:
                    text_parts.append(page.markdown)
                elif hasattr(page, 'text') and page.text:
                    text_parts.append(page.text)
            result = "\n\n".join(text_parts)
            print(f"Mistral OCR extracted {len(result)} characters", file=sys.stderr)
            return result

        return ""
    except Exception as e:
        print(f"Mistral OCR failed: {e}", file=sys.stderr)
        raise RuntimeError(f"Mistral OCR error: {e}")


async def fetch_indices_async(journal_url_base: str) -> str:
    """Index bilgisini async HTTP ile çeker (Playwright kullanmadan)."""
    if not journal_url_base:
        return ''
    try:
        index_url = f"{journal_url_base.rstrip('/')}/indexes"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(index_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html5lib')
            indices_list = [
                i.text.strip() for i in soup.select('h5.j-index-listing-index-title') if i.text
            ]
            return ', '.join(indices_list)
    except Exception as e:
        print(f"Async index fetch failed for {journal_url_base}: {e}", file=sys.stderr)
        return ''


# --- Browser Pool Management ---
class BrowserPool:
    """Browser pool manager for Playwright instances."""

    def __init__(self, pool_size: int = BROWSER_POOL_SIZE):
        self.pool_size = pool_size
        self.browsers = []
        self.authenticated_browsers = set()
        self.lock = asyncio.Lock()
        self.playwright_instance = None

    async def initialize(self):
        """Initialize browser pool on startup."""
        try:
            print(f"Initializing browser pool with {self.pool_size} browsers...", file=sys.stderr)
            self.playwright_instance = await async_playwright().start()

            for i in range(self.pool_size):
                browser = await self.create_browser()
                self.browsers.append(browser)
                print(f"Browser {i+1}/{self.pool_size} created", file=sys.stderr)

            print("Browser pool initialization complete!", file=sys.stderr)
        except Exception as e:
            print(f"Failed to initialize browser pool: {e}", file=sys.stderr)
            raise

    async def create_browser(self):
        """Create a single browser instance."""
        browser = await self.playwright_instance.chromium.launch(
            headless=HEADLESS_MODE,
            args=[
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=AutomationControlled',
                '--disable-client-side-phishing-detection',
                '--disable-component-update',
                '--no-first-run',
            ]
        )
        return browser

    async def get_browser_and_context(self) -> Tuple[Any, BrowserContext, Page]:
        """Get browser from pool and create new context."""
        async with self.lock:
            if not self.browsers:
                raise RuntimeError("No browsers available in pool")

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
                print("No healthy browser found, creating new one...", file=sys.stderr)
                browser = await self.create_browser()
                self.browsers[0] = browser

            # Create fresh context
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale='tr-TR',
                viewport={'width': 1920, 'height': 1080},
                ignore_https_errors=True
            )
            page = await context.new_page()

            print(f"Using browser from pool (authenticated: {browser in self.authenticated_browsers})", file=sys.stderr)
            return browser, context, page

    async def mark_authenticated(self, browser):
        """Mark browser as CAPTCHA-solved."""
        async with self.lock:
            self.authenticated_browsers.add(browser)
            print("Browser marked as authenticated", file=sys.stderr)

    async def cleanup(self):
        """Close all browsers in pool."""
        print("Cleaning up browser pool...", file=sys.stderr)
        async with self.lock:
            for browser in self.browsers:
                try:
                    if browser.is_connected():
                        await browser.close()
                except Exception as e:
                    print(f"Error closing browser: {e}", file=sys.stderr)
            self.browsers.clear()
            self.authenticated_browsers.clear()

        if self.playwright_instance:
            try:
                await self.playwright_instance.stop()
                print("Playwright instance stopped", file=sys.stderr)
            except Exception as e:
                print(f"Error stopping playwright: {e}", file=sys.stderr)


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
            print(f"Warning: Error closing context/page: {e}", file=sys.stderr)


async def fetch_article_details_parallel(
    links_to_process: List[Dict[str, str]],
    referer_url: str,
    index_filter: Optional[str] = "hepsi",
    max_concurrent: int = 3
) -> List[dict]:
    """
    Paralel olarak makale detaylarını çeker.

    Args:
        links_to_process: Makale URL ve başlık listesi
        referer_url: Referer header için URL
        index_filter: Index filtresi
        max_concurrent: Maksimum eşzamanlı istek sayısı

    Returns:
        İşlenmiş makale listesi
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    results = []

    async def fetch_single(link_info: dict, client: httpx.AsyncClient) -> dict:
        async with semaphore:
            try:
                print(f"  httpx fetch: {link_info['url'][:60]}...", file=sys.stderr)

                # Makale detaylarını httpx ile çek
                response = await client.get(
                    link_info['url'],
                    headers={'Referer': referer_url},
                    timeout=30.0
                )
                response.raise_for_status()
                html_content = response.text

                if any(s in html_content.lower() for s in ["cloudflare", "captcha", "blocked"]):
                    return {
                        'title': link_info['title'],
                        'url': link_info['url'],
                        'error': "Blocked",
                        'details': None,
                        'indices': '',
                        'pdf_url': None
                    }

                soup = BeautifulSoup(html_content, 'html5lib')
                meta_tags = soup.find_all('meta')
                raw_details = {tag.get('name'): tag.get('content', '').strip() for tag in meta_tags if tag.get('name')}

                pdf_url = raw_details.get('citation_pdf_url')
                journal_url_base = raw_details.get('DC.Source.URI')

                # İstatistikler
                citation_count = raw_details.get('stats_trdizin_citation_count', '0')
                reference_tags = [tag for tag in meta_tags if tag.get('name') == 'citation_reference']
                reference_count = len(reference_tags)

                details = {
                    'citation_title': raw_details.get('citation_title'),
                    'citation_author': raw_details.get('DC.Creator.PersonalName'),
                    'citation_journal_title': raw_details.get('citation_journal_title'),
                    'citation_publication_date': raw_details.get('citation_publication_date'),
                    'citation_keywords': raw_details.get('citation_keywords'),
                    'citation_doi': raw_details.get('citation_doi'),
                    'citation_issn': raw_details.get('citation_issn'),
                    'citation_abstract': raw_details.get('citation_abstract', ''),
                    'stats_citation_count': citation_count,
                    'stats_reference_count': reference_count,
                }

                # Async index fetch
                indices = await fetch_indices_async(journal_url_base)

                # PDF URL'sini düzelt
                if pdf_url:
                    full_pdf_url = f"https://dergipark.org.tr{pdf_url}" if pdf_url.startswith('/') else pdf_url
                else:
                    full_pdf_url = None

                return {
                    'title': link_info['title'],
                    'url': link_info['url'],
                    'error': None,
                    'details': details,
                    'indices': indices,
                    'pdf_url': full_pdf_url
                }

            except Exception as e:
                print(f"  httpx fetch error: {link_info['url'][:40]}... - {e}", file=sys.stderr)
                return {
                    'title': link_info['title'],
                    'url': link_info['url'],
                    'error': str(e),
                    'details': None,
                    'indices': '',
                    'pdf_url': None
                }

    # Tüm makaleleri paralel olarak çek (httpx ile)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'tr-TR,tr;q=0.9,en;q=0.8',
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [fetch_single(link, client) for link in links_to_process]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Sonuçları işle ve filtrele
    for result in all_results:
        if isinstance(result, Exception):
            print(f"  Gather exception: {result}", file=sys.stderr)
            continue

        # Index filtresi uygula
        indices_str = result.get('indices', '')
        passes = not (
            (index_filter == "tr_dizin_icerenler" and "TR Dizin" not in indices_str) or
            (index_filter == "bos_olmayanlar" and not indices_str)
        )
        if passes:
            results.append(result)
        else:
            print(f"  Filtered out by index_filter: {result.get('url', '')[:50]}", file=sys.stderr)

    return results


async def get_article_references_core(article_url: str) -> dict:
    """
    Makale URL'inden referans listesini çeker.

    Args:
        article_url: DergiPark makale URL'i

    Returns:
        Referans bilgilerini içeren dict
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'tr-TR,tr;q=0.9,en;q=0.8',
    }

    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            response = await client.get(article_url, timeout=30.0)
            response.raise_for_status()
            html_content = response.text

        soup = BeautifulSoup(html_content, 'html5lib')
        meta_tags = soup.find_all('meta')

        # Referansları çek
        reference_tags = [tag for tag in meta_tags if tag.get('name') == 'citation_reference']
        references = [tag.get('content', '').strip() for tag in reference_tags if tag.get('content')]

        # Makale başlığını da al
        title = None
        for tag in meta_tags:
            if tag.get('name') == 'citation_title':
                title = tag.get('content', '').strip()
                break

        return {
            'article_url': article_url,
            'title': title,
            'reference_count': len(references),
            'references': references
        }

    except Exception as e:
        print(f"get_article_references_core error: {e}", file=sys.stderr)
        return {
            'article_url': article_url,
            'error': str(e),
            'reference_count': 0,
            'references': []
        }


# --- CAPTCHA Handling ---
async def _inject_and_submit_captcha(page: Page, token: str, verification_submit_selector: str, captcha_type: str = "recaptcha") -> bool:
    """Helper: Injects token (with events), clicks submit, checks result."""
    if captcha_type == "turnstile":
        injection_target_selector = '[name="cf-turnstile-response"]'
        js_func = """(t)=>{let e=document.querySelector('[name="cf-turnstile-response"]');if(e){console.log('Injecting Turnstile token...');e.value=t;e.dispatchEvent(new Event('input',{bubbles:!0}));e.dispatchEvent(new Event('change',{bubbles:!0}));console.log('Injected/dispatched.');return!0}return console.error('cf-turnstile-response missing!'),!1}"""
    else:
        injection_target_selector = '#g-recaptcha-response'
        js_func = """(t)=>{let e=document.getElementById('g-recaptcha-response');if(e){console.log('Injecting token...');e.value=t;e.dispatchEvent(new Event('input',{bubbles:!0}));e.dispatchEvent(new Event('change',{bubbles:!0}));console.log('Injected/dispatched.');return!0}return console.error('#g-recaptcha-response missing!'),!1}"""

    try:
        print(f"Injecting {captcha_type} token via JS: {token[:15]}...", file=sys.stderr)
        injection_success = await page.evaluate(js_func, token)
        if not injection_success:
            print(f"Error: Injection JS failed, target '{injection_target_selector}' not found?.", file=sys.stderr)
            return False

        print("Token injection script executed successfully.", file=sys.stderr)

        if captcha_type == "turnstile":
            print("Waiting for Turnstile to process token...", file=sys.stderr)
            await asyncio.sleep(random.uniform(2.0, 3.5))
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
                print("Attempted to unhide submit button.", file=sys.stderr)
            except Exception as e:
                print(f"Could not unhide button via JS: {e}", file=sys.stderr)
        else:
            await asyncio.sleep(random.uniform(0.5, 1.2))

        submit_button = page.locator(verification_submit_selector)
        print(f"Looking for submit button ('{verification_submit_selector}')...", file=sys.stderr)
        try:
            await submit_button.wait_for(state="attached", timeout=7000)
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
            await submit_button.wait_for(state="visible", timeout=5000)
            print("Clicking 'Devam Et' button...", file=sys.stderr)
            async with page.expect_navigation(wait_until='load', timeout=35000):
                await submit_button.click()
            print("Submit clicked, navigation finished ('load' event).", file=sys.stderr)

            current_url = page.url
            print(f"URL after submit: {current_url}", file=sys.stderr)
            if "verification" in current_url:
                print("Submission failed: Still on verification page.", file=sys.stderr)
                return False
            else:
                print("Submission seems successful: Navigated away from verification page.", file=sys.stderr)
                return True

        except Exception as e_sub:
            print(f"Error during submit/navigation: {e_sub}", file=sys.stderr)
            return False

    except Exception as e_js:
        print(f"Error executing JS injection: {e_js}", file=sys.stderr)
        return False


async def solve_recaptcha_v2_capsolver_direct_async(page: Page) -> bool:
    """Solves reCAPTCHA v2 by fetching a *new* token from CapSolver."""
    print("CAPTCHA detected. Fetching NEW token from CapSolver...", file=sys.stderr)
    site_key_element_selector = '.g-recaptcha[data-sitekey]'
    verification_submit_selector = 'form[name="search_verification"] button[type="submit"]:has-text("Devam Et")'

    if not CAPSOLVER_API_KEY:
        print("Error: CAPSOLVER_API_KEY environment variable is not set.", file=sys.stderr)
        return False

    try:
        # Fetch Site Key
        site_key = None
        page_url = page.url
        print(f"Waiting for sitekey element on {page_url}...", file=sys.stderr)
        try:
            site_key_element = await page.wait_for_selector(site_key_element_selector, state="attached", timeout=15000)
            site_key = await site_key_element.get_attribute('data-sitekey')
            if not site_key:
                raise ValueError("Sitekey attribute empty.")
            print("Sitekey element found.", file=sys.stderr)
        except (PlaywrightTimeoutError, ValueError, Exception) as e:
            print(f"Error finding/getting sitekey: {e}", file=sys.stderr)
            print("Trying fallback: extracting sitekey from page source...", file=sys.stderr)
            page_content = await page.content()
            sitekey_match = re.search(r'data-sitekey=["\']([^"\']+)["\']', page_content)
            if sitekey_match:
                site_key = sitekey_match.group(1)
                print(f"Sitekey found via regex: {site_key}", file=sys.stderr)
            else:
                print("Fallback failed: No sitekey found in page source.", file=sys.stderr)
                return False

        print(f"Sitekey: {site_key}, URL: {page_url}", file=sys.stderr)

        # Determine CAPTCHA Type
        if site_key.startswith("0x4"):
            task_type = "AntiTurnstileTaskProxyLess"
            captcha_type = "turnstile"
            injection_target_selector = '[name="cf-turnstile-response"]'
            print("Detected Cloudflare Turnstile CAPTCHA", file=sys.stderr)
        else:
            task_type = "ReCaptchaV2TaskProxyless"
            captcha_type = "recaptcha"
            injection_target_selector = '#g-recaptcha-response'
            print("Detected reCAPTCHA v2", file=sys.stderr)

        # Call CapSolver API
        task_payload = {"clientKey": CAPSOLVER_API_KEY, "task": {"type": task_type, "websiteURL": page_url, "websiteKey": site_key}}
        captcha_token = None
        async with httpx.AsyncClient(timeout=20.0) as client:
            print("Sending task to CapSolver...", file=sys.stderr)
            task_id = None
            try:
                create_response = await client.post(CAPSOLVER_CREATE_TASK_URL, json=task_payload)
                create_response.raise_for_status()
                create_result = create_response.json()
                if create_result.get("errorId", 0) != 0:
                    raise ValueError(f"API Error Create: {create_result}")
                task_id = create_result.get("taskId")
                if not task_id:
                    raise ValueError("No Task ID received.")
                print(f"CapSolver Task created: {task_id}", file=sys.stderr)
            except Exception as e:
                print(f"Error Creating CapSolver Task: {e}", file=sys.stderr)
                return False

            # Poll for Result
            start_time = time.time()
            timeout_seconds = 180
            fail_count = 0
            max_failures = 3
            while time.time() - start_time < timeout_seconds:
                await asyncio.sleep(6)
                print(f"Polling CapSolver (ID: {task_id})...", file=sys.stderr)
                result_payload = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
                try:
                    get_response = await client.post(CAPSOLVER_GET_RESULT_URL, json=result_payload, timeout=15)
                    get_response.raise_for_status()
                    get_result = get_response.json()
                    error_code = get_result.get("errorCode", "")
                    status = get_result.get("status")
                    print(f"Task status: {status}", file=sys.stderr)

                    # Definitively failed - don't retry
                    if status in ["failed", "error"] or error_code == "ERROR_CAPTCHA_SOLVE_FAILED":
                        print(f"CapSolver task definitively failed: {get_result.get('errorDescription', 'N/A')}", file=sys.stderr)
                        break

                    if status == "ready":
                        solution = get_result.get("solution")
                        if solution:
                            captcha_token = solution.get("token") or solution.get("gRecaptchaResponse")
                        if captcha_token:
                            print("CapSolver solution received!", file=sys.stderr)
                            break
                except Exception as e:
                    fail_count += 1
                    print(f"Warning: Error Polling CapSolver Task ({fail_count}/{max_failures}): {e}", file=sys.stderr)
                    if fail_count >= max_failures:
                        print("Max polling failures reached, giving up.", file=sys.stderr)
                        break
                    await asyncio.sleep(5)

            if not captcha_token:
                print("Polling timeout or final error getting token.", file=sys.stderr)
                return False

        # Submit with the new token
        print("New token received. Attempting submission...", file=sys.stderr)
        try:
            print(f"Waiting for injection target ('{injection_target_selector}')...", file=sys.stderr)
            await page.wait_for_selector(injection_target_selector, state="attached", timeout=10000)
            print("Injection target found.", file=sys.stderr)
        except PlaywrightTimeoutError:
            print("Timeout waiting for injection target before submission.", file=sys.stderr)
            return False

        submission_successful = await _inject_and_submit_captcha(page, captcha_token, verification_submit_selector, captcha_type)

        if not submission_successful:
            print("Submission failed with the new token from CapSolver.", file=sys.stderr)
        return submission_successful

    except Exception as e:
        print(f"Unexpected error during CAPTCHA solving process: {e}", file=sys.stderr)
        return False


# --- Article Details Fetching ---
async def get_article_details_pw(page: Page, article_url: str, referer_url: Optional[str] = None) -> dict:
    """Fetches metadata and index info for a single article URL with retries."""
    print(f"Fetching details: {article_url}", file=sys.stderr)
    details = {'error': None}
    pdf_url = None
    indices = ''
    retries = 0
    max_retries = 1

    while retries <= max_retries:
        try:
            print(f"Attempt {retries + 1} for {article_url}", file=sys.stderr)
            await page.set_extra_http_headers({'Referer': referer_url or page.url})
            await page.goto(article_url, wait_until='domcontentloaded', timeout=30000)
            html_content = await page.content()

            if any(s in html_content.lower() for s in ["cloudflare", "captcha", "blocked", "erişim engellendi"]):
                print(f"Blocking pattern detected on details page: {article_url}", file=sys.stderr)
                details['error'] = "Blocked"
                break

            soup = BeautifulSoup(html_content, 'html5lib')
            meta_tags = soup.find_all('meta')
            if not meta_tags:
                print(f"No meta tags found (Attempt {retries + 1}).", file=sys.stderr)
                if retries < max_retries:
                    await asyncio.sleep(1.5 * (retries + 1))
                    retries += 1
                    continue
                else:
                    details['error'] = "No meta tags found after retries"
                    break

            raw_details = {tag.get('name'): tag.get('content', '').strip() for tag in meta_tags if tag.get('name')}
            pdf_url = raw_details.get('citation_pdf_url')
            journal_url_base = raw_details.get('DC.Source.URI')

            details = {
                'citation_title': raw_details.get('citation_title'),
                'citation_author': raw_details.get('DC.Creator.PersonalName'),
                'citation_journal_title': raw_details.get('citation_journal_title'),
                'citation_publication_date': raw_details.get('citation_publication_date'),
                'citation_keywords': raw_details.get('citation_keywords'),
                'citation_doi': raw_details.get('citation_doi'),
                'citation_issn': raw_details.get('citation_issn'),
                'citation_abstract': truncate_text(raw_details.get('citation_abstract', ''), 100)
            }

            if journal_url_base:
                try:
                    index_url = f"{journal_url_base.rstrip('/')}/indexes"
                    print(f"Fetching indexes from: {index_url}", file=sys.stderr)
                    await page.goto(index_url, wait_until='domcontentloaded', timeout=12000)
                    index_soup = BeautifulSoup(await page.content(), 'html5lib')
                    indices_list = [
                        i.text.strip() for i in index_soup.select('h5.j-index-listing-index-title') if i.text
                    ]
                    indices = ', '.join(indices_list)
                    print(f"Found indexes: {indices or 'None'}", file=sys.stderr)
                except Exception as e_idx:
                    print(f"Warning: Index page error/timeout for {journal_url_base}: {e_idx}", file=sys.stderr)
                finally:
                    try:
                        if page.url != article_url:
                            print("Navigating back to article page after index check...", file=sys.stderr)
                            await page.goto(article_url, wait_until='domcontentloaded', timeout=10000)
                    except Exception as e_back:
                        print(f"Warning: Failed to navigate back to article page: {e_back}", file=sys.stderr)

            details['error'] = None
            print(f"Successfully fetched details for {article_url}", file=sys.stderr)
            break

        except PlaywrightTimeoutError:
            print(f"Timeout fetching details (Attempt {retries + 1})", file=sys.stderr)
            if retries < max_retries:
                await asyncio.sleep(2 * (retries + 1))
                retries += 1
                continue
            else:
                details['error'] = "Timeout after retries"
                break

        except Exception as e:
            print(f"Error fetching details (Attempt {retries + 1}): {e}", file=sys.stderr)
            if retries < max_retries:
                await asyncio.sleep(2 * (retries + 1))
                retries += 1
                continue
            else:
                details['error'] = f"Error after retries: {e}"
                break

    return {'details': details, 'pdf_url': pdf_url, 'indices': indices}


# --- Article Links with Cache ---
async def get_article_links_with_cache(
    page: Page, search_url: str, cache_key: Any
) -> List[Dict[str, str]]:
    """Gets links. Uses global TTLCache. Fetches if miss. Handles CAPTCHA. Saves cookies if solved."""
    # Check Cache
    try:
        cached_data = links_cache.get(cache_key)
        if cached_data is not None:
            print(f"Cache HIT: Links {str(cache_key)[:100]}...", file=sys.stderr)
            return cached_data
    except Exception as e:
        print(f"Warning: Links cache GET error for key {str(cache_key)[:100]}...: {e}", file=sys.stderr)

    print(f"Cache MISS: Links {str(cache_key)[:100]}... Fetching from DergiPark...", file=sys.stderr)
    article_links = []
    article_card_selector = 'div.card.article-card.dp-card-outline'
    captcha_was_solved = False

    try:
        print(f"Navigating to: {search_url}", file=sys.stderr)
        await page.goto(search_url, wait_until='load', timeout=40000)
        print(f"Nav complete. URL: {page.url}", file=sys.stderr)

        if "/search/verification" in page.url or "verification" in page.url:
            print("CAPTCHA page detected.", file=sys.stderr)
            captcha_passed = await solve_recaptcha_v2_capsolver_direct_async(page)
            if not captcha_passed:
                raise RuntimeError("CAPTCHA solving failed.")
            print("CAPTCHA passed. Waiting for results page to load...", file=sys.stderr)
            captcha_was_solved = True
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await page.wait_for_load_state("networkidle", timeout=20000)
                print("Results page loaded after CAPTCHA.", file=sys.stderr)
            except Exception as e:
                print(f"Load state wait warning: {e}", file=sys.stderr)
        else:
            print("No CAPTCHA detected.", file=sys.stderr)

        current_url = page.url
        if "section=article" not in current_url:
            try:
                print("Not on article section yet, looking for article section link to click...", file=sys.stderr)
                article_section_link = await page.query_selector('a.search-section-link[href*="section=article"]')
                if article_section_link:
                    print("Clicking on article section...", file=sys.stderr)
                    await article_section_link.click()
                    await page.wait_for_load_state("networkidle", timeout=20000)
                    print("Article section loaded.", file=sys.stderr)
                else:
                    print("Article section link not found.", file=sys.stderr)
            except Exception as e:
                print(f"Warning: Could not click article section: {e}", file=sys.stderr)
        else:
            print("Already on article section (URL contains section=article), skipping click to preserve filters.", file=sys.stderr)

        print("Waiting for client-side JavaScript filtering...", file=sys.stderr)
        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle", timeout=10000)
        print("JavaScript filtering should be complete.", file=sys.stderr)

        try:
            print(f"Waiting for article cards with selector: {article_card_selector}", file=sys.stderr)
            await page.wait_for_selector(article_card_selector, state="attached", timeout=15000)
            article_cards = await page.query_selector_all(article_card_selector)
            print(f"{len(article_cards)} article cards found.", file=sys.stderr)
        except PlaywrightTimeoutError:
            page_content = await page.content()
            if "sonuç bulunamadı" in page_content.lower():
                print("No results message detected.", file=sys.stderr)
                article_links = []
            else:
                print("DEBUG: Searching for alternative selectors...", file=sys.stderr)
                alt_cards = await page.query_selector_all("div.card")
                print(f"DEBUG: Found {len(alt_cards)} elements with class 'card'", file=sys.stderr)
                article_divs = await page.query_selector_all("div.article-card")
                print(f"DEBUG: Found {len(article_divs)} elements with class 'article-card'", file=sys.stderr)
                raise RuntimeError("Link extraction failed (timeout finding cards).")

        if article_cards:
            base_page_url = page.url
            for card in article_cards:
                a_tag = await card.query_selector('h5.card-title > a[href]')
                if a_tag:
                    url = await a_tag.get_attribute('href')
                    title = await a_tag.text_content()
                    absolute_url = urllib.parse.urljoin(base_page_url, url.strip())
                    article_links.append({'url': absolute_url, 'title': title.strip() or "N/A"})

        try:
            links_cache[cache_key] = article_links
            print(f"Stored {len(article_links)} links in link cache: {str(cache_key)[:100]}...", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Links cache SET error for key {str(cache_key)[:100]}...: {e}", file=sys.stderr)

        if captcha_was_solved:
            try:
                print("Saving cookies post-CAPTCHA to in-memory cache...", file=sys.stderr)
                browser_context = page.context
                current_cookies = await browser_context.cookies(urls=[page.url])
                if current_cookies:
                    for c in current_cookies:
                        if 'expires' in c and isinstance(c['expires'], float):
                            c['expires'] = int(c['expires'])
                    cookie_cache[COOKIES_CACHE_KEY] = current_cookies
                    print(f"Saved {len(current_cookies)} cookies to cache '{COOKIES_CACHE_KEY}' (TTL: {COOKIES_TTL}s).", file=sys.stderr)
                    save_cookies_to_disk(current_cookies)
                    browser = page.context.browser
                    await browser_pool_manager.mark_authenticated(browser)
                else:
                    print("No relevant cookies found to save.", file=sys.stderr)
            except Exception as e:
                print(f"Warning: Failed to save cookies to cache: {e}", file=sys.stderr)

        return article_links

    except Exception as e:
        print(f"Error in get_article_links_with_cache: {e}\n{traceback.format_exc()}", file=sys.stderr)
        raise RuntimeError(f"Link fetching/processing failed: {e}")


# --- PDF to HTML Conversion ---
async def pdf_to_html_core(pdf_url: str) -> str:
    """Downloads and converts PDF URL to readable HTML."""
    if not pdf_url or not pdf_url.startswith("http"):
        raise ValueError("Invalid or missing PDF URL.")

    # Check cache first
    cached_html = pdf_cache.get(pdf_url)
    if cached_html:
        print(f"PDF cache hit: {pdf_url}", file=sys.stderr)
        return cached_html
    print(f"PDF cache miss: {pdf_url}", file=sys.stderr)

    tmp_name = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True) as client:
            print(f"Downloading PDF from: {pdf_url}", file=sys.stderr)
            response = await client.get(pdf_url)
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            if 'application/pdf' not in content_type:
                print(f"Warning: URL content type ('{content_type}') is not 'application/pdf'.", file=sys.stderr)
            pdf_content = response.content

        if not pdf_content:
            raise ValueError("Downloaded PDF content is empty.")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            await asyncio.to_thread(tmp.write, pdf_content)
            tmp_name = tmp.name

        if not tmp_name or not os.path.exists(tmp_name):
            raise FileNotFoundError("Temporary PDF file not found after write.")

        # Try PyMuPDF first
        markdown_text = ""
        use_mistral_fallback = False

        try:
            print(f"Converting PDF {tmp_name} to text using PyMuPDF (fitz)...", file=sys.stderr)
            markdown_text = await asyncio.to_thread(_extract_text_with_fitz_sync, tmp_name)
            print(f"PyMuPDF result length: {len(markdown_text)}", file=sys.stderr)

            # Check if PyMuPDF result is too short (likely scanned PDF)
            if not markdown_text or len(markdown_text.strip()) < 100:
                print("PyMuPDF returned insufficient text, will try Mistral OCR...", file=sys.stderr)
                use_mistral_fallback = True

        except Exception as convert_err:
            print(f"PyMuPDF (fitz) conversion failed: {convert_err}", file=sys.stderr)
            use_mistral_fallback = True

        # Fallback to Mistral OCR if PyMuPDF failed or returned too little
        if use_mistral_fallback and MISTRAL_API_KEY:
            try:
                print("Attempting Mistral OCR fallback...", file=sys.stderr)
                markdown_text = await _ocr_with_mistral(pdf_url)
                if not markdown_text:
                    markdown_text = "PDF icerigi okunamadi veya bos."
            except Exception as mistral_err:
                print(f"Mistral OCR fallback also failed: {mistral_err}", file=sys.stderr)
                if not markdown_text:
                    markdown_text = "PDF icerigi okunamadi veya bos."
        elif use_mistral_fallback:
            print("Mistral OCR not available (no API key), using PyMuPDF result", file=sys.stderr)
            if not markdown_text:
                markdown_text = "PDF icerigi okunamadi veya bos."

        escaped_pdf_url = html.escape(pdf_url)
        escaped_filename = html.escape(os.path.basename(urllib.parse.urlparse(pdf_url).path) or "document")
        escaped_markdown = html.escape(markdown_text)
        html_content = f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>PDF Icerigi - {escaped_filename}</title><style>body{{font-family:sans-serif;line-height:1.6;padding:20px;max-width:900px;margin:auto;background-color:#f8f9fa;}}pre{{background:#fff;padding:15px;border-radius:5px;overflow-x:auto;white-space:pre-wrap;word-wrap:break-word;border:1px solid #dee2e6;}}a button{{padding:10px 15px;cursor:pointer;}}h1{{text-align:center;}}</style></head><body><h1>Metne Donusturulmus PDF Icerigi</h1><p style="text-align:center;"><a href="{escaped_pdf_url}" target="_blank"><button>Orijinal PDF'yi Goruntule</button></a></p><pre>{escaped_markdown}</pre></body></html>"""

        pdf_cache[pdf_url] = html_content
        return html_content

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        raise RuntimeError(f"PDF download failed ({status_code}) for URL: {pdf_url}")
    except httpx.RequestError as e:
        raise RuntimeError(f"Network error downloading PDF: {e}")
    except Exception as e:
        raise RuntimeError(f"PDF processing failed unexpectedly: {e}")
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
                print(f"Temporary PDF file deleted: {tmp_name}", file=sys.stderr)
            except OSError as e_remove:
                print(f"Error removing temporary file {tmp_name}: {e_remove}", file=sys.stderr)


# --- Core Search Function ---
async def search_articles_core(
    q: Optional[str] = None,
    dergipark_page: int = 1,
    api_page: int = 1,
    sort_by: Optional[Literal["newest", "oldest"]] = None,
    article_type: Optional[str] = None,
    publication_year: Optional[str] = None,
    index_filter: Optional[Literal["tr_dizin_icerenler", "bos_olmayanlar", "hepsi"]] = "hepsi"
) -> dict:
    """
    Core search function for DergiPark articles.

    Returns a dictionary with pagination info and articles list.
    """
    # Construct DergiPark Search URL
    base_url = "https://dergipark.org.tr/tr/search"
    query_params = {}

    query_params['q'] = q if q else '*'
    query_params['section'] = 'article'
    if dergipark_page > 1:
        query_params['page'] = dergipark_page
    if article_type:
        query_params['filter[article_type][]'] = article_type
    if sort_by:
        query_params['sortBy'] = sort_by
    if publication_year:
        query_params['filter[publication_year][]'] = publication_year

    target_search_url = f"{base_url}?{urllib.parse.urlencode(query_params, quote_via=urllib.parse.quote)}"
    page_size = 24
    print(f"Target DP URL: {target_search_url} | API Page: {api_page} | Size: {page_size}", file=sys.stderr)

    browser = context = page = None
    total_items_on_page = 0

    try:
        # Get Browser from Pool
        browser, context, page = await browser_pool_manager.get_browser_and_context()

        # Attempt to Inject Cookies from Cache
        try:
            print(f"Checking in-memory cache for cookies: {COOKIES_CACHE_KEY}", file=sys.stderr)
            saved_cookies = cookie_cache.get(COOKIES_CACHE_KEY)

            if not saved_cookies:
                print("Memory cache miss, checking disk...", file=sys.stderr)
                saved_cookies = load_cookies_from_disk()
                if saved_cookies:
                    cookie_cache[COOKIES_CACHE_KEY] = saved_cookies

            if saved_cookies:
                required_keys = {'name', 'value', 'domain', 'path'}
                valid_cookies = []
                for c in saved_cookies:
                    if required_keys.issubset(c.keys()):
                        if 'expires' in c and isinstance(c['expires'], float):
                            c['expires'] = int(c['expires'])
                        if 'sameSite' in c and c['sameSite'] not in ['Strict', 'Lax', 'None']:
                            del c['sameSite']
                        valid_cookies.append(c)
                if valid_cookies:
                    print(f"Injecting {len(valid_cookies)} cookies from cache...", file=sys.stderr)
                    await context.add_cookies(valid_cookies)
                    print("Cookies injected.", file=sys.stderr)
                else:
                    print("No valid cookies found in cache.", file=sys.stderr)
            else:
                print("No saved cookies found in cache.", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Cookie load/injection error from cache: {e}", file=sys.stderr)

        # Generate cache key
        cache_key_data = {
            'q': q,
            'dergipark_page': dergipark_page,
            'sort_by': sort_by,
            'article_type': article_type,
            'publication_year': publication_year,
        }
        sorted_items = tuple(sorted(cache_key_data.items()))
        links_cache_key = (sorted_items, dergipark_page)

        # Get Article Links
        full_link_list = await get_article_links_with_cache(page, target_search_url, links_cache_key)

        # Process Results & Pagination
        total_items_on_page = len(full_link_list)
        total_api_pages = math.ceil(total_items_on_page / page_size) if total_items_on_page > 0 else 0
        pagination_info = {
            "api_page": api_page,
            "page_size": page_size,
            "total_items_on_dergipark_page": total_items_on_page,
            "total_api_pages_for_dergipark_page": total_api_pages
        }

        if total_items_on_page == 0:
            return {"pagination": pagination_info, "articles": []}

        # Calculate slice
        offset = (api_page - 1) * page_size
        limit = page_size
        links_to_process = full_link_list[offset:offset + limit]
        print(f"Links: Total={total_items_on_page}, Slice={len(links_to_process)} (API Page {api_page}/{total_api_pages})", file=sys.stderr)

        if not links_to_process:
            return {"pagination": pagination_info, "articles": []}

        # Paralel Fetch - Performans iyileştirmesi
        referer_url = page.url
        print(f"Paralel fetch başlıyor: {len(links_to_process)} makale (max_concurrent=3)...", file=sys.stderr)

        articles_details = await fetch_article_details_parallel(
            links_to_process=links_to_process,
            referer_url=referer_url,
            index_filter=index_filter,
            max_concurrent=3
        )

        print(f"Paralel fetch tamamlandı: {len(articles_details)} makale döndü", file=sys.stderr)
        return {"pagination": pagination_info, "articles": articles_details}

    except Exception as e:
        print(f"General search error: {e}\n{traceback.format_exc()}", file=sys.stderr)
        raise RuntimeError(f"Unexpected search error: {e}")
    finally:
        if context or page:
            await close_context_and_page(context, page)


# --- Get Article Details (Single Article) ---
async def get_article_details_core(article_url: str) -> dict:
    """
    Fetches detailed information for a single article URL.

    Returns a dictionary with article metadata.
    """
    if not article_url or not article_url.startswith("http"):
        raise ValueError("Invalid or missing article URL.")

    browser = context = page = None

    try:
        browser, context, page = await browser_pool_manager.get_browser_and_context()

        result = await get_article_details_pw(page, article_url)

        details = result.get('details', {})
        pdf_url = result.get('pdf_url')
        indices = result.get('indices', '')

        if pdf_url:
            full_pdf_url = f"https://dergipark.org.tr{pdf_url}" if pdf_url.startswith('/') else pdf_url
        else:
            full_pdf_url = None

        return {
            'url': article_url,
            'error': details.get('error'),
            'details': details if not details.get('error') else None,
            'indices': indices,
            'pdf_url': full_pdf_url
        }

    except Exception as e:
        print(f"Error fetching article details: {e}", file=sys.stderr)
        raise RuntimeError(f"Failed to fetch article details: {e}")
    finally:
        if context or page:
            await close_context_and_page(context, page)
