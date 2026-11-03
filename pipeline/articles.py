"""
Write site/articles.json from the Britain Elects Substack RSS feed, for the
"From Britain Elects" strip on the website. Keeps the last good file if the
feed can't be fetched, so a Substack outage never empties the strip.

Usage: python pipeline/articles.py
"""
import email.utils
import json
import os
import re
import xml.etree.ElementTree as ET

FEED = "https://britainelects.substack.com/feed"
OUT = "site/articles.json"
KEEP = 30


def text(node, tag):
    el = node.find(tag)
    return (el.text or "").strip() if el is not None else ""


def main():
    import requests
    try:
        r = requests.get(FEED, timeout=20, headers={"User-Agent": "britain-elects-site/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        print(f"articles: feed unavailable ({e.__class__.__name__}); keeping {OUT}")
        return
    items = []
    for it in list(root.iter("item"))[:KEEP]:
        date = email.utils.parsedate_to_datetime(text(it, "pubDate")).date().isoformat() if text(it, "pubDate") else ""
        desc = re.sub(r"<[^>]+>", "", text(it, "description")).strip()
        items.append({"title": text(it, "title"), "url": text(it, "link"), "date": date, "desc": desc})
    if not items:
        print("articles: feed had no items; keeping existing file")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"source": FEED, "items": items}, f, indent=1, ensure_ascii=False)
    print(f"articles: wrote {len(items)} items")


if __name__ == "__main__":
    main()
