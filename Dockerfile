FROM python:3.12-slim

# ffmpeg for audio, curl/unzip to fetch Deno. Deno is yt-dlp's JS runtime for
# solving YouTube's signature ("nsig") challenges — without it YouTube only
# serves a flaky combined format from datacenter IPs.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && apt-get purge -y unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Secrets are supplied at runtime (never baked into the image):
#   DISCORD_TOKEN env var, and a Render Secret File at /etc/secrets/cookies.txt
CMD ["python", "bot.py"]
