# -*- coding: utf-8 -*-
import asyncio
import hashlib
import html
import io
import json
import math
import os
import random
import tempfile
import traceback
import urllib.parse
import time
from typing import List, Optional, Literal, Dict, Any
# from contextlib import asynccontextmanager # Removed as lifespan removed

# --- Removed Redis Imports ---

import aiofiles
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Body, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from markitdown import MarkItDown
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page, BrowserContext

from pydantic import BaseModel, Field

# --- Configuration ---
# Removed Redis settings

# --- In-Memory Cache Configuration ---
COOKIES_TTL = 1800; MAX_COOKIE_SETS = 10
ARTICLE_LINKS_TTL = 600; MAX_LINK_LISTS = 100
cookie_cache = TTLCache(maxsize=MAX_COOKIE_SETS, ttl=COOKIES_TTL)
links_cache = TTLCache(maxsize=MAX_LINK_LISTS, ttl=ARTICLE_LINKS_TTL)
COOKIES_CACHE_KEY = "dergipark_scraper:session:last_cookies"

# CapSolver settings
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "CAP-1E1D6F5F97285F22927DFC04FA04116A4A5FCC9211E28F36195D8372CC7D6739")
CAPSOLVER_CREATE_TASK_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"

# Playwright settings
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]
HEADLESS_MODE = os.getenv("HEADLESS_MODE", "true").lower() == "true"

# Other settings
PDF_CACHE_TTL = int(os.getenv("PDF_CACHE_TTL", 86400))
pdf_cache = TTLCache(maxsize=500, ttl=PDF_CACHE_TTL)


# --- FastAPI App Initialization ---
app = FastAPI(
    title="DergiPark Scraper API (In-Memory Cache)",
    version="1.11.1", # Version bumped for syntax fix
    description="API to search DergiPark articles with pagination. Uses in-memory cache for session cookies and links (requires single worker process).",
)

# --- API Key Check ---
if CAPSOLVER_API_KEY == "YOUR_CAPSOLVER_API_KEY_HERE" or not CAPSOLVER_API_KEY:
    print("\n" + "="*60 + "\nUYARI: CAPSOLVER_API_KEY ortam değişkeni ayarlanmamış...\n" + "="*60 + "\n")

# --- HTTP Client ---
pdf_http_client = httpx.AsyncClient( timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True )

# --- Pydantic Models ---
class SearchParams(BaseModel):
    title: Optional[str] = Field(None); running_title: Optional[str] = Field(None); journal: Optional[str] = Field(None); issn: Optional[str] = Field(None); eissn: Optional[str] = Field(None); abstract: Optional[str] = Field(None); keywords: Optional[str] = Field(None); doi: Optional[str] = Field(None); doi_url: Optional[str] = Field(None); doi_prefix: Optional[str] = Field(None); author: Optional[str] = Field(None); orcid: Optional[str] = Field(None); institution: Optional[str] = Field(None); translator: Optional[str] = Field(None); pubyear: Optional[str] = Field(None); citation: Optional[str] = Field(None)
    dergipark_page: int = Field(default=1, ge=1); api_page: int = Field(default=1, ge=1); page_size: int = Field(default=5, ge=1, le=20)
    sort_by: Optional[Literal["newest", "oldest"]] = Field(None)
    article_type: Optional[Literal["54", "56", "58", "55", "60", "65", "57", "1", "5", "62", "73", "2", "10", "59", "66", "72"]] = Field(None)
    index_filter: Optional[Literal["tr_dizin_icerenler", "bos_olmayanlar", "hepsi"]] = Field(default="hepsi")

# --- Utility Functions ---
def truncate_text(text: str, word_limit: int) -> str:
    if not text: return ""; words = text.split(); return ' '.join(words[:word_limit]) + '...' if len(words) > word_limit else text

def generate_links_cache_key(params: SearchParams) -> Any:
    key_data = params.model_dump(exclude={'api_page', 'page_size'}, exclude_unset=True, mode='python')
    sorted_items = tuple(sorted(key_data.items()))
    cache_key = (sorted_items, params.dergipark_page)
    return cache_key

