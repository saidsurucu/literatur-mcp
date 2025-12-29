# Python 3.11 base image
FROM python:3.11-slim-bookworm

WORKDIR /app

# browser-use (Chromium) ve xvfb için gerekli sistem bağımlılıkları
RUN apt-get update && apt-get install -y \
    xvfb \
    wget \
    gnupg \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# uv kur
RUN pip install uv

# Uygulama dosyalarını kopyala
COPY . .

# Bağımlılıkları uv ile kur
RUN uv sync

# browser-use için Chromium kur (Playwright backend)
RUN uv run playwright install chromium

EXPOSE 8000

ENV HEADLESS_MODE=false
ENV DISPLAY=:99

# Başlatma scripti
RUN echo '#!/bin/bash\nXvfb :99 -screen 0 1280x720x24 &\nsleep 1\nexec uv run uvicorn app:app --host 0.0.0.0 --port 8000' > /app/start.sh && chmod +x /app/start.sh

CMD ["/app/start.sh"]
