from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from typing import List, Optional
import httpx
from bs4 import BeautifulSoup
import io
from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams
import asyncio
from cachetools import TTLCache
from functools import lru_cache
import html

app = FastAPI()

# 24 saat süreyle 1000 öğeyi önbelleğe alabilecek bir TTLCache oluşturun
cache = TTLCache(maxsize=1000, ttl=86400)

@lru_cache(maxsize=100)
async def get_article_details(article_url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(article_url)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    details = {}
    meta_tags = soup.find_all('meta')

    for tag in meta_tags:
        if tag.get('name') and tag.get('content'):
            details[tag.get('name')] = tag.get('content')

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
            title = title_tag.text.strip()
            url = title_tag['href']
            tasks.append(asyncio.create_task(get_article_details(url)))

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
async def search_articles(request: Request):
    base_url = "https://dergipark.org.tr/tr/search"
    query_params = []

    form = await request.form()
    search_params = {key: value for key, value in form.items()}

    for field, value in search_params.items():
        if field not in ['pages', 'sort_by', 'article_type']:
            query_params.append(f"{field}:{value}")

    query_string = "+".join(query_params)
    
    pages = int(search_params.get('pages', 1))
    sort_by = search_params.get('sort_by')
    article_type = search_params.get('article_type')

    # Önbellek anahtarı oluştur
    cache_key = f"{query_string}_{pages}_{sort_by}_{article_type}"
    
    # Önbellekte varsa, önbellekten döndür
    if cache_key in cache:
        return {"articles": cache[cache_key]}

    all_articles = []
    tasks = []

    # Host adını al
    host = str(request.base_url).rstrip('/')

    for page in range(1, pages + 1):
        page_url = f"{base_url}?q={query_string}&section=articles"
        if article_type:
            page_url += f"&aggs%5BarticleType.id%5D%5B0%5D={article_type}"
        if sort_by:
            page_url += f"&sortBy={sort_by}"
        
        tasks.append(asyncio.create_task(fetch_articles(page_url, host)))

    results = await asyncio.gather(*tasks)
    for articles in results:
        all_articles.extend(articles)

    # Sonuçları önbelleğe al
    cache[cache_key] = all_articles

    return {"articles": all_articles}

@app.get("/api/pdf-to-html", response_class=HTMLResponse)
async def pdf_to_html(pdf_url: str):
    # Önbellekte varsa, önbellekten döndür
    if pdf_url in cache:
        return HTMLResponse(content=cache[pdf_url], status_code=200)

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
        cache[pdf_url] = html_content

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
