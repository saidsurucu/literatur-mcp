# 1. Adım: Playwright'ın Python imajını temel al (Belirttiğiniz sürüm)
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

# 2. Adım: Çalışma dizinini ayarla
WORKDIR /app

# 3. Adım: Bağımlılık dosyasını önce kopyala (Docker önbelleği için)
# Dosyaların pwuser'a ait olmasını sağla
COPY --chown=pwuser:pwuser requirements.txt .

# 4. Adım: Python bağımlılıklarını 'pwuser' olarak yükle
# Root olmayan kullanıcıyla yükleme yapmak daha güvenlidir.
# ffmpeg kurulumu kaldırıldı (CapSolver API kullanımı için genellikle gerekmez).
USER pwuser
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
# NOT: Resmi Playwright imajında 'playwright install' genellikle gerekmez.

# 5. Adım: Uygulama kodunu 'pwuser' olarak kopyala
# Bağımlılıklar yüklendikten sonra kopyalama yapılır.
COPY --chown=pwuser:pwuser . .

# 6. Adım: Uygulamanın çalışacağı portu belirt (bilgilendirme)
EXPOSE 8000

# 7. Adım: Önemli Not
# Bu Dockerfile, Redis sunucusu içermez ve uygulamanın hafıza içi (in-memory)
# TTLCache kullandığı varsayılır. Bu önbelleğin düzgün çalışması için
# uygulamanın TEK BİR WORKER PROCESS ile çalıştırılması gerekir.
# Aşağıdaki CMD komutu bunu sağlar (--workers 1).

# 8. Adım: Konteyner başladığında çalıştırılacak komut (Basitleştirilmiş)
# Gunicorn yerine doğrudan Uvicorn kullanılıyor.
# --host 0.0.0.0: Dışarıdan erişim için tüm arayüzlere bağlanır.
# --port 8000: Belirtilen portu kullanır.
# --workers 1: Sadece tek bir işlem çalıştırır (Hafıza içi cache için ZORUNLU).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

# Alternatif CMD (Gunicorn ile tek worker):
# Daha gelişmiş süreç yönetimi istenirse Gunicorn kullanılabilir, ancak worker sayısı 1 olmalı.
#CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8000", "--timeout", "120", "--log-level", "info"]