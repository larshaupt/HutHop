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
