"""The service worker's app shell (web/sw.js) lists every frontend file, so the app also starts offline."""

import re

from app.db import ROOT


def test_shell_lists_every_frontend_file():
    web = ROOT / "web"
    sw = (web / "sw.js").read_text()
    listed = set(re.search(r"const FILES = \[(.*?)\];", sw, re.S).group(1).replace('"', "").replace(",", " ").split())
    files = {f"/{p.relative_to(web)}" for p in web.rglob("*") if p.suffix in (".js", ".css", ".png") and p.name != "sw.js"}
    assert files <= listed, f"missing in sw.js: {files - listed}"
    assert all(path == "/" or (web / path[1:]).is_file() for path in listed), "sw.js lists a file that doesn't exist"


def test_offline_copy_covers_every_base_map_host():
    """A saved tour's map tiles come from several hosts (js/map.js: swisstopo, basemap.at and OpenTopoMap past the
    border); sw.js must answer all of them from the cache. The trail overlay isn't saved."""
    web = ROOT / "web"
    hosts = set(re.findall(r'["`](https://[^/"`]+/)[^"`]*\{z\}', (web / "js" / "map.js").read_text()))
    hosts.discard("https://tile.waymarkedtrails.org/")
    cached = re.search(r"const MAP_HOSTS = \[(.*?)\];", (web / "sw.js").read_text(), re.S).group(1)
    assert hosts and all(host in cached for host in hosts), f"missing in sw.js MAP_HOSTS: {hosts}"
