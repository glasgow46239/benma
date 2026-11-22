#!/usr/bin/env python3
"""Write trackers/index.csv: one row per published CSV, with a raw-GitHub URL."""
import csv
import glob
import json
import os

REPO = "glasgow46239/benma"
central = json.load(open("config/smoothing.json")).get("trackers", {})
rows = []
for cfg in sorted(glob.glob("trackers/*/config.json")):
    tid = os.path.basename(os.path.dirname(cfg))
    c = json.load(open(cfg))
    name = central.get(tid, {}).get("name") or c.get("trackerName", tid)
    for path in sorted(glob.glob(f"trackers/{tid}/data/*.csv")):
        f = os.path.basename(path)[:-4]
        kind, _, sub = f.partition("-")
        rows.append({
            "tracker": tid, "name": name, "kind": kind,   # line / bar / table / summary
            "subsample": sub or "all", "path": path,
            "url": f"https://raw.githubusercontent.com/{REPO}/main/{path}",
            "updated": os.popen(f"git log -1 --format=%cs -- {path}").read().strip(),
        })
with open("trackers/index.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)
print(f"trackers/index.csv: {len(rows)} files across {len({r['tracker'] for r in rows})} trackers")
