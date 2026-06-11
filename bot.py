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

SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)"
)
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

YTDL_PLAY_OPTS = {
    "format": "bestaudio[acodec!=none]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "skip_download": True,
}

YTDL_QUEUE_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "skip_download": True,
    "playlistend": MAX_PLAYLIST_TRACKS,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTS = "-vn"

# FFmpeg's own errors land here — first place to look if a song won't play
FFMPEG_LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.log"), "ab")

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


def fmt_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "live/unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


@dataclass
class Track:
    query: str                      # URL or "ytsearch1:..." fed to yt-dlp at play time
    title: str
    requested_by: str
    duration: Optional[float] = None
    webpage_url: Optional[str] = None
    thumbnail: Optional[str] = None


@dataclass
class GuildPlayer:
    guild: discord.Guild
    queue: deque = field(default_factory=deque)
    queue_event: asyncio.Event = field(default_factory=asyncio.Event)
    next_event: asyncio.Event = field(default_factory=asyncio.Event)
    loop_mode: str = "off"          # off | track | queue
    volume: float = 0.75
    current: Optional[Track] = None
    text_channel: Optional[discord.abc.Messageable] = None
    task: Optional[asyncio.Task] = None


def load_playlists() -> dict:
    try:
        with open(PLAYLISTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.players: dict[int, GuildPlayer] = {}
        # saved playlists: {user_id: {name: [{query,title,duration,webpage_url}]}}
        self.playlists: dict = load_playlists()
        self._playlists_lock = asyncio.Lock()
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
        await self.tree.sync()  # global sync (can take up to an hour to propagate the first time)

    async def on_ready(self):
        log.info("Logged in as %s (%s) — in %d server(s)", self.user, self.user.id, len(self.guilds))
        # Commands live in the global set only (synced in setup_hook). Earlier
        # versions also pushed per-guild copies, which Discord displays as
        # duplicates — wipe any guild-scoped leftovers.
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            except discord.HTTPException as e:
                log.warning("Guild command cleanup failed for %s: %s", guild.name, e)
        if not getattr(self, "_controls_registered", False):
            self.add_view(CONTROLS)  # keeps buttons working after restarts
            self._controls_registered = True
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


bot = MusicBot()


# --------------------------------------------------------------------------
# Track resolution
# --------------------------------------------------------------------------

async def ytdl_extract(opts: dict, query: str) -> dict:
    loop = asyncio.get_running_loop()
    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)
    return await loop.run_in_executor(None, _run)


async def resolve_spotify(url: str, requester: str) -> tuple[list[Track], str]:
    """Spotify URL -> list of YouTube-search Tracks + a human description."""
    m = SPOTIFY_URL_RE.search(url)
    kind, sid = m.group(1), m.group(2)

    if bot.spotify:
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
            tracks.append(Track(
                query=f"ytsearch1:{artist} - {t['name']} audio",
                title=f"{artist} - {t['name']}",
                requested_by=requester,
                duration=(t.get("duration_ms") or 0) / 1000 or None,
            ))
        return tracks, name

    # No API credentials: scrape og: meta tags (works for single tracks)
    if kind != "track":
        raise RuntimeError(
            "Spotify albums/playlists need free Spotify API credentials in the .env file "
            "(single track links work without them). See README.md."
        )
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MusicBot/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"https://open.spotify.com/track/{sid}") as resp:
            html = await resp.text()
    title_m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    desc_m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
    if not title_m:
        raise RuntimeError("Couldn't read that Spotify link — try adding Spotify API credentials.")
    title = title_m.group(1)
    artist = desc_m.group(1).split("·")[0].strip() if desc_m else ""
    display = f"{artist} - {title}" if artist else title
    return [Track(query=f"ytsearch1:{display} audio", title=display, requested_by=requester)], display


