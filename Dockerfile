# 1. Adım: Playwright'ın Python imajını temel al
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

# 2. Adım: Çalışma dizinini ayarla
WORKDIR /app

# 3. Adım: Bağımlılık dosyasını önce kopyala
COPY --chown=pwuser:pwuser requirements.txt .

# 4. Adım: Python bağımlılıklarını 'pwuser' olarak yükle
USER pwuser
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 5. Adım: Uygulama kodunu 'pwuser' olarak kopyala
COPY --chown=pwuser:pwuser . .

# 6. Adım: Uygulamanın çalışacağı portu belirt
EXPOSE 8000

# --- PATH Ayarı ---
# 7. Adım: pwuser'ın local bin dizinini PATH'e ekle
# Bu, pip ile kurulan uvicorn, gunicorn gibi komutların bulunmasını sağlar.
ENV PATH=/home/pwuser/.local/bin:$PATH
# --- Bitti: PATH Ayarı ---

# 8. Adım: Redis Sunucusu Hakkında Not (Hatırlatma)
# ÖNEMLİ: Redis sunucusu bu imajda DEĞİLDİR... (önceki gibi)

# 9. Adım: Konteyner başladığında çalıştırılacak komut (Basitleştirilmiş)
# Uvicorn artık PATH üzerinden bulunabilmeli.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

# Alternatif CMD (Gunicorn ile tek worker):
# CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8000", "--timeout", "120", "--log-level", "info"]