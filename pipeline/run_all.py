#!/usr/bin/env python3
"""
Run every tracker (in parallel) and write trackers/index.csv.

    python pipeline/run_all.py                      # all enabled trackers
    python pipeline/run_all.py --only ldr_davey_all vi_westminster_24
    python pipeline/run_all.py --workers 4 --push-to-datawrapper

A tracker is skipped if config/smoothing.json sets "enabled": false for it.
Exit code is non-zero if any tracker failed (the others still complete).
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def tracker_list(only=None):
    central = json.load(open("config/smoothing.json")).get("trackers", {})
    out = []
    for cfg in sorted(glob.glob("trackers/*/config.json")):
        tid = os.path.basename(os.path.dirname(cfg))
        if only and tid not in only:
            continue
        try:
            c = json.load(open(cfg))
        except Exception:
            print(f"Skipping {tid}: invalid config.json")
            continue
        if central.get(tid, {}).get("enabled", True) is False:
            print(f"Skipping {tid}: disabled in config/smoothing.json")
            continue
        out.append((tid, cfg, c.get("pipeline", "smooth")))
    return out


def run_one(item, push):
    tid, cfg, pipeline = item
    script = "pipeline/approval.py" if pipeline == "approval" else "pipeline/smooth.py"
    cmd = [sys.executable, script, "--config", cfg]
    if push:
        cmd.append("--push-to-datawrapper")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    return tid, r.returncode, time.time() - t0, r.stdout + r.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--push-to-datawrapper", action="store_true")
    a = ap.parse_args()

    items = tracker_list(set(a.only) if a.only else None)
    failed = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for tid, code, secs, log in ex.map(lambda i: run_one(i, a.push_to_datawrapper), items):
            print(f"::group::{tid} ({'ok' if code == 0 else 'FAILED'}, {secs:.0f}s)")
            print(log)
            print("::endgroup::")
            if code:
                failed.append(tid)

    subprocess.run([sys.executable, "pipeline/index_trackers.py"], check=False)
    if failed:
        print("Failed trackers:", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