async def resolve_query(query: str, requester: str) -> tuple[list[Track], str]:
    """Anything the user typed -> list of Tracks + description for the reply."""
    query = query.strip()

    if SPOTIFY_URL_RE.search(query):
        return await resolve_spotify(query, requester)

    if not URL_RE.match(query):
        query = f"ytsearch1:{query}"

    info = await ytdl_extract(YTDL_QUEUE_OPTS, query)
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

        # fresh stream URL at play time (stored URLs expire)
        try:
            info = await ytdl_extract(YTDL_PLAY_OPTS, track.query)
            if info and "entries" in info:
                entries = [e for e in info["entries"] if e]
                info = entries[0] if entries else None
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

        log.info("Playing %r (protocol=%s, codec=%s)", track.title, info.get("protocol"), info.get("acodec"))
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                stream_url,
                executable=FFMPEG_EXE,
                before_options=ffmpeg_before_options(info),
                options=FFMPEG_OPTS,
                stderr=FFMPEG_LOG,
            ),
            volume=player.volume,
        )
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

        await update_presence()

        if player.text_channel and player.loop_mode != "track":
            try:
                await player.text_channel.send(embed=now_playing_embed(player), view=CONTROLS)
            except discord.HTTPException:
                pass

        await player.next_event.wait()


def ensure_player_task(player: GuildPlayer):
    if player.task is None or player.task.done():
        player.task = bot.loop.create_task(player_loop(player))


# --------------------------------------------------------------------------
# Presence, embeds, button controls
# --------------------------------------------------------------------------

ACCENT = 0x8B5CF6
LOOP_BADGE = {"off": "", "track": " · 🔂 looping track", "queue": " · 🔁 looping queue"}


async def update_presence():
    active = [p for p in bot.players.values() if p.current and p.guild.voice_client]
    if len(active) == 1:
        name = active[0].current.title[:100]
    elif active:
        name = f"music in {len(active)} servers"
    else:
        name = "/play 🎶"
    try:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=name),
            status=discord.Status.online,
        )
    except Exception:
        pass


def now_playing_embed(player: GuildPlayer) -> discord.Embed:
    t = player.current
    title_md = f"## [{t.title}]({t.webpage_url})" if t.webpage_url else f"## {t.title}"
    embed = discord.Embed(
        description=f"{title_md}\n`{fmt_duration(t.duration)}` · requested by **{t.requested_by}**",
        color=ACCENT,
    )
    if bot.user:
        embed.set_author(name="Now Playing", icon_url=bot.user.display_avatar.url)
    if t.thumbnail:
        embed.set_thumbnail(url=t.thumbnail)
    n = len(player.queue)
    up_next = f"{n} track{'s' if n != 1 else ''} up next" if n else "nothing up next"
    embed.set_footer(text=f"{up_next} · 🔊 {int(player.volume * 100)}%{LOOP_BADGE[player.loop_mode]}")
    return embed


async def do_stop(guild: discord.Guild):
    player = bot.get_player(guild)
    player.queue.clear()
    player.loop_mode = "off"
    player.current = None
    vc = guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect(force=False)
    if player.task:
        player.task.cancel()
        player.task = None
    await update_presence()


