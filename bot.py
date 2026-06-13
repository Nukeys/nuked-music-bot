"""
Discord music bot — plays audio from YouTube, Spotify, SoundCloud, Bandcamp,
direct links, and plain-text search queries.

Spotify links are resolved to track metadata, then the audio is sourced from
YouTube (Spotify does not permit third-party audio streaming).

Requires: DISCORD_TOKEN in .env
Optional: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env for full
          Spotify album/playlist support (single tracks work without them).
"""

import asyncio
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

if not TOKEN or TOKEN == "paste-your-token-here":
    print(
        "\n[!] No Discord token found.\n"
        "    Open the .env file in this folder and paste your bot token after DISCORD_TOKEN=\n"
        "    (Get one at https://discord.com/developers/applications)\n"
    )
    sys.exit(1)

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            os.path.join(_BOT_DIR, "bot.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        ),
    ],
)
log = logging.getLogger("musicbot")

IDLE_DISCONNECT_SECONDS = 300
MAX_PLAYLIST_TRACKS = 200
MAX_SAVED_PLAYLISTS = 25
PLAYLISTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playlists.json")
# Optional: a private channel ID where the bot mirrors playlists.json, so user
# data survives hosts with ephemeral disks (Render free tier wipes files on deploy).
PLAYLISTS_CHANNEL_ID = int(os.getenv("PLAYLISTS_CHANNEL_ID", "0") or 0)

# Per-guild toggles (search picker, autoplay). Mirrored to the data channel too,
# so they survive a redeploy on hosts with ephemeral disks.
SETTINGS_PATH = os.path.join(_BOT_DIR, "settings.json")

SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)"
)
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# YouTube on datacenter IPs (Render) needs three things to serve clean audio:
#   1. cookies (below) to pass the "confirm you're not a bot" check
#   2. the `tv` client, which returns audio-only opus formats when cookied
#      (web only yields a flaky combined 360p format; android rejects cookies)
#   3. Deno on PATH so yt-dlp can solve the JS signature ("nsig") challenge
# Fast path queries ONLY tv and skips the ~1.5MB watch-page download — both are
# mostly CPU-bound parsing, which is what hurts on a 0.1-CPU host. extract_play()
# falls back to the full tv+web pass for the rare video the fast path can't serve.
_YT_CLIENTS = {"youtube": {"player_client": ["tv"], "player_skip": ["webpage"]}}
_YT_CLIENTS_FALLBACK = {"youtube": {"player_client": ["tv", "web"]}}

# Make a bundled/installed Deno discoverable to yt-dlp's signature solver.
for _deno_dir in ("/usr/local/bin", os.path.join(_BOT_DIR, ".deno", "bin")):
    if os.path.isdir(_deno_dir) and _deno_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _deno_dir + os.pathsep + os.environ.get("PATH", "")
# Cap the solver's V8 heap: an unbounded Deno spike helped OOM the 512MB
# instance, and the signature challenges need nowhere near this much.
os.environ.setdefault("DENO_V8_FLAGS", "--max-old-space-size=128")

YTDL_PLAY_OPTS = {
    "format": "bestaudio[acodec!=none]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "skip_download": True,
    "extractor_args": _YT_CLIENTS,
}

YTDL_PLAY_FALLBACK_OPTS = {**YTDL_PLAY_OPTS, "extractor_args": _YT_CLIENTS_FALLBACK}

YTDL_QUEUE_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "skip_download": True,
    "playlistend": MAX_PLAYLIST_TRACKS,
    # full args here: playlist listing needs the regular webpage path
    "extractor_args": _YT_CLIENTS_FALLBACK,
}

# YouTube cookies make requests look like a logged-in user, which bypasses the
# bot checks that datacenter IPs trigger. Supply via Render Secret File
# (cookies.txt) or the YT_COOKIES_FILE env var. Use a throwaway account.
_COOKIES_SRC = os.getenv("YT_COOKIES_FILE") or (
    "/etc/secrets/cookies.txt" if os.path.exists("/etc/secrets/cookies.txt") else None
)
if _COOKIES_SRC and os.path.exists(_COOKIES_SRC):
    # yt-dlp rewrites the cookie jar on close, but Render mounts secret files
    # read-only — so work from a writable copy to avoid an OSError every call.
    import shutil
    _COOKIES = os.path.join(_BOT_DIR, "cookies_active.txt")
    try:
        shutil.copyfile(_COOKIES_SRC, _COOKIES)
    except OSError:
        _COOKIES = _COOKIES_SRC  # fall back to in-place (writable host)
    YTDL_PLAY_OPTS["cookiefile"] = _COOKIES
    YTDL_PLAY_FALLBACK_OPTS["cookiefile"] = _COOKIES
    YTDL_QUEUE_OPTS["cookiefile"] = _COOKIES
    log.info("YouTube cookies loaded from %s (working copy: %s)", _COOKIES_SRC, _COOKIES)

FFMPEG_BEFORE = "-loglevel error -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTS = "-vn"

# Optional audio effects, applied as an ffmpeg -af chain alongside the volume
# filter. The tempo-shifting ones (nightcore/vaporwave) move the pitch too.
AUDIO_FILTERS = {
    "off": "",
    "bassboost": "bass=g=9",
    "nightcore": "asetrate=48000*1.25,aresample=48000",
    "vaporwave": "asetrate=48000*0.85,aresample=48000",
    "8d": "apulsator=hz=0.09",
    "treble": "treble=g=6",
}

# FFmpeg's own errors land here — first place to look if a song won't play
_FFLOG_PATH = os.path.join(_BOT_DIR, "ffmpeg.log")
try:
    if os.path.getsize(_FFLOG_PATH) > 1_000_000:
        open(_FFLOG_PATH, "wb").close()
except OSError:
    pass
FFMPEG_LOG = open(_FFLOG_PATH, "ab")

# Prefer FFMPEG_PATH env var, then a bundled ./ffmpeg binary (for hosts without
# ffmpeg preinstalled), then whatever is on PATH.
_BUNDLED_FFMPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")
FFMPEG_EXE = os.getenv("FFMPEG_PATH") or (_BUNDLED_FFMPEG if os.path.exists(_BUNDLED_FFMPEG) else "ffmpeg")


def ffmpeg_before_options(info: dict) -> str:
    """Forward yt-dlp's HTTP headers to FFmpeg — without them some CDNs (YouTube) return 403."""
    headers = info.get("http_headers") or {}
    if not headers:
        return FFMPEG_BEFORE
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if '"' not in str(v))
    return f'{FFMPEG_BEFORE} -headers "{hdr}"'


def make_source(stream_url: str, info: dict, volume_pct: int, seek: float = 0.0,
                audio_filter: str = "off") -> discord.FFmpegOpusAudio:
    """Build the audio source with decode, volume, and opus encode all inside
    ffmpeg. Keeping the bot process out of the audio path means the event loop
    stays responsive on tiny cloud CPUs (buttons answer instantly even mid-song)."""
    before = ffmpeg_before_options(info)
    if seek > 0:
        before = f"-ss {seek:.2f} " + before
    chain = [f"volume={volume_amp(volume_pct):.4f}"]
    fx = AUDIO_FILTERS.get(audio_filter, "")
    if fx:
        chain.append(fx)
    opts = f"-vn -af {','.join(chain)} -compression_level 5"
    # NOTE: do not pass codec= here — discord.py treats codec="libopus" as "the
    # source is already opus, just copy it", which conflicts with the volume
    # filter and kills ffmpeg instantly. Default (None) means encode with libopus.
    return discord.FFmpegOpusAudio(
        stream_url,
        executable=FFMPEG_EXE,
        bitrate=96,
        before_options=before,
        options=opts,
        stderr=FFMPEG_LOG,
    )


def volume_amp(pct: int) -> float:
    """Perceptual volume curve: human loudness is logarithmic, so a linear
    multiplier makes 50% sound like ~75% and 10% nearly inaudible. The 1.7
    exponent makes N% *sound* like roughly N% of full loudness."""
    return (max(0, min(100, pct)) / 100) ** 1.7


def fmt_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "live/unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def progress_bar(elapsed: float, duration: Optional[float], width: int = 16) -> str:
    """A `0:42 ▬▬🔘▬▬ 3:30` style position bar for the now-playing card."""
    if not duration:
        return f"`{fmt_duration(elapsed)}` 🔴 LIVE"
    frac = max(0.0, min(1.0, elapsed / duration))
    pos = min(width - 1, int(frac * width))
    bar = "".join("🔘" if i == pos else "▬" for i in range(width))
    return f"`{fmt_duration(elapsed)}` {bar} `{fmt_duration(duration)}`"


