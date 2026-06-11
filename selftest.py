"""Offline self-test: exercises track resolution + audio pipeline without Discord."""
import asyncio
import os
import subprocess
import sys

os.environ.setdefault("DISCORD_TOKEN", "selftest-dummy")
import bot as botmod


async def main():
    ok = True

    # 1. plain-text search
    tracks, desc = await botmod.resolve_query("rick astley never gonna give you up", "tester")
    print(f"[1] search        -> {tracks[0].title!r}")

    # 2. YouTube URL
    tracks, _ = await botmod.resolve_query("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "tester")
    print(f"[2] youtube url   -> {tracks[0].title!r} ({botmod.fmt_duration(tracks[0].duration)})")

    # 3. Spotify track link, no API creds (og-meta scrape)
    try:
        tracks, _ = await botmod.resolve_query(
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", "tester")
        print(f"[3] spotify track -> {tracks[0].title!r} (query: {tracks[0].query!r})")
    except Exception as e:
        ok = False
        print(f"[3] spotify track FAILED: {e}")

    # 4. play-time stream extraction + 3s FFmpeg decode
    info = await botmod.ytdl_extract(botmod.YTDL_PLAY_OPTS, "ytsearch1:lofi hip hop test")
    if info and "entries" in info:
        info = [e for e in info["entries"] if e][0]
    stream = info["url"]
    print(f"[4] stream url    -> {info['title']!r}, url length {len(stream)}")
    headers = info.get("http_headers") or {}
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if hdr:
        cmd += ["-headers", hdr]
    cmd += ["-i", stream, "-t", "3", "-vn", "-af", "volume=0.3",
            "-c:a", "libopus", "-b:a", "96k", "-compression_level", "5", "-f", "opus", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    opus_bytes = len(proc.stdout)
    print(f"[4] ffmpeg encode -> {opus_bytes} opus bytes (expect >5000)")
    if opus_bytes < 5000:
        ok = False
        print(f"    FFmpeg stderr: {proc.stderr.decode(errors='replace')[:500]}")

    # 5. opus library loads (needed for voice transmission)
    import discord
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
        except Exception:
            pass
    print(f"[5] opus loaded   -> {discord.opus.is_loaded()}")
    if not discord.opus.is_loaded():
        ok = False

    print("\nSELF-TEST " + ("PASSED" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


asyncio.run(main())
