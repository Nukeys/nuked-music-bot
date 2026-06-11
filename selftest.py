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
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
         "-i", stream, "-t", "3", "-vn", "-f", "s16le", "-ar", "48000", "-ac", "2", "-"],
        capture_output=True, timeout=60)
    pcm_bytes = len(proc.stdout)
    expected = 48000 * 2 * 2 * 3  # 3s of 48kHz stereo s16
    print(f"[4] ffmpeg decode -> {pcm_bytes} PCM bytes (expected ~{expected})")
    if pcm_bytes < expected * 0.9:
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
