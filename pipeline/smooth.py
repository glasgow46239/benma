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
# Historical series loader
# ---------------------------------------------------------------------------

def load_historical_series(config):
    """
    Fetch a pre-smoothed line.csv from a historical tracker and return
    a dict of { col_name -> { date_str -> value } } for the requested parties,
    with the configured suffix appended to party names.

    Expected historical CSV columns:
        date, Con, Con (low), Con (high), Con (poll), Lab, ...

    Returns:
        hist  — { suffixed_col_name: { date_str: float_or_none } }
        polls — { party_name: { date_str: float_or_none } }  (raw poll dots, no suffix)
    """
    hist_config = config.get("historicalSeries")
    if not hist_config:
        return {}, {}

    url = hist_config["url"]
    suffix = hist_config.get("labelSuffix", " (hist)")
    wanted_parties = hist_config.get("parties", [])

    print(f"Fetching historical series from: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)

    hist = defaultdict(dict)   # suffixed smooth + CI columns
    polls = defaultdict(dict)  # raw poll dots — no suffix, merged with live

    for row in rows:
        date = row.get("date", "").strip()
        if not date:
            continue

        for party in wanted_parties:
            # Smooth value
            smooth_key = party
            if smooth_key in row and row[smooth_key].strip():
                try:
                    hist[f"{party}{suffix}"][date] = float(row[smooth_key])
                except ValueError:
                    pass

            # CI low
            low_key = f"{party} (low)"
            if low_key in row and row[low_key].strip():
                try:
                    hist[f"{party}{suffix} (low)"][date] = float(row[low_key])
                except ValueError:
                    pass

            # CI high
            high_key = f"{party} (high)"
            if high_key in row and row[high_key].strip():
                try:
                    hist[f"{party}{suffix} (high)"][date] = float(row[high_key])
                except ValueError:
                    pass

            # Poll dots — merged into live poll column (no suffix)
            poll_key = f"{party} (poll)"
            if poll_key in row and row[poll_key].strip():
                try:
                    polls[party][date] = float(row[poll_key])
                except ValueError:
                    pass

    print(f"  Loaded historical series: {list(hist.keys())[:6]}...")
    return dict(hist), dict(polls)

# ---------------------------------------------------------------------------
# Kernel LOESS (Tricube, fixed day bandwidth)
# ---------------------------------------------------------------------------

def tricube_weight(u):
    if abs(u) >= 1:
        return 0.0
    return (1 - abs(u) ** 3) ** 3

def kernel_loess(data_points, bandwidth_days, min_polls, step_days=1):
    """
    data_points: list of (timestamp_days, value) tuples
    step_days: output one smoothed point every N days (default 1 = daily)
    Returns list of (timestamp_days, smoothed_value, is_sparse)
    """
    if not data_points:
        return []

    bw = bandwidth_days
    ts = [d[0] for d in data_points]
    t_min, t_max = min(ts), max(ts)

    results = []
    t = t_min
    while t <= t_max:
        nearby = [(tx, y) for tx, y in data_points if abs(tx - t) <= bw]
        if len(nearby) < min_polls:
            results.append((t, None, False))
            t += step_days
            continue

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
        t += step_days

    return results

def kernel_loess_simple(data_points, bandwidth_days, min_polls, t_values):
    """Run LOESS at specific t values (used for bootstrap)."""
    bw = bandwidth_days
    results = []
    for t in t_values:
        nearby = [(tx, y) for tx, y in data_points if abs(tx - t) <= bw]
        if len(nearby) < min_polls:
            results.append(None)
            continue
        w_sum = 0.0
        wy_sum = 0.0
        for tx, y in nearby:
            u = (tx - t) / bw
            w = tricube_weight(u)
            w_sum += w
            wy_sum += w * y
        results.append(round(wy_sum / w_sum, 2) if w_sum > 0 else None)
    return results

def bootstrap_ci(data_points, bandwidth_days, min_polls, t_values,
                 n_boot=200, ci_low=5, ci_high=95):
    """Bootstrap confidence intervals for LOESS."""
    if len(data_points) < min_polls:
        return [None] * len(t_values), [None] * len(t_values)

    rng = np.random.default_rng(42)
    boot_results = []

    for _ in range(n_boot):
        indices = rng.integers(0, len(data_points), size=len(data_points))
        resampled = [data_points[i] for i in indices]
        smoothed = kernel_loess_simple(resampled, bandwidth_days, min_polls, t_values)
        boot_results.append(smoothed)

    arr = np.array([[v if v is not None else np.nan for v in row]
                    for row in boot_results])

    lows, highs = [], []
    for col_idx in range(len(t_values)):
        col = arr[:, col_idx]
        valid = col[~np.isnan(col)]
        if len(valid) < min_polls:
            lows.append(None)
            highs.append(None)
        else:
            lows.append(round(float(np.percentile(valid, ci_low)), 2))
            highs.append(round(float(np.percentile(valid, ci_high)), 2))

    return lows, highs

def days_since_epoch(dt):
    return (dt - datetime(1970, 1, 1)).days

def epoch_to_date_str(d):
    return (datetime(1970, 1, 1) + timedelta(days=int(d))).strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Headline generation
# ---------------------------------------------------------------------------

def generate_headline(polls, config, latest_smooth):
    if not polls:
        return "", ""

    latest_poll = polls[-1]
    latest_date = latest_poll["date"].strftime("%-d %B %Y")
    pollster = latest_poll["pollster"]

    party_vals = {
        p: latest_poll["values"].get(p)
        for p in [pt["name"] for pt in config["parties"] if pt["includeInLine"]]
        if latest_poll["values"].get(p) is not None
    }
    if not party_vals:
        return "", ""

    leader = max(party_vals, key=party_vals.get)
    leader_val = party_vals[leader]

    ref = config.get("referenceElection", {})
    ref_results = ref.get("results", {})
    ref_label = ref.get("label", "last election")
    ref_leader_val = ref_results.get(leader)
    change_str = ""
    if ref_leader_val is not None:
        change = round(leader_val - ref_leader_val, 1)
        direction = "up" if change > 0 else "down"
        change_str = f", {direction} {abs(change)} points since the {ref_label}"

    headline = f"{leader} lead at {leader_val}%{change_str}"

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
    n_boot = config["smoothing"].get("bootstrapIterations", 200)
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

        smoothed = kernel_loess(pts, bw, min_polls, step_days=1)
        t_values = [s[0] for s in smoothed]

        print(f"  Bootstrapping CI for {name} ({n_boot} iterations)...")
        lows, highs = bootstrap_ci(pts, bw, min_polls, t_values, n_boot=n_boot)

        series[name] = [
            {
                "t": int(t),
                "date": epoch_to_date_str(t),
                "value": v,
                "low": lows[i],
                "high": highs[i],
                "sparse": s
            }
            for i, (t, v, s) in enumerate(smoothed)
        ]

    latest_smooth = {}
    for name, pts in series.items():
        non_null = [p for p in pts if p["value"] is not None]
        if non_null:
            latest_smooth[name] = non_null[-1]["value"]

    headline, intro = generate_headline(polls, config, latest_smooth)
    latest_poll_date = polls[-1]["date"].strftime("%d-%b-%Y") if polls else ""

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
            "bootstrapIterations": n_boot,
            "headline": headline,
            "intro": intro,
            "latestSmoothed": latest_smooth,
            "latestPollDate": latest_poll_date,
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

def build_line_csv(json_data, polls, hist_series=None, hist_polls=None):
    """
    Smoothed series (daily) + CI bounds + historical series + raw poll averages.

    Column order:
        date,
        [Party], [Party (low)], [Party (high)],        ← live smooth + CI
        [Party (suffix)], [Party (suffix) (low)], ..., ← historical smooth + CI
        [Party (poll)]                                  ← merged poll dots
    """
    series = json_data["series"]
    if not series:
        return ""

    hist_series = hist_series or {}
    hist_polls  = hist_polls  or {}

    party_names = list(series.keys())

    # All historical column names (smooth + CI, no poll)
    hist_smooth_cols = sorted(set(
        k for k in hist_series
        if not k.endswith(" (low)") and not k.endswith(" (high)")
    ))

    # Raw poll daily averages from live polls
    daily = defaultdict(lambda: defaultdict(list))
    for poll in polls:
        date_str = poll["date"].strftime("%Y-%m-%d")
        for name in party_names:
            val = poll["values"].get(name)
            if val is not None:
                daily[date_str][name].append(val)

    # Merge historical poll dots into daily
    for party, date_vals in hist_polls.items():
        for date_str, val in date_vals.items():
            daily[date_str][party].append(val)

    raw_dates = sorted(daily.keys())

    # Live smooth lookup: date -> {party -> (value, low, high)}
    smooth_lookup = {}
    for name in party_names:
        for pt in series.get(name, []):
            if pt["value"] is not None:
                smooth_lookup.setdefault(pt["date"], {})[name] = (
                    pt["value"], pt.get("low"), pt.get("high")
                )

    # All dates: live smooth + historical smooth + poll dates
    all_hist_dates = set()
    for col_data in hist_series.values():
        all_hist_dates.update(col_data.keys())

    all_smooth_dates = set(smooth_lookup.keys())
    all_dates = sorted(all_smooth_dates | all_hist_dates | set(raw_dates))

    # Build header
    header = ["date"]
    for name in party_names:
        header += [name, f"{name} (low)", f"{name} (high)"]
    for col in hist_smooth_cols:
        header += [col, f"{col} (low)", f"{col} (high)"]
    for name in party_names:
        header.append(f"{name} (poll)")
    lines = [",".join(header)]

    for date in all_dates:
        row = [date]

        # Live smooth + CI
        for name in party_names:
            entry = smooth_lookup.get(date, {}).get(name)
            if entry:
                v, lo, hi = entry
                row += [str(v),
                        "" if lo is None else str(lo),
                        "" if hi is None else str(hi)]
            else:
                row += ["", "", ""]

        # Historical smooth + CI
        for col in hist_smooth_cols:
            v  = hist_series.get(col, {}).get(date)
            lo = hist_series.get(f"{col} (low)", {}).get(date)
            hi = hist_series.get(f"{col} (high)", {}).get(date)
            row += [
                "" if v  is None else str(v),
                "" if lo is None else str(lo),
                "" if hi is None else str(hi),
            ]

        # Merged poll dots
        for name in party_names:
            vals = daily.get(date, {}).get(name, [])
            avg = round(sum(vals) / len(vals), 1) if vals else ""
            row.append("" if avg == "" else str(avg))

        lines.append(",".join(row))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Datawrapper push
# ---------------------------------------------------------------------------

def push_to_datawrapper(json_data, bar_csv, polls, config,
                        hist_series=None, hist_polls=None):
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
    updated = json_data["meta"].get("latestPollDate", "")
    byline_html = (
        f'<span style="background-color:#f0f0f0; padding:1px 3px; border-radius:4px">'
        f'Last updated {updated}</span>'
    )

    if line_id:
        line_csv = build_line_csv(json_data, polls, hist_series, hist_polls)
        requests.put(
            f"https://api.datawrapper.de/v3/charts/{line_id}/data",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/csv"},
            data=line_csv.encode("utf-8")
        )
        line_patch_resp = requests.patch(
            f"https://api.datawrapper.de/v3/charts/{line_id}",
            headers=headers,
            json={
                "metadata": {
                    "describe": {
                        "byline": byline_html,
                    }
                }
            }
        )
        print(f"Line chart patch response: {line_patch_resp.status_code}")
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
        bar_patch_resp = requests.patch(
            f"https://api.datawrapper.de/v3/charts/{bar_id}",
            headers=headers,
            json={
                "metadata": {
                    "describe": {
                        "byline": byline_html,
                    }
                }
            }
        )
        print(f"Bar chart patch response: {bar_patch_resp.status_code}")
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

    # Load historical series if configured
    hist_series, hist_polls = load_historical_series(config)

    print("Building smoothed series with confidence intervals...")
    line_json = build_line_json(polls, config)
    bar_csv   = build_bar_csv(polls, config)

    line_path     = config["output"]["lineJsonPath"]
    bar_path      = config["output"]["barCsvPath"]
    line_csv_path = config["output"].get("lineCsvPath", "")

    os.makedirs(os.path.dirname(line_path), exist_ok=True)

    with open(line_path, "w") as f:
        json.dump(line_json, f, indent=2)
    print(f"Written: {line_path}")

    with open(bar_path, "w") as f:
        f.write(bar_csv)
    print(f"Written: {bar_path}")

    if line_csv_path:
        os.makedirs(os.path.dirname(line_csv_path), exist_ok=True)
        line_csv_data = build_line_csv(line_json, polls, hist_series, hist_polls)
        with open(line_csv_path, "w") as f:
            f.write(line_csv_data)
        print(f"Written: {line_csv_path}")

    print(f"\nHeadline: {line_json['meta']['headline']}")
    print(f"Intro:    {line_json['meta']['intro']}")

    if args.push_to_datawrapper:
        push_to_datawrapper(line_json, bar_csv, polls, config, hist_series, hist_polls)

if __name__ == "__main__":
    main()