# --- Playwright Functions ---
async def get_playwright_page(p: async_playwright) -> tuple[Any, BrowserContext, Page]:
    try: print(f"Launching browser (Headless: {HEADLESS_MODE})..."); browser = await p.chromium.launch(headless=HEADLESS_MODE, args=['--disable-dev-shm-usage','--no-sandbox']); context = await browser.new_context(user_agent=random.choice(USER_AGENTS), locale='tr-TR', viewport={'width': 1920, 'height': 1080}, ignore_https_errors=True); page = await context.new_page(); print("Browser page created."); return browser, context, page
    except Exception as e: print(f"FATAL: PW init error: {e}"); raise HTTPException(503, f"Browser service unavailable: {e}")

async def close_playwright(browser, context, page):
    print("--- PW Cleanup ---"); closed=[]
    try:
        if page and not page.is_closed(): await page.close(); closed.append("Page")
        if context: await context.close(); closed.append("Context")
        if browser and browser.is_connected(): await browser.close(); closed.append("Browser")
        print(f"PW close attempted: {', '.join(closed)}")
    except Exception as e:
        if "Target page" not in str(e): print(f"Warning: Error closing PW: {e}")
    print("--- PW Cleanup Finished ---")


# --- CORRECTED get_article_details_pw Function ---
async def get_article_details_pw(page: Page, article_url: str, referer_url: Optional[str] = None) -> dict:
    """Fetches metadata and index info for a single article URL."""
    print(f"Fetching details: {article_url}")
    details={'error':None}; pdf_url=None; indices=''; retries=0
    while retries <= 1: # Max 1 retry
        try:
            print(f"Attempt {retries + 1} for {article_url}")
            await page.set_extra_http_headers({'Referer': referer_url or page.url})
            # Use 'domcontentloaded' for faster initial load, check content after
            await page.goto(article_url, wait_until='domcontentloaded', timeout=30000)
            html_content = await page.content() # Get content after load

            # Check for blocking patterns
            if any(s in html_content.lower() for s in ["cloudflare", "captcha", "blocked", "erişim engellendi"]):
                print(f"Blocking pattern detected on details page: {article_url}")
                details['error'] = "Blocked"
                break # Exit loop, no retry needed if blocked

            # Check for meta tags
            soup=BeautifulSoup(html_content, 'html5lib'); meta_tags=soup.find_all('meta')
            if not meta_tags:
                print(f"No meta tags found (Attempt {retries + 1}).")
                if retries < 1:
                    await asyncio.sleep(1.5); retries += 1; continue # Retry
                else:
                    details['error'] = "No meta tags"; break # Exit loop

            # Extract details
            raw={t.get('name'): t.get('content','').strip() for t in meta_tags if t.get('name')};
            pdf_url=raw.get('citation_pdf_url')
            journal_url=raw.get('DC.Source.URI')
            details={
                'citation_title':raw.get('citation_title'),
                'citation_author':raw.get('DC.Creator.PersonalName'),
                'citation_journal_title':raw.get('citation_journal_title'),
                'citation_publication_date':raw.get('citation_publication_date'),
                'citation_keywords':raw.get('citation_keywords'),
                'citation_doi':raw.get('citation_doi'),
                'citation_issn':raw.get('citation_issn'),
                'citation_abstract':truncate_text(raw.get('citation_abstract',''), 100)
            }

            # Fetch indexes (optional)
            if journal_url:
                try:
                    index_url=f"{journal_url.rstrip('/')}/indexes"
                    await page.goto(index_url, wait_until='domcontentloaded', timeout=12000)
                    index_soup=BeautifulSoup(await page.content(),'html5lib')
                    indices=', '.join([i.text.strip() for i in index_soup.select('h5.j-index-listing-index-title') if i.text])
                    print(f"Indexes: {indices or 'None'}")
                except Exception as e_idx:
                    print(f"Index page error/timeout for {journal_url}: {e_idx}")
                finally: # Ensure navigation back
                   try:
                       if page.url != article_url:
                           print("Navigating back to article page after index check...")
                           await page.goto(article_url, wait_until='domcontentloaded', timeout=10000)
                   except Exception as e_back:
                       print(f"Warning: Failed to navigate back to article page: {e_back}")

            # Success
            details['error']=None
            print(f"Successfully fetched details for {article_url}")
            break # Exit loop on success

        except PlaywrightTimeoutError:
            print(f"Timeout fetching details (Attempt {retries + 1})")
            if retries < 1:
                await asyncio.sleep(2); retries += 1; continue # Retry
            else:
                details['error'] = "Timeout"; break # Exit loop

        except Exception as e:
            print(f"Error fetching details (Attempt {retries + 1}): {e}")
            # print(traceback.format_exc()) # Optional detailed traceback
            if retries < 1:
                await asyncio.sleep(2); retries += 1; continue # Retry
            else:
                details['error'] = f"Error: {e}"; break # Exit loop

    # End of while loop
    return {'details': details, 'pdf_url': pdf_url, 'indices': indices}