def current_elapsed(player: "GuildPlayer") -> float:
    """Playback position of the current song in seconds, read from how many
    20ms opus frames have actually been sent — so it doesn't advance while paused."""
    vc = player.guild.voice_client
    audio = getattr(vc, "_player", None) if vc else None
    return (audio.loops * 0.02) if audio else 0.0


@dataclass
class Track:
    query: str                      # URL or "ytsearch1:..." fed to yt-dlp at play time
    title: str
    requested_by: str               # display name, shown in embeds
    requester_id: Optional[int] = None  # who queued it — decides whose autoplay setting applies
    duration: Optional[float] = None
    webpage_url: Optional[str] = None
    thumbnail: Optional[str] = None
    # Full yt-dlp info resolved ahead of time (at /play, or while the previous
    # song was playing). Stream URLs go stale, so it expires after PREFETCH_TTL.
    prefetched: Optional[dict] = None
    prefetched_at: float = 0.0
    prefetch_task: Optional[asyncio.Task] = None
    prefetch_failed: bool = False   # don't background-retry a dead video forever


PREFETCH_TTL = 600   # seconds a prefetched stream URL is trusted before re-extracting
PREFETCH_AHEAD = 3   # upcoming tracks kept resolved so several quick skips land instantly


@dataclass
class GuildPlayer:
    guild: discord.Guild
    queue: deque = field(default_factory=deque)
    queue_event: asyncio.Event = field(default_factory=asyncio.Event)
    next_event: asyncio.Event = field(default_factory=asyncio.Event)
    loop_mode: str = "off"          # off | track | queue
    volume_pct: int = 50
    current: Optional[Track] = None
    text_channel: Optional[discord.abc.Messageable] = None
    task: Optional[asyncio.Task] = None
    stream_url: Optional[str] = None    # current track's resolved stream (for live volume swaps)
    stream_info: Optional[dict] = None
    audio_filter: str = "off"           # ffmpeg effect applied to the whole session
    autoplay_seed: Optional[Track] = None       # last track played, used to find related songs
    autoplay_history: deque = field(default_factory=lambda: deque(maxlen=60))  # video ids already played
    np_message: Optional[discord.Message] = None   # live now-playing card to keep updating
    np_task: Optional[asyncio.Task] = None


def load_playlists() -> dict:
    try:
        with open(PLAYLISTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def user_settings(user_id: int) -> dict:
    """Per-user toggle dict with defaults filled in (both default OFF, so the
    bot behaves exactly as before until someone flips them in /settings)."""
    s = bot.settings.setdefault(str(user_id), {})
    s.setdefault("search_picker", False)
    s.setdefault("autoplay", False)
    return s


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.players: dict[int, GuildPlayer] = {}
        # saved playlists: {user_id: {name: [{query,title,duration,webpage_url}]}}
        self.playlists: dict = load_playlists()
        self._playlists_lock = asyncio.Lock()
        self._backup_msg_id: Optional[int] = None
        # per-guild settings: {guild_id: {search_picker: bool, autoplay: bool}}
        self.settings: dict = load_settings()
        self._settings_backup_msg_id: Optional[int] = None
        self.spotify = None
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            import spotipy
            from spotipy.oauth2 import SpotifyClientCredentials
            self.spotify = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
                )
            )
            log.info("Spotify API credentials loaded — full playlist/album support enabled.")
        else:
            log.info("No Spotify API credentials — Spotify single tracks still work via metadata lookup.")

    async def setup_hook(self):
        global CONTROLS
        CONTROLS = MusicControls()
        self.add_view(CONTROLS)  # persistent: buttons keep working after restarts
        self.loop.create_task(start_keepalive())
        self.loop.create_task(warm_ytdlp())
        await self.tree.sync()  # global sync (can take up to an hour to propagate the first time)

    async def on_ready(self):
        log.info("Logged in as %s (%s) — in %d server(s)", self.user, self.user.id, len(self.guilds))
        await self.restore_playlists()
        await self.restore_settings()
        # Commands live in the global set only (synced in setup_hook). Earlier
        # versions also pushed per-guild copies, which Discord displays as
        # duplicates — wipe any guild-scoped leftovers.
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            except discord.HTTPException as e:
                log.warning("Guild command cleanup failed for %s: %s", guild.name, e)
        await update_presence()
        log.info("Slash commands synced. Invite URL:")
        log.info(
            "https://discord.com/api/oauth2/authorize?client_id=%s&permissions=8&scope=bot%%20applications.commands",
            self.user.id,
        )

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(guild=guild)
        return self.players[guild.id]

    async def save_playlists(self):
        async with self._playlists_lock:
            tmp = PLAYLISTS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.playlists, f, ensure_ascii=False, indent=1)
            os.replace(tmp, PLAYLISTS_PATH)
        self.loop.create_task(self._backup_playlists())

    async def _backup_file(self, path: str, label: str, id_attr: str):
        """Mirror a data file to the data channel, replacing the previous copy
        (survives hosts with ephemeral disks, e.g. Render free tier)."""
        if not PLAYLISTS_CHANNEL_ID or not self.is_ready():
            return
        channel = self.get_channel(PLAYLISTS_CHANNEL_ID)
        if channel is None:
            return
        try:
            msg = await channel.send(content=label, file=discord.File(path))
            old_id = getattr(self, id_attr)
            if old_id:
                try:
                    old = await channel.fetch_message(old_id)
                    await old.delete()
                except discord.HTTPException:
                    pass
            setattr(self, id_attr, msg.id)
        except Exception:
            log.exception("Backup failed for %s", path)

    async def _backup_playlists(self):
        await self._backup_file(
            PLAYLISTS_PATH, f"📦 playlists backup · {len(self.playlists)} user(s)", "_backup_msg_id"
        )

    async def save_settings(self):
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_PATH)
        self.loop.create_task(self._backup_settings())

    async def _backup_settings(self):
        await self._backup_file(
            SETTINGS_PATH, f"⚙️ settings backup · {len(self.settings)} user(s)", "_settings_backup_msg_id"
        )

    async def restore_playlists(self):
        """On a fresh disk (new deploy), pull the latest backup from the data channel."""
        if not PLAYLISTS_CHANNEL_ID or self.playlists:
            return
        channel = self.get_channel(PLAYLISTS_CHANNEL_ID)
        if channel is None:
            log.warning("PLAYLISTS_CHANNEL_ID set but channel not found")
            return
        try:
            async for msg in channel.history(limit=25):
                for att in msg.attachments:
                    if att.filename == "playlists.json":
                        await att.save(PLAYLISTS_PATH)
                        self.playlists = load_playlists()
                        self._backup_msg_id = msg.id
                        log.info("Restored playlists for %d user(s) from backup channel", len(self.playlists))
                        return
            log.info("No playlist backup found in data channel yet")
        except Exception:
            log.exception("Playlist restore failed")

    async def restore_settings(self):
        """On a fresh disk, pull the latest settings backup from the data channel."""
        if not PLAYLISTS_CHANNEL_ID or self.settings:
            return
        channel = self.get_channel(PLAYLISTS_CHANNEL_ID)
        if channel is None:
            return
        try:
            async for msg in channel.history(limit=40):
                for att in msg.attachments:
                    if att.filename == "settings.json":
                        await att.save(SETTINGS_PATH)
                        self.settings = load_settings()
                        self._settings_backup_msg_id = msg.id
                        log.info("Restored settings for %d user(s) from backup channel", len(self.settings))
                        return
        except Exception:
            log.exception("Settings restore failed")


bot = MusicBot()


async def start_keepalive():
    """On hosts like Render, a web service must answer HTTP on $PORT, and the
    free tier idles out without inbound traffic — so serve a health page and
    ping our own public URL every 10 minutes to stay awake."""
    port = os.getenv("PORT")
    if not port:
        return
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="Nuked Music is alive 🎶"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Keepalive HTTP server listening on :%s", port)

    url = os.getenv("RENDER_EXTERNAL_URL")
    if url:
        async def self_ping():
            while True:
                await asyncio.sleep(600)
                try:
                    async with aiohttp.ClientSession() as s:
                        await s.get(url, timeout=aiohttp.ClientTimeout(total=30))
                except Exception:
                    pass
        bot.loop.create_task(self_ping())
        log.info("Self-ping enabled for %s", url)


# --------------------------------------------------------------------------
# Track resolution
# --------------------------------------------------------------------------

async def warm_ytdlp():
    """First extraction after a deploy pays one-time costs (YouTube player JS
    download + Deno signature solve, cached afterwards) — several seconds on a
    0.1-CPU host. Pay them at boot so the first /play doesn't."""
    try:
        t0 = time.monotonic()
        await ytdl_extract(YTDL_PLAY_OPTS, "https://www.youtube.com/watch?v=jNQXAC9IVRw")
        log.info("yt-dlp warmed up in %.1fs", time.monotonic() - t0)
    except Exception as e:
        log.info("yt-dlp warm-up failed (harmless): %s", e)


