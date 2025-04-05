import asyncio
import html
import io
import os
import random
import tempfile
import traceback
from typing import List, Optional, Literal

import aiofiles
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Body, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from markitdown import MarkItDown
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

# --- FastAPI App Initialization ---
app = FastAPI()

# --- HTTP Client for PDF Downloads ---
pdf_http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=5.0),
)

# --- Cache for PDF-to-HTML ---
pdf_cache = TTLCache(maxsize=500, ttl=86400)

# --- Pydantic Models ---
class SearchParams(BaseModel):
    title: Optional[str] = None
    running_title: Optional[str] = None
    journal: Optional[str] = None
    issn: Optional[str] = None
    eissn: Optional[str] = None
    abstract: Optional[str] = None
    keywords: Optional[str] = None
    doi: Optional[str] = None
    doi_url: Optional[str] = None
    doi_prefix: Optional[str] = None
    author: Optional[str] = None
    orcid: Optional[str] = None
    institution: Optional[str] = None
    translator: Optional[str] = None
    pubyear: Optional[str] = None
    citation: Optional[str] = None
    page: int = 1
    sort_by: Optional[Literal["newest", "oldest"]] = None
    article_type: Optional[
        Literal[
            "54", "56", "58", "55", "60", "65", "57", "1", "5",
            "62", "73", "2", "10", "59", "66", "72",
        ]
    ] = None
    index_filter: Optional[Literal["tr_dizin_icerenler", "bos_olmayanlar", "hepsi"]] = "hepsi"

# --- Utility Functions ---
def truncate_text(text: str, word_limit: int) -> str:
    if not text:
        return ""
    words = text.split()
    if len(words) > word_limit:
        return ' '.join(words[:word_limit]) + '...'
    return text

# --- Playwright Functions ---
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36',
]

async def get_playwright_page(p):
    """Initializes Playwright browser and returns a new page."""
    try:
        # Using the chromium bundled with the official Playwright Docker image
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-dev-shm-usage'] # Recommended for Docker/CI environments
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale='tr-TR',
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        return browser, context, page
    except Exception as e:
        print(f"Playwright initialization error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Browser could not be initialized: {e}")

async def close_playwright(browser, context, page):
    """Safely closes Playwright resources."""
    try:
        if page and not page.is_closed(): await page.close()
        if context: await context.close()
        if browser: await browser.close()
    except Exception as e:
        print(f"Error closing Playwright resources: {e}") # Log error but don't crash

async def get_article_details_pw(page, article_url: str, referer_url: Optional[str] = None) -> dict:
    """Fetches article details using Playwright."""
    print(f"Fetching details with Playwright: {article_url}")
    details = {'error': None}
    pdf_url = None
    indices = ''

    try:
        if referer_url:
            await page.set_extra_http_headers({'Referer': referer_url})

        await page.goto(article_url, wait_until='domcontentloaded', timeout=20000) # Increased timeout slightly

        html_content = await page.content()
        soup = BeautifulSoup(html_content, 'html5lib')

        meta_tags = soup.find_all('meta')
        raw_details = {}
        journal_url_base = None
        for tag in meta_tags:
            name = tag.get('name')
            content = tag.get('content')
            if name and content:
                raw_details[name] = content
                if name == 'DC.Source.URI':
                    journal_url_base = content

        pdf_url = raw_details.get('citation_pdf_url')

        details['citation_title'] = raw_details.get('citation_title')
        details['citation_author'] = raw_details.get('DC.Creator.PersonalName')
        details['citation_journal_title'] = raw_details.get('citation_journal_title')
        details['citation_publication_date'] = raw_details.get('citation_publication_date')
        details['citation_abstract'] = truncate_text(raw_details.get('citation_abstract', ''), 100)
        details['citation_keywords'] = raw_details.get('citation_keywords')
        details['citation_doi'] = raw_details.get('citation_doi')
        details['citation_issn'] = raw_details.get('citation_issn')

        if journal_url_base:
            index_url = f"{journal_url_base}/indexes"
            print(f"Fetching indexes with Playwright: {index_url}")
            try:
                await page.goto(index_url, wait_until='domcontentloaded', timeout=15000)
                index_html = await page.content()
                index_soup = BeautifulSoup(index_html, 'html5lib')
                index_elements = index_soup.select('table.journal-index-listing h5.j-index-listing-index-title')
                indices_list = [index.text.strip() for index in index_elements]
                indices = ', '.join(indices_list)
                print(f"Found indexes: {indices}")
                # Consider going back if necessary: await page.go_back(wait_until='domcontentloaded')
            except PlaywrightTimeoutError:
                print(f"Index page timed out: {index_url}")
                # Don't mark the whole detail fetch as error, just missing indices
            except Exception as e:
                print(f"Index page error: {e}")

    except PlaywrightTimeoutError:
        print(f"Article detail page timed out: {article_url}")
        details['error'] = "Article detail page timed out"
    except Exception as e:
        print(f"Error fetching article details: {e}")
        print(traceback.format_exc())
        details['error'] = f"Unexpected error fetching details: {e}"

    return {'details': details, 'pdf_url': pdf_url, 'indices': indices}

