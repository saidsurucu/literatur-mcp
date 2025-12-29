# Playwright Python image (browser + dependencies included)
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

WORKDIR /app

# uv kur
RUN pip install uv

COPY . .

RUN uv sync

EXPOSE 8000

ENV HEADLESS_MODE=false
ENV DISPLAY=:99

RUN echo '#!/bin/bash\nXvfb :99 -screen 0 1280x720x24 &\nsleep 1\nexec uv run uvicorn app:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 180' > /app/start.sh && chmod +x /app/start.sh

CMD ["/app/start.sh"]
