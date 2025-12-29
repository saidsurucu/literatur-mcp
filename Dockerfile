# Playwright Python imajı
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app

# xvfb ve uv kur
RUN apt-get update && apt-get install -y xvfb && rm -rf /var/lib/apt/lists/*
RUN pip install uv

# Uygulama dosyalarını kopyala
COPY . .

# Bağımlılıkları uv ile kur
RUN uv sync --python /usr/bin/python3

EXPOSE 8000

ENV HEADLESS_MODE=false
ENV DISPLAY=:99

# Başlatma scripti
RUN echo '#!/bin/bash\nXvfb :99 -screen 0 1280x720x24 &\nsleep 1\nexec uv run --python /usr/bin/python3 uvicorn app:app --host 0.0.0.0 --port 8000' > /app/start.sh && chmod +x /app/start.sh

CMD ["/app/start.sh"]
