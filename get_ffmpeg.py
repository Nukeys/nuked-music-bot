"""Downloads a static Linux ffmpeg build next to the bot (used by hosts whose
containers lack ffmpeg). Pure stdlib — no curl/wget/xz needed."""
import os
import shutil
import tarfile
import urllib.request

URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "ffmpeg")
TMP = os.path.join(HERE, ".ffmpeg_tmp")

if os.path.exists(DEST):
    print("ffmpeg already present, skipping download")
    raise SystemExit(0)

print("Downloading static ffmpeg (~80 MB), one-time setup...")
os.makedirs(TMP, exist_ok=True)
tar_path = os.path.join(TMP, "ffmpeg.tar.xz")
urllib.request.urlretrieve(URL, tar_path)
with tarfile.open(tar_path) as tf:
    member = next(m for m in tf.getmembers() if m.name.endswith("/ffmpeg"))
    member.name = "ffmpeg"
    tf.extract(member, TMP)
shutil.move(os.path.join(TMP, "ffmpeg"), DEST)
shutil.rmtree(TMP, ignore_errors=True)
os.chmod(DEST, 0o755)
print("ffmpeg installed at", DEST)
