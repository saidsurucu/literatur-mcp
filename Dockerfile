# 1. Adım: Playwright'ın resmi Python imajını temel al
# Belirli bir sürüm kullanmak (örneğin 1.44.0) tekrarlanabilirliği artırır
# 'jammy' Ubuntu 22.04 tabanlıdır, güncel bir seçenektir
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# 2. Adım: Çalışma dizinini ayarla
WORKDIR /app

# 3. Adım: Bağımlılık dosyasını kopyala
# Bu adımı önce yapmak Docker katman önbelleklemesini optimize eder
COPY requirements.txt .

# 4. Adım: Bağımlılıkları yükle
# Pip'i güncellemek iyi bir pratiktir
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt
# ÖNEMLİ: Playwright'ın resmi imajını kullandığımız için
# 'playwright install' komutunu BURADA çalıştırmamıza gerek YOKTUR.
# Tarayıcılar ve OS bağımlılıkları zaten imajın içindedir.

# 5. Adım: Uygulama kodunu kopyala
# Proje kökündeki her şeyi (Dockerfile, .dockerignore hariç) /app içine kopyala
COPY . .

# 6. Adım: Uygulamanın çalışacağı portu belirt (bilgilendirme amaçlı)
EXPOSE 8000

# 7. Adım: Konteyner başladığında çalıştırılacak komut
# Gunicorn kullanarak FastAPI uygulamasını başlat
# -w 4: Worker sayısı (CPU sayısına göre ayarlanabilir)
# -k uvicorn.workers.UvicornWorker: Uvicorn'u Gunicorn ile kullanmak için
# main:app: main.py dosyasındaki app nesnesi
# --bind 0.0.0.0:8000: Konteyner dışından erişim için 0.0.0.0'a ve belirtilen porta bağlan
CMD ["gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8000", "--timeout", "120"]
# Worker sayısını (-w) ve timeout değerini sunucu kaynaklarınıza göre ayarlayın.
# Timeout'u Playwright işlemlerinin uzun sürebilme ihtimaline karşı artırdım.