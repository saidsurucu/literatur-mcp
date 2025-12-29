# Playwright Python imajı
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app

# xvfb ve uv kur
RUN apt-get update && apt-get install -y xvfb && rm -rf /var/lib/apt/lists/*
RUN pip install uv

# Uygulama dosyalarını kopyala ve sahipliği ayarla
COPY --chown=pwuser:pwuser . .

# /app dizininin sahipliğini pwuser'a ver
RUN chown -R pwuser:pwuser /app

USER pwuser

# Bağımlılıkları uv ile kur (mevcut Python'u kullan)
RUN uv sync --python /usr/bin/python3

EXPOSE 8000

ENV HEADLESS_MODE=false

# xvfb ile başlat
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x720x24", "uv", "run", "--python", "/usr/bin/python3", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
