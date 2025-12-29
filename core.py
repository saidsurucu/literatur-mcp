# -*- coding: utf-8 -*-
"""
DergiPark Scraper Core Module

Bu modül, DergiPark akademik makale arama ve PDF dönüştürme için
temel işlevselliği sağlar. Hem FastAPI hem de FastMCP sunucuları
tarafından kullanılabilir.
"""

import asyncio
import html
import json
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
from mistralai import Mistral
from browser_use import Browser as BrowserUseBrowser

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

# Browser Ayarları (browser-use)
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]
HEADLESS_MODE = False

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
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
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


# --- Browser-Use Manager ---
class BrowserUseManager:
    """Browser manager using browser-use library (verify-lawyer pattern)."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self):
        """Initialize browser-use manager."""
        print("=== BROWSER-USE MANAGER READY ===", file=sys.stderr)
        self._initialized = True

    async def create_browser(self) -> BrowserUseBrowser:
        """Create a new browser-use instance with verify-lawyer parameters."""
        browser = BrowserUseBrowser(
            headless=False,
            window_size={'width': 1, 'height': 1},
            args=['--window-position=-2400,-2400']
        )
        return browser

    async def cleanup(self):
        """Cleanup (no-op for browser-use, each browser is closed after use)."""
        print("Browser-use manager cleanup complete.", file=sys.stderr)


# Global browser-use manager instance
browser_manager = BrowserUseManager()


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
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, verify=False) as client:
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
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, verify=False) as client:
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


# --- browser-use Based Scraping (verify-lawyer pattern) ---

async def scrape_article_links_browser_use(search_url: str, cache_key: Any) -> List[Dict[str, str]]:
    """
    browser-use ile DergiPark'tan makale linklerini çeker.
    CAPTCHA varsa: 5s Turnstile auto-pass + CapSolver fallback.
    verify-lawyer pattern'i ile birebir aynı parametreler.
    """
    # Check Cache first
    try:
        cached_data = links_cache.get(cache_key)
        if cached_data is not None:
            print(f"Cache HIT: Links {str(cache_key)[:100]}...", file=sys.stderr)
            return cached_data
    except Exception as e:
        print(f"Warning: Links cache GET error: {e}", file=sys.stderr)

    print(f"Cache MISS: Fetching from DergiPark with browser-use...", file=sys.stderr)
    browser = None
    article_links = []

    try:
        # browser-use Browser (verify-lawyer ile birebir aynı parametreler)
        browser = BrowserUseBrowser(
            headless=False,
            window_size={'width': 1, 'height': 1},
            args=['--window-position=-2400,-2400']
        )
        await browser.start()
        print("browser-use started.", file=sys.stderr)

        # Search sayfasına git
        print(f"Navigating to: {search_url}", file=sys.stderr)
        page = await browser.new_page(search_url)
        await asyncio.sleep(2)

        # URL kontrolü - CAPTCHA sayfasında mıyız?
        current_url = await page.evaluate("() => window.location.href")
        print(f"Current URL: {current_url}", file=sys.stderr)

        if "verification" in current_url:
            print("CAPTCHA page detected. Trying Turnstile auto-pass...", file=sys.stderr)

            # 5 saniye bekle - Turnstile auto-pass
            await asyncio.sleep(5)

            # Submit butonuna tıkla
            await page.evaluate("""() => {
                const btn = document.querySelector('form[name="search_verification"] button[type="submit"]');
                if (btn) {
                    btn.classList.remove('kt-hidden');
                    btn.style.display = 'block';
                    btn.disabled = false;
                    btn.click();
                }
            }""")

            # Navigation bekle
            await asyncio.sleep(3)

            # URL kontrolü
            current_url = await page.evaluate("() => window.location.href")
            print(f"URL after Turnstile attempt: {current_url}", file=sys.stderr)

            if "verification" in current_url:
                # Turnstile başarısız, CapSolver dene
                print("Turnstile auto-pass failed, trying CapSolver...", file=sys.stderr)
                captcha_solved = await solve_captcha_with_capsolver_browser_use(browser, page)
                if not captcha_solved:
                    raise RuntimeError("CAPTCHA solving failed (Turnstile + CapSolver).")
                current_url = await page.evaluate("() => window.location.href")
            else:
                print("Turnstile auto-pass SUCCESS!", file=sys.stderr)

        # Artık search sonuçları sayfasındayız
        print(f"On results page: {current_url}", file=sys.stderr)

        # Article section'a tıkla (eğer gerekiyorsa)
        if "section=article" not in current_url:
            print("Clicking on article section...", file=sys.stderr)
            await page.evaluate("""() => {
                const link = document.querySelector('a.search-section-link[href*="section=article"]');
                if (link) link.click();
            }""")
            await asyncio.sleep(3)

        # JavaScript filtering bekle
        print("Waiting for JavaScript filtering...", file=sys.stderr)
        await asyncio.sleep(3)

        # Makale kartlarını çek
        print("Extracting article links...", file=sys.stderr)
        article_links_raw = await page.evaluate("""() => {
            const cards = document.querySelectorAll('div.card.article-card.dp-card-outline');
            const links = [];
            cards.forEach(card => {
                const a = card.querySelector('h5.card-title > a[href]');
                if (a) {
                    links.push({
                        url: a.href,
                        title: a.textContent.trim() || 'N/A'
                    });
                }
            });
            return links;
        }""")

        # browser-use page.evaluate JSON string dönüyor - parse et
        if isinstance(article_links_raw, str):
            try:
                article_links = json.loads(article_links_raw)
                print(f"Parsed JSON: {len(article_links)} article links.", file=sys.stderr)
            except json.JSONDecodeError as e:
                print(f"JSON parse error: {e}", file=sys.stderr)
                article_links = []
        else:
            article_links = article_links_raw if article_links_raw else []

        print(f"Found {len(article_links)} article links.", file=sys.stderr)

        # Cache'e kaydet
        try:
            links_cache[cache_key] = article_links
            print(f"Stored {len(article_links)} links in cache.", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Cache SET error: {e}", file=sys.stderr)

        # Cookie'leri kaydet
        try:
            cdp_cookies = await browser.cookies()
            if cdp_cookies:
                cookies = []
                for c in cdp_cookies:
                    if 'dergipark' in c.get('domain', ''):
                        cookies.append({
                            'name': c.get('name', ''),
                            'value': c.get('value', ''),
                            'domain': c.get('domain', ''),
                            'path': c.get('path', '/'),
                        })
                if cookies:
                    cookie_cache[COOKIES_CACHE_KEY] = cookies
                    save_cookies_to_disk(cookies)
                    print(f"Saved {len(cookies)} cookies.", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Cookie save error: {e}", file=sys.stderr)

        return article_links

    except Exception as e:
        print(f"browser-use scraping error: {e}\n{traceback.format_exc()}", file=sys.stderr)
        raise RuntimeError(f"Scraping failed: {e}")
    finally:
        if browser:
            try:
                await browser.stop()
                print("browser-use stopped.", file=sys.stderr)
            except Exception:
                pass


async def solve_captcha_with_capsolver_browser_use(browser: BrowserUseBrowser, page) -> bool:
    """CapSolver ile CAPTCHA çözer (browser-use page için)."""
    print("Solving CAPTCHA with CapSolver...", file=sys.stderr)

    if not CAPSOLVER_API_KEY:
        print("Error: CAPSOLVER_API_KEY not set.", file=sys.stderr)
        return False

    try:
        # Sitekey ve URL al
        page_url = await page.evaluate("() => window.location.href")
        page_content = await page.evaluate("() => document.documentElement.outerHTML")

        # Turnstile sitekey bul
        sitekey_match = re.search(r'data-sitekey=["\']([^"\']+)["\']', page_content)
        if not sitekey_match:
            print("No sitekey found.", file=sys.stderr)
            return False

        site_key = sitekey_match.group(1)
        print(f"Sitekey: {site_key}", file=sys.stderr)

        # CAPTCHA türünü belirle
        is_turnstile = 'cf-turnstile' in page_content or 'challenges.cloudflare.com' in page_content
        task_type = "AntiTurnstileTaskProxyLess" if is_turnstile else "ReCaptchaV2TaskProxyLess"
        print(f"CAPTCHA type: {'Turnstile' if is_turnstile else 'reCAPTCHA v2'}", file=sys.stderr)

        # CapSolver'a task gönder
        async with httpx.AsyncClient(timeout=120) as client:
            create_payload = {
                "clientKey": CAPSOLVER_API_KEY,
                "task": {
                    "type": task_type,
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                }
            }
            resp = await client.post(CAPSOLVER_CREATE_TASK_URL, json=create_payload)
            result = resp.json()

            if result.get("errorId") != 0:
                print(f"CapSolver create error: {result}", file=sys.stderr)
                return False

            task_id = result.get("taskId")
            print(f"CapSolver task created: {task_id}", file=sys.stderr)

            # Sonucu bekle
            for _ in range(60):
                await asyncio.sleep(2)
                get_payload = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
                resp = await client.post(CAPSOLVER_GET_RESULT_URL, json=get_payload)
                result = resp.json()

                if result.get("status") == "ready":
                    token = result.get("solution", {}).get("token")
                    if token:
                        print("CapSolver token received.", file=sys.stderr)

                        # Token'ı inject et
                        if is_turnstile:
                            await page.evaluate(f"""() => {{
                                const input = document.querySelector('[name="cf-turnstile-response"]');
                                if (input) input.value = "{token}";
                                const hidden = document.querySelector('input[name="cf-turnstile-response"]');
                                if (hidden) hidden.value = "{token}";
                            }}""")
                        else:
                            await page.evaluate(f"""() => {{
                                document.querySelector('#g-recaptcha-response').value = "{token}";
                            }}""")

                        await asyncio.sleep(2)

                        # Submit butonuna tıkla
                        await page.evaluate("""() => {
                            const btn = document.querySelector('form[name="search_verification"] button[type="submit"]');
                            if (btn) {
                                btn.classList.remove('kt-hidden');
                                btn.style.display = 'block';
                                btn.disabled = false;
                                btn.click();
                            }
                        }""")

                        await asyncio.sleep(3)

                        # Başarılı mı?
                        new_url = await page.evaluate("() => window.location.href")
                        if "verification" not in new_url:
                            print("CapSolver CAPTCHA solved!", file=sys.stderr)
                            return True
                        else:
                            print("CapSolver: still on verification page.", file=sys.stderr)
                            return False

                elif result.get("status") == "failed":
                    print(f"CapSolver task failed: {result}", file=sys.stderr)
                    return False

            print("CapSolver timeout.", file=sys.stderr)
            return False

    except Exception as e:
        print(f"CapSolver error: {e}", file=sys.stderr)
        return False


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
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True, verify=False) as client:
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
    Uses browser-use for scraping (verify-lawyer pattern).

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

    try:
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

        # Get Article Links using browser-use
        full_link_list = await scrape_article_links_browser_use(target_search_url, links_cache_key)

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

        # Paralel Fetch - httpx ile makale detaylarını çek
        referer_url = target_search_url
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