# One extraction at a time, across all guilds: every yt-dlp run spawns a Deno
# process for YouTube's JS challenge, and two of those at once is exactly how
# the 512MB instance got OOM-killed (Render events, 2026-06-12). On 0.1 CPU,
# serializing is also no slower in wall-clock time than letting them compete.
_EXTRACT_GATE = asyncio.Semaphore(1)


async def ytdl_extract(opts: dict, query: str, gated: bool = True) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)
    if not gated:
        # flat metadata lookups (search results, playlist listings) are one
        # light HTTP call — no Deno, no formats — and may run alongside a
        # heavy extraction so /play can answer while the gate is busy
        return await loop.run_in_executor(None, _run)
    async with _EXTRACT_GATE:
        return await loop.run_in_executor(None, _run)


# Everything playback/embeds actually use — the rest of a yt-dlp result (the
# formats list above all) is hundreds of KB we'd otherwise keep per track.
_INFO_KEEP = ("id", "url", "title", "duration", "webpage_url", "thumbnail",
              "protocol", "acodec", "abr", "http_headers", "is_live")


def _slim_info(info: dict) -> dict:
    return {k: info[k] for k in _INFO_KEEP if k in info}


# Replays of recent songs (queue loop, a favourite on repeat) skip YouTube
# entirely. Stream URLs live ~6 hours; stay comfortably inside that.
_STREAM_CACHE: dict[str, tuple[float, dict]] = {}
_STREAM_CACHE_TTL = 5400
_STREAM_CACHE_MAX = 64


async def extract_play(query: str) -> Optional[dict]:
    """Play-time extraction: fast path first (tv client only, no watch-page
    download), full tv+web pass as a safety net for whatever the fast path
    can't serve. Bot-check errors propagate so callers can back off and retry."""
    cached = _STREAM_CACHE.get(query)
    if cached and time.monotonic() - cached[0] < _STREAM_CACHE_TTL:
        return cached[1]
    info = None
    try:
        info = _first_entry(await ytdl_extract(YTDL_PLAY_OPTS, query))
        if not (info and info.get("url")):
            log.info("Fast extraction gave no stream for %r — retrying with web fallback", query)
            info = None
    except Exception as e:
        if "Sign in to confirm" in str(e):
            raise
        log.info("Fast extraction failed for %r (%s) — retrying with web fallback", query, e)
    if info is None:
        info = _first_entry(await ytdl_extract(YTDL_PLAY_FALLBACK_OPTS, query))
    if info and info.get("url"):
        info = _slim_info(info)
        if not info.get("is_live"):
            _STREAM_CACHE[query] = (time.monotonic(), info)
            while len(_STREAM_CACHE) > _STREAM_CACHE_MAX:
                _STREAM_CACHE.pop(next(iter(_STREAM_CACHE)))
    return info


_SPOTIFY_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


async def _spotify_via_api(kind: str, sid: str, requester: str) -> tuple[list[Track], str]:
    """Best metadata path — needs API creds, and the app owner needs Premium for
    albums/playlists (single tracks work regardless)."""
    loop = asyncio.get_running_loop()
    if kind == "track":
        t = await loop.run_in_executor(None, bot.spotify.track, sid)
        items, name = [t], t["name"]
    elif kind == "album":
        album = await loop.run_in_executor(None, bot.spotify.album, sid)
        items, name = album["tracks"]["items"], f"album **{album['name']}**"
    else:
        pl = await loop.run_in_executor(None, bot.spotify.playlist, sid)
        items = [i["track"] for i in pl["tracks"]["items"] if i.get("track")]
        name = f"playlist **{pl['name']}**"
    tracks = []
    for t in items[:MAX_PLAYLIST_TRACKS]:
        artist = t["artists"][0]["name"] if t.get("artists") else ""
        disp = f"{artist} - {t['name']}" if artist else t["name"]
        tracks.append(Track(
            query=f"ytsearch1:{disp} audio", title=disp, requested_by=requester,
            duration=(t.get("duration_ms") or 0) / 1000 or None,
        ))
    if not tracks:
        raise RuntimeError("empty Spotify result")
    return tracks, (tracks[0].title if kind == "track" else name)


async def _scrape_spotify(kind: str, sid: str, requester: str) -> tuple[list[Track], str]:
    """No-creds path: Spotify's public *embed* page ships the full tracklist as
    JSON, so this covers albums/playlists even when the API blocks them."""
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    async with aiohttp.ClientSession(headers=_SPOTIFY_UA) as s:
        async with s.get(url) as r:
            html = await r.text()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("no embed data")
    entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    name = entity.get("name") or entity.get("title") or kind
    rows = entity.get("trackList") or []
    tracks = []
    for row in rows[:MAX_PLAYLIST_TRACKS]:
        title = (row.get("title") or "").strip()
        artist = (row.get("subtitle") or "").strip()
        if not title:
            continue
        disp = f"{artist} - {title}" if artist else title
        tracks.append(Track(
            query=f"ytsearch1:{disp} audio", title=disp, requested_by=requester,
            duration=(row.get("duration") or 0) / 1000 or None,
        ))
    if not tracks:
        raise RuntimeError("empty embed tracklist")
    desc = tracks[0].title if (kind == "track" or len(tracks) == 1) else f"{kind} **{name}**"
    return tracks, desc


async def _spotify_track_og(sid: str, requester: str) -> tuple[list[Track], str]:
    """Last-ditch single-track fallback: read the og: meta tags."""
    async with aiohttp.ClientSession(headers=_SPOTIFY_UA) as session:
        async with session.get(f"https://open.spotify.com/track/{sid}") as resp:
            html = await resp.text()
    title_m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    desc_m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
    if not title_m:
        raise RuntimeError("Couldn't read that Spotify link.")
    title = title_m.group(1)
    artist = desc_m.group(1).split("·")[0].strip() if desc_m else ""
    display = f"{artist} - {title}" if artist else title
    return [Track(query=f"ytsearch1:{display} audio", title=display, requested_by=requester)], display


async def resolve_spotify(url: str, requester: str) -> tuple[list[Track], str]:
    """Spotify URL -> list of YouTube-search Tracks + a human description.
    Tries the API (if configured), then the public embed scrape, then og tags."""
    m = SPOTIFY_URL_RE.search(url)
    kind, sid = m.group(1), m.group(2)
    if bot.spotify:
        try:
            return await _spotify_via_api(kind, sid, requester)
        except Exception as e:
            log.info("Spotify API path failed for %s/%s (%s) — scraping instead", kind, sid, e)
    try:
        return await _scrape_spotify(kind, sid, requester)
    except Exception as e:
        log.info("Spotify embed scrape failed for %s/%s (%s)", kind, sid, e)
    if kind == "track":
        return await _spotify_track_og(sid, requester)
    raise RuntimeError(
        "Couldn't read that Spotify album/playlist — paste a YouTube link or search by name instead."
    )


def _first_entry(info: Optional[dict]) -> Optional[dict]:
    """Unwrap a search/playlist result down to its first real entry."""
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        return entries[0] if entries else None
    return info


async def resolve_query(query: str, requester: str) -> tuple[list[Track], str]:
    """Anything the user typed -> list of Tracks + description for the reply.

    Deliberately LIGHT: one flat metadata lookup, no stream extraction — so
    /play can answer in a couple of seconds. The heavy per-track extraction
    (player API + JS challenge, ~10s on this host) happens in the player loop
    and the prefetch pump after the reply is already on screen."""
    query = query.strip()

    if SPOTIFY_URL_RE.search(query):
        return await resolve_spotify(query, requester)

    if not URL_RE.match(query):
        query = f"ytsearch1:{query}"

    info = await ytdl_extract(YTDL_QUEUE_OPTS, query, gated=False)
    if info is None:
        raise RuntimeError("Nothing found for that query.")

    if "entries" in info:  # playlist or search results
        entries = [e for e in info["entries"] if e][:MAX_PLAYLIST_TRACKS]
        if not entries:
            raise RuntimeError("Nothing found for that query.")
        if query.startswith("ytsearch"):
            entries = entries[:1]
        tracks = [
            Track(
                query=e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}",
                title=e.get("title") or "Unknown title",
                requested_by=requester,
                duration=e.get("duration"),
                webpage_url=e.get("webpage_url") or e.get("url"),
            )
            for e in entries
        ]
        desc = tracks[0].title if len(tracks) == 1 else f"playlist **{info.get('title', 'playlist')}**"
        return tracks, desc

    track = Track(
        query=info.get("webpage_url") or query,
        title=info.get("title") or "Unknown title",
        requested_by=requester,
        duration=info.get("duration"),
        webpage_url=info.get("webpage_url"),
    )
    return [track], track.title


