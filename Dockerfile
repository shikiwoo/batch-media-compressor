FROM python:3.13-slim

# ffmpeg from Debian includes libx265, libsvtav1, libx264, libwebp, flac and VAAPI.
# intel-media-va-driver is the driver that lets ffmpeg use an Intel iGPU (Quick Sync)
# for hardware encoding; it only exists for amd64, so the arm64 image skips it.
# vainfo is a small tool for debugging: docker exec <container> vainfo
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         apt-get install -y --no-install-recommends intel-media-va-driver vainfo; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV INPUT_DIR=/input \
    OUTPUT_DIR=/output \
    CONCURRENCY=1 \
    LIBVA_DRIVER_NAME=iHD \
    PYTHONUNBUFFERED=1

VOLUME ["/input", "/output"]
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
