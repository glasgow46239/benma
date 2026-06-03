"""
Tracker — smoothing pipeline
--------------------------------------
Fetches poll data from Google Sheets, runs kernel LOESS smoothing,
outputs a JSON file for the Datawrapper line chart and a CSV for the bar chart.

Usage:
    python pipeline/smooth.py [--config config.json] [--push-to-datawrapper]

Dependencies:
    pip install requests numpy
"""

import argparse
import csv
import io
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import requests

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Smoothing pipeline")
    p.add_argument("--config", default="config.json", help="Path to config.json")
    p.add_argument("--push-to-datawrapper", action="store_true",
                   help="Push outputs to Datawrapper via API (requires DATAWRAPPER_TOKEN env var)")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path) as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# Data fetching & parsing
# ---------------------------------------------------------------------------

def parse_date(s):
    return datetime.strptime(s.strip(), "%d/%m/%Y")

def should_skip(pollster, skip_patterns):
    for pat in skip_patterns:
        if pat.lower() in pollster.lower():
            return True
    return False

def fetch_polls(config):
    url = config["dataUrl"]
    skip_patterns = config.get("skipPollsterPatterns", [])
    parties = config["parties"]

    print(f"Fetching data from: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)

    # Find the header row (first row with 'date' in col 0)
    header_idx = 0
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "date":
            header_idx = i
            break

    data_rows = rows[header_idx + 1:]

    polls = []
    for row in data_rows:
        if not row or not row[0].strip():
            continue
        try:
            date = parse_date(row[0])
        except ValueError:
            continue

        pollster = row[1].strip() if len(row) > 1 else ""
        if should_skip(pollster, skip_patterns):
            continue

        entry = {"date": date, "pollster": pollster, "values": {}}
        for party in parties:
            col = party["col"]
            try:
                raw = row[col].strip() if col < len(row) else ""
                val = float(raw) if raw else None
            except ValueError:
                val = None
            entry["values"][party["name"]] = val

        polls.append(entry)

    polls.sort(key=lambda p: p["date"])
    print(f"Loaded {len(polls)} polls (after skipping reference rows)")
    return polls

# ---------------------------------------------------------------------------
# Kernel LOESS (Tricube, fixed day bandwidth)
# ---------------------------------------------------------------------------

def tricube_weight(u):
    if abs(u) >= 1:
        return 0.0
    return (1 - abs(u) ** 3) ** 3

def kernel_loess(data_points, bandwidth_days, min_polls, n_steps=300):
    """
    data_points: list of (timestamp_days, value) tuples
    Returns list of (timestamp_days, smoothed_value, is_sparse) or None where no line
    """
    if not data_points:
        return []

    bw = bandwidth_days
    ts = [d[0] for d in data_points]
    t_min, t_max = min(ts), max(ts)

    results = []
    for i in range(n_steps + 1):
        t = t_min + (t_max - t_min) * (i / n_steps)

        nearby = [(tx, y) for tx, y in data_points if abs(tx - t) <= bw]
        if len(nearby) < min_polls:
            results.append((t, None, False))
            continue

        # Sparse flag: fewer than 2x min_polls within 1.8x bandwidth
        wider = [(tx, y) for tx, y in data_points if abs(tx - t) <= bw * 1.8]
        is_sparse = len(wider) < min_polls * 2

        w_sum = 0.0
        wy_sum = 0.0
        for tx, y in nearby:
            u = (tx - t) / bw
            w = tricube_weight(u)
            w_sum += w
            wy_sum += w * y

        smoothed = wy_sum / w_sum if w_sum > 0 else None
        results.append((t, round(smoothed, 2) if smoothed is not None else None, is_sparse))

    return results

def days_since_epoch(dt):
    return (dt - datetime(1970, 1, 1)).days

# ---------------------------------------------------------------------------
# Headline generation
# ---------------------------------------------------------------------------

def generate_headline(polls, config, latest_smooth):
    """Generate a plain-English headline and intro line from the latest data."""
    if not polls:
        return "", ""

    latest_poll = polls[-1]
    latest_date = latest_poll["date"].strftime("%-d %B %Y")
    pollster = latest_poll["pollster"]

    # Find leading party in latest poll
    party_vals = {
        p: latest_poll["values"].get(p)
        for p in [pt["name"] for pt in config["parties"] if pt["includeInLine"]]
        if latest_poll["values"].get(p) is not None
    }
    if not party_vals:
        return "", ""

    leader = max(party_vals, key=party_vals.get)
    leader_val = party_vals[leader]

    # Compare to reference election
    ref = config.get("referenceElection", {})
    ref_results = ref.get("results", {})
    ref_label = ref.get("label", "last election")
    ref_leader_val = ref_results.get(leader)
    change_str = ""
    if ref_leader_val is not None:
        change = round(leader_val - ref_leader_val, 1)
        direction = "up" if change > 0 else "down"
        change_str = f", {direction} {abs(change)} points since the {ref_label}"

    headline = f"{leader} lead in Wales at {leader_val}%{change_str}"

    intro = (
        f"Latest poll: {pollster} · {latest_date} · "
        + " · ".join(f"{p} {v}%" for p, v in sorted(party_vals.items(), key=lambda x: -x[1]))
    )

    return headline, intro

# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------

def build_line_json(polls, config):
    bw = config["smoothing"]["bandwidthDays"]
    min_polls = config["smoothing"]["minPollsInWindow"]
    parties = [p for p in config["parties"] if p["includeInLine"]]

    series = {}
    for party in parties:
        name = party["name"]
        pts = [
            (days_since_epoch(p["date"]), p["values"][name])
            for p in polls
            if p["values"].get(name) is not None
        ]
        if not pts:
            continue
        smoothed = kernel_loess(pts, bw, min_polls)
        series[name] = [
            {
                "t": int(t),
                "date": (datetime(1970, 1, 1) + timedelta(days=int(t))).strftime("%Y-%m-%d"),
                "value": v,
                "sparse": s
            }
            for t, v, s in smoothed
        ]

    # Latest smoothed values (last non-None point per party)
    latest_smooth = {}
    for name, pts in series.items():
        non_null = [p for p in pts if p["value"] is not None]
        if non_null:
            latest_smooth[name] = non_null[-1]["value"]

    headline, intro = generate_headline(polls, config, latest_smooth)

    # Raw poll dots (for reference)
    raw_dots = [
        {
            "date": p["date"].strftime("%Y-%m-%d"),
            "pollster": p["pollster"],
            "values": {k: v for k, v in p["values"].items() if v is not None}
        }
        for p in polls
    ]

    return {
        "meta": {
            "trackerName": config["trackerName"],
            "generated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bandwidthDays": bw,
            "minPollsInWindow": min_polls,
            "headline": headline,
            "intro": intro,
            "latestSmoothed": latest_smooth
        },
        "series": series,
        "rawPolls": raw_dots
    }

def build_bar_csv(polls, config):
    if not polls:
        return ""

    parties = [p for p in config["parties"] if p["includeInBar"]]
    ref_results = config.get("referenceElection", {}).get("results", {})
    ref_label = config.get("referenceElection", {}).get("label", "Reference")

    # Latest poll values per party (most recent non-null)
    latest = {}
    for p in reversed(polls):
        for party in parties:
            name = party["name"]
            if name not in latest and p["values"].get(name) is not None:
                latest[name] = p["values"][name]
        if len(latest) == len(parties):
            break

    lines = [f"Party,Latest poll,{ref_label},Change"]
    for party in parties:
        name = party["name"]
        curr = latest.get(name)
        ref = ref_results.get(name)
        if curr is None:
            continue
        if ref is not None:
            change = round(curr - ref, 1)
            change_str = f"+{change}" if change >= 0 else str(change)
        else:
            change_str = "n/a"
            ref = "n/a"
        lines.append(f"{name},{curr},{ref},{change_str}")

    return "\n".join(lines)

def build_line_csv(json_data, polls):
    """Smoothed series + daily-averaged raw polls for Datawrapper line chart."""
    series = json_data["series"]
    if not series:
        return ""

    party_names = list(series.keys())

    # Smoothed date index
    all_smooth_dates = sorted(set(
        pt["date"] for name in party_names
        for pt in series[name] if pt["value"] is not None
    ))

    # Raw poll daily averages
    daily = defaultdict(lambda: defaultdict(list))
    for poll in polls:
        date_str = poll["date"].strftime("%Y-%m-%d")
        for name in party_names:
            val = poll["values"].get(name)
            if val is not None:
                daily[date_str][name].append(val)

    raw_dates = sorted(daily.keys())

    # Header: date, smoothed parties..., raw poll parties...
    raw_cols = [f"{n} (poll)" for n in party_names]
    lines = ["date," + ",".join(party_names) + "," + ",".join(raw_cols)]

    # Smoothed lookup
    smooth_lookup = {}
    for name in party_names:
        for pt in series[name]:
            smooth_lookup.setdefault(pt["date"], {})[name] = pt["value"]

    all_dates = sorted(set(all_smooth_dates) | set(raw_dates))

    for date in all_dates:
        row = [date]
        for name in party_names:
            val = smooth_lookup.get(date, {}).get(name)
            row.append("" if val is None else str(val))
        for name in party_names:
            vals = daily.get(date, {}).get(name, [])
            avg = round(sum(vals) / len(vals), 1) if vals else ""
            row.append("" if avg == "" else str(avg))
        lines.append(",".join(row))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Datawrapper push
# ---------------------------------------------------------------------------

def push_to_datawrapper(json_data, bar_csv, polls, config):
    token = os.environ.get("DATAWRAPPER_TOKEN")
    if not token:
        print("Warning: DATAWRAPPER_TOKEN not set, skipping Datawrapper push")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    line_id = config["output"].get("datawrapperLineChartId")
    bar_id  = config["output"].get("datawrapperBarChartId")
    updated = datetime.utcnow().strftime("%-d %B %Y")

    if line_id:
        line_csv = build_line_csv(json_data, polls)
        requests.put(
            f"https://api.datawrapper.de/v3/charts/{line_id}/data",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/csv"},
            data=line_csv.encode("utf-8")
        )
        requests.patch(
            f"https://api.datawrapper.de/v3/charts/{line_id}",
            headers=headers,
            json={
                "title": json_data["meta"]["headline"],
                "describe": {
                    "intro": json_data["meta"]["intro"],
                    "byline": f"Last updated {updated}",
                    "source-name": "",
                }
            }
        )
        requests.post(
            f"https://api.datawrapper.de/v3/charts/{line_id}/publish",
            headers=headers
        )
        print(f"Pushed and republished line chart: {line_id}")

    if bar_id:
        requests.put(
            f"https://api.datawrapper.de/v3/charts/{bar_id}/data",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/csv"},
            data=bar_csv.encode("utf-8")
        )
        requests.patch(
            f"https://api.datawrapper.de/v3/charts/{bar_id}",
            headers=headers,
            json={
                "describe": {
                    "byline": f"Last updated {updated}"
                }
            }
        )
        requests.post(
            f"https://api.datawrapper.de/v3/charts/{bar_id}/publish",
            headers=headers
        )
        print(f"Pushed and republished bar chart: {bar_id}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    config = load_config(args.config)

    polls = fetch_polls(config)
    if not polls:
        print("No polls loaded, exiting")
        sys.exit(1)

    line_json = build_line_json(polls, config)
    bar_csv   = build_bar_csv(polls, config)

    # Write outputs
    line_path = config["output"]["lineJsonPath"]
    bar_path  = config["output"]["barCsvPath"]

    os.makedirs(os.path.dirname(line_path), exist_ok=True)

    with open(line_path, "w") as f:
        json.dump(line_json, f, indent=2)
    print(f"Written: {line_path}")

    with open(bar_path, "w") as f:
        f.write(bar_csv)
    print(f"Written: {bar_path}")

    print(f"\nHeadline: {line_json['meta']['headline']}")
    print(f"Intro:    {line_json['meta']['intro']}")

    if args.push_to_datawrapper:
        push_to_datawrapper(line_json, bar_csv, polls, config)

if __name__ == "__main__":
    main()
