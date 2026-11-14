"""
Write site/articles.json from the Britain Elects Substack, for the website's article strips
and homepage. Tries the RSS feed first, then Substack's archive API. Keeps the last good file
if both fail, so a Substack outage never empties the site.

Each item: title, url, date (YYYY-MM-DD), desc, image (cover image URL or "").

Usage: python pipeline/articles.py
"""
import email.utils
import json
import os
import re
import xml.etree.ElementTree as ET

SITE = "https://britainelects.substack.com"
FEED = SITE + "/feed"
API = SITE + "/api/v1/archive?sort=new&limit=30"
OUT = "site/articles.json"
KEEP = 30
# Substack refuses requests that don't look like a browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml;q=0.9, application/json;q=0.8, */*;q=0.5",
    "Accept-Language": "en-GB,en;q=0.9",
}
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def text(node, tag):
    el = node.find(tag)
    return (el.text or "").strip() if el is not None else ""


def from_feed(body):
    root = ET.fromstring(body)
    items = []
    for it in list(root.iter("item"))[:KEEP]:
        pub = text(it, "pubDate")
        date = email.utils.parsedate_to_datetime(pub).date().isoformat() if pub else ""
        image = ""
        enc = it.find("enclosure")
        if enc is not None and (enc.get("type") or "").startswith("image"):
            image = enc.get("url") or ""
        if not image:
            m = re.search(r'<img[^>]+src="([^"]+)"', text(it, CONTENT))
            image = m.group(1) if m else ""
        desc = re.sub(r"<[^>]+>", "", text(it, "description")).strip()
        items.append({"title": text(it, "title"), "url": text(it, "link"), "date": date, "desc": desc, "image": image})
    return items


def from_api(posts):
    items = []
    for p in posts[:KEEP]:
        if p.get("type") not in (None, "newsletter", "thread", "podcast"):
            continue
        items.append({
            "title": p.get("title") or "",
            "url": p.get("canonical_url") or "",
            "date": (p.get("post_date") or "")[:10],
            "desc": p.get("subtitle") or p.get("description") or "",
            "image": p.get("cover_image") or "",
        })
    return items


def main():
    import requests
    items = []
    for name, url, parse in (("feed", FEED, lambda r: from_feed(r.content)), ("api", API, lambda r: from_api(r.json()))):
        try:
            r = requests.get(url, timeout=20, headers=HEADERS)
            if r.status_code != 200:
                print(f"articles: {name} returned HTTP {r.status_code}")
                continue
            items = parse(r)
            if items:
                print(f"articles: {len(items)} items from the {name}")
                break
        except Exception as e:  # noqa: BLE001
            print(f"articles: {name} failed ({e.__class__.__name__}: {e})")
    if not items:
        print(f"articles: nothing fetched; keeping {OUT}")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"source": SITE, "items": items}, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
