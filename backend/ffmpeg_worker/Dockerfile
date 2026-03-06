FROM python:3.12-slim

# Install FFmpeg (with GPU support hints for faster Veo clip processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/ffmpeg_worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ffmpeg_worker/assemble.py .
COPY shared /app/shared

ENTRYPOINT ["python", "assemble.py"]
