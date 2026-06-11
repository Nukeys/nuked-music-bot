"""One-off bot profile setup: avatar, banner, description, tags.

Reads images from assets/pfp.png and assets/banner.png (either may be absent).
Safe to re-run, but Discord rate-limits avatar changes — don't run it repeatedly.
"""
import base64
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN or TOKEN == "paste-your-token-here":
    sys.exit("No token in .env")

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {TOKEN}"}

DESCRIPTION = (
    "music bot for the server. throw it a youtube, spotify or soundcloud link, "
    "or just type the song name and it'll find it.\n\n"
    "/play - play something\n"
    "/playlists - your saved playlists\n\n"
    "everything else is buttons. made for nuked community"
)
TAGS = ["music", "youtube", "spotify", "playlists", "soundcloud"]


def data_uri(path: str) -> str:
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def patch(url: str, payload: dict, what: str):
    r = requests.patch(url, headers=HEADERS, json=payload, timeout=30)
    if r.ok:
        print(f"[ok] {what}")
    else:
        print(f"[FAILED] {what}: HTTP {r.status_code} {r.text[:300]}")


user_payload = {}
pfp = os.path.join(BOT_DIR, "assets", "pfp.png")
banner = os.path.join(BOT_DIR, "assets", "banner.png")
if os.path.exists(pfp):
    user_payload["avatar"] = data_uri(pfp)
else:
    print("[skip] no assets/pfp.png")
if os.path.exists(banner):
    user_payload["banner"] = data_uri(banner)
else:
    print("[skip] no assets/banner.png — drop the banner image there and re-run")

if user_payload:
    patch(f"{API}/users/@me", user_payload, "avatar/banner")

patch(f"{API}/applications/@me", {"description": DESCRIPTION, "tags": TAGS}, "description + tags")
print("Done. Profile changes can take a minute (or a Discord restart, Ctrl+R) to show.")
