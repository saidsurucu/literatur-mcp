# Playwright Python imajı
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app

# uv kur
RUN pip install uv

# Uygulama dosyalarını kopyala ve sahipliği ayarla
COPY --chown=pwuser:pwuser . .

# /app dizininin sahipliğini pwuser'a ver
RUN chown -R pwuser:pwuser /app

USER pwuser

# Bağımlılıkları uv ile kur (mevcut Python'u kullan)
RUN uv sync --python /usr/bin/python3

EXPOSE 8000

# uv run ile başlat
CMD ["uv", "run", "--python", "/usr/bin/python3", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