async def search_results(query: str, n: int = 5) -> list[dict]:
    """Top N YouTube results for the search picker — a flat/light lookup (no
    stream extraction), same cheap call /play already makes for a single hit."""
    opts = {**YTDL_QUEUE_OPTS, "default_search": f"ytsearch{n}"}
    info = await ytdl_extract(opts, f"ytsearch{n}:{query}", gated=False)
    entries = [e for e in (info or {}).get("entries", []) if e][:n]
    return [
        {
            "title": e.get("title") or "Unknown title",
            "url": e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}",
            "duration": e.get("duration"),
            "uploader": e.get("uploader") or e.get("channel") or "",
        }
        for e in entries
    ]


_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([\w-]{11})")


def _youtube_id(s: Optional[str]) -> Optional[str]:
    m = _YT_ID_RE.search(s) if s else None
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Playback loop
# --------------------------------------------------------------------------

async def player_loop(player: GuildPlayer):
    while True:
        # pick next track
        if player.loop_mode == "track" and player.current:
            track = player.current
        else:
            if player.loop_mode == "queue" and player.current:
                player.queue.append(player.current)
            player.current = None
            # Autoplay is decided by whoever queued the song that just ended (the
            # seed): we keep going only if *that* person has autoplay on. Autoplay
            # picks inherit the seed's requester, so a radio one user starts keeps
            # flowing under their preference until someone else queues something.
            seed = player.autoplay_seed
            if (not player.queue and seed and seed.requester_id
                    and user_settings(seed.requester_id)["autoplay"]):
                await try_autoplay(player)
            if not player.queue:
                player.queue_event.clear()
                try:
                    await asyncio.wait_for(player.queue_event.wait(), timeout=IDLE_DISCONNECT_SECONDS)
                except asyncio.TimeoutError:
                    vc = player.guild.voice_client
                    if vc:
                        await vc.disconnect(force=False)
                    if player.text_channel:
                        try:
                            await player.text_channel.send("Left voice channel after 5 minutes of inactivity. 👋")
                        except discord.HTTPException:
                            pass
                    player.task = None
                    await update_presence()
                    return
                continue
            track = player.queue.popleft()

        vc = player.guild.voice_client
        if vc is None or not vc.is_connected():
            player.current = None
            player.task = None
            return

        # fresh stream URL at play time (stored URLs expire). YouTube's bot
        # checks on datacenter IPs are intermittent, so failed extractions get
        # a couple of retries before we give up on the track.
        started = time.monotonic()
        try:
            if track.prefetch_task and not track.prefetch_task.done():
                # a background resolve is already in flight — wait for it
                # instead of racing it with a second extraction
                try:
                    await asyncio.wait_for(asyncio.shield(track.prefetch_task), timeout=60)
                except asyncio.TimeoutError:
                    pass
            track.prefetch_task = None
            info = None
            if track.prefetched and time.monotonic() - track.prefetched_at < PREFETCH_TTL:
                info = track.prefetched  # resolved during /play or the previous song
            track.prefetched = None
            for attempt in range(3):
                if info is not None:
                    break
                try:
                    info = await extract_play(track.query)
                    break
                except Exception as e:
                    if "Sign in to confirm" not in str(e) or attempt == 2:
                        raise
                    log.info("Bot-check on attempt %d for %r, retrying…", attempt + 1, track.title)
                    await asyncio.sleep(1.5)
            if not info or not info.get("url"):
                raise RuntimeError("no stream URL")
            track.title = info.get("title") or track.title
            track.duration = info.get("duration") or track.duration
            track.webpage_url = info.get("webpage_url") or track.webpage_url
            track.thumbnail = info.get("thumbnail") or track.thumbnail
            stream_url = info["url"]
        except Exception as e:
            log.warning("Extraction failed for %r: %s", track.title, e)
            if player.text_channel:
                try:
                    await player.text_channel.send(f"⚠️ Couldn't play **{track.title}**, skipping it.")
                except discord.HTTPException:
                    pass
            player.current = None
            continue

        log.info("Playing %r (protocol=%s, codec=%s, resolved in %.1fs)",
                 track.title, info.get("protocol"), info.get("acodec"), time.monotonic() - started)
        source = make_source(stream_url, info, player.volume_pct, audio_filter=player.audio_filter)
        player.stream_url = stream_url
        player.stream_info = info
        player.current = track
        player.next_event.clear()

        def _after(err):
            if err:
                log.error("Player error: %s", err)
            bot.loop.call_soon_threadsafe(player.next_event.set)

        try:
            vc.play(source, after=_after)
        except discord.ClientException as e:
            log.error("vc.play failed: %s", e)
            player.current = None
            continue

        # Remember this track as the autoplay seed, and note its id so autoplay
        # doesn't loop back to songs we just heard.
        player.autoplay_seed = track
        vid = _youtube_id(track.webpage_url or track.query)
        if vid:
            player.autoplay_history.append(vid)

        await update_presence()

        if player.text_channel and player.loop_mode != "track":
            try:
                msg = await player.text_channel.send(embed=now_playing_embed(player, 0.0), view=CONTROLS)
                player.np_message = msg
                if player.np_task and not player.np_task.done():
                    player.np_task.cancel()
                player.np_task = bot.loop.create_task(np_updater(player, msg, track))
            except discord.HTTPException:
                pass

        # Resolve upcoming streams while this one plays, so the gap between
        # tracks — and a few quick skips in a row — costs no extraction wait.
        pump_prefetch(player)

        await player.next_event.wait()


async def prefetch_track(track: Track):
    """Best-effort early extraction; the player loop falls back to a normal
    extraction if this failed or expired by the time the track comes up."""
    try:
        info = await extract_play(track.query)
        if info and info.get("url"):
            track.prefetched = info
            track.prefetched_at = time.monotonic()
        else:
            track.prefetch_failed = True
    except Exception as e:
        track.prefetch_failed = True
        log.debug("Prefetch failed for %r: %s", track.title, e)


def pump_prefetch(player: GuildPlayer):
    """Keep the next few queued tracks resolved, one extraction at a time;
    each completed prefetch pumps again until PREFETCH_AHEAD are ready."""
    now = time.monotonic()
    for track in list(player.queue)[:PREFETCH_AHEAD]:
        if track.prefetch_failed or (track.prefetched and now - track.prefetched_at < PREFETCH_TTL):
            continue
        if track.prefetch_task and not track.prefetch_task.done():
            return  # one already in flight; its done-callback pumps again
        track.prefetch_task = asyncio.ensure_future(prefetch_track(track))
        track.prefetch_task.add_done_callback(lambda _, p=player: pump_prefetch(p))
        return


def _resolved_first(tracks: list) -> list:
    """After a shuffle, float already-resolved tracks to the front — the order
    is random either way, and it makes the next few skips instant."""
    now = time.monotonic()
    return sorted(tracks, key=lambda t: not (t.prefetched and now - t.prefetched_at < PREFETCH_TTL))


async def try_autoplay(player: GuildPlayer):
    """When the queue empties with autoplay on, queue a song from the YouTube
    'mix' for the last track. Best-effort: gives up on non-YouTube seeds, when
    nothing fresh comes back, or when nobody's left in the channel."""
    vc = player.guild.voice_client
    if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
        return  # don't autoplay to an empty room — let the idle timer disconnect
    seed = player.autoplay_seed
    vid = _youtube_id(seed.webpage_url or seed.query) if seed else None
    if not vid:
        return
    # RD<id> is YouTube's auto-generated radio/mix playlist for that video.
    url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    try:
        info = await ytdl_extract({**YTDL_QUEUE_OPTS, "playlistend": 25}, url, gated=False)
    except Exception as e:
        log.info("Autoplay lookup failed: %s", e)
        return
    for e in [x for x in (info or {}).get("entries", []) if x]:
        eid = e.get("id") or _youtube_id(e.get("url") or "")
        if not eid or eid == vid or eid in player.autoplay_history:
            continue
        track = Track(
            query=e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={eid}",
            title=e.get("title") or "Unknown title",
            requested_by="Autoplay 🔮",
            requester_id=seed.requester_id,  # inherit so the radio stays under the same preference
            duration=e.get("duration"),
            webpage_url=e.get("webpage_url") or e.get("url"),
        )
        player.queue.append(track)
        player.queue_event.set()
        log.info("Autoplay queued %r (seed %s)", track.title, vid)
        return
    log.info("Autoplay found no fresh tracks for seed %s", vid)


