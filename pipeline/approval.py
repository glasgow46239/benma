"""
Approval ratings pipeline
--------------------------
Reads a wide-format CSV where each leader has their own approve/disapprove columns.
Runs kernel LOESS smoothing with bootstrap CI and outputs:
  - approval-lines.csv   : smoothed approve/disapprove/net per leader per day
  - approval-days.csv    : smoothed net approval indexed by days in office
  - approval-raw.csv     : raw pivoted poll data

Usage:
    python pipeline/approval.py --config trackers/pm-approval/config.json
    python pipeline/approval.py --config trackers/pm-approval/config.json --push-to-datawrapper

Dependencies:
    pip install requests numpy
"""

import argparse
import csv
import io
import json
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
    p = argparse.ArgumentParser(description="Approval ratings pipeline")
    p.add_argument("--config", default="config.json", help="Path to config.json")
    p.add_argument("--push-to-datawrapper", action="store_true",
                   help="Push outputs to Datawrapper via API")
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
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {s}")

def fetch_approval_polls(config):
    """
    Fetch wide-format approval CSV.
    Each leader has their own approveCol and disapproveCol.
    The leader column is used to know which leader this row refers to,
    and we read the values from that leader's specific columns.

    Returns list of polls:
        { date, pollster, leader (short name), approve, disapprove, net }
    """
    url = config["dataUrl"]
    col_date     = config.get("columns", {}).get("date",     0)
    col_pollster = config.get("columns", {}).get("pollster", 1)
    col_leader   = config.get("columns", {}).get("leader",   2)
    skip_patterns = config.get("skipPollsterPatterns", [])

    # Build matchName -> leader config lookup
    leader_by_fullname = {}
    for l in config["leaders"]:
        match_name = l.get("matchName", l["name"]).strip().lower()
        leader_by_fullname[match_name] = l

    print(f"Fetching data from: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)

    # Find header row
    header_idx = 0
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "date":
            header_idx = i
            break

    polls = []
    skipped = 0
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        try:
            date = parse_date(row[col_date])
        except (ValueError, IndexError):
            continue

        pollster = row[col_pollster].strip() if col_pollster < len(row) else ""
        if any(pat.lower() in pollster.lower() for pat in skip_patterns):
            continue

        full_name = row[col_leader].strip().lower() if col_leader < len(row) else ""
        leader_cfg = leader_by_fullname.get(full_name)
        if not leader_cfg:
            skipped += 1
            continue

        def get_float(col):
            try:
                v = row[col].strip() if col < len(row) else ""
                return float(v) if v else None
            except ValueError:
                return None

        approve    = get_float(leader_cfg["approveCol"])
        disapprove = get_float(leader_cfg["disapproveCol"])

        if approve is None and disapprove is None:
            continue

        net = round(approve - disapprove, 1) if approve is not None and disapprove is not None else None

        polls.append({
            "date":       date,
            "pollster":   pollster,
            "leader":     leader_cfg["name"],
            "approve":    approve,
            "disapprove": disapprove,
            "net":        net
        })

    polls.sort(key=lambda p: p["date"])
    print(f"Loaded {len(polls)} approval polls ({skipped} rows skipped — unrecognised leader name)")

    # Summary per leader
    from collections import Counter
    counts = Counter(p["leader"] for p in polls)
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count} polls")

    return polls

# ---------------------------------------------------------------------------
# LOESS + bootstrap CI
# ---------------------------------------------------------------------------

def days_since_epoch(dt):
    return (dt - datetime(1970, 1, 1)).days

def epoch_to_date_str(d):
    return (datetime(1970, 1, 1) + timedelta(days=int(d))).strftime("%Y-%m-%d")

def tricube_weight(u):
    if abs(u) >= 1:
        return 0.0
    return (1 - abs(u) ** 3) ** 3

def kernel_loess(data_points, bandwidth_days, min_polls, step_days=1):
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
        w_sum = wy_sum = 0.0
        for tx, y in nearby:
            w = tricube_weight((tx - t) / bw)
            w_sum += w
            wy_sum += w * y
        smoothed = round(wy_sum / w_sum, 2) if w_sum > 0 else None
        results.append((t, smoothed, is_sparse))
        t += step_days
    return results

def kernel_loess_simple(data_points, bandwidth_days, min_polls, t_values):
    bw = bandwidth_days
    results = []
    for t in t_values:
        nearby = [(tx, y) for tx, y in data_points if abs(tx - t) <= bw]
        if len(nearby) < min_polls:
            results.append(None)
            continue
        w_sum = wy_sum = 0.0
        for tx, y in nearby:
            w = tricube_weight((tx - t) / bw)
            w_sum += w
            wy_sum += w * y
        results.append(round(wy_sum / w_sum, 2) if w_sum > 0 else None)
    return results