# --- END CORRECTED Function ---


async def _inject_and_submit_captcha(page: Page, token: str, verification_submit_selector: str) -> bool:
    # (Implementation remains the same)
    injection_target_selector = '#g-recaptcha-response'
    try:
        print(f"Injecting token via JS: {token[:15]}..."); js_func="""(t)=>{let e=document.getElementById('g-recaptcha-response');if(e){console.log('Injecting token...');e.value=t;e.dispatchEvent(new Event('input',{bubbles:!0}));e.dispatchEvent(new Event('change',{bubbles:!0}));console.log('Injected/dispatched.');return!0}return console.error('#g-recaptcha-response missing!'),!1}"""
        if not await page.evaluate(js_func, token): return False
        print("Token injection script succeeded."); await asyncio.sleep(random.uniform(0.5, 1.2))
        submit_button=page.locator(verification_submit_selector); print(f"Clicking submit button ('{verification_submit_selector}')...")
        try:
            await submit_button.wait_for(state="visible", timeout=7000)
            async with page.expect_navigation(wait_until='load', timeout=35000): await submit_button.click()
            print("Submit clicked, navigation finished ('load')."); current_url=page.url; print(f"URL after submit: {current_url}")
            return "/search-verification" not in current_url
        except Exception as e_sub: print(f"Submit/Nav Error: {e_sub}"); return False
    except Exception as e_js: print(f"JS Injection Error: {e_js}"); return False


