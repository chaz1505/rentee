FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . ./

CMD ["sh", "-c", "xvfb-run -a python3 propertyguru_scraper.py --condo 'One Menerung' --dry-run --limit 1; exec gunicorn app:app --bind 0.0.0.0:${PORT:-10000}"]
