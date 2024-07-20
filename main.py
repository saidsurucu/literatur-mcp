from fastapi import FastAPI, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional, Literal
import httpx
from bs4 import BeautifulSoup
import io
from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams
import asyncio
from cachetools import TTLCache
import html
import os

app = FastAPI()

@app.get("/gizlilik", response_class=HTMLResponse)
async def get_gizlilik():
    file_path = os.path.join("gizlilik", "index.html")
    with open(file_path, "r", encoding="utf-8") as file:
        return HTMLResponse(content=file.read(), status_code=200)

# Cache only for pdf-to-html
pdf_cache = TTLCache(maxsize=1000, ttl=86400)

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
    pages: int = 1
    sort_by: Optional[Literal["newest", "oldest"]] = None
    article_type: Optional[
        Literal[
            "54", "56", "58", "55", "60", "65", "57", "1", "5",
            "62", "73", "2", "10", "59", "66", "72",
        ]
    ] = None

async def get_article_details(article_url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(article_url)
    soup = BeautifulSoup(response.text, 'html.parser')
   
    details = {}
    meta_tags = soup.find_all('meta')

    for tag in meta_tags:
        if tag.get('name') and tag.get('content'):
            details[tag.get('name')] = tag.get('content')

    # Remove unwanted keys
    for key in [
        'Diplab.Event.ArticleView', 'citation_firstpage', 'citation_lastpage',
        'DC.Language', 'DC.Source.URI', 'viewport', 'generator', 'citation_volume',
        'citation_issue', 'stats_total_article_view', 'stats_total_article_download',
        'stats_total_article_favorite', 'stats_updated_at', 'stats_trdizin_citation_count',
        'DC.Source.Issue', 'DC.Source.Volume', 'citation_volume', 'citation_issue',
        'DC.Source', 'citation_journal_title', 'DC.Creator.PersonalName'
    ]:
        details.pop(key, None)

    # PDF URL'sini 'citation_pdf_url' meta etiketinden al
    pdf_url = details.get('citation_pdf_url')
    if pdf_url:
        details['pdf_url'] = pdf_url

    return {'details': details}

async def fetch_articles(page_url: str, host: str) -> List[dict]:
    async with httpx.AsyncClient() as client:
        response = await client.get(page_url)
    soup = BeautifulSoup(response.text, 'html.parser')

    article_cards = soup.find_all('div', class_='card article-card dp-card-outline')
    tasks = []

    for card in article_cards:
        title_tag = card.find('h5', class_='card-title').find('a')
        if title_tag:
            url = title_tag['href']
            tasks.append(get_article_details(url))

    details_list = await asyncio.gather(*tasks)
   
    return [
        {
            'title': card.find('h5', class_='card-title').find('a').text.strip(),
            'url': card.find('h5', class_='card-title').find('a')['href'],
            'details': details.get('details', {}),
            'readable_pdf': f"{host}/api/pdf-to-html?pdf_url={details.get('details', {}).get('pdf_url', '')}"
        }
        for card, details in zip(article_cards, details_list)
        if card.find('h5', class_='card-title').find('a')
    ]

@app.post("/api/search")
async def search_articles(request: Request, search_params: SearchParams = Body(...)):
    base_url = "https://dergipark.org.tr/tr/search"
    query_params = []

    for field, value in search_params.dict(exclude_unset=True).items():
        if field not in ['pages', 'sort_by', 'article_type']:
            query_params.append(f"{field}:{value}")

    query_string = "+".join(query_params)
   
    all_articles = []
    tasks = []

    # Host adını al
    host = str(request.base_url).rstrip('/')

    for page in range(1, search_params.pages + 1):
        page_url = f"{base_url}?q={query_string}&section=articles"
        if search_params.article_type:
            page_url += f"&aggs%5BarticleType.id%5D%5B0%5D={search_params.article_type}"
        if search_params.sort_by:
            page_url += f"&sortBy={search_params.sort_by}"
       
        tasks.append(fetch_articles(page_url, host))

    results = await asyncio.gather(*tasks)
    for articles in results:
        all_articles.extend(articles)

    return {"articles": all_articles}

@app.get("/api/pdf-to-html", response_class=HTMLResponse)
async def pdf_to_html(pdf_url: str):
    # Önbellekte varsa, önbellekten döndür
    if pdf_url in pdf_cache:
        return HTMLResponse(content=pdf_cache[pdf_url], status_code=200)

    try:
        # PDF'yi indir
        async with httpx.AsyncClient() as client:
            response = await client.get(pdf_url)
            response.raise_for_status()
       
        # PDF içeriğini oku ve metne dönüştür
        pdf_content = io.BytesIO(response.content)
        output_string = io.StringIO()
        extract_text_to_fp(pdf_content, output_string, laparams=LAParams(), codec='utf-8')
        text = output_string.getvalue()
       
        # Metni basit HTML'e dönüştür
        html_content = f"""
        <!DOCTYPE html>
        <html lang="tr">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>PDF Content</title>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; padding: 20px; max-width: 800px; margin: 0 auto; }}
                p {{ margin-bottom: 15px; text-align: justify; }}
            </style>
        </head>
        <body>
            <pre>{html.escape(text)}</pre>
        </body>
        </html>
        """
       
        # Sonucu önbelleğe al
        pdf_cache[pdf_url] = html_content

        return HTMLResponse(content=html_content, status_code=200)

    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"PDF indirilemedi: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF dönüştürme hatası: {str(e)}")

# Vercel için gerekli yapılandırma
from mangum import Mangum
handler = Mangum(app)

# Lokalde çalıştırmak için
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