async def np_updater(player: GuildPlayer, message: discord.Message, track: Track):
    """Refresh the now-playing card's progress bar every few seconds while its
    song is still the one playing, then stop."""
    try:
        while True:
            await asyncio.sleep(15)
            if player.current is not track:
                return
            vc = player.guild.voice_client
            if not vc or not (vc.is_playing() or vc.is_paused()):
                return
            elapsed = current_elapsed(player)
            try:
                await message.edit(embed=now_playing_embed(player, elapsed), view=CONTROLS)
            except discord.HTTPException:
                return
            if track.duration and elapsed >= track.duration:
                return
    except asyncio.CancelledError:
        return


def ensure_player_task(player: GuildPlayer):
    if player.task is None or player.task.done():
        player.task = bot.loop.create_task(player_loop(player))


# --------------------------------------------------------------------------
# Presence, embeds, button controls
# --------------------------------------------------------------------------

ACCENT = 0x8B5CF6
LOOP_BADGE = {"off": "", "track": " · 🔂 looping track", "queue": " · 🔁 looping queue"}


async def update_presence():
    # Status shows only a count — never song titles or server counts (privacy).
    active = [p for p in bot.players.values() if p.current and p.guild.voice_client]
    if active:
        name = "1 song 🎶" if len(active) == 1 else f"{len(active)} songs 🎶"
    else:
        name = "/play 🎶"
    try:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=name),
            status=discord.Status.online,
        )
    except Exception:
        pass


def now_playing_embed(player: GuildPlayer, elapsed: Optional[float] = None) -> discord.Embed:
    t = player.current
    if elapsed is None:
        elapsed = current_elapsed(player)
    title_md = f"## [{t.title}]({t.webpage_url})" if t.webpage_url else f"## {t.title}"
    desc = (
        f"{title_md}\n{progress_bar(elapsed, t.duration)}\n"
        f"`🔊 {player.volume_pct}%`  ·  requested by **{t.requested_by}**"
    )
    if player.queue:
        desc += f"\n-# ⏭️ Up next: {player.queue[0].title}"
    embed = discord.Embed(description=desc, color=ACCENT)
    if bot.user:
        embed.set_author(name="♪ NOW PLAYING", icon_url=bot.user.display_avatar.url)
    if t.thumbnail:
        embed.set_image(url=t.thumbnail)
    n = len(player.queue)
    badges = (f"{n} track{'s' if n != 1 else ''} in queue" if n else "queue empty") + LOOP_BADGE[player.loop_mode]
    if player.audio_filter != "off":
        badges += f" · 🎚️ {player.audio_filter}"
    if t.requester_id and user_settings(t.requester_id)["autoplay"]:
        badges += " · 🔮 autoplay"
    embed.set_footer(text=badges)
    return embed


def queue_embed(player: GuildPlayer) -> discord.Embed:
    lines = []
    if player.current:
        t = player.current
        now = f"[{t.title}]({t.webpage_url})" if t.webpage_url else t.title
        lines.append(f"**▶️ Now** · {now} `{fmt_duration(t.duration)}`")
    for i, t in enumerate(list(player.queue)[:12], start=1):
        lines.append(f"`{i:>2}.` {t.title} `{fmt_duration(t.duration)}`")
    if len(player.queue) > 12:
        lines.append(f"-# …and {len(player.queue) - 12} more")
    embed = discord.Embed(
        title="Queue",
        description="\n".join(lines) if lines else "Nothing playing and nothing queued. `/play` something!",
        color=ACCENT,
    )
    total = sum(t.duration or 0 for t in player.queue)
    embed.set_footer(
        text=f"{len(player.queue)} queued · {fmt_duration(total) if total else '0:00'} total"
             f" · 🔊 {player.volume_pct}%{LOOP_BADGE[player.loop_mode]}"
    )
    return embed


async def do_stop(guild: discord.Guild):
    player = bot.get_player(guild)
    player.queue.clear()
    player.loop_mode = "off"
    player.current = None
    if player.np_task and not player.np_task.done():
        player.np_task.cancel()
    vc = guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect(force=False)
    if player.task:
        player.task.cancel()
        player.task = None
    await update_presence()


def skip_notice(user, player: GuildPlayer) -> str:
    """Public skip confirmation; warns when the next song isn't resolved yet
    so a few seconds of silence doesn't look like a dead button."""
    nxt = player.queue[0] if player.queue else None
    loading = "" if nxt is None or nxt.prefetched else " ⏳ Getting the next song ready…"
    return f"⏭️ **{user.display_name}** skipped.{loading}"


class MusicControls(discord.ui.View):
    """Persistent button bar attached to now-playing messages."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        log.info(
            "Button %s pressed by %s (gateway latency %.2fs)",
            (interaction.data or {}).get("custom_id", "?"), interaction.user, bot.latency,
        )
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        if isinstance(error, discord.NotFound):
            # interaction expired before our ACK arrived — log it so we can see how often
            log.warning("Button %s ACK arrived too late (interaction expired)", getattr(item, "custom_id", "?"))
            return
        log.error("Button %s failed", getattr(item, "custom_id", "?"), exc_info=error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ That didn't work — try again.", ephemeral=True)
        except discord.HTTPException:
            pass

    # State-changing actions reply publicly with the presser's name — everyone
    # in the channel should see WHO paused/skipped/stopped, not just the presser.
    @discord.ui.button(label="Pause", emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:toggle", row=0)
    async def toggle(self, interaction: discord.Interaction, _):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message(f"⏸️ **{interaction.user.display_name}** paused.")
        elif vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message(f"▶️ **{interaction.user.display_name}** resumed.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip", row=0)
    async def skip_btn(self, interaction: discord.Interaction, _):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            player = bot.get_player(interaction.guild)
            await interaction.response.send_message(skip_notice(interaction.user, player))
            if player.loop_mode == "track":
                player.loop_mode = "off"
            vc.stop()
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(
            f"⏹️ **{interaction.user.display_name}** stopped the music — leaving the voice channel."
        )
        await do_stop(interaction.guild)

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="music:queue", row=1)
    async def queue_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(
            embed=queue_embed(bot.get_player(interaction.guild)), ephemeral=True
        )

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, _):
        player = bot.get_player(interaction.guild)
        if len(player.queue) < 2:
            await interaction.response.send_message("Not enough queued songs to shuffle.", ephemeral=True)
            return
        q = _resolved_first(random.sample(list(player.queue), len(player.queue)))
        player.queue = deque(q)
        pump_prefetch(player)
        await interaction.response.send_message(
            f"🔀 **{interaction.user.display_name}** shuffled {len(q)} tracks."
        )

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1)
    async def loop_btn(self, interaction: discord.Interaction, _):
        player = bot.get_player(interaction.guild)
        player.loop_mode = {"off": "track", "track": "queue", "queue": "off"}[player.loop_mode]
        badge = {"off": "➡️ Loop off", "track": "🔂 Looping this track", "queue": "🔁 Looping the queue"}
        await interaction.response.send_message(
            f"{badge[player.loop_mode]} — **{interaction.user.display_name}**"
        )

# Created in setup_hook, NOT here: a View instantiated before the event loop
# exists has no internal "stopped" future, and discord.py 2.7 silently discards
# every press on it — the client just shows "This interaction failed".
CONTROLS: Optional[MusicControls] = None


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    orig = getattr(error, "original", error)
    if isinstance(orig, discord.NotFound) and getattr(orig, "code", None) == 10062:
        # interaction expired before we could answer (bot busy/restarting) — retrying is the only fix
        log.warning("Interaction for /%s expired before response", interaction.command.name if interaction.command else "?")
        return
    log.error("Command /%s failed", interaction.command.name if interaction.command else "?", exc_info=error)
    try:
        msg = "⚠️ Something went wrong — try that again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# --------------------------------------------------------------------------
# Slash commands
# --------------------------------------------------------------------------

async def require_voice(interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("Join a voice channel first, then use the command again.")
        return None
    return interaction.user.voice.channel


async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """Join (or move to) the caller's voice channel. Interaction must be deferred."""
    channel = await require_voice(interaction)
    if channel is None:
        return None
    vc = interaction.guild.voice_client
    if vc is None:
        try:
            # hard cap: a broken Discord voice region otherwise retries for minutes
            vc = await asyncio.wait_for(channel.connect(timeout=20.0, self_deaf=True), timeout=25.0)
        except (asyncio.TimeoutError, discord.ClientException) as e:
            log.warning("Voice connect failed in guild %s: %r", interaction.guild.id, e)
            try:
                if interaction.guild.voice_client:  # tear down half-open voice state
                    await interaction.guild.voice_client.disconnect(force=True)
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                "⚠️ Discord's voice server for this channel isn't responding. Try the command again — "
                "if it keeps happening, set the channel's **Region Override** (Edit Channel → Overview) "
                "to a region near you instead of Automatic."
            )
            return None
    elif vc.channel != channel and not vc.is_playing():
        await vc.move_to(channel)
    if isinstance(vc.channel, discord.StageChannel) and interaction.guild.me.voice and interaction.guild.me.voice.suppress:
        try:
            await interaction.guild.me.edit(suppress=False)  # become a speaker, not audience
        except discord.HTTPException:
            await interaction.followup.send("⚠️ I'm in a Stage channel and couldn't make myself a speaker — promote me or use a normal voice channel.")
    return vc


