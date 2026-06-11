FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Token is supplied via environment variable on the host (never baked into the image):
#   docker run -e DISCORD_TOKEN=... musicbot
CMD ["python", "bot.py"]
