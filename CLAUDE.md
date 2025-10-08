# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a FastAPI-based web scraper API that extracts academic articles from DergiPark (Turkish academic journal platform). The application handles CAPTCHA challenges, caches results, and provides PDF-to-HTML conversion for academic papers.

## Architecture

### Core Components

- **FastAPI Application** (`main.py`): Main web server with search and PDF conversion endpoints
- **Playwright Integration**: Browser automation for web scraping with CAPTCHA handling
- **CapSolver API**: Third-party service for reCAPTCHA v2 solving
- **Caching System**: In-memory TTL cache for cookies, article links, and PDF conversions
- **PDF Processing**: Uses PyMuPDF (fitz) for PDF text extraction

### Key Dependencies

- **FastAPI**: Web framework and API server
- **Playwright**: Browser automation for scraping
- **PyMuPDF**: PDF text extraction
- **httpx**: Async HTTP client
- **BeautifulSoup4**: HTML parsing
- **cachetools**: In-memory caching

## Development Commands

### Running the Application

**Local Development:**
```bash
python main.py
```
This starts the server on `http://0.0.0.0:8000` with single worker (required for cache effectiveness).

**Production with uvicorn:**
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Docker Build:**
```bash
docker build -t dergipark-api .
```

**Docker Run:**
```bash
docker run -p 8000:8000 -e CAPSOLVER_API_KEY=your_key_here dergipark-api
```

### Modal.com Deployment (Serverless)

**Initial Setup:**
```bash
# Install Modal SDK
uv add modal

# Authenticate
modal token new
```

**Development Mode (with hot reload):**
```bash
modal serve modal_app.py
```

**Production Deployment:**
```bash
uv run modal deploy modal_app.py
```

**Set Environment Variables:**
```bash
uv run modal secret create dergipark-secrets CAPSOLVER_API_KEY=your_actual_key_here
```

**Current Production Deployment:**
- URL: `https://saidsrc--dergipark-api-fastapi-app.modal.run`
- Dashboard: https://modal.com/apps/saidsrc/main/deployed/dergipark-api
- Status: ✅ Active and working

**Important Modal Notes:**
- Serverless deployment with pay-per-use pricing (~$1-2/month for light usage)
- Uses Microsoft's Playwright Python image (`mcr.microsoft.com/playwright/python:v1.51.0-jammy`)
- Dependencies loaded from `requirements.txt`
- Cookie persistence via Modal Volume (`dergipark-cookies`)
- Cold start: ~55-82 seconds, warm requests: faster
- Auto-scales to zero when not in use
- Development URL: `https://[username]--dergipark-api-fastapi-app-dev.modal.run`
- Production URL: `https://[username]--dergipark-api-fastapi-app.modal.run`

### Fly.io Deployment (Always-on)

**Initial Setup:**
```bash
# Install flyctl if not already installed
curl -L https://fly.io/install.sh | sh

# Launch app (creates fly.toml)
flyctl launch
```

**Deploy:**
```bash
flyctl deploy
```

**Set Environment Variables:**
```bash
flyctl secrets set CAPSOLVER_API_KEY=your_actual_key_here
flyctl secrets set HEADLESS_MODE=true
```

**View Logs:**
```bash
flyctl logs
```

**Scale (Important for cache effectiveness):**
```bash
flyctl scale count 1
```

**Important Fly.io Notes:**
- The app MUST run with exactly 1 instance due to in-memory cache requirements
- Uses the existing Dockerfile which includes Playwright browser setup
- Environment variables should be set via `flyctl secrets` for security
- The health endpoint (`/health`) can be used for Fly.io health checks
- Auto-stop/start machines feature reduces costs when idle (~$0-7/month)
- Faster response times than serverless (no cold starts)

### Testing

```bash
python test.py
```

Note: No formal test framework is configured. The `test.py` file is a simple hello world script.

## Configuration

### Environment Variables