async def begin_playback(interaction: discord.Interaction, tracks: list, desc: str,
                         vc: Optional[discord.VoiceClient] = None):
    """Shared tail of /play and the search picker: join voice (unless already
    joined), enqueue the tracks, and post the confirmation. Interaction must be
    deferred or already responded to (followup is used)."""
    if vc is None:
        vc = await ensure_voice(interaction)
    if vc is None:
        return

    player = bot.get_player(interaction.guild)
    player.text_channel = interaction.channel

    for t in tracks:  # mark the requester so autoplay knows whose setting applies
        t.requester_id = interaction.user.id
    starting_now = player.current is None and not player.queue
    player.queue.extend(tracks)
    player.queue_event.set()
    ensure_player_task(player)
    pump_prefetch(player)  # so skips right now land instantly

    if len(tracks) == 1:
        t = tracks[0]
        line = f"**[{t.title}]({t.webpage_url})**" if t.webpage_url else f"**{t.title}**"
        text = f"▶️ {line} — starting now" if starting_now else f"➕ {line} · `#{len(player.queue)} in queue`"
    elif starting_now:
        text = f"▶️ Queued **{len(tracks)}** tracks from {desc} — starting now"
    else:
        text = f"➕ Queued **{len(tracks)}** tracks from {desc} · `{len(player.queue)} in queue`"
    await interaction.followup.send(embed=discord.Embed(description=text, color=ACCENT))


NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def search_picker_embed(query: str, results: list) -> discord.Embed:
    lines = []
    for i, r in enumerate(results):
        lines.append(f"{NUM_EMOJI[i]} **{r['title']}** `{fmt_duration(r['duration'])}`")
        if r["uploader"]:
            lines.append(f"-# {r['uploader']}")
    embed = discord.Embed(title=f"🔎 Results for “{query}”", description="\n".join(lines), color=ACCENT)
    embed.set_footer(text="Pick a track below — expires in 60s")
    return embed


class SearchPicker(discord.ui.View):
    """Ephemeral dropdown of search hits, shown when the search-picker setting
    is on. Created per /play call (never at module level)."""

    def __init__(self, user: discord.abc.User, results: list):
        super().__init__(timeout=60)
        self.owner_id = user.id
        self.requester = user.display_name
        self.results = results
        self.message: Optional[discord.Message] = None
        self.pick.options = [
            discord.SelectOption(
                label=(r["title"][:95] or "Unknown title"),
                value=str(i),
                description=(f"{r['uploader']} · {fmt_duration(r['duration'])}"[:95]) or None,
                emoji=NUM_EMOJI[i],
            )
            for i, r in enumerate(results)
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Start your own search with `/play`.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            if self.message:
                await self.message.edit(content="⌛ Search timed out — run `/play` again.", embed=None, view=self)
        except discord.HTTPException:
            pass

    @discord.ui.select(placeholder="Choose a track…", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        r = self.results[int(select.values[0])]
        track = Track(query=r["url"], title=r["title"], requested_by=self.requester,
                      duration=r["duration"], webpage_url=r["url"])
        await interaction.response.edit_message(content=f"✅ Picked **{r['title']}**", embed=None, view=None)
        self.stop()
        await begin_playback(interaction, [track], track.title)


@bot.tree.command(description="Play music from a link or search — YouTube, Spotify, SoundCloud, and more")
@app_commands.describe(query="Song name or link")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    q = query.strip()
    is_search = not URL_RE.match(q) and not SPOTIFY_URL_RE.search(q)

    # Search picker (opt-in): show the top hits and let the user choose, instead
    # of auto-playing the first result. Only for plain-text searches.
    if is_search and user_settings(interaction.user.id)["search_picker"]:
        try:
            results = await search_results(q, 5)
        except Exception:
            log.exception("search_results failed")
            results = None
        if not results:
            await interaction.followup.send("Nothing found for that query.")
            return
        view = SearchPicker(interaction.user, results)
        view.message = await interaction.followup.send(embed=search_picker_embed(q, results), view=view)
        return

    # Direct path: join voice and resolve the song at the same time — they don't
    # depend on each other, and doing them back-to-back used to double the wait.
    voice_task = asyncio.ensure_future(ensure_voice(interaction))
    try:
        tracks, desc = await resolve_query(query, interaction.user.display_name)
    except RuntimeError as e:
        await voice_task
        await interaction.followup.send(str(e))
        return
    except Exception:
        log.exception("resolve_query failed")
        await voice_task
        await interaction.followup.send("Something went wrong finding that — try a different link or search.")
        return

    vc = await voice_task
    if vc is None:
        return
    await begin_playback(interaction, tracks, desc, vc=vc)


@bot.tree.command(description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        player = bot.get_player(interaction.guild)
        if player.loop_mode == "track":
            player.loop_mode = "off"
        vc.stop()
        await interaction.response.send_message(skip_notice(interaction.user, player))
    else:
        await interaction.response.send_message("Nothing is playing.")


@bot.tree.command(description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.")


@bot.tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.")


@bot.tree.command(description="Stop playback, clear the queue, and disconnect")
async def stop(interaction: discord.Interaction):
    await do_stop(interaction.guild)
    await interaction.response.send_message("⏹️ Stopped and left the voice channel.")


@bot.tree.command(description="View the current queue")
async def queue(interaction: discord.Interaction):
    await interaction.response.send_message(embed=queue_embed(bot.get_player(interaction.guild)))


@bot.tree.command(name="nowplaying", description="Show the current song and playback controls")
async def nowplaying(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild)
    if not player.current:
        await interaction.response.send_message("Nothing is playing.")
        return
    await interaction.response.send_message(embed=now_playing_embed(player), view=CONTROLS)


@bot.tree.command(description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild)
    if len(player.queue) < 2:
        await interaction.response.send_message("Not enough songs in the queue to shuffle.")
        return
    q = _resolved_first(random.sample(list(player.queue), len(player.queue)))
    player.queue = deque(q)
    pump_prefetch(player)
    await interaction.response.send_message(f"🔀 Shuffled {len(q)} tracks.")


@bot.tree.command(description="Loop the current song or the entire queue")
@app_commands.describe(mode="off, track, or queue")
@app_commands.choices(mode=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="track (repeat current song)", value="track"),
    app_commands.Choice(name="queue (repeat whole queue)", value="queue"),
])
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    player = bot.get_player(interaction.guild)
    player.loop_mode = mode.value
    await interaction.response.send_message(f"🔁 Loop mode: **{mode.value}**")


@bot.tree.command(description="Set the volume (1-100)")
@app_commands.describe(percent="Volume percentage, e.g. 50")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 1, 100]):
    player = bot.get_player(interaction.guild)
    player.volume_pct = percent
    await interaction.response.send_message(f"🔊 Volume: **{percent}%**")
    # restart the current stream at the same position with the new volume baked in
    vc = interaction.guild.voice_client
    audio = getattr(vc, "_player", None) if vc else None
    if vc and audio and player.stream_url and (vc.is_playing() or vc.is_paused()):
        try:
            elapsed = audio.loops * 0.02  # 20ms opus frames played so far
            vc.source = make_source(player.stream_url, player.stream_info or {}, percent,
                                    seek=elapsed, audio_filter=player.audio_filter)
        except Exception:
            log.exception("Live volume swap failed — new volume applies from the next song")


