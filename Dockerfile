FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.12-slim
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask gunicorn "yt-dlp[default]"
COPY app.py .
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-8080} app:app"]
