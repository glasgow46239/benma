"""
Export compact page data for the Britain Elects website (britainelectsdotcom).
-------------------------------------------------------------------------------
Runs after smooth.py. For every tracker whose config.json has a "page" block it
reads the line-<subgroup>.csv files smooth.py already wrote and writes:

    <data dir>/page.json            index: page settings, groups, latest figures,
                                    figures `changeDays` before the latest data
                                    (and, for leader pages, the comparison set)
    <data dir>/page-<slug>.json     one file per group: smoothed segments,
                                    uncertainty bands and individual polls

Pages that combine several trackers (the all-leaders page) are defined in
pages/<name>.json instead; see export_leaders_page().

Numbers are stored as integer tenths and dates as day offsets, to keep files
small. The site loads page.json first and then only the group being viewed.

Usage:
    python pipeline/export_page.py --all
    python pipeline/export_page.py --config trackers/vi_westminster_24/config.json

The "page" block (all keys optional except "type"):

    "page": {
      "type": "vi" | "leader",
      "cards": ["Con", "Lab", "LDem", "Grn", "Reform"],    # headline-card parties
      "cardsBy": {"scotland": [..., "SNP"]},               # per-group overrides
      "extras": ["Ukip"],                                  # drawn on the chart only
      "extrasBy": {"18-34": []},
      "windows": {"Ukip": [null, "2019-12-12"]},           # drop data outside dates
      "changeDays": 60,
      "groups": [{"id": "overall", "label": "All voters", "section": "", "short": "voters"}, ...],
      # leader pages only
      "person": "Andy Burnham", "tookOffice": "2026-07-16",
      "compare": [{"name": "Keir Starmer", "surname": "Starmer", "party": "Lab",
                   "tracker": "ldr_starmer_all", "from": "2024-07-05", "to": "2026-07-16"}, ...],
      "compareMaxDays": 1100
    }
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os

EPOCH = dt.date(1970, 1, 1)

DEFAULT_GROUPS = [
    {"id": "overall", "label": "All voters", "section": "", "short": "voters"},
    {"id": "18-34", "label": "18–34 year olds", "section": "Age", "short": "18–34 year olds"},
    {"id": "35-49", "label": "35–49 year olds", "section": "Age", "short": "35–49 year olds"},
    {"id": "50-64", "label": "50–64 year olds", "section": "Age", "short": "50–64 year olds"},
    {"id": "65+", "label": "Over-65s", "section": "Age", "short": "over-65s"},
    {"id": "abc1", "label": "ABC1 voters", "section": "Social grade", "short": "ABC1 voters"},
    {"id": "c2de", "label": "C2DE voters", "section": "Social grade", "short": "C2DE voters"},
    {"id": "scotland", "label": "Scotland", "section": "Nation and region", "short": "Scottish voters"},
    {"id": "wales", "label": "Wales", "section": "Nation and region", "short": "Welsh voters"},
    {"id": "london", "label": "London", "section": "Nation and region", "short": "voters in London"},
    {"id": "southern-england", "label": "Southern England", "section": "Nation and region", "short": "voters in southern England"},
    {"id": "midlands", "label": "Midlands", "section": "Nation and region", "short": "voters in the Midlands"},
    {"id": "northern-england", "label": "Northern England", "section": "Nation and region", "short": "voters in northern England"},
]


def day(s):
    return (dt.date.fromisoformat(s) - EPOCH).days


def t10(v):
    return int(round(v * 10))


def read_line_csv(path, parties, windows):
    """Return {party: {"segs": [[(day, mean, low, high), ...], ...], "polls": [(day, value), ...]}}."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    head, data = rows[0], rows[1:]
    days = [day(r[0]) for r in data]
    out = {}
    for p in parties:
        lo_d, hi_d = -10 ** 9, 10 ** 9
        if p in windows:
            a, b = windows[p]
            lo_d = day(a) if a else lo_d
            hi_d = day(b) if b else hi_d
        seg_cols = [c for c in head
                    if (c == p or (c.startswith(p + " (") and c.endswith(")")))
                    and not c.endswith("(low)") and not c.endswith("(high)") and c != p + " (poll)"]
        segs = []
        for c in seg_cols:
            im, il, ih = head.index(c), head.index(c + " (low)"), head.index(c + " (high)")
            pts = []
            for k, r in enumerate(data):
                if r[im] == "" or not (lo_d <= days[k] <= hi_d):
                    continue
                m = float(r[im])
                pts.append((days[k], m, float(r[il] or m), float(r[ih] or m)))
            if len(pts) >= 2:
                segs.append(pts)
        segs.sort(key=lambda s: s[0][0])
        polls = []
        pc = p + " (poll)"
        if pc in head:
            ip = head.index(pc)
            polls = [(days[k], float(r[ip])) for k, r in enumerate(data)
                     if r[ip] != "" and lo_d <= days[k] <= hi_d]
        if segs or polls:
            out[p] = {"segs": segs, "polls": polls}
    return out


