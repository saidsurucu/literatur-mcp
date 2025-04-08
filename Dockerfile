# 1. Adım: Playwright'ın Python imajını temel al (Sürüm sizin belirttiğiniz gibi)
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

# 2. Adım: Çalışma dizinini ayarla
WORKDIR /app

# 3. Adım: Bağımlılık dosyasını önce kopyala (Docker önbelleği için)
# Dosyaların pwuser'a ait olmasını sağla
COPY --chown=pwuser:pwuser requirements.txt .

# --- ffmpeg kurulumu kaldırıldı ---

# 4. Adım: Python bağımlılıklarını 'pwuser' olarak yükle
# Root olmayan kullanıcıyla yükleme yapmak daha güvenlidir.
USER pwuser
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
# NOT: Resmi Playwright imajında 'playwright install' gerekmez.

# 5. Adım: Uygulama kodunu 'pwuser' olarak kopyala
# Bağımlılıklar yüklendikten sonra kopyalama yapılır.
COPY --chown=pwuser:pwuser . .

# 6. Adım: Uygulamanın çalışacağı portu belirt (bilgilendirme)
EXPOSE 8000

# 7. Adım: Redis Sunucusu Hakkında Not (Hatırlatma)
# ÖNEMLİ: Redis sunucusu bu imajda DEĞİLDİR. Harici olarak çalıştırılmalı
# ve `REDIS_URL` ortam değişkeni ile uygulamaya bildirilmelidir.

# 8. Adım: Konteyner başladığında çalıştırılacak komut (Basitleştirilmiş)
# Gunicorn yerine doğrudan Uvicorn kullanılıyor.
# --host 0.0.0.0: Dışarıdan erişim için tüm arayüzlere bağlanır.
# --port 8000: Belirtilen portu kullanır.
# --workers 1: Sadece tek bir işlem çalıştırır (kaynakları az kullanmak için önemli).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]