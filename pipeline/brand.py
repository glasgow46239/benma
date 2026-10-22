"""
Britain Elects house colours for the pipeline, read from the central style guide.

    from brand import party_colour, known_colours
    party_colour("Reform")            # '#15B6C2'
    party_colour("Con (to 2024 election)")  # '#05718F'

Source of truth: https://glasgow46239.github.io/style/tokens.json
If that can't be fetched (offline, Pages outage), falls back to the bundled
snapshot in brand_tokens.json so nightly runs never fail on colour.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

TOKENS_URL = "https://glasgow46239.github.io/style/tokens.json"
SNAPSHOT = Path(__file__).with_name("brand_tokens.json")


@lru_cache(maxsize=1)
def tokens():
    try:
        import requests
        r = requests.get(TOKENS_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        try:
            SNAPSHOT.write_text(json.dumps(data, indent=2))  # keep the fallback fresh
        except OSError:
            pass
        return data
    except Exception as e:  # noqa: BLE001 - any failure falls back to the snapshot
        print(f"brand: using bundled snapshot ({e.__class__.__name__})")
        return json.loads(SNAPSHOT.read_text())


def _all_parties():
    t = tokens()
    return {**t["parties"], **{k: v for k, v in t["secondaryParties"].items() if not k.startswith("$")}}


def party_key(name):
    """'Lib Dem', 'LD', 'LDem', 'Con (post-2024 election)' -> canonical key, or None."""
    t, parties = tokens(), _all_parties()
    for candidate in (name, str(name).split("(")[0]):
        n = re.sub(r"[^a-z]", "", str(candidate).lower())
        if n in parties:
            return n
        if n in t["aliases"]:
            return t["aliases"][n]
    return None


def party_colour(name, kind="fill", dark=False, default=None):
    """kind: 'fill' (lines, bars, maps) or 'text' (party-coloured text)."""
    k = party_key(name)
    if k:
        p = _all_parties()[k]
    else:
        t, n = tokens(), re.sub(r"[^a-z]", "", str(name).split("(")[0].lower())
        p = t["signal"].get(n) or t["signal"].get(t.get("signalAliases", {}).get(n, ""))
        if not p:
            return default
    field = {("fill", False): "fill", ("fill", True): "fillDark",
             ("text", False): "text", ("text", True): "textDark"}[(kind, dark)]
    return p[field]


def signal_colour(which, kind="fill"):
    """which: 'positive' | 'negative' | 'neutral'."""
    return tokens()["signal"][which][kind]


def known_colours():
    """{alias_or_key: fill} for every spelling the style guide knows."""
    t, parties = tokens(), _all_parties()
    out = {k: v["fill"] for k, v in parties.items()}
    out.update({a: parties[k]["fill"] for a, k in t["aliases"].items() if k in parties})
    return out


def datawrapper_colours(labels):
    """Map Datawrapper column/row labels to house fills, skipping anything unrecognised."""
    out = {}
    for label in labels:
        c = party_colour(label)
        if c:
            out[label] = c
    return out


party_color = party_colour  # US spelling alias
