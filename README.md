# DergiPark MCP Sunucusu

[DergiPark](https://dergipark.org.tr) üzerinden Türkçe akademik dergi makalelerini aramak ve analiz etmek için MCP (Model Context Protocol) sunucusu.

## Özellikler

- **Makale Arama**: Yıl, tür, dizin ve sıralama filtrelerine göre akademik makaleleri arayın
- **PDF'den HTML'e**: Akademik PDF'leri okunabilir HTML formatına dönüştürün
- **Akıllı OCR**: Taranmış PDF'ler için otomatik Mistral OCR fallback
- **CAPTCHA Çözme**: CapSolver API ile otomatik Turnstile/reCAPTCHA çözümü
- **Cookie Kalıcılığı**: Cookie'ler disk ve belleğe kaydedilir, CAPTCHA tekrarını önler
- **Paralel İşleme**: 3 eşzamanlı HTTP isteği ile hızlı makale çekme
- **Önbellekleme**: Cookie (30dk), link (10dk) ve PDF (24s) için bellek içi önbellek

## Kurulum

### Gereksinimler

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) paket yöneticisi

### Kurulum Adımları

```bash
# Repoyu klonlayın
git clone https://github.com/saidsurucu/literatur-mcp.git
cd literatur-mcp/dergipark-api

# Bağımlılıkları yükleyin
uv sync
```

### Yapılandırma

Ortam değişkenleri:

| Değişken | Zorunlu | Açıklama |
|----------|---------|----------|
| `CAPSOLVER_API_KEY` | Evet | CAPTCHA çözümü için CapSolver API anahtarı |
| `MISTRAL_API_KEY` | Hayır | Taranmış PDF'ler için Mistral OCR API anahtarı |
| `HEADLESS_MODE` | Hayır | Tarayıcı modu: `true` veya `false` (varsayılan) |

## Kullanım

### Claude Desktop Entegrasyonu

Claude Desktop yapılandırma dosyanıza ekleyin:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "DergiPark MCP": {
      "command": "uv",
      "args": ["run", "python", "mcp_server.py"],
      "cwd": "/path/to/literatur-mcp/dergipark-api",
      "env": {
        "CAPSOLVER_API_KEY": "capsolver_anahtariniz",
        "MISTRAL_API_KEY": "mistral_anahtariniz"
      }
    }
  }
}
```

### Geliştirme Modu

```bash
uv run fastmcp dev mcp_server.py
```

### Doğrudan Çalıştırma

```bash
uv run python mcp_server.py
```

## MCP Araçları

### search_articles

DergiPark'ta akademik makale arar. Sayfa başına 24 makale döndürür.

**Parametreler:**

| Parametre | Tip | Varsayılan | Açıklama |
|-----------|-----|------------|----------|
| `query` | string | `""` | Arama sorgusu (ör: "yapay zeka") |
| `page` | int | `1` | Sayfa numarası (sayfa başına 24 makale) |
| `sort` | string | `null` | Sıralama: `newest` veya `oldest` |
| `article_type` | string | `null` | Makale türü (ör: `54` = Araştırma Makalesi) |
| `year` | string | `null` | Yayın yılı filtresi (ör: `2024`) |
| `index_filter` | string | `hepsi` | Dizin filtresi: `tr_dizin_icerenler`, `bos_olmayanlar`, `hepsi` |

**Örnek Yanıt:**

```json
{
  "pagination": {
    "page": 1,
    "per_page": 24,
    "count": 24
  },
  "articles": [
    {
      "title": "Makale Başlığı",
      "url": "https://dergipark.org.tr/tr/pub/dergi/article/123456",
      "details": {
        "citation_title": "Makale Başlığı",
        "citation_author": "Yazar Adı",
        "citation_journal_title": "Dergi Adı",
        "citation_publication_date": "2024",
        "citation_abstract": "Makale özeti...",
        "citation_keywords": "anahtar1, anahtar2",
        "citation_doi": "10.1234/ornek"
      },
      "indices": "TR Dizin, DOAJ",
      "pdf_url": "https://dergipark.org.tr/tr/download/article-file/123456"
    }
  ]
}
```

### pdf_to_html

DergiPark PDF'ini okunabilir HTML formatına dönüştürür.

**Parametreler:**

| Parametre | Tip | Açıklama |
|-----------|-----|----------|
| `pdf_id` | string | DergiPark makale dosya ID'si (ör: `118146`) |

URL otomatik oluşturulur: `https://dergipark.org.tr/tr/download/article-file/{pdf_id}`

### get_article_references

Makale referans listesini çeker.

**Parametreler:**

| Parametre | Tip | Açıklama |
|-----------|-----|----------|
| `article_url` | string | DergiPark makale URL'i |

## Docker ile Çalıştırma

```bash
# Build
docker build -t dergipark-mcp .

# Run
docker run -p 8000:8000 \
  -e CAPSOLVER_API_KEY=your_key \
  -e HEADLESS_MODE=false \
  dergipark-mcp
```

## Mimari

```
dergipark-api/
├── mcp_server.py    # MCP sunucusu (FastMCP)
├── core.py          # Ortak iş mantığı
│   ├── browser-use      # Tarayıcı otomasyonu
│   ├── CAPTCHA çözme    # CapSolver entegrasyonu
│   ├── Cookie kalıcılığı # Bellek + disk
│   ├── Paralel çekme    # httpx async
│   └── PDF işleme       # PyMuPDF + Mistral OCR
├── Dockerfile       # Docker build
└── fly.toml         # Fly.io deployment
```

## Lisans

MIT