def bootstrap_ci(data_points, bandwidth_days, min_polls, t_values,
                 n_boot=200, ci_low=5, ci_high=95):
    if len(data_points) < min_polls:
        return [None] * len(t_values), [None] * len(t_values)
    rng = np.random.default_rng(42)
    boot_results = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(data_points), size=len(data_points))
        resampled = [data_points[i] for i in idx]
        boot_results.append(
            kernel_loess_simple(resampled, bandwidth_days, min_polls, t_values)
        )
    arr = np.array([
        [v if v is not None else np.nan for v in row]
        for row in boot_results
    ])
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

# ---------------------------------------------------------------------------
# Smooth all leaders
# ---------------------------------------------------------------------------

def smooth_all(polls, config):
    """
    Returns dict: leader_name -> {
        'approve':    [ {date, t, value, low, high} ],
        'disapprove': [ {date, t, value, low, high} ],
        'net':        [ {date, t, value, low, high} ]
    }
    """
    bw        = config["smoothing"]["bandwidthDays"]
    min_polls = config["smoothing"]["minPollsInWindow"]
    n_boot    = config["smoothing"].get("bootstrapIterations", 200)

    results = {}

    for leader_cfg in config["leaders"]:
        name = leader_cfg["name"]
        print(f"  Smoothing {name}...")

        def get_pts(metric):
            return [
                (days_since_epoch(p["date"]), p[metric])
                for p in polls
                if p["leader"] == name and p.get(metric) is not None
            ]

        leader_result = {}
        for metric in ("approve", "disapprove", "net"):
            pts = get_pts(metric)
            if not pts:
                leader_result[metric] = []
                continue

            smoothed  = kernel_loess(pts, bw, min_polls, step_days=1)
            t_values  = [s[0] for s in smoothed]
            lows, highs = bootstrap_ci(pts, bw, min_polls, t_values, n_boot=n_boot)

            leader_result[metric] = [
                {
                    "t":     int(t),
                    "date":  epoch_to_date_str(t),
                    "value": v,
                    "low":   lows[i],
                    "high":  highs[i],
                }
                for i, (t, v, _) in enumerate(smoothed)
            ]

        results[name] = leader_result

    return results

# ---------------------------------------------------------------------------
# Output: approval-lines.csv
# ---------------------------------------------------------------------------

def build_lines_csv(smoothed, config):
    """
    Columns: date,
             [Leader] approve, [Leader] approve (low), [Leader] approve (high),
             [Leader] disapprove, [Leader] disapprove (low), [Leader] disapprove (high),
             [Leader] net, [Leader] net (low), [Leader] net (high), ...
    """
    leaders = [l["name"] for l in config["leaders"]]

    lookup = {}
    all_dates = set()
    for leader in leaders:
        lookup[leader] = {}
        for metric in ("approve", "disapprove", "net"):
            pts = smoothed.get(leader, {}).get(metric, [])
            lookup[leader][metric] = {
                pt["date"]: pt for pt in pts if pt["value"] is not None
            }
            all_dates.update(lookup[leader][metric].keys())

    all_dates = sorted(all_dates)

    header = ["date"]
    for leader in leaders:
        for metric in ("approve", "disapprove", "net"):
            label = f"{leader} {metric}"
            header += [label, f"{label} (low)", f"{label} (high)"]

    lines = [",".join(header)]
    for date in all_dates:
        row = [date]
        for leader in leaders:
            for metric in ("approve", "disapprove", "net"):
                pt = lookup[leader][metric].get(date)
                if pt:
                    row += [
                        str(pt["value"]),
                        "" if pt["low"]  is None else str(pt["low"]),
                        "" if pt["high"] is None else str(pt["high"]),
                    ]
                else:
                    row += ["", "", ""]
        lines.append(",".join(row))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Output: approval-days.csv
# ---------------------------------------------------------------------------

