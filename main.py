from fastapi import FastAPI, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional, Literal
import httpx
from bs4 import BeautifulSoup
import io
import asyncio
from cachetools import TTLCache
import html
import os
import aiofiles
from markitdown import MarkItDown   # <-- Yeni eklendi

app = FastAPI()

# Singleton HTTP client with optimized settings
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0, connect=5.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
)

@app.get("/gizlilik", response_class=HTMLResponse)
async def get_gizlilik():
    file_path = os.path.join("gizlilik", "index.html")
    async with aiofiles.open(file_path, "r", encoding="utf-8") as file:
        return HTMLResponse(content=await file.read(), status_code=200)

# Cache only for pdf-to-html
pdf_cache = TTLCache(maxsize=1000, ttl=86400)

# --------------------------------------------------------
# Arama için kullanılan Pydantic model
# --------------------------------------------------------
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

# --------------------------------------------------------
# Metin kısaltma fonksiyonu
# --------------------------------------------------------
def truncate_text(text: str, word_limit: int) -> str:
    words = text.split()
    if len(words) > word_limit:
        return ' '.join(words[:word_limit]) + '...'
    return text

# --------------------------------------------------------
# Makale detaylarını parse eden fonksiyon
# --------------------------------------------------------
async def get_article_details(article_url: str) -> dict:
    print(f"Fetching article details for URL: {article_url}")
    try:
        response = await http_client.get(article_url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        print(f"Error fetching article details: {e}")
        return {'details': {}, 'pdf_url': None, 'indices': ''}

    soup = BeautifulSoup(response.text, 'html.parser')
   
    details = {}
    meta_tags = soup.find_all('meta')
    journal_url_base = None

    for tag in meta_tags:
        if tag.get('name') and tag.get('content'):
            details[tag.get('name')] = tag.get('content')
            if tag.get('name') == 'DC.Source.URI':
                journal_url_base = tag.get('content')
    
    pdf_url = details.pop('citation_pdf_url', None)
    
    # Gereksiz anahtarları temizle
    for key in [
        'Diplab.Event.ArticleView', 'citation_firstpage', 'citation_lastpage',
        'DC.Language', 'DC.Source.URI', 'viewport', 'generator', 'citation_volume',
        'citation_issue', 'stats_total_article_view', 'stats_total_article_download',
        'stats_total_article_favorite', 'stats_updated_at', 'stats_trdizin_citation_count',
        'DC.Source.Issue', 'DC.Source.Volume', 'citation_volume', 'citation_issue',
        'DC.Source', 'citation_journal_title', 'DC.Creator.PersonalName',
        'citation_language', 'DC.Type', 'DC.Identifier.pageNumber', 'DC.Identifier.URI',
        'citation_reference', 'citation_journal_abbrev', 'citation_abstract_html_url',
        'DC.Type.articleType', 'DC.Identifier', 'citation_funding_source',
        'stats_trdizin_citation_updated_at', 'DC.Identifier.DOI', 'DC.Title', 'stats_trdizin_url',
        'DC.Source.ISSN', 'citation_issn', 'citation_keywords', 'citation_author_orcid', 'citation_title',
        'citation_doi', 'citation_author_institution'
    ]:
        details.pop(key, None)

    # Abstract'ı kısalt
    if 'citation_abstract' in details:
        details['citation_abstract'] = truncate_text(details['citation_abstract'], 100)

    # Dergi index bilgisi
    indices = []
    if journal_url_base:
        journal_url = f"{journal_url_base}/indexes"
        print(f"Fetching journal indices from URL: {journal_url}")
        try:
            index_response = await http_client.get(journal_url)
            index_response.raise_for_status()
            index_soup = BeautifulSoup(index_response.text, 'html.parser')
            index_elements = index_soup.select('table.journal-index-listing h5.j-index-listing-index-title')
            indices = [index.text.strip() for index in index_elements]
            print(f"Found indices: {indices}")
        except httpx.HTTPError as e:
            print(f"Error fetching journal indices: {e}")
    
    return {'details': details, 'pdf_url': pdf_url, 'indices': ', '.join(indices)}

# --------------------------------------------------------
# Belirli bir sayfadaki makaleleri listeleyen fonksiyon
# --------------------------------------------------------
async def fetch_articles(page_url: str, host: str, index_filter: str) -> List[dict]:
    print(f"Fetching articles from URL: {page_url}")
    try:
        response = await http_client.get(page_url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        print(f"Error fetching articles: {e}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    article_cards = soup.find_all('div', class_='card article-card dp-card-outline')
    tasks = []

    for card in article_cards:
        title_tag = card.find('h5', class_='card-title').find('a')
        if title_tag:
            url = title_tag['href']
            tasks.append(get_article_details(url))

    details_list = await asyncio.gather(*tasks)
   
    articles = [
        {
            'title': card.find('h5', class_='card-title').find('a').text.strip(),
            'url': card.find('h5', class_='card-title').find('a')['href'],
            'details': details.get('details', {}),
            'indices': details.get('indices', ''),
            'readable_pdf': f"{host}/api/pdf-to-html?pdf_url={details['pdf_url']}" if details['pdf_url'] else None
        }
        for card, details in zip(article_cards, details_list)
        if card.find('h5', class_='card-title').find('a')
    ]

    # index_filter'a göre filtrele
    if index_filter == "tr_dizin_icerenler":
        articles = [article for article in articles if "TR Dizin" in article['indices']]
    elif index_filter == "bos_olmayanlar":
        articles = [article for article in articles if article['indices']]

    return articles

# --------------------------------------------------------
# Arama endpoint (POST)
# --------------------------------------------------------
@app.post("/api/search")
async def search_articles(request: Request, search_params: SearchParams = Body(...)):
    base_url = "https://dergipark.org.tr/tr/search"
    query_params = []

    for field, value in search_params.dict(exclude_unset=True).items():
        if field not in ['page', 'sort_by', 'article_type', 'index_filter']:
            query_params.append(f"{field}:{value}")

    query_string = "+".join(query_params)
   
    # Construct URL for the requested page
    host = str(request.base_url).rstrip('/')
    page_url = f"{base_url}/{search_params.page}?q={query_string}&section=articles"
    
    if search_params.article_type:
        page_url += f"&aggs%5BarticleType.id%5D%5B0%5D={search_params.article_type}"
    if search_params.sort_by:
        page_url += f"&sortBy={search_params.sort_by}"
   
    print(f"Constructed page URL: {page_url}")
    articles = await fetch_articles(page_url, host, search_params.index_filter)
    return {"articles": articles}

# --------------------------------------------------------
# PDF -> Markdown Endpoint (Aynı isim, farklı içerik)
# --------------------------------------------------------
@app.get("/api/pdf-to-html", response_class=HTMLResponse)
async def pdf_to_html(pdf_url: str):
    """
    Eskiden PDF'yi HTML'e dönüştüren endpoint,
    artık pdfminer kullanmadan markitdown ile doğrudan
    PDF'den Markdown üretiyor ve HTMLResponse ile dönüyor.
    """

    # Eğer cache'de varsa
    if pdf_url in pdf_cache:
        return HTMLResponse(content=pdf_cache[pdf_url], status_code=200)

    try:
        # PDF'yi indir
        response = await http_client.get(pdf_url)
        response.raise_for_status()
        pdf_content = response.content

        # MarkItDown, bir dosya yoluna ihtiyaç duyduğu için geçici dosya oluşturuyoruz
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_content)
            tmp_name = tmp.name
        
        try:
            # MarkItDown ile PDF'den Markdown'a dönüştür
            md = MarkItDown()
            markdown_result = md.convert(tmp_name)
        finally:
            # Geçici dosyayı silelim
            os.remove(tmp_name)

        # Markdown içeriği HTML içerisinde göstermek ( <pre> ... </pre> )
        html_content = f"""
        <!DOCTYPE html>
        <html lang="tr">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>PDF to Markdown</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    padding: 20px;
                    max-width: 800px;
                    margin: 0 auto;
                }}
                pre {{
                    background: #f8f8f8;
                    padding: 10px;
                    border-radius: 5px;
                    overflow-x: auto;
                }}
                .pdf-button {{
                    margin-bottom: 20px;
                }}
                .pdf-button button {{
                    font-size: 18px;
                    padding: 10px 20px;
                    background-color: #007BFF;
                    color: white;
                    border: none;
                    border-radius: 5px;
                    cursor: pointer;
                }}
                .pdf-button button:hover {{
                    background-color: #0056b3;
                }}
            </style>
        </head>
        <body>
            <div class="pdf-button">
                <a href="{pdf_url}" target="_blank">
                    <button>Orijinal PDF'yi Görüntüle</button>
                </a>
            </div>
            <pre>{html.escape(markdown_result.text_content)}</pre>
        </body>
        </html>
        """

        # Sonucu cache'e yazalım
        pdf_cache[pdf_url] = html_content

        return HTMLResponse(content=html_content, status_code=200)

    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"PDF indirilemedi: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF dönüştürme hatası: {str(e)}")

# --------------------------------------------------------
# Vercel / Serverless uyumluluğu
# --------------------------------------------------------
from mangum import Mangum
handler = Mangum(app)

# --------------------------------------------------------
# Lokalde çalıştırmak için
# --------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
