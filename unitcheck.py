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
assert len(sel) == 10, len(sel)  # select + 9 buttons
# legacy command group is gone, /playlists exists
cmds = [c.name for c in b.bot.tree.get_commands()]
assert "playlists" in cmds and "playlist" not in cmds, cmds
# audio source builder produces an opus source with volume baked in
import inspect
sig = inspect.signature(b.make_source)
assert list(sig.parameters) == ["stream_url", "info", "volume_pct", "seek"]
print("unit checks PASSED")