async def fetch_articles_pw(page, page_url: str, host: str, index_filter: str) -> List[dict]:
    """Fetches a list of articles from a search results page using Playwright."""
    print(f"Fetching articles with Playwright: {page_url}")
    articles_data = []

    try:
        await page.goto(page_url, wait_until='domcontentloaded', timeout=30000) # Longer timeout for search page

        page_content_lower = (await page.content()).lower()
        if 'g-recaptcha' in page_content_lower or 'recaptcha' in page_content_lower:
             print("CAPTCHA element potentially detected on search page.")
             # Playwright might handle invisible reCAPTCHA automatically.
             # If visible CAPTCHA appears, this will likely fail later.

        article_cards = await page.query_selector_all('div.card.article-card.dp-card-outline')

        if not article_cards:
            print("No article cards found on the page.")
            # Check if it's a valid page but simply no results, or if the selector is broken.
            # You might want to inspect the page content here for error messages from DergiPark.
            if "sonuç bulunamadı" in page_content_lower or "no results found" in page_content_lower:
                 print("Search returned no results.")
                 return [] # Return empty list for no results
            else:
                 print("Could not find article cards, maybe page structure changed or blocked?")
                 # Consider raising an error or returning specific status
                 # raise HTTPException(status_code=404, detail="Article cards selector failed on page.")
                 return [] # Or return empty

        print(f"{len(article_cards)} article cards found.")

        tasks = []
        # Create URLs first to avoid issues with page navigation during iteration
        article_links = []
        for card_handle in article_cards:
            title_tag_handle = await card_handle.query_selector('h5.card-title a')
            if title_tag_handle:
                url = await title_tag_handle.get_attribute('href')
                title = await title_tag_handle.text_content()
                if url:
                    # Ensure URL is absolute
                    base_url_parts = page.url.split('/')[:3]
                    base_url = '/'.join(base_url_parts)
                    if url.startswith('/'):
                        url = base_url + url
                    article_links.append({'url': url, 'title': title.strip()})


        # Fetch details sequentially or with limited concurrency if needed
        # Sequential fetching is safer for stability and avoiding rate limits
        details_list = []
        for i, link_info in enumerate(article_links):
            print(f"  Processing article {i+1}/{len(article_links)}: {link_info['url']}")
            details = await get_article_details_pw(page, link_info['url'], referer_url=page_url)
            details_list.append(details)
            await asyncio.sleep(random.uniform(0.5, 1.5)) # Delay between detail fetches


        # Combine results
        for link_info, details_result in zip(article_links, details_list):
             pdf_url = details_result.get('pdf_url')
             article_info = {
                 'title': link_info['title'],
                 'url': link_info['url'],
                 'details': details_result.get('details', {'error': 'Details could not be retrieved'}),
                 'indices': details_result.get('indices', ''),
                 'readable_pdf': f"{host}/api/pdf-to-html?pdf_url={pdf_url}" if pdf_url else None
             }

             passes_filter = True
             if index_filter == "tr_dizin_icerenler" and "TR Dizin" not in article_info['indices']:
                 passes_filter = False
             elif index_filter == "bos_olmayanlar" and not article_info['indices']:
                 passes_filter = False

             if passes_filter:
                  articles_data.append(article_info)

    except PlaywrightTimeoutError:
        print(f"Search results page timed out: {page_url}")
        raise HTTPException(status_code=504, detail="Search results page timed out")
    except Exception as e:
        print(f"Error fetching article list: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to fetch article list: {e}")

    return articles_data

# --- FastAPI Endpoints ---

@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """Health check endpoint for Docker/Render."""
    return {"status": "ok"}

@app.get("/gizlilik", response_class=HTMLResponse)
async def get_gizlilik():
    file_path = os.path.join("gizlilik", "index.html")
    if not os.path.exists(file_path):
         # Ensure the 'gizlilik' folder and 'index.html' are copied in Dockerfile
         print(f"Error: Gizlilik file not found at {file_path}")
         raise HTTPException(status_code=404, detail="Privacy policy file not found.")
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as file:
            content = await file.read()
        return HTMLResponse(content=content, status_code=200)
    except Exception as e:
         print(f"Error reading gizlilik file: {e}")
         raise HTTPException(status_code=500, detail="Error reading privacy policy file.")


