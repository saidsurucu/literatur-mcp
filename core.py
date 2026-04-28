# -*- coding: utf-8 -*-
"""
DergiPark Scraper Core Module

Bu modül, DergiPark akademik makale arama ve PDF dönüştürme için
temel işlevselliği sağlar. Hem FastAPI hem de FastMCP sunucuları
tarafından kullanılabilir.
"""

import asyncio
import html
import os
import sys
import tempfile
import traceback
import urllib.parse
from typing import List, Optional, Literal, Dict, Any

# --- Gerekli Kütüphaneler ---
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
import fitz
from mistralai import Mistral
from scrapling.fetchers import StealthyFetcher

# --- Configuration ---
ARTICLE_LINKS_TTL = 600
MAX_LINK_LISTS = 100
links_cache = TTLCache(maxsize=MAX_LINK_LISTS, ttl=ARTICLE_LINKS_TTL)

# Mistral OCR Ayarları (PDF fallback)
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")

# PDF Cache
PDF_CACHE_TTL = int(os.getenv("PDF_CACHE_TTL", 86400))
pdf_cache = TTLCache(maxsize=500, ttl=PDF_CACHE_TTL)


# --- Helper Functions ---
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


# --- Scrapling-Based Scraping (StealthyFetcher with auto Cloudflare/Turnstile bypass) ---

async def scrape_article_links(search_url: str, cache_key: Any) -> List[Dict[str, str]]:
    """Fetch article cards from a DergiPark search URL using Scrapling's StealthyFetcher.

    Camoufox (stealth Firefox) handles fingerprinting; solve_cloudflare resolves the
    embedded Turnstile that DergiPark serves on /tr/search/verification.
    """
    cached = links_cache.get(cache_key)
    if cached is not None:
        print(f"Cache HIT: Links {str(cache_key)[:100]}", file=sys.stderr)
        return cached

    print(f"Cache MISS: Fetching {search_url} via Scrapling StealthyFetcher", file=sys.stderr)
    page = None
    last_err: Optional[str] = None
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            page = await StealthyFetcher.async_fetch(
                search_url,
                solve_cloudflare=True,
                network_idle=True,
                wait_selector="div.card.article-card.dp-card-outline, div.alert.alert-warning",
                timeout=180000,
            )
        except Exception as e:
            last_err = f"StealthyFetcher exception: {e}"
            print(f"Attempt {attempt}/{max_attempts} raised: {e}", file=sys.stderr)
            page = None
        else:
            if page.status != 200:
                last_err = f"HTTP {page.status}"
                print(f"Attempt {attempt}/{max_attempts} returned {last_err}", file=sys.stderr)
            else:
                final_url = getattr(page, "url", "") or ""
                if "verification" in final_url:
                    last_err = f"final URL still on /verification ({final_url})"
                    print(f"Attempt {attempt}/{max_attempts} {last_err}", file=sys.stderr)
                else:
                    break
        if attempt < max_attempts:
            await asyncio.sleep(2 + attempt)
    else:
        raise RuntimeError(f"CAPTCHA bypass failed after {max_attempts} attempts: {last_err}")

    article_links: List[Dict[str, str]] = []
    for card in page.css("div.card.article-card.dp-card-outline"):
        href = card.css("h5.card-title > a::attr(href)").get()
        title = card.css("h5.card-title > a::text").get()
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://dergipark.org.tr{href}"
        article_links.append({"url": href, "title": (title or "N/A").strip()})

    print(f"Found {len(article_links)} article links.", file=sys.stderr)
    try:
        links_cache[cache_key] = article_links
    except Exception as e:
        print(f"Warning: links_cache SET error: {e}", file=sys.stderr)
    return article_links


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
    page: int = 1,
    sort_by: Optional[Literal["newest", "oldest"]] = None,
    article_type: Optional[str] = None,
    publication_year: Optional[str] = None,
    index_filter: Optional[Literal["tr_dizin_icerenler", "bos_olmayanlar", "hepsi"]] = "hepsi"
) -> dict:
    """
    Core search function for DergiPark articles.
    Uses Scrapling's StealthyFetcher to bypass DergiPark's verification page.

    Returns a dictionary with pagination info and articles list.
    """
    # Construct DergiPark Search URL
    base_url = "https://dergipark.org.tr/tr/search"
    query_params = {}

    query_params['q'] = q if q else '*'
    query_params['section'] = 'article'
    if page > 1:
        query_params['page'] = page
    if article_type:
        query_params['filter[article_type][]'] = article_type
    if sort_by:
        query_params['sortBy'] = sort_by
    if publication_year:
        query_params['filter[publication_year][]'] = publication_year

    target_search_url = f"{base_url}?{urllib.parse.urlencode(query_params, quote_via=urllib.parse.quote)}"
    print(f"Target DP URL: {target_search_url} | Page: {page}", file=sys.stderr)

    try:
        # Generate cache key
        cache_key_data = {
            'q': q,
            'page': page,
            'sort_by': sort_by,
            'article_type': article_type,
            'publication_year': publication_year,
        }
        sorted_items = tuple(sorted(cache_key_data.items()))
        links_cache_key = (sorted_items, page)

        full_link_list = await scrape_article_links(target_search_url, links_cache_key)

        # Process Results & Pagination
        total_items = len(full_link_list)
        pagination_info = {
            "page": page,
            "per_page": 24,
            "count": total_items
        }

        if total_items == 0:
            return {"pagination": pagination_info, "articles": []}

        print(f"Found {total_items} article links", file=sys.stderr)

        # Paralel Fetch - httpx ile makale detaylarını çek
        referer_url = target_search_url
        print(f"Fetching {total_items} articles (max_concurrent=3)...", file=sys.stderr)

        articles_details = await fetch_article_details_parallel(
            links_to_process=full_link_list,
            referer_url=referer_url,
            index_filter=index_filter,
            max_concurrent=3
        )

        print(f"Fetch complete: {len(articles_details)} articles", file=sys.stderr)
        return {"pagination": pagination_info, "articles": articles_details}

    except Exception as e:
        print(f"General search error: {e}\n{traceback.format_exc()}", file=sys.stderr)
        raise RuntimeError(f"Unexpected search error: {e}")