async def solve_recaptcha_v2_capsolver_direct_async(page: Page) -> bool:
    """
    Solves reCAPTCHA v2 when encountered by fetching a *new* token from CapSolver.
    Waits for necessary elements before interacting.
    (Removed redis_client parameter and token reuse logic).
    """
    print("CAPTCHA detected. Fetching NEW token from CapSolver...")
    site_key_element_selector = '.g-recaptcha[data-sitekey]'
    injection_target_selector = '#g-recaptcha-response'
    verification_submit_selector = 'form[name="search_verification"] button[type="submit"]:has-text("Devam Et")'

    # Check API Key early
    if not CAPSOLVER_API_KEY or CAPSOLVER_API_KEY == "YOUR_CAPSOLVER_API_KEY_HERE":
        print("Error: CAPSOLVER_API_KEY is not configured.")
        return False

    # Outer try block to catch unexpected errors during the whole process
    try:
        # --- Fetch Site Key ---
        site_key = None
        page_url = page.url # Get URL before potential errors
        print(f"Waiting for sitekey element ('{site_key_element_selector}') on {page_url}...")
        try:
            # Use inner try specifically for Playwright interactions for sitekey
            site_key_element = await page.wait_for_selector(site_key_element_selector, state="attached", timeout=15000)
            site_key = await site_key_element.get_attribute('data-sitekey')
            if not site_key:
                # Raise ValueError if attribute is empty or None
                raise ValueError("Sitekey attribute is empty or None.")
            print("Sitekey element found and attribute retrieved.")
        except (PlaywrightTimeoutError, ValueError, Exception) as e:
            # Catch specific Playwright/ValueError or any other exception during sitekey fetch
             print(f"Error finding/getting sitekey: {e}")
             return False # Critical error, cannot proceed

        print(f"Sitekey: {site_key}, URL: {page_url}")

        # --- Call CapSolver API for NEW token ---
        task_payload = { "clientKey": CAPSOLVER_API_KEY, "task": { "type": "ReCaptchaV2TaskProxyless", "websiteURL": page_url, "websiteKey": site_key } }
        captcha_token = None
        async with httpx.AsyncClient(timeout=20.0) as client:
            # --- Create Task ---
            print("Sending task to CapSolver...")
            task_id = None
            try:
                create_response = await client.post(CAPSOLVER_CREATE_TASK_URL, json=task_payload)
                create_response.raise_for_status() # Raise HTTP errors
                create_result = create_response.json()
                if create_result.get("errorId", 0) != 0:
                    raise ValueError(f"API Error Create: {create_result}")
                task_id = create_result.get("taskId")
                if not task_id:
                    raise ValueError("No Task ID received from CapSolver.")
                print(f"CapSolver Task created: {task_id}")
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError, Exception) as e:
                # Catch HTTP, network, value errors or others during task creation
                print(f"Error Creating CapSolver Task: {e}")
                return False # Cannot proceed

            # --- Poll for Result ---
            start_time = time.time(); timeout_seconds = 180
            while time.time() - start_time < timeout_seconds:
                await asyncio.sleep(6)
                print(f"Polling CapSolver (ID: {task_id})...")
                result_payload = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
                try:
                    get_response = await client.post(CAPSOLVER_GET_RESULT_URL, json=result_payload, timeout=15)
                    get_response.raise_for_status() # Raise HTTP errors
                    get_result = get_response.json()
                    if get_result.get("errorId", 0) != 0:
                        raise ValueError(f"API Error Poll: {get_result}")
                    status = get_result.get("status")
                    print(f"Task status: {status}")
                    if status == "ready":
                        solution = get_result.get("solution")
                        captcha_token = solution.get("gRecaptchaResponse") if solution else None
                        if captcha_token:
                            print("CapSolver solution received!")
                            break # Exit polling loop
                        else:
                            raise ValueError("Task ready but no token in solution.")
                    elif status in ["failed", "error"]:
                        raise ValueError(f"CapSolver task failed/errored: {get_result.get('errorDescription', 'N/A')}")
                    # If status is 'processing' or unknown, continue loop
                except (httpx.HTTPStatusError, httpx.RequestError, ValueError, Exception) as e:
                    # Catch errors during polling, log, and continue loop (allow retries)
                    print(f"Warning: Error Polling CapSolver Task (will retry): {e}")
                    await asyncio.sleep(5) # Wait a bit longer before next poll after error

            # Check if token was obtained after loop
            if not captcha_token:
                print("Polling timeout or final error getting token from CapSolver.")
                return False

        # --- Submit with the new token ---
        print("New token received. Attempting submission...")
        try:
            # Wait for injection target element
            print(f"Waiting for injection target ('{injection_target_selector}')...")
            await page.wait_for_selector(injection_target_selector, state="attached", timeout=10000)
            print("Injection target found.")
        except PlaywrightTimeoutError:
            # If target element doesn't appear, cannot submit
            print(f"Timeout waiting for injection target ('{injection_target_selector}') before submission.")
            return False

        # Call the helper function to inject and submit
        submission_successful = await _inject_and_submit_captcha(page, captcha_token, verification_submit_selector)

        if submission_successful:
            print("Successfully submitted with the new CAPTCHA token.")
        else:
            print("Submission failed with the new token from CapSolver.")

        return submission_successful # Return final status

    # --- Outer Exception Handling ---
    except Exception as e:
        # Catch any other unexpected error during the whole process
        print(f"Unexpected error during CAPTCHA solving process: {e}")
        print(traceback.format_exc()) # Print full traceback for debugging
        return False