@app.post("/api/search")
async def search_articles(request: Request, search_params: SearchParams = Body(...)):
    base_url = "https://dergipark.org.tr/tr/search"
    query_params = []
    for field, value in search_params.dict(exclude_unset=True).items():
        if field not in ['page', 'sort_by', 'article_type', 'index_filter'] and value:
            query_params.append(f"{field}:{value}")

    query_string = "+".join(query_params)
    host = str(request.base_url).rstrip('/')
    page_url = f"{base_url}/{search_params.page}?q={query_string}§ion=articles"
    if search_params.article_type:
        page_url += f"&aggs%5BarticleType.id%5D%5B0%5D={search_params.article_type}"
    if search_params.sort_by:
        page_url += f"&sortBy={search_params.sort_by}"

    print(f"Constructed Search URL: {page_url}")

    articles = []
    browser = None
    context = None
    page = None

    try:
        async with async_playwright() as p:
            browser, context, page = await get_playwright_page(p)
            articles = await fetch_articles_pw(page, page_url, host, search_params.index_filter)
    except Exception as e:
         if not isinstance(e, HTTPException):
             print(f"General error during search: {e}")
             print(traceback.format_exc())
             raise HTTPException(status_code=500, detail=f"General search error: {e}")
         else:
             raise e # Re-raise if it's already an HTTPException
    finally:
        print("Attempting to close Playwright resources...")
        await close_playwright(browser, context, page)
        print("Playwright resources closed.")

    return {"articles": articles}


@app.get("/api/pdf-to-html", response_class=HTMLResponse)
async def pdf_to_html(pdf_url: str):
    if not pdf_url or not pdf_url.startswith("http"):
         raise HTTPException(status_code=400, detail="Invalid PDF URL.")

    if pdf_url in pdf_cache:
        print(f"PDF cache hit: {pdf_url}")
        return HTMLResponse(content=pdf_cache[pdf_url], status_code=200)
    print(f"PDF cache miss, downloading: {pdf_url}")

    try:
        async with pdf_http_client as client: # Ensure client is used in context
            response = await client.get(pdf_url)
            response.raise_for_status()
            pdf_content = response.content

        tmp_name = None
        try:
             # Use a context manager for the temporary file
             with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                 tmp.write(pdf_content)
                 tmp_name = tmp.name

             if not os.path.exists(tmp_name):
                  raise FileNotFoundError(f"Temporary file creation failed: {tmp_name}")

             md = MarkItDown()
             # Ensure MarkItDown can handle the path correctly
             markdown_result = md.convert(tmp_name)

        finally:
             if tmp_name and os.path.exists(tmp_name):
                  try:
                      os.remove(tmp_name)
                      print(f"Temporary PDF file deleted: {tmp_name}")
                  except OSError as e:
                      print(f"Error removing temporary file {tmp_name}: {e}")

        html_content = f"""
        <!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>PDF to Markdown</title><style>body {{ font-family: sans-serif; line-height: 1.6; padding: 20px; max-width: 800px; margin: 0 auto; }} pre {{ background: #f4f4f4; padding: 15px; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }} .pdf-button {{ margin-bottom: 20px; }} .pdf-button a button {{ font-size: 16px; padding: 10px 15px; background-color: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; }} .pdf-button a button:hover {{ background-color: #0056b3; }}</style></head><body><div class="pdf-button"><a href="{html.escape(pdf_url)}" target="_blank"><button>Orijinal PDF'yi Görüntüle</button></a></div><pre>{html.escape(markdown_result.text_content)}</pre></body></html>
        """

        pdf_cache[pdf_url] = html_content
        return HTMLResponse(content=html_content, status_code=200)

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        detail = f"PDF download failed ({status_code}): {pdf_url}"
        # Return specific codes for common errors
        if status_code == 404:
            detail = f"PDF not found at URL ({status_code}): {pdf_url}"
        elif status_code == 403:
             detail = f"Access denied downloading PDF ({status_code}): {pdf_url}"
        print(detail)
        raise HTTPException(status_code=status_code, detail=detail)
    except httpx.RequestError as e:
        print(f"Network error downloading PDF: {e}")
        raise HTTPException(status_code=504, detail=f"Network error downloading PDF: {e}")
    except FileNotFoundError as e:
         print(f"File system error during PDF processing: {e}")
         raise HTTPException(status_code=500, detail="Internal error processing PDF file.")
    except Exception as e:
        print(f"PDF conversion error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"PDF conversion failed: {e}")


# --- Local Development Runner ---
if __name__ == "__main__":
    import uvicorn
    print("Starting application locally on http://127.0.0.1:8000")
    print("Ensure Playwright browsers are installed locally if not using Docker for tests:")
    print("python -m playwright install --with-deps chromium")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)