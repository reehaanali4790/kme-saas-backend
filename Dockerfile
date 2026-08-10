FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

WORKDIR /app/backend

RUN chmod +x start_railway.sh predeploy_railway.sh

ENV PORT=8000
EXPOSE 8000

# Railway injects $PORT; railway.json startCommand uses start_railway.sh.
CMD ["sh", "start_railway.sh"]
