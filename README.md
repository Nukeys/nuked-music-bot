# Discord Music Bot

Plays music in voice channels from **YouTube links, Spotify links, SoundCloud,
Bandcamp, direct audio links, or plain-text search** (`/play never gonna give you up`).

Spotify note: Spotify forbids third-party audio streaming, so (like every music
bot) Spotify links are resolved to song titles and the audio is played from
YouTube.

## Commands

| Command | What it does |
|---|---|
| `/play <link or search>` | Queue a song or playlist; joins your voice channel |
| `/skip` | Skip current song |
| `/pause` / `/resume` | Pause / resume |
| `/stop` | Stop, clear queue, leave channel |
| `/queue` | Show the queue |
| `/nowplaying` | Show current song |
| `/shuffle` | Shuffle the queue |
| `/loop <off\|track\|queue>` | Loop mode |
| `/volume <1-150>` | Set volume |
| `/remove <position>` | Remove one queued song |
| `/clear` | Clear the queue |

### Saved playlists (per user, persist across restarts)

| Command | What it does |
|---|---|
| `/playlist create <name>` | Create an empty playlist |
| `/playlist add <name> <link or search>` | Add a song — or a whole YouTube/Spotify playlist link |
| `/playlist play <name> [shuffled]` | Queue the whole playlist, optionally shuffled |
| `/playlist save <name>` | Save what's currently playing + queued as a playlist |
| `/playlist list` / `/playlist show <name>` | Browse your playlists |
| `/playlist remove <name> <position>` | Remove one song |
| `/playlist delete <name>` | Delete a playlist |

Playlists are yours (tied to your Discord account) and work in any server the
bot is in. They're stored in `playlists.json` next to the bot — when moving to
a server host, copy that file along (on ephemeral hosts like Render free tier,
use a persistent disk/volume or playlists reset on redeploy).

## One-time setup (~5 minutes)

### 1. Create the bot on Discord

1. Go to https://discord.com/developers/applications → **New Application** → name it.
2. Left sidebar → **Bot** → click **Reset Token** → copy the token.
   *Treat this token like a password. Anyone who has it controls your bot.*
3. Still under **Bot**: no privileged intents are needed — leave them off.
4. Left sidebar → **Installation**: under *Install Link* pick **Discord provided link**, and under *Default Install Settings → Guild Install* add scopes `bot` + `applications.commands` with permissions: **Connect, Speak, Send Messages, Embed Links**.
   (Or just use the invite URL the bot prints to its console when it starts.)

### 2. Give the bot your token (safely)

Run this in the bot folder — input is hidden, nothing is stored in shell history:

```powershell
powershell -ExecutionPolicy Bypass -File .\set-token.ps1
```

(Or open `.env` in Notepad and paste the token after `DISCORD_TOKEN=`.)
The `.env` file stays on this machine and is git-ignored. Never paste the token
into chat, Discord messages, or code.

### 3. (Optional) Spotify album/playlist support

Single Spotify track links already work. For **albums and playlists**:
1. https://developer.spotify.com/dashboard → **Create app** (any name, redirect URI `http://localhost`).
2. Copy *Client ID* and *Client Secret* into `.env`.

### 4. Start it

Double-click **`start.bat`** (or `.venv\Scripts\python.exe bot.py`).
The console prints an invite URL — open it to add the bot to your server.
Join a voice channel and type `/play`.

## Running 24/7 on a free server

The bot must be running for music to play. Free options, best first:

1. **Oracle Cloud "Always Free"** (best: a real VPS, never sleeps, free forever)
   - Sign up at https://www.oracle.com/cloud/free/ (needs a credit card for
     identity verification — it is not charged).
   - Create an *Always Free* ARM (Ampere A1) Ubuntu instance.
   - Copy this folder to the server, then:
     ```bash
     sudo apt update && sudo apt install -y ffmpeg python3-venv
     python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
     # put the token in .env, then run it as a service:
     .venv/bin/python bot.py
     ```
   - Or use the included `Dockerfile`:
     `docker build -t musicbot . && docker run -d --restart unless-stopped -e DISCORD_TOKEN=... musicbot`

2. **bot-hosting.net** (free, made for Discord bots, no credit card — sign in
   with Discord, create a Python server, upload `bot.py` + `requirements.txt`,
   set `DISCORD_TOKEN` as an environment variable/secret in their panel).
   Note: ffmpeg availability varies on free panels — test `/play` after deploying.

3. **This PC** — just run `start.bat` whenever you want music. Free and zero
   setup; bot is offline when the PC is off.

On any host, supply the token via the host's **environment variable / secrets
panel**, never by hard-coding it in the source.

## Troubleshooting

- **"Couldn't play X"** on many videos → YouTube changed something; update the
  extractor: `.venv\Scripts\python.exe -m pip install -U yt-dlp` and restart.
- **Slash commands not appearing** → wait a minute, restart Discord (Ctrl+R),
  or kick + re-invite the bot.
- **Robotic/choppy audio on a server** → the free instance is CPU-starved;
  Oracle's A1 instances are plenty, web-panel free tiers sometimes are not.
