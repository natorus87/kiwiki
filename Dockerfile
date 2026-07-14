FROM node:22-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf AS frontend-build

WORKDIR /build

COPY package*.json ./
RUN npm ci

COPY frontend/ ./frontend/
COPY app/static/ ./app/static/
RUN npm run build:motion

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

WORKDIR /app

RUN groupadd --system --gid 1000 kiwiki \
    && useradd --system --uid 1000 --gid kiwiki --home-dir /app --shell /usr/sbin/nologin kiwiki \
    && mkdir -p /data \
    && chown -R kiwiki:kiwiki /app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --from=frontend-build /build/app/static/kiwiki-motion.bundle.js ./app/static/kiwiki-motion.bundle.js
RUN chown -R kiwiki:kiwiki /app

ENV KIWIKI_DATA_DIR=/data
ENV KIWIKI_BASE_URL="http://localhost:8080"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

USER kiwiki

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
