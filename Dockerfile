FROM python:3.11-slim-bookworm

# Chromium system libraries — install via apt instead of playwright --with-deps
# (avoids OOM / silent build kills on Railway's builder).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-liberation wget \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libx11-6 libx11-xcb1 \
    libxcb1 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxshmfence1 \
    libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
ENV PIP_NO_CACHE_DIR=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN pip install -r requirements.txt

# Browser binary only — system deps installed above (no --with-deps).
RUN playwright install chromium

COPY . .

WORKDIR /app/backend

RUN chmod +x start_railway.sh predeploy_railway.sh

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "start_railway.sh"]