def encode_segs(segs):
    enc = []
    for pts in segs:
        enc.append({
            "d0": pts[0][0],
            "dd": [b[0] - a[0] for a, b in zip(pts, pts[1:])],
            "m": [t10(x[1]) for x in pts],
            "lo": [t10(x[1] - x[2]) for x in pts],
            "hi": [t10(x[3] - x[1]) for x in pts],
        })
    return enc


def value_near(segs, target):
    pts = [x for s in segs for x in s]
    return min(pts, key=lambda x: abs(x[0] - target))[1] if pts else None


def export_tracker(config_path):
    with open(config_path) as f:
        config = json.load(f)
    page = config.get("page")
    if not page:
        return None
    data_dir = os.path.dirname(config["output"]["lineCsvPath"])
    base_parties = [p["name"] for p in config.get("parties", []) if p.get("includeInLine", True)]
    cards_default = page.get("cards", base_parties)
    extras_default = page.get("extras", [])
    windows = page.get("windows", {})
    change_days = page.get("changeDays", 60)
    groups_out = []
    for g in page.get("groups", DEFAULT_GROUPS):
        path = os.path.join(data_dir, f"line-{g['id']}.csv")
        if not os.path.exists(path):
            continue
        cards = page.get("cardsBy", {}).get(g["id"], cards_default)
        lines = cards + [p for p in page.get("extrasBy", {}).get(g["id"], extras_default) if p not in cards]
        series = read_line_csv(path, lines, windows)
        latest, ago = {}, {}
        end = max(s[-1][0] for v in series.values() for s in v["segs"])
        start = min(s[0][0] for v in series.values() for s in v["segs"])
        for p in cards:
            if p not in series or not series[p]["segs"]:
                continue
            last = series[p]["segs"][-1]
            latest[p] = round(last[-1][1], 2)
            ago[p] = round(value_near(series[p]["segs"], last[-1][0] - change_days), 2)
        group_file = f"page-{g['id']}.json"
        with open(os.path.join(data_dir, group_file), "w") as f:
            json.dump({p: {"segs": encode_segs(v["segs"]), "polls": [[d, t10(x)] for d, x in v["polls"]]}
                       for p, v in series.items()}, f, separators=(",", ":"))
        groups_out.append({**g, "file": group_file, "cards": [p for p in cards if p in latest],
                           "lines": [p for p in lines if p in series], "start": start, "end": end,
                           "latest": latest, "ago": ago,
                           "bandwidthDays": next((s.get("bandwidthDays") for s in config.get("subgroups", [])
                                                  if s["value"].replace(" ", "-") == g["id"]),
                                                 config.get("smoothing", {}).get("bandwidthDays"))})
    index = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "type": page["type"],
        "title": config.get("trackerName", ""),
        "changeDays": change_days,
        "breaks": [b["date"] for b in config.get("breaks", [])],
        "groups": groups_out,
    }
    for k in ("person", "tookOffice", "office"):
        if k in page:
            index[k] = page[k]
    if page.get("compare"):
        index["compare"] = export_compare(page["compare"], page.get("compareMaxDays", 1100), page.get("tookOffice"),
                                          config["output"]["lineCsvPath"])
    with open(os.path.join(data_dir, "page.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))
    return data_dir, [g["id"] for g in groups_out]


def export_compare(leaders, max_days, own_start, own_line_csv):
    """Net, positive and negative views of each leader by day in office, from their overall tracker."""
    out = []
    for L in leaders:
        if L.get("tracker"):
            with open(f"trackers/{L['tracker']}/config.json") as f:
                c = json.load(f)
            line_csv = c["output"]["lineCsvPath"]
        else:
            line_csv = own_line_csv
        path = os.path.join(os.path.dirname(line_csv), "line-overall.csv")
        if not os.path.exists(path):
            print(f"  compare: missing {path}, skipping {L['name']}")
            continue
        s = read_line_csv(path, ["Positive", "Negative"], {})
        s0 = day(L["from"])
        s1 = day(L["to"]) if L.get("to") else 10 ** 9
        vals = {}
        for p in ("Positive", "Negative"):
            vals[p] = {x[0] - s0: x[1] for seg in s.get(p, {}).get("segs", []) for x in seg
                       if s0 <= x[0] < s1 and x[0] - s0 <= max_days}
        common = sorted(set(vals["Positive"]) & set(vals["Negative"]))
        if not common:
            continue
        first, last = common[0], common[-1]
        out.append({
            "name": L["name"], "surname": L.get("surname", L["name"].split()[-1]), "party": L["party"],
            "from": L["from"], "to": L.get("to"),
            "officeDays": (s1 - s0) if L.get("to") else None,
            "first": first, "last": last,
            "pos": [t10(vals["Positive"][k]) if k in vals["Positive"] else None for k in range(first, last + 1)],
            "neg": [t10(vals["Negative"][k]) if k in vals["Negative"] else None for k in range(first, last + 1)],
        })
    return out


def encode_lines(segs):
    """Like encode_segs but means only (no bands): for multi-leader comparison charts."""
    return [{"d0": pts[0][0], "dd": [b[0] - a[0] for a, b in zip(pts, pts[1:])], "m": [t10(x[1]) for x in pts]}
            for pts in segs]


def export_leaders_page(path):
    """A page comparing several leaders, defined in pages/<name>.json:

    {"type": "leaders", "out": "site/leaders", "changeDays": 60,
     "leaders": [{"name": "Andy Burnham", "surname": "Burnham", "party": "Lab", "role": "Prime Minister",
                  "tracker": "ldr_burnham_all", "since": "2026-07-16", "url": "leaders/burnham/"}, ...]}

    Each leader's data is taken from the day they became leader ("since"). Writes <out>/page.json
    (groups, and per leader its latest and earlier figures) and <out>/page-<group>.json
    (net, positive and negative lines per leader).
    """
    with open(path) as f:
        spec = json.load(f)
    out_dir = spec["out"]
    os.makedirs(out_dir, exist_ok=True)
    change_days = spec.get("changeDays", 60)
    groups_out = []
    for g in spec.get("groups", DEFAULT_GROUPS):
        payload, meta, start, end = {}, {}, 10 ** 9, -10 ** 9
        for L in spec["leaders"]:
            with open(f"trackers/{L['tracker']}/config.json") as f:
                c = json.load(f)
            csv_path = os.path.join(os.path.dirname(c["output"]["lineCsvPath"]), f"line-{g['id']}.csv")
            if not os.path.exists(csv_path):
                continue
            since = day(L["since"]) if L.get("since") else -10 ** 9
            s = read_line_csv(csv_path, ["Positive", "Negative"], {"Positive": [L.get("since"), None], "Negative": [L.get("since"), None]})
            if "Positive" not in s or "Negative" not in s:
                continue
            pos_segs, neg_segs = s["Positive"]["segs"], s["Negative"]["segs"]
            net_segs = []
            for ps in pos_segs:
                negmap = {x[0]: x[1] for ns in neg_segs for x in ns}
                pts = [(x[0], x[1] - negmap[x[0]], 0, 0) for x in ps if x[0] in negmap]
                if len(pts) >= 2:
                    net_segs.append(pts)
            if not net_segs:
                continue
            key = L["surname"]
            payload[key] = {"net": encode_lines(net_segs), "pos": encode_lines(pos_segs), "neg": encode_lines(neg_segs)}
            last_d = net_segs[-1][-1][0]
            pos_now, neg_now = pos_segs[-1][-1][1], neg_segs[-1][-1][1]
            pos_ago, neg_ago = value_near(pos_segs, last_d - change_days), value_near(neg_segs, last_d - change_days)
            meta[key] = {"pos": round(pos_now, 2), "neg": round(neg_now, 2), "posAgo": round(pos_ago, 2), "negAgo": round(neg_ago, 2),
                         "start": net_segs[0][0][0], "end": last_d, "since": since if since > -10 ** 9 else None}
            start, end = min(start, net_segs[0][0][0]), max(end, last_d)
        if not payload:
            continue
        group_file = f"page-{g['id']}.json"
        with open(os.path.join(out_dir, group_file), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        groups_out.append({**g, "file": group_file, "start": start, "end": end, "leaders": meta})
    index = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "type": spec["type"], "changeDays": change_days,
        "leaders": [{k: L[k] for k in ("name", "surname", "party", "role", "url", "since") if k in L} for L in spec["leaders"]],
        "groups": groups_out,
    }
    with open(os.path.join(out_dir, "page.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))
    return out_dir, [g["id"] for g in groups_out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if args.all:
        for p in sorted(glob.glob("pages/*.json")):
            try:
                res = export_leaders_page(p)
                print(f"export_page: {res[0]} ({len(res[1])} groups)")
            except Exception as e:  # noqa: BLE001
                print(f"export_page: {p} failed: {e.__class__.__name__}: {e}")
    configs = sorted(glob.glob("trackers/*/config.json")) if args.all else [args.config]
    for c in configs:
        try:
            res = export_tracker(c)
        except Exception as e:  # one bad tracker must not stop the rest
            print(f"export_page: {c} failed: {e.__class__.__name__}: {e}")
            continue
        if res:
            print(f"export_page: {res[0]} ({len(res[1])} groups)")


if __name__ == "__main__":
    main()