class MusicControls(discord.ui.View):
    """Persistent button bar attached to now-playing messages."""

    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        if isinstance(error, discord.NotFound):
            return  # interaction expired (bot was busy or restarting mid-click) — nothing useful to do
        log.error("Button %s failed", getattr(item, "custom_id", "?"), exc_info=error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ That didn't work — try again.", ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Pause", emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:toggle")
    async def toggle(self, interaction: discord.Interaction, _):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        elif vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip_btn(self, interaction: discord.Interaction, _):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)
            player = bot.get_player(interaction.guild)
            if player.loop_mode == "track":
                player.loop_mode = "off"
            vc.stop()
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, _):
        player = bot.get_player(interaction.guild)
        if len(player.queue) < 2:
            await interaction.response.send_message("Not enough queued songs to shuffle.", ephemeral=True)
            return
        q = list(player.queue)
        random.shuffle(q)
        player.queue = deque(q)
        await interaction.response.send_message(f"🔀 Shuffled {len(q)} tracks.", ephemeral=True)

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop_btn(self, interaction: discord.Interaction, _):
        player = bot.get_player(interaction.guild)
        player.loop_mode = {"off": "track", "track": "queue", "queue": "off"}[player.loop_mode]
        badge = {"off": "➡️ Loop off", "track": "🔂 Looping this track", "queue": "🔁 Looping the queue"}
        await interaction.response.send_message(badge[player.loop_mode], ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_message("⏹️ Stopped and left the voice channel.", ephemeral=True)
        await do_stop(interaction.guild)


CONTROLS = MusicControls()


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


@bot.tree.command(description="Play a song from a link (YouTube/Spotify/SoundCloud/...) or a search query")
@app_commands.describe(query="A link or something to search for")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc is None:
        return

    player = bot.get_player(interaction.guild)
    player.text_channel = interaction.channel

    try:
        tracks, desc = await resolve_query(query, interaction.user.display_name)
    except RuntimeError as e:
        await interaction.followup.send(str(e))
        return
    except Exception:
        log.exception("resolve_query failed")
        await interaction.followup.send("Something went wrong finding that — try a different link or search.")
        return

    starting_now = player.current is None and not player.queue
    player.queue.extend(tracks)
    player.queue_event.set()
    ensure_player_task(player)

    if len(tracks) == 1:
        t = tracks[0]
        line = f"**[{t.title}]({t.webpage_url})**" if t.webpage_url else f"**{t.title}**"
        if starting_now:
            text = f"▶️ {line} — starting now"
        else:
            text = f"➕ {line} · `#{len(player.queue)} in queue`"
    elif starting_now:
        text = f"▶️ Queued **{len(tracks)}** tracks from {desc} — starting now"
    else:
        text = f"➕ Queued **{len(tracks)}** tracks from {desc} · `{len(player.queue)} in queue`"
    await interaction.followup.send(embed=discord.Embed(description=text, color=ACCENT))


@bot.tree.command(description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        player = bot.get_player(interaction.guild)
        if player.loop_mode == "track":
            player.loop_mode = "off"
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.")
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


@bot.tree.command(description="Stop playback, clear the queue, and leave the voice channel")
async def stop(interaction: discord.Interaction):
    await do_stop(interaction.guild)
    await interaction.response.send_message("⏹️ Stopped and left the voice channel.")


@bot.tree.command(description="Show the queue")
async def queue(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild)
    lines = []
    if player.current:
        lines.append(f"**Now:** {player.current.title} ({fmt_duration(player.current.duration)})")
    for i, t in enumerate(list(player.queue)[:15], start=1):
        lines.append(f"`{i}.` {t.title} ({fmt_duration(t.duration)})")
    if len(player.queue) > 15:
        lines.append(f"...and {len(player.queue) - 15} more")
    if not lines:
        await interaction.response.send_message("The queue is empty.")
        return
    embed = discord.Embed(title="Queue", description="\n".join(lines), color=ACCENT)
    if player.loop_mode != "off":
        embed.set_footer(text=f"Loop: {player.loop_mode}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Show the current song")
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
    q = list(player.queue)
    random.shuffle(q)
    player.queue = deque(q)
    await interaction.response.send_message(f"🔀 Shuffled {len(q)} tracks.")


@bot.tree.command(description="Set loop mode")
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


@bot.tree.command(description="Set the volume (1-150)")
@app_commands.describe(percent="Volume percentage, e.g. 80")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 1, 150]):
    player = bot.get_player(interaction.guild)
    player.volume = percent / 100
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to {percent}%.")


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
# Saved playlists (/playlist ...)
# --------------------------------------------------------------------------

playlist_group = app_commands.Group(name="playlist", description="Create and play your saved playlists")


