# Container image for any Docker-based host (Hugging Face Spaces, Koyeb, Fly.io,
# Railway, etc.). Binds to $PORT if the host sets one, else 7860 (the port
# Hugging Face Spaces expects).
FROM python:3.11-slim

WORKDIR /app

# Install deps first so they cache between code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Serve the bundled fictional demo data as the quick-load files.
ENV DT_DATA_DIR=./sample_data
# Shared instance: confine all file access to the data folder (no host paths).
ENV DT_LOCKED=1
# One worker on purpose: per-visitor uploads live in an in-memory store.
ENV WEB_CONCURRENCY=1

EXPOSE 7860
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-7860} --workers 1 --threads 8 --timeout 120"]
