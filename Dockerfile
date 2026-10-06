FROM python:3.13-slim

# ffmpeg from Debian includes libx265, libsvtav1, libx264, libwebp, flac and VAAPI.
#
# intel-media-va-driver-non-free is the driver that lets ffmpeg use an Intel iGPU
# (Quick Sync) for hardware encoding. The "non-free" build is needed because Debian's
# free build leaves out the proprietary encoder kernels: on a UHD 630 it only offers
# H.264 low-power encoding and no HEVC encoding at all (`vainfo` shows decode-only
# VAEntrypointVLD for HEVC). It is redistributable, just not open source, and lives
# in Debian's "non-free" component, which is switched on only for the amd64 build.
# The driver doesn't exist for arm64, so that image skips it.
#
# vainfo is a small tool for debugging: docker exec <container> vainfo
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
         && apt-get update \
         && apt-get install -y --no-install-recommends intel-media-va-driver-non-free vainfo; \
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
