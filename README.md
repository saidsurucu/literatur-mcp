# DergiPark MCP Sunucusu

[DergiPark](https://dergipark.org.tr) üzerinden Türkçe akademik dergi makalelerini aramak ve analiz etmek için MCP (Model Context Protocol) sunucusu.

## Özellikler

- **Makale Arama**: Yıl, tür, dizin ve sıralama filtrelerine göre akademik makaleleri arayın
- **PDF'den HTML'e**: Akademik PDF'leri okunabilir HTML formatına dönüştürün
- **Akıllı OCR**: Taranmış PDF'ler için otomatik Mistral OCR fallback
- **CAPTCHA Çözme**: CapSolver API ile otomatik CAPTCHA çözümü
- **Paralel İşleme**: 5 eşzamanlı tarayıcı ile hızlı makale çekme
- **Önbellekleme**: Cookie, link ve PDF dönüşümleri için bellek içi önbellek

## Kurulum

### Gereksinimler

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) paket yöneticisi

### Kurulum Adımları

```bash
# Repoyu klonlayın
git clone https://github.com/saidsurucu/dergipark-mcp.git
cd dergipark-mcp

# Bağımlılıkları yükleyin
uv sync

# Playwright tarayıcısını yükleyin
uv run playwright install chromium
```

### Yapılandırma

`.env.example` dosyasını `.env` olarak kopyalayıp API anahtarlarınızı girin:

```bash
cp .env.example .env
```

Ortam değişkenleri:

| Değişken | Zorunlu | Açıklama |
|----------|---------|----------|
| `CAPSOLVER_API_KEY` | Evet | CAPTCHA çözümü için CapSolver API anahtarı |
| `MISTRAL_API_KEY` | Hayır | Taranmış PDF'ler için Mistral OCR API anahtarı |
| `HEADLESS_MODE` | Hayır | Tarayıcı modu: `true` (varsayılan) veya `false` |

## Kullanım

### Claude Desktop Entegrasyonu

Claude Desktop yapılandırma dosyanıza ekleyin:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "DergiPark MCP": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/saidsurucu/literatur-mcp",
        "literatur-mcp"
      ],
      "env": {
        "HEADLESS_MODE": "true",
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

DergiPark'ta akademik makale arar.

**Parametreler:**

| Parametre | Tip | Varsayılan | Açıklama |
|-----------|-----|------------|----------|
| `query` | string | `""` | Arama sorgusu (ör: "yapay zeka") |
| `dergipark_page` | int | `1` | DergiPark sayfa numarası |
| `page` | int | `1` | API sayfalama (sayfa başına 5 makale) |
| `sort` | string | `null` | Sıralama: `newest` veya `oldest` |
| `article_type` | string | `null` | Makale türü filtresi (ör: `54` = Araştırma Makalesi) |
| `year` | string | `null` | Yayın yılı filtresi (ör: `2024`) |
| `index_filter` | string | `hepsi` | Dizin filtresi: `tr_dizin_icerenler`, `bos_olmayanlar`, `hepsi` |

**Örnek Yanıt:**

```json
{
  "pagination": {
    "dergipark_page": 1,
    "api_page": 1,
    "items_per_api_page": 5,
    "total_items_on_dergipark_page": 20
  },
  "articles": [
    {
      "title": "Makale Başlığı",
      "authors": "Yazar Adı",
      "journal": "Dergi Adı",
      "year": "2024",
      "abstract": "Makale özeti...",
      "keywords": "anahtar1, anahtar2",
      "doi": "10.1234/ornek",
      "indexes": "TR Dizin, DOAJ",
      "pdf_link": "https://dergipark.org.tr/tr/download/article-file/123456",
      "article_url": "https://dergipark.org.tr/tr/pub/dergi/issue/123/456"
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

**PDF İşleme Akışı:**

1. PDF'i DergiPark'tan indir
2. PyMuPDF ile metin çıkar
3. Metin < 100 karakter ise (taranmış PDF) Mistral OCR kullan
4. Formatlanmış HTML döndür

## REST API

Proje ayrıca FastAPI REST sunucusu içerir (`main.py`):

```bash
# API sunucusunu çalıştır
python main.py
```

**Endpoint'ler:**

- `POST /api/search` - Makale ara
- `GET /api/pdf-to-html?pdf_url=...` - PDF'i HTML'e dönüştür
- `GET /health` - Sağlık kontrolü

## Mimari

```
dergipark-api/
├── mcp_server.py    # MCP sunucusu (FastMCP)
├── main.py          # REST API (FastAPI)
├── core.py          # Ortak iş mantığı
│   ├── BrowserPoolManager  # Playwright tarayıcı havuzu
│   ├── CAPTCHA çözme       # CapSolver entegrasyonu
│   ├── Makale kazıma       # Paralel çekme
│   ├── PDF işleme          # PyMuPDF + Mistral OCR
│   └── Önbellekleme        # TTL cache'ler
├── .env.example     # Ortam değişkenleri şablonu
└── requirements.txt # Bağımlılıklar
```

## Performans

- **Tarayıcı Havuzu**: Paralel işleme için 5 eşzamanlı tarayıcı
- **Paralel Çekme**: 3 eşzamanlı makale detay çekme
- **Async Dizin Çekme**: Dergi dizinleri için bloklamayan HTTP istekleri
- **Önbellekleme**: Cookie (30dk), Link (10dk), PDF (24s)

## Lisans

MIT

## Katkıda Bulunma

Katkılarınızı bekliyoruz! Issue açabilir veya pull request gönderebilirsiniz.