def user_playlists(user: discord.abc.User) -> dict:
    return bot.playlists.setdefault(str(user.id), {})


def track_to_dict(t: Track) -> dict:
    return {"query": t.query, "title": t.title, "duration": t.duration, "webpage_url": t.webpage_url}


def dict_to_track(d: dict, requester: str) -> Track:
    return Track(
        query=d["query"], title=d.get("title", "Unknown title"), requested_by=requester,
        duration=d.get("duration"), webpage_url=d.get("webpage_url"),
    )


async def playlist_name_autocomplete(interaction: discord.Interaction, current: str):
    names = bot.playlists.get(str(interaction.user.id), {})
    return [
        app_commands.Choice(name=n, value=n)
        for n in names if current.lower() in n.lower()
    ][:25]


@playlist_group.command(name="create", description="Create a new empty playlist")
@app_commands.describe(name="Name for the new playlist")
async def pl_create(interaction: discord.Interaction, name: app_commands.Range[str, 1, 60]):
    name = name.strip()
    pls = user_playlists(interaction.user)
    if name in pls:
        await interaction.response.send_message(f"You already have a playlist called **{name}**.")
        return
    if len(pls) >= MAX_SAVED_PLAYLISTS:
        await interaction.response.send_message(f"You've hit the limit of {MAX_SAVED_PLAYLISTS} playlists — delete one first.")
        return
    pls[name] = []
    await bot.save_playlists()
    await interaction.response.send_message(f"🎶 Created playlist **{name}**. Add songs with `/playlist add`.")


@playlist_group.command(name="add", description="Add a song (or a whole playlist link) to one of your playlists")
@app_commands.describe(name="Your playlist", query="Link or search query to add")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_add(interaction: discord.Interaction, name: str, query: str):
    await interaction.response.defer()
    pls = user_playlists(interaction.user)
    if name not in pls:
        await interaction.followup.send(f"You don't have a playlist called **{name}**. Create it with `/playlist create`.")
        return
    try:
        tracks, _ = await resolve_query(query, interaction.user.display_name)
    except RuntimeError as e:
        await interaction.followup.send(str(e))
        return
    except Exception:
        log.exception("resolve_query failed in /playlist add")
        await interaction.followup.send("Something went wrong finding that — try a different link or search.")
        return
    space = MAX_PLAYLIST_TRACKS - len(pls[name])
    if space <= 0:
        await interaction.followup.send(f"**{name}** is full ({MAX_PLAYLIST_TRACKS} tracks).")
        return
    added = tracks[:space]
    pls[name].extend(track_to_dict(t) for t in added)
    await bot.save_playlists()
    if len(added) == 1:
        await interaction.followup.send(f"➕ Added **{added[0].title}** to **{name}** ({len(pls[name])} tracks).")
    else:
        await interaction.followup.send(f"➕ Added **{len(added)}** tracks to **{name}** ({len(pls[name])} total).")


@playlist_group.command(name="play", description="Queue up one of your saved playlists")
@app_commands.describe(name="Your playlist", shuffled="Shuffle it before queueing")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_play(interaction: discord.Interaction, name: str, shuffled: bool = False):
    await interaction.response.defer()
    pls = user_playlists(interaction.user)
    if name not in pls:
        await interaction.followup.send(f"You don't have a playlist called **{name}**.")
        return
    if not pls[name]:
        await interaction.followup.send(f"**{name}** is empty — add songs with `/playlist add`.")
        return
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    tracks = [dict_to_track(d, interaction.user.display_name) for d in pls[name]]
    if shuffled:
        random.shuffle(tracks)
    player = bot.get_player(interaction.guild)
    player.text_channel = interaction.channel
    player.queue.extend(tracks)
    player.queue_event.set()
    ensure_player_task(player)
    await interaction.followup.send(
        f"▶️ Queued **{len(tracks)}** tracks from your playlist **{name}**" + (" (shuffled)." if shuffled else ".")
    )