def build_days_csv(smoothed, config):
    """
    Rows are days in office (0, 1, 2, ...).
    Columns: day, [Leader] net, [Leader] net (low), [Leader] net (high), ...
    """
    leaders_cfg = {l["name"]: l for l in config["leaders"]}
    leaders = [l["name"] for l in config["leaders"]]

    leader_days = {}
    max_days = 0

    for leader in leaders:
        in_office = leaders_cfg[leader].get("inOfficeSince", "")
        if not in_office:
            print(f"  WARNING: no inOfficeSince for {leader}, skipping days index")
            continue

        start = None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                start = datetime.strptime(in_office.strip(), fmt)
                break
            except ValueError:
                continue

        if not start:
            print(f"  WARNING: could not parse inOfficeSince for {leader}: {in_office}")
            continue

        pts_by_date = {
            pt["date"]: pt
            for pt in smoothed.get(leader, {}).get("net", [])
            if pt["value"] is not None
        }

        if not pts_by_date:
            continue

        days_data = {}
        for date_str, pt in pts_by_date.items():
            try:
                date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            day_num = (date - start).days
            if day_num >= 0:
                days_data[day_num] = pt
                max_days = max(max_days, day_num)

        leader_days[leader] = days_data

    if not leader_days:
        return ""

    active_leaders = [l for l in leaders if l in leader_days]

    header = ["day"]
    for leader in active_leaders:
        header += [
            f"{leader} net",
            f"{leader} net (low)",
            f"{leader} net (high)"
        ]

    lines = [",".join(header)]
    for day in range(max_days + 1):
        row = [str(day)]
        has_data = False
        for leader in active_leaders:
            pt = leader_days[leader].get(day)
            if pt and pt["value"] is not None:
                row += [
                    str(pt["value"]),
                    "" if pt["low"]  is None else str(pt["low"]),
                    "" if pt["high"] is None else str(pt["high"]),
                ]
                has_data = True
            else:
                row += ["", "", ""]
        if has_data:
            lines.append(",".join(row))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Output: approval-raw.csv
# ---------------------------------------------------------------------------

def build_raw_csv(polls, config):
    """Wide-format raw poll data."""
    leaders = [l["name"] for l in config["leaders"]]

    grouped = defaultdict(dict)
    for p in polls:
        key = (p["date"], p["pollster"])
        grouped[key][p["leader"]] = p

    header = ["date", "pollster"]
    for leader in leaders:
        header += [
            f"{leader} approve",
            f"{leader} disapprove",
            f"{leader} net"
        ]

    lines = [",".join(header)]
    for (date, pollster), leader_data in sorted(grouped.items()):
        row = [date.strftime("%Y-%m-%d"), pollster]
        for leader in leaders:
            p = leader_data.get(leader)
            if p:
                row += [
                    "" if p["approve"]    is None else str(p["approve"]),
                    "" if p["disapprove"] is None else str(p["disapprove"]),
                    "" if p["net"]        is None else str(p["net"]),
                ]
            else:
                row += ["", "", ""]
        lines.append(",".join(row))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Datawrapper push
# ---------------------------------------------------------------------------

def push_to_datawrapper(config, lines_csv, days_csv):
    token = os.environ.get("DATAWRAPPER_TOKEN")
    if not token:
        print("Warning: DATAWRAPPER_TOKEN not set, skipping Datawrapper push")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    output  = config.get("output", {})

    lines_id = output.get("datawrapperLinesChartId")
    days_id  = output.get("datawrapperDaysChartId")

    updated    = datetime.utcnow().strftime("%d-%b-%Y")
    byline_html = (
        f'<span style="background-color:#f0f0f0; padding:1px 3px; border-radius:4px">'
        f'Last updated {updated}</span>'
    )

    for chart_id, data, label in [
        (lines_id, lines_csv, "lines"),
        (days_id,  days_csv,  "days"),
    ]:
        if not chart_id or not data:
            continue
        requests.put(
            f"https://api.datawrapper.de/v3/charts/{chart_id}/data",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/csv"},
            data=data.encode("utf-8")
        )
        requests.patch(
            f"https://api.datawrapper.de/v3/charts/{chart_id}",
            headers=headers,
            json={"metadata": {"describe": {"byline": byline_html}}}
        )
        requests.post(
            f"https://api.datawrapper.de/v3/charts/{chart_id}/publish",
            headers=headers
        )
        print(f"Pushed {label} chart: {chart_id}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args   = parse_args()
    config = load_config(args.config)

    polls = fetch_approval_polls(config)
    if not polls:
        print("No polls loaded, exiting")
        sys.exit(1)

    print("Smoothing approval ratings...")
    smoothed = smooth_all(polls, config)

    output = config.get("output", {})

    def write(path, content):
        if not path or not content:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        print(f"Written: {path}")

    lines_csv = build_lines_csv(smoothed, config)
    days_csv  = build_days_csv(smoothed, config)
    raw_csv   = build_raw_csv(polls, config)

    write(output.get("linesCsvPath"), lines_csv)
    write(output.get("daysCsvPath"),  days_csv)
    write(output.get("rawCsvPath"),   raw_csv)

    if args.push_to_datawrapper:
        push_to_datawrapper(config, lines_csv, days_csv)

if __name__ == "__main__":
    main()
