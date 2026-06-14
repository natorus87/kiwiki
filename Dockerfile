FROM node:22-slim AS frontend-build

WORKDIR /build

COPY package*.json ./
RUN npm ci

COPY frontend/ ./frontend/
COPY app/static/ ./app/static/
RUN npm run build:motion

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --from=frontend-build /build/app/static/kiwiki-motion.bundle.js ./app/static/kiwiki-motion.bundle.js

ENV KIWIKI_DATA_DIR=/data
ENV KIWIKI_BASE_URL="http://localhost:8080"

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
