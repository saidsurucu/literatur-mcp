# 1. Adım: Playwright'ın Python kütüphanesiyle uyumlu resmi imajını temel al
# Belirli bir sürümü kullanmak tutarlılık sağlar (requirements.txt ile eşleşmeli)
# Jammy (Ubuntu 22.04) tabanlı güncel bir sürüm tercih edilebilir, örn: v1.41.0-jammy veya daha yenisi
# Önceki hataya göre 1.51.0 sürümünü kullanıyoruz, ancak daha güncel bir sürüm olup olmadığını kontrol etmek iyi olabilir.
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

# 2. Adım: Çalışma dizinini ayarla
WORKDIR /app

# --- ffmpeg Kurulumu (Bazı CAPTCHA çözümleri veya medya işlemleri için gerekebilir) ---
# 3. Adım: Sistem paketlerini kurmak için root kullanıcısına geç
USER root

# 4. Adım: Paket listesini güncelle ve ffmpeg'i kur
# --no-install-recommends gereksiz paketleri önler
# && \ ile komutları birleştirerek katman sayısını azalt
# Kurulum sonrası temizlik yaparak imaj boyutunu küçült
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 5. Adım: Varsayılan Playwright kullanıcısına geri dön (genellikle pwuser)
# Bu, konteyner içinde root olarak çalışmamak için güvenlik açısından önemlidir.
USER pwuser
# --- ffmpeg Kurulumu Bitti ---

# 6. Adım: Bağımlılık dosyasını kopyala (ffmpeg kurulduktan sonra)
# Bu adımın kod kopyalamadan önce olması Docker katman önbelleklemesini optimize eder.
COPY --chown=pwuser:pwuser requirements.txt .
# --chown=pwuser:pwuser: Dosyaların doğru kullanıcıya ait olmasını sağlar.

# 7. Adım: Python bağımlılıklarını yükle
# Pip'i güncellemek iyi bir pratiktir
# --no-cache-dir imaj boyutunu küçültür
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt
# NOT: Resmi Playwright imajı kullanıldığı için 'playwright install' komutuna genellikle GEREK YOKTUR,
# tarayıcılar imaj içinde zaten bulunur.

# 8. Adım: Uygulama kodunu kopyala
# Bağımlılıklar yüklendikten sonra kopyalama yapılır, böylece kod değişikliklerinde
# bağımlılıkların tekrar yüklenmesi gerekmez (önbellek kullanılır).
# Proje kökündeki her şeyi (Dockerfile, .dockerignore hariç) /app içine kopyala
# ve dosyaların sahibini pwuser olarak ayarla.
COPY --chown=pwuser:pwuser . .

# 9. Adım: Uygulamanın çalışacağı portu belirt (bilgilendirme amaçlı)
# Bu satır portu otomatik olarak yayınlamaz, sadece imajın hangi portu
# kullanmayı amaçladığını belirtir. Portu yayınlamak için docker run -p veya
# docker-compose.yml içinde ports kullanılır.
EXPOSE 8000

# 10. Adım: Redis Sunucusu Hakkında Not
# ÖNEMLİ: Bu Dockerfile Redis sunucusunu İÇERMEZ. Redis sunucusu
# ayrı bir konteynerde (örn. Docker Compose ile) veya harici bir servis
# (örn. Render Redis) olarak çalıştırılmalı ve uygulama `REDIS_URL`
# ortam değişkeni aracılığıyla ona bağlanmalıdır.

# 11. Adım: Konteyner başladığında çalıştırılacak komut
# Gunicorn kullanarak FastAPI uygulamasını başlat
# -w 2: Worker sayısı (genellikle CPU çekirdek sayısının 2 katı + 1 önerilir, sunucuya göre ayarlanmalı)
# -k uvicorn.workers.UvicornWorker: Uvicorn'u Gunicorn ile asenkron çalıştırmak için
# main:app: main.py dosyasındaki FastAPI app nesnesi
# --bind 0.0.0.0:8000: Konteyner dışından erişim için tüm ağ arayüzlerine ve 8000 portuna bağlan
# --timeout 120: Bir worker'ın yanıt vermezse ne kadar süre sonra yeniden başlatılacağı (saniye).
#                 Playwright işlemleri uzun sürebileceği için timeout değeri yüksek tutulabilir.
# --log-level info: Loglama seviyesini ayarla (debug, warning, error vb. olabilir)
CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8000", "--timeout", "120", "--log-level", "info"]