# Playwright Python imajı
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app

# uv kur
RUN pip install uv

# Uygulama dosyalarını kopyala
COPY --chown=pwuser:pwuser . .

USER pwuser

# Bağımlılıkları uv ile kur
RUN uv sync

EXPOSE 8000

ENV PATH=/home/pwuser/.local/bin:$PATH

# uv run ile başlat
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