@bot.tree.command(name="filter", description="Apply an audio effect (bass boost, nightcore, …)")
@app_commands.describe(effect="Audio effect to apply")
@app_commands.choices(effect=[
    app_commands.Choice(name="Off (normal)", value="off"),
    app_commands.Choice(name="Bass boost", value="bassboost"),
    app_commands.Choice(name="Nightcore (faster + higher)", value="nightcore"),
    app_commands.Choice(name="Vaporwave (slower + deeper)", value="vaporwave"),
    app_commands.Choice(name="8D (rotating)", value="8d"),
    app_commands.Choice(name="Treble boost", value="treble"),
])
async def filter_cmd(interaction: discord.Interaction, effect: app_commands.Choice[str]):
    player = bot.get_player(interaction.guild)
    player.audio_filter = effect.value
    vc = interaction.guild.voice_client
    audio = getattr(vc, "_player", None) if vc else None
    live = bool(vc and audio and player.stream_url and (vc.is_playing() or vc.is_paused()))
    note = "" if (live or not player.current) else " — applies from the next song"
    await interaction.response.send_message(f"🎚️ Filter set to **{effect.value}**{note}.")
    if live:
        # restart the current stream at the same position with the effect baked in
        try:
            elapsed = audio.loops * 0.02
            vc.source = make_source(player.stream_url, player.stream_info or {}, player.volume_pct,
                                    seek=elapsed, audio_filter=effect.value)
        except Exception:
            log.exception("Live filter swap failed — new filter applies from the next song")


@bot.tree.command(description="Remove a song from the queue by its position")
@app_commands.describe(position="Position shown in /queue (1 = next song)")
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1, None]):
    player = bot.get_player(interaction.guild)
    if position > len(player.queue):
        await interaction.response.send_message("No song at that position.")
        return
    q = list(player.queue)
    removed = q.pop(position - 1)
    player.queue = deque(q)
    await interaction.response.send_message(f"🗑️ Removed **{removed.title}**.")


@bot.tree.command(description="Clear the queue (keeps the current song playing)")
async def clear(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild)
    n = len(player.queue)
    player.queue.clear()
    await interaction.response.send_message(f"🧹 Cleared {n} queued tracks.")


# --------------------------------------------------------------------------
# Personal settings (/settings panel)
# --------------------------------------------------------------------------

def settings_embed(user: discord.abc.User) -> discord.Embed:
    s = user_settings(user.id)
    onoff = lambda b: "🟢 **On**" if b else "⚪ Off"
    desc = (
        f"🔎 **Search picker** — {onoff(s['search_picker'])}\n"
        "-# When *you* run `/play` with a search, show a menu of the top results to "
        "choose from instead of auto-playing the first hit. Only affects your own searches.\n\n"
        f"🔮 **Autoplay** — {onoff(s['autoplay'])}\n"
        "-# When the queue runs out after a song *you* queued, keep the music going with "
        "related songs instead of leaving."
    )
    embed = discord.Embed(title=f"⚙️ {user.display_name}'s music settings", description=desc, color=ACCENT)
    embed.set_footer(text="These are personal to you — everyone has their own.")
    return embed


