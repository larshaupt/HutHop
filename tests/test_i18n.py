"""Translations (web/js/i18n/): every text of the page has a German entry, and the entries fit their keys."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
UNTRANSLATED = {"HutHop", "SAC"}  # the same in every language


def dictionary(lang):
    """A dictionary file: `export default {…}` with strict JSON inside, after the comment lines."""
    text = (WEB / "js" / "i18n" / f"{lang}.js").read_text()
    body = "\n".join(line for line in text.splitlines() if not line.startswith("//"))
    return json.loads(body.removeprefix("export default"))


def js_keys():
    """The keys of t("…") calls in the scripts (tr("…") where `t` is a tour)."""
    keys = set()
    for path in (WEB / "js").glob("*.js"):
        if path.name == "i18n.js":
            continue  # its comments show t() calls
        for m in re.finditer(r'\bt[r]?\("((?:[^"\\]|\\.)*)"', path.read_text()):
            keys.add(json.loads(f'"{m.group(1)}"'))
    return keys


class PageText(HTMLParser):
    """The text i18n.js translatePage() translates: text nodes and a few attributes of index.html."""

    def __init__(self):
        super().__init__()
        self.texts = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("aria-label", "title", "placeholder") or (tag == "meta" and name == "content"
                                                                   and ("name", "description") in attrs):
                self.texts.add(value)

    def handle_data(self, data):
        self.texts.add(data.strip())


def page_texts():
    parser = PageText()
    parser.feed((WEB / "index.html").read_text())
    return {s for s in parser.texts if re.search(r"[A-Za-z]{2}", s) and s not in UNTRANSLATED}


def placeholders(entry):
    texts = entry.values() if isinstance(entry, dict) else [entry]
    return set().union(*(re.findall(r"\{\w+\}", s) for s in texts))


def test_every_text_has_german():
    de = dictionary("de")
    missing = sorted((js_keys() | page_texts()) - de.keys())
    assert not missing, f"no German for: {missing}"


def test_german_fits_its_keys():
    de, en = dictionary("de"), dictionary("en")
    for key, value in de.items():
        assert "ß" not in json.dumps(value, ensure_ascii=False), key  # Swiss usage: ss
        assert placeholders(value) == placeholders(en.get(key, key)), key
        if isinstance(value, dict) or isinstance(en.get(key), dict):
            assert set(value) == {"one", "other"} == set(en[key]), key


def test_no_unused_entries():
    """A German entry whose English text is nowhere in the code (changed or removed) is stale. Keys passed to t()
    as variables are found in the API code (app/) instead."""
    code = "".join(p.read_text() for p in [*(WEB / "js").glob("*.js"), WEB / "index.html"])
    code += re.sub(r'"\s*\n\s*"', "", "".join(p.read_text() for p in (ROOT / "app").rglob("*.py")))  # "a" "b" → "ab"
    stale = sorted(k for k in dictionary("de") if k not in code)
    assert not stale, f"unused German entries: {stale}"