async def get_article_links_with_cache(
    page: Page, search_url: str, cache_key: Any
) -> List[Dict[str, str]]:
    """
    Gets article links. Uses global TTLCache. Fetches if miss.
    Handles CAPTCHA (always fetches new token). Saves cookies to global cookie_cache if CAPTCHA solved.
    """
    # 1. Check Cache
    try:  # <--- TRY Block Start
        # Use global links_cache (TTLCache)
        cached_data = links_cache.get(cache_key)
        if cached_data is not None: # Check explicitly for None as empty list is valid cache value
            print(f"Cache HIT: Links {str(cache_key)[:100]}...") # Log truncated key
            return cached_data # Return early if hit
    except Exception as e:  # <--- EXCEPT Block (Must align with TRY)
        # Log the error but continue, treating it as a cache miss
        print(f"Warning: Links cache GET error for key {str(cache_key)[:100]}...: {e}")

    # Execution continues here ONLY if cache miss or cache get error
    print(f"Cache MISS: Links {str(cache_key)[:100]}... Fetching from DergiPark...")
    article_links = []; article_card_selector = 'div.card.article-card.dp-card-outline'; captcha_was_solved = False

    # Main try block for the fetching process
    try: # <--- Main Fetching TRY Block Start
        # 2. Navigate
        print(f"Navigating to: {search_url}"); await page.goto(search_url, wait_until='load', timeout=40000); print(f"Nav complete. URL: {page.url}")

        # 3. Handle CAPTCHA
        if "/search-verification" in page.url:
            print("CAPTCHA page detected.");
            # Pass page directly, no redis_client needed
            captcha_passed = await solve_recaptcha_v2_capsolver_direct_async(page) # Assumes solve function is defined correctly elsewhere
            if not captcha_passed: raise HTTPException(429, "CAPTCHA solving failed.")
            print("CAPTCHA passed. Checking results page..."); captcha_was_solved = True
            try: await page.wait_for_selector(article_card_selector, state="visible", timeout=15000); print("Results page elements confirmed.")
            except PlaywrightTimeoutError: raise HTTPException(500, f"Failed to find results elements after CAPTCHA. URL: {page.url}")
        else: print("No CAPTCHA detected.")

        # 4. Extract Links
        try: # Inner try for finding cards
            await page.wait_for_selector(article_card_selector, state="attached", timeout=10000)
            article_cards = await page.query_selector_all(article_card_selector); print(f"{len(article_cards)} article cards found.")
        except PlaywrightTimeoutError: # Inner except for finding cards
            if "sonuç bulunamadı" in (await page.content()).lower(): print("No results msg found."); article_links = []
            else: raise HTTPException(500, "Link extraction failed (timeout finding cards).")

        if article_cards: # Process cards if found
            base_page_url = page.url
            for card in article_cards:
                a_tag = await card.query_selector('h5.card-title > a[href]')
                if a_tag: url = await a_tag.get_attribute('href'); title = await a_tag.text_content(); article_links.append({'url': urllib.parse.urljoin(base_page_url, url.strip()), 'title': title.strip() or "N/A"})

        # 5. Cache Links in global TTLCache
        try: # Inner try for setting cache
            links_cache[cache_key] = article_links
            print(f"Stored {len(article_links)} links in link cache: {str(cache_key)[:100]}...")
        except Exception as e: # Inner except for setting cache
            print(f"Warning: Links cache SET error for key {str(cache_key)[:100]}...: {e}")

        # 6. Save Cookies to global TTLCache if CAPTCHA was solved
        if captcha_was_solved:
            try: # Inner try for saving cookies
                print("Saving cookies post-CAPTCHA to in-memory cache..."); browser_context = page.context
                current_cookies = await browser_context.cookies(urls=[page.url])
                if current_cookies:
                    for c in current_cookies: # Clean expires format
                        if 'expires' in c and isinstance(c['expires'], float): c['expires'] = int(c['expires'])
                    cookie_cache[COOKIES_CACHE_KEY] = current_cookies
                    print(f"Saved {len(current_cookies)} cookies to in-memory cache '{COOKIES_CACHE_KEY}' (TTL: {COOKIES_TTL}s).")
                else: print("No cookies found to save.")
            except Exception as e: # Inner except for saving cookies
                print(f"Warning: Failed to save cookies to in-memory cache: {e}")

        return article_links # Return the fetched links

    except Exception as e: # <--- Main Fetching EXCEPT Block (Must align with the main TRY)
        # Catch any exception during the fetching process (Navigation, CAPTCHA, Extraction etc.)
        if isinstance(e, HTTPException): raise e # Re-raise HTTP exceptions
        print(f"Error in get_article_links_with_cache: {e}\n{traceback.format_exc()}");
        raise HTTPException(500, f"Link fetching failed: {e}") # Wrap other exceptions

# --- FastAPI Endpoints ---

