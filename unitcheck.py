import os
os.environ.setdefault("DISCORD_TOKEN", "selftest-dummy")
import bot as b

assert abs(b.volume_amp(100) - 1.0) < 1e-9
assert 0.29 < b.volume_amp(50) < 0.32, b.volume_amp(50)
assert b.volume_amp(0) == 0.0
assert hasattr(b, "PlaylistPanel") and hasattr(b, "ConfirmDeletePlaylist")
assert hasattr(b, "queue_embed") and hasattr(b, "start_keepalive")
p = b.GuildPlayer(guild=None)
assert p.volume_pct == 50
rows = sorted(set(i.row for i in b.CONTROLS.children))
assert len(b.CONTROLS.children) == 6 and rows == [0, 1], (len(b.CONTROLS.children), rows)
sel = [i for i in b.PlaylistPanel.__view_children_items__]
assert len(sel) == 7  # select + 6 buttons
print("unit checks PASSED")