- `CAPSOLVER_API_KEY`: Required for CapSolver CAPTCHA solving (defaults to hardcoded key in main.py)
- `USE_BROWSER_USE`: Enable browser-use AI CAPTCHA solver (default: "false")
- `GEMINI_API_KEY`: Required for browser-use AI CAPTCHA solver (Google Gemini API key)
- `HEADLESS_MODE`: Set to "false" for non-headless browser mode (default: "true")
- `PDF_CACHE_TTL`: TTL for PDF cache in seconds (default: 86400)

### Cache Configuration

- **Cookies Cache**: TTL=1800s, Max=10 sets (in-memory + disk persistence)
- **Cookie Disk File**: `cookies_persistent.pkl` (survives server restarts)
- **Article Links Cache**: TTL=600s, Max=100 lists
- **PDF Cache**: TTL=86400s (configurable), Max=500 items

## API Endpoints

### Primary Endpoints

- `POST /api/search`: Main search endpoint with comprehensive article metadata
- `GET /api/pdf-to-html`: Converts PDF URLs to readable HTML
- `GET /health`: Health check
- `GET /gizlilik`: Privacy policy page

### Search Parameters

The search endpoint accepts various academic search filters including:
- Article metadata: title, author, DOI, keywords, abstract
- Journal filters: journal name, ISSN, eISSN
- Pagination: dergipark_page, api_page, page_size
- Sorting: newest/oldest
- Article type filtering
- Index filtering (TR Dizin, etc.)

## Important Technical Notes

### Single Worker Requirement

The application MUST run with exactly one worker process (`--workers 1`) because it uses in-memory caches that are not shared across processes.

### CAPTCHA Handling

The application supports two methods for solving CAPTCHA challenges:

1. **Browser-use AI Solver** (Primary, if enabled): Uses Google Gemini LLM to intelligently interact with CAPTCHA challenges
   - Enable with `USE_BROWSER_USE=true` and provide `GEMINI_API_KEY`
   - More natural and human-like behavior
   - May have better success rates for complex CAPTCHAs

2. **CapSolver API** (Fallback): Traditional token-based CAPTCHA solving service
   - Supports both reCAPTCHA v2 and Cloudflare Turnstile
   - Automatically used if browser-use fails or is not configured
   - Requires `CAPSOLVER_API_KEY`

**CAPTCHA Flow:**
- Detects both reCAPTCHA v2 and Cloudflare Turnstile automatically
- For Turnstile: Token is injected, then waits 2-3.5s for widget processing
- Submit button (`kt-hidden` class) is forcefully unhidden via JavaScript
- After CAPTCHA is solved, the application automatically clicks on the "Makale" (Articles) section to load article results
- Cookies are saved both to memory and disk for persistence across server restarts
- If both methods fail, the API returns HTTP 429 status

**Cookie Persistence:**
- Cookies are stored in-memory (TTL: 30 minutes) for fast access
- Cookies are also saved to disk (`cookies_persistent.pkl`) to survive server restarts
- On startup, the application loads cookies from disk if available and not expired

### Rate Limiting

Built-in delays between requests (0.8-1.8s for details, 1.5-3.0s for articles) to avoid overwhelming the target site.

### Error Handling

Comprehensive error handling with specific HTTP status codes:
- 400: Invalid parameters
- 404: No results found
- 429: CAPTCHA solving failed
- 500: Internal server errors
- 503: Browser service unavailable
- 504: Timeouts

## File Structure

- `main.py`: Core application with all endpoints and scraping logic
- `modal_app.py`: Modal.com serverless deployment configuration
- `cookies_persistent.pkl`: Disk-persisted cookies (auto-generated, survives restarts)
- `test.py`: Simple test script
- `hello.py`: Hello world script
- `requirements.txt`: Python dependencies
- `pyproject.toml`: Project configuration with extended dependencies
- `Dockerfile`: Multi-stage Docker build with Playwright (compatible with Fly.io)
- `vercel.json`: Vercel deployment configuration
- `fly.toml`: Fly.io configuration (created after `flyctl launch`)
- `gizlilik/index.html`: Privacy policy page

## Development Tips

- Use the browser in non-headless mode for debugging: `HEADLESS_MODE=false`
- Monitor cache effectiveness through console logs
- The application includes extensive logging for debugging scraping issues
- PDF processing uses PyMuPDF instead of MarkItDown for better text extraction