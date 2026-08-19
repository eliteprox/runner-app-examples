# ffmpeg tool app: an aiohttp server wrapping ffmpeg that self-registers as a
# Live Runner. CPU (libx264) by default so it runs anywhere; for an NVIDIA GPU
# layer compose.gpu.yml (the apt ffmpeg already has h264_nvenc — see README).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "livepeer-gateway>=1.0.0"

WORKDIR /app
COPY runner.py ./

EXPOSE 5000

ENTRYPOINT ["python", "runner.py"]
