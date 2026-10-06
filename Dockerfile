FROM python:3.13-slim

# ffmpeg from Debian includes libx265, libsvtav1, libx264, libwebp and flac
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV INPUT_DIR=/input \
    OUTPUT_DIR=/output \
    CONCURRENCY=1 \
    PYTHONUNBUFFERED=1

VOLUME ["/input", "/output"]
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