@playlist_group.command(name="save", description="Save the current queue (including the playing song) as a playlist")
@app_commands.describe(name="Name for the new playlist")
async def pl_save(interaction: discord.Interaction, name: app_commands.Range[str, 1, 60]):
    name = name.strip()
    player = bot.get_player(interaction.guild)
    tracks = ([player.current] if player.current else []) + list(player.queue)
    if not tracks:
        await interaction.response.send_message("Nothing is playing or queued to save.")
        return
    pls = user_playlists(interaction.user)
    if name in pls:
        await interaction.response.send_message(f"You already have a playlist called **{name}** — pick another name.")
        return
    if len(pls) >= MAX_SAVED_PLAYLISTS:
        await interaction.response.send_message(f"You've hit the limit of {MAX_SAVED_PLAYLISTS} playlists — delete one first.")
        return
    pls[name] = [track_to_dict(t) for t in tracks[:MAX_PLAYLIST_TRACKS]]
    await bot.save_playlists()
    await interaction.response.send_message(f"💾 Saved **{len(pls[name])}** tracks as playlist **{name}**.")


@playlist_group.command(name="list", description="List your saved playlists")
async def pl_list(interaction: discord.Interaction):
    pls = bot.playlists.get(str(interaction.user.id), {})
    if not pls:
        await interaction.response.send_message("You have no playlists yet. Start with `/playlist create`.")
        return
    lines = [f"• **{n}** — {len(ts)} track{'s' if len(ts) != 1 else ''}" for n, ts in pls.items()]
    embed = discord.Embed(title=f"{interaction.user.display_name}'s playlists",
                          description="\n".join(lines), color=ACCENT)
    await interaction.response.send_message(embed=embed)


@playlist_group.command(name="show", description="Show the songs in one of your playlists")
@app_commands.describe(name="Your playlist")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_show(interaction: discord.Interaction, name: str):
    pls = user_playlists(interaction.user)
    if name not in pls:
        await interaction.response.send_message(f"You don't have a playlist called **{name}**.")
        return
    tracks = pls[name]
    if not tracks:
        await interaction.response.send_message(f"**{name}** is empty.")
        return
    lines = [f"`{i}.` {t.get('title', '?')} ({fmt_duration(t.get('duration'))})"
             for i, t in enumerate(tracks[:20], start=1)]
    if len(tracks) > 20:
        lines.append(f"...and {len(tracks) - 20} more")
    embed = discord.Embed(title=f"Playlist: {name}", description="\n".join(lines), color=ACCENT)
    await interaction.response.send_message(embed=embed)


@playlist_group.command(name="remove", description="Remove one song from a playlist by its position")
@app_commands.describe(name="Your playlist", position="Position shown in /playlist show")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_remove(interaction: discord.Interaction, name: str, position: app_commands.Range[int, 1, None]):
    pls = user_playlists(interaction.user)
    if name not in pls:
        await interaction.response.send_message(f"You don't have a playlist called **{name}**.")
        return
    if position > len(pls[name]):
        await interaction.response.send_message("No song at that position.")
        return
    removed = pls[name].pop(position - 1)
    await bot.save_playlists()
    await interaction.response.send_message(f"🗑️ Removed **{removed.get('title', '?')}** from **{name}**.")


@playlist_group.command(name="delete", description="Delete one of your playlists entirely")
@app_commands.describe(name="Your playlist")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_delete(interaction: discord.Interaction, name: str):
    pls = user_playlists(interaction.user)
    if name not in pls:
        await interaction.response.send_message(f"You don't have a playlist called **{name}**.")
        return
    n = len(pls.pop(name))
    await bot.save_playlists()
    await interaction.response.send_message(f"🗑️ Deleted playlist **{name}** ({n} tracks).")


bot.tree.add_command(playlist_group)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
