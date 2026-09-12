FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 moderbot && chown -R moderbot:moderbot /app
USER moderbot

CMD ["sh", "-c", "python -m alembic upgrade head && exec python -m app.bot"]