class SettingsView(discord.ui.View):
    """Ephemeral toggle panel for the caller's personal settings. Per-invocation."""

    def __init__(self, user: discord.abc.User):
        super().__init__(timeout=180)
        self.user = user
        self.owner_id = user.id
        self.message: Optional[discord.Message] = None
        self._sync_labels()

    def _sync_labels(self):
        s = user_settings(self.owner_id)
        self.toggle_picker.label = f"Search picker: {'ON' if s['search_picker'] else 'OFF'}"
        self.toggle_picker.style = discord.ButtonStyle.success if s["search_picker"] else discord.ButtonStyle.secondary
        self.toggle_autoplay.label = f"Autoplay: {'ON' if s['autoplay'] else 'OFF'}"
        self.toggle_autoplay.style = discord.ButtonStyle.success if s["autoplay"] else discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("These are someone else's — open yours with `/settings`.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _flip(self, interaction: discord.Interaction, key: str):
        s = user_settings(self.owner_id)
        s[key] = not s[key]
        await bot.save_settings()
        self._sync_labels()
        await interaction.response.edit_message(embed=settings_embed(self.user), view=self)

    @discord.ui.button(label="Search picker", emoji="🔎", row=0)
    async def toggle_picker(self, interaction: discord.Interaction, _):
        await self._flip(interaction, "search_picker")

    @discord.ui.button(label="Autoplay", emoji="🔮", row=0)
    async def toggle_autoplay(self, interaction: discord.Interaction, _):
        await self._flip(interaction, "autoplay")


@bot.tree.command(description="Your personal music settings (search picker, autoplay)")
async def settings(interaction: discord.Interaction):
    view = SettingsView(interaction.user)
    await interaction.response.send_message(embed=settings_embed(interaction.user), view=view, ephemeral=True)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        pass


# --------------------------------------------------------------------------
# Saved playlists (/playlists panel)
# --------------------------------------------------------------------------

def user_playlists(user: discord.abc.User) -> dict:
    return bot.playlists.setdefault(str(user.id), {})


def track_to_dict(t: Track) -> dict:
    return {"query": t.query, "title": t.title, "duration": t.duration, "webpage_url": t.webpage_url}


def dict_to_track(d: dict, requester: str) -> Track:
    return Track(
        query=d["query"], title=d.get("title", "Unknown title"), requested_by=requester,
        duration=d.get("duration"), webpage_url=d.get("webpage_url"),
    )


def playlist_songs_embed(name: str, tracks: list) -> discord.Embed:
    lines = [
        f"`{i:>2}.` {t.get('title', '?')} `{fmt_duration(t.get('duration'))}`"
        for i, t in enumerate(tracks[:20], start=1)
    ]
    if len(tracks) > 20:
        lines.append(f"-# …and {len(tracks) - 20} more")
    embed = discord.Embed(title=f"🎶 {name}", description="\n".join(lines) or "This playlist is empty.", color=ACCENT)
    embed.set_footer(text=f"{len(tracks)} song{'s' if len(tracks) != 1 else ''}")
    return embed


async def queue_saved_playlist(interaction: discord.Interaction, name: str, shuffled: bool):
    """Queues one of the caller's saved playlists. Interaction must be deferred."""
    tracks_data = user_playlists(interaction.user).get(name)
    if not tracks_data:
        await interaction.followup.send(f"Playlist **{name}** is empty or missing.")
        return
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    tracks = [dict_to_track(d, interaction.user.display_name) for d in tracks_data]
    for t in tracks:
        t.requester_id = interaction.user.id
    if shuffled:
        random.shuffle(tracks)
    player = bot.get_player(interaction.guild)
    player.text_channel = interaction.channel
    player.queue.extend(tracks)
    player.queue_event.set()
    ensure_player_task(player)
    pump_prefetch(player)
    await interaction.followup.send(
        embed=discord.Embed(
            description=f"▶️ Queued **{len(tracks)}** songs from playlist **{name}**" + (" 🔀" if shuffled else ""),
            color=ACCENT,
        )
    )


class ConfirmDeletePlaylist(discord.ui.View):
    def __init__(self, owner_id: int, name: str):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.name = name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Yes, delete it", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _):
        pls = bot.playlists.get(str(self.owner_id), {})
        n = len(pls.pop(self.name, []))
        await bot.save_playlists()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"🗑️ Deleted playlist **{self.name}** ({n} songs).", view=self
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Kept it — nothing was deleted.", view=self)
        self.stop()


def panel_embed(user: discord.abc.User, selected: Optional[str]) -> discord.Embed:
    pls = bot.playlists.get(str(user.id), {})
    if pls:
        desc = "\n".join(
            f"{'▶️' if n == selected else '🎶'} **{n}** — {len(ts)} song{'s' if len(ts) != 1 else ''}"
            for n, ts in pls.items()
        )
    else:
        desc = "You don't have any playlists yet — hit **✨ New** to make one!"
    embed = discord.Embed(title=f"🎛️ {user.display_name}'s playlists", description=desc, color=ACCENT)
    embed.set_footer(text=f"Selected: {selected}" if selected else "Pick a playlist below, then choose an action")
    return embed


class AddSongModal(discord.ui.Modal, title="Add a song"):
    query = discord.ui.TextInput(label="Song name or link", placeholder="song name — or a YouTube/Spotify link", max_length=300)

    def __init__(self, name: str):
        super().__init__()
        self.playlist_name = name

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pls = user_playlists(interaction.user)
        if self.playlist_name not in pls:
            await interaction.followup.send(f"Playlist **{self.playlist_name}** no longer exists.")
            return
        try:
            tracks, _ = await resolve_query(str(self.query), interaction.user.display_name)
        except RuntimeError as e:
            await interaction.followup.send(str(e))
            return
        except Exception:
            log.exception("resolve_query failed in AddSongModal")
            await interaction.followup.send("Something went wrong finding that — try a different link or search.")
            return
        space = MAX_PLAYLIST_TRACKS - len(pls[self.playlist_name])
        if space <= 0:
            await interaction.followup.send(f"**{self.playlist_name}** is full ({MAX_PLAYLIST_TRACKS} songs).")
            return
        added = tracks[:space]
        pls[self.playlist_name].extend(track_to_dict(t) for t in added)
        await bot.save_playlists()
        what = f"**{added[0].title}**" if len(added) == 1 else f"**{len(added)}** songs"
        await interaction.followup.send(f"➕ Added {what} to **{self.playlist_name}** ({len(pls[self.playlist_name])} songs).")


class SaveQueueModal(discord.ui.Modal, title="Save queue as playlist"):
    name = discord.ui.TextInput(label="Playlist name", max_length=60)

    def __init__(self, panel: "PlaylistPanel"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        nm = str(self.name).strip()
        player = bot.get_player(interaction.guild)
        tracks = ([player.current] if player.current else []) + list(player.queue)
        if not tracks:
            await interaction.response.send_message("Nothing is playing or queued to save.", ephemeral=True)
            return
        pls = user_playlists(interaction.user)
        if nm in pls:
            await interaction.response.send_message(f"You already have a playlist called **{nm}**.", ephemeral=True)
            return
        if len(pls) >= MAX_SAVED_PLAYLISTS:
            await interaction.response.send_message(f"Limit of {MAX_SAVED_PLAYLISTS} playlists reached — delete one first.", ephemeral=True)
            return
        pls[nm] = [track_to_dict(t) for t in tracks[:MAX_PLAYLIST_TRACKS]]
        await bot.save_playlists()
        self.panel.selected = nm
        self.panel.rebuild_select(interaction.user)
        await interaction.response.edit_message(embed=panel_embed(interaction.user, nm), view=self.panel)


class RemoveSongModal(discord.ui.Modal, title="Remove a song"):
    position = discord.ui.TextInput(label="Song number (check the Songs list)", max_length=4)

    def __init__(self, name: str):
        super().__init__()
        self.playlist_name = name

    async def on_submit(self, interaction: discord.Interaction):
        pls = user_playlists(interaction.user)
        tracks = pls.get(self.playlist_name)
        if not tracks:
            await interaction.response.send_message(f"Playlist **{self.playlist_name}** is empty or missing.", ephemeral=True)
            return
        try:
            pos = int(str(self.position).strip())
        except ValueError:
            await interaction.response.send_message("That's not a number.", ephemeral=True)
            return
        if not 1 <= pos <= len(tracks):
            await interaction.response.send_message(f"Pick a number between 1 and {len(tracks)}.", ephemeral=True)
            return
        removed = tracks.pop(pos - 1)
        await bot.save_playlists()
        await interaction.response.send_message(
            f"🗑️ Removed the **song** `{removed.get('title', '?')}` from **{self.playlist_name}** "
            f"— {len(tracks)} left. (The playlist itself is safe!)",
            ephemeral=True,
        )


class NewPlaylistModal(discord.ui.Modal, title="New playlist"):
    name = discord.ui.TextInput(label="Playlist name", max_length=60)

    def __init__(self, panel: "PlaylistPanel"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        nm = str(self.name).strip()
        pls = user_playlists(interaction.user)
        if nm in pls:
            await interaction.response.send_message(f"You already have a playlist called **{nm}**.", ephemeral=True)
            return
        if len(pls) >= MAX_SAVED_PLAYLISTS:
            await interaction.response.send_message(f"Limit of {MAX_SAVED_PLAYLISTS} playlists reached — delete one first.", ephemeral=True)
            return
        pls[nm] = []
        await bot.save_playlists()
        self.panel.selected = nm
        self.panel.rebuild_select(interaction.user)
        await interaction.response.edit_message(embed=panel_embed(interaction.user, nm), view=self.panel)


class PlaylistPanel(discord.ui.View):
    """Interactive ephemeral panel: dropdown + actions for the caller's playlists."""

    def __init__(self, user: discord.abc.User):
        super().__init__(timeout=600)
        self.owner_id = user.id
        self.selected: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self.rebuild_select(user)

    def rebuild_select(self, user: discord.abc.User):
        names = list(bot.playlists.get(str(user.id), {}).keys())
        if names:
            self.pick.options = [
                discord.SelectOption(label=n, value=n, emoji="🎶", default=(n == self.selected))
                for n in names
            ]
            self.pick.disabled = False
        else:
            self.pick.options = [discord.SelectOption(label="(no playlists yet)", value="__none__")]
            self.pick.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This panel belongs to someone else — open yours with `/playlists`.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    def _picked(self) -> Optional[str]:
        pls = bot.playlists.get(str(self.owner_id), {})
        return self.selected if self.selected in pls else None

    @discord.ui.select(placeholder="Pick a playlist…", row=0, min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        if select.values[0] != "__none__":
            self.selected = select.values[0]
        self.rebuild_select(interaction.user)
        await interaction.response.edit_message(embed=panel_embed(interaction.user, self.selected), view=self)

    @discord.ui.button(label="Play", emoji="▶️", style=discord.ButtonStyle.success, row=1)
    async def play_btn(self, interaction: discord.Interaction, _):
        await self._start(interaction, shuffled=False)

    @discord.ui.button(label="Shuffle play", emoji="🔀", style=discord.ButtonStyle.success, row=1)
    async def shuffle_play_btn(self, interaction: discord.Interaction, _):
        await self._start(interaction, shuffled=True)

    async def _start(self, interaction: discord.Interaction, shuffled: bool):
        name = self._picked()
        if not name:
            await interaction.response.send_message("Pick a playlist in the dropdown first.", ephemeral=True)
            return
        await interaction.response.defer()
        await queue_saved_playlist(interaction, name, shuffled)

    @discord.ui.button(label="Songs", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def show_btn(self, interaction: discord.Interaction, _):
        name = self._picked()
        if not name:
            await interaction.response.send_message("Pick a playlist in the dropdown first.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=playlist_songs_embed(name, user_playlists(interaction.user)[name]), ephemeral=True
        )

    @discord.ui.button(label="Add song", emoji="🎵", style=discord.ButtonStyle.secondary, row=2)
    async def add_song_btn(self, interaction: discord.Interaction, _):
        name = self._picked()
        if not name:
            await interaction.response.send_message("Pick a playlist in the dropdown first.", ephemeral=True)
            return
        await interaction.response.send_modal(AddSongModal(name))

    @discord.ui.button(label="Add current song", emoji="➕", style=discord.ButtonStyle.secondary, row=2)
    async def add_current_btn(self, interaction: discord.Interaction, _):
        name = self._picked()
        if not name:
            await interaction.response.send_message("Pick a playlist in the dropdown first.", ephemeral=True)
            return
        player = bot.get_player(interaction.guild)
        if not player.current:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        pls = user_playlists(interaction.user)
        if len(pls[name]) >= MAX_PLAYLIST_TRACKS:
            await interaction.response.send_message(f"**{name}** is full ({MAX_PLAYLIST_TRACKS} songs).", ephemeral=True)
            return
        pls[name].append(track_to_dict(player.current))
        await bot.save_playlists()
        await interaction.response.send_message(
            f"➕ Added **{player.current.title}** to **{name}** ({len(pls[name])} songs).", ephemeral=True
        )

    @discord.ui.button(label="Save queue", emoji="💾", style=discord.ButtonStyle.secondary, row=2)
    async def save_queue_btn(self, interaction: discord.Interaction, _):
        player = bot.get_player(interaction.guild)
        if not player.current and not player.queue:
            await interaction.response.send_message("Nothing is playing or queued to save.", ephemeral=True)
            return
        await interaction.response.send_modal(SaveQueueModal(self))

    @discord.ui.button(label="New", emoji="✨", style=discord.ButtonStyle.primary, row=3)
    async def new_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(NewPlaylistModal(self))

    @discord.ui.button(label="Remove song", emoji="➖", style=discord.ButtonStyle.secondary, row=3)
    async def remove_song_btn(self, interaction: discord.Interaction, _):
        name = self._picked()
        if not name:
            await interaction.response.send_message("Pick a playlist in the dropdown first.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveSongModal(name))

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=3)
    async def delete_btn(self, interaction: discord.Interaction, _):
        name = self._picked()
        if not name:
            await interaction.response.send_message("Pick a playlist in the dropdown first.", ephemeral=True)
            return
        count = len(user_playlists(interaction.user)[name])
        await interaction.response.send_message(
            f"Really delete playlist **{name}** and its **{count}** songs? This can't be undone.",
            view=ConfirmDeletePlaylist(interaction.user.id, name),
            ephemeral=True,
        )


@bot.tree.command(name="playlists", description="Create, play, and manage your saved playlists")
async def playlists_cmd(interaction: discord.Interaction):
    view = PlaylistPanel(interaction.user)
    await interaction.response.send_message(embed=panel_embed(interaction.user, None), view=view, ephemeral=True)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    try:
        import uvloop  # faster event loop on Linux hosts
        uvloop.install()
    except ImportError:
        pass
    bot.run(TOKEN, log_handler=None)