@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """API health check."""
    return {"status": "ok"} # Removed redis check

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
    """Search DergiPark articles. Uses in-memory cache for session cookies and links. REQUIRES SINGLE WORKER."""
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
    browser = context = page = playwright_instance = None; total_items_on_page = 0

    try:
        # --- Start Playwright ---
        playwright_instance = await async_playwright().start()
        browser, context, page = await get_playwright_page(playwright_instance)

        # --- Attempt to Inject Cookies from In-Memory Cache ---
        try:
            print(f"Checking in-memory cache for cookies: {COOKIES_CACHE_KEY}")
            saved_cookies = cookie_cache.get(COOKIES_CACHE_KEY)
            if saved_cookies:
                 required_keys={'name','value','domain','path'}; valid_cookies=[]
                 for c in saved_cookies:
                     if required_keys.issubset(c.keys()):
                         if 'expires' in c and isinstance(c['expires'],float): c['expires']=int(c['expires'])
                         if 'sameSite' in c and c['sameSite'] not in ['Strict','Lax','None']: del c['sameSite']
                         valid_cookies.append(c)
                 if valid_cookies: print(f"Injecting {len(valid_cookies)} cookies..."); await context.add_cookies(valid_cookies); print("Cookies injected.")
                 else: print("No valid cookies found in cache.")
            else: print("No saved cookies found in cache.")
        except Exception as e: print(f"Warning: Cookie load/injection error from cache: {e}")

        # --- Get Article Links ---
        links_cache_key = generate_links_cache_key(search_params)
        full_link_list = await get_article_links_with_cache(page, target_search_url, links_cache_key)

        # --- Process Results & Pagination ---
        total_items_on_page = len(full_link_list)
        total_api_pages = math.ceil(total_items_on_page / search_params.page_size) if total_items_on_page > 0 else 0
        pagination_info = {"api_page": search_params.api_page, "page_size": search_params.page_size, "total_items_on_dergipark_page": total_items_on_page, "total_api_pages_for_dergipark_page": total_api_pages}
        if total_items_on_page == 0: return JSONResponse({"pagination": pagination_info, "articles": []})
        offset = (search_params.api_page - 1) * search_params.page_size; limit = search_params.page_size
        links_to_process = full_link_list[offset : offset + limit]
        print(f"Links: Total={total_items_on_page}, Slice={len(links_to_process)} (API Page {search_params.api_page}/{total_api_pages})")
        if not links_to_process: return JSONResponse({"pagination": pagination_info, "articles": []})

        # --- Fetch Details for Slice ---
        articles_details = []
        print(f"Fetching details for {len(links_to_process)} articles...")
        referer_url = page.url
        for i, link_info in enumerate(links_to_process):
            print(f"  Processing {offset + i + 1}/{total_items_on_page}: {link_info['url']}")
            details_result = await get_article_details_pw(page, link_info['url'], referer_url=referer_url)
            pdf_url=details_result.get('pdf_url'); article_details=details_result.get('details',{}); indices_str=details_result.get('indices','')
            if article_details.get('error'): article_data={'title':link_info['title'],'url':link_info['url'],'error':f"Detail error: {article_details['error']}",'details':None,'indices':'','readable_pdf':None}
            else: article_data={'title':link_info['title'],'url':link_info['url'],'error':None,'details':article_details,'indices':indices_str,'readable_pdf':f"{host}/api/pdf-to-html?pdf_url={urllib.parse.quote(pdf_url)}" if pdf_url else None}
            passes = not ((search_params.index_filter=="tr_dizin_icerenler" and "TR Dizin" not in indices_str) or \
                          (search_params.index_filter=="bos_olmayanlar" and not indices_str))
            if passes: articles_details.append(article_data)
            else: print(f"  Filtered out: {link_info['url']}")
            await asyncio.sleep(random.uniform(0.8, 1.8))

        return JSONResponse({"pagination": pagination_info, "articles": articles_details})

    except HTTPException as e: return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    except Exception as e: print(f"General search error: {e}\n{traceback.format_exc()}"); return JSONResponse(status_code=500, content={"detail": f"Unexpected search error: {e}"})
    finally: await close_playwright(browser, context, page);


@app.get("/api/pdf-to-html", response_class=HTMLResponse)
async def pdf_to_html(pdf_url: str):
    """Downloads and converts PDF URL to readable HTML."""
    # (Implementation remains the same)
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
    print("--- Starting FastAPI Application (In-Memory Cache Strategy) ---")
    print("--- WARNING: This strategy requires running with a SINGLE WORKER PROCESS (--workers 1) ---")
    print(f"Headless mode: {HEADLESS_MODE}")
    print("API available at: http://127.0.0.1:8000")
    print(f"CapSolver Key Provided: {'Yes' if CAPSOLVER_API_KEY != 'YOUR_CAPSOLVER_API_KEY_HERE' and CAPSOLVER_API_KEY else 'NO'}")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, workers=1) # Explicitly set workers=1