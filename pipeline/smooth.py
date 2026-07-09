"""
Tracker — smoothing pipeline
--------------------------------------
Fetches poll data from Google Sheets, runs kernel LOESS smoothing,
outputs a JSON file for the Datawrapper line chart and a CSV for the bar chart.

Supports optional subgroup column — if config contains "subgroupColumn" and
"subgroups", the pipeline runs separately for each subgroup and produces
separate output files per subgroup.

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
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {s}")

def should_skip(pollster, skip_patterns):
    for pat in skip_patterns:
        if pat.lower() in pollster.lower():
            return True
    return False

def fetch_polls(config):
    url           = config["dataUrl"]
    skip_patterns = config.get("skipPollsterPatterns", [])
    parties       = config["parties"]
    subgroup_col  = config.get("subgroupColumn")

    print(f"Fetching data from: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    rows   = list(reader)

    header_idx = 0
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "date":
            header_idx = i
            break

    polls = []
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        try:
            date = parse_date(row[0])
        except ValueError:
            continue

        pollster = row[1].strip() if len(row) > 1 else ""
        if should_skip(pollster, skip_patterns):
            continue

        subgroup = None
        if subgroup_col is not None:
            subgroup = row[subgroup_col].strip().lower() if subgroup_col < len(row) else ""

        entry = {"date": date, "pollster": pollster, "subgroup": subgroup, "values": {}}
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

def filter_polls_by_subgroup(polls, subgroup_value):
    """Return only polls matching a given subgroup value (case-insensitive)."""
    return [p for p in polls if p["subgroup"] == subgroup_value.lower()]

# ---------------------------------------------------------------------------
# Kernel LOESS (Tricube, fixed day bandwidth)
# ---------------------------------------------------------------------------

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
        wider    = [(tx, y) for tx, y in data_points if abs(tx - t) <= bw * 1.8]
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
                 n_boot=200, ci_low=5, ci_high=95, margin_of_error=0):
    if len(data_points) < min_polls:
        return [None] * len(t_values), [None] * len(t_values)
    rng = np.random.default_rng(42)
    boot_results = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(data_points), size=len(data_points))
        resampled = [data_points[i] for i in idx]
        # Apply margin of error jitter if specified
        if margin_of_error > 0:
            jitter = rng.normal(0, margin_of_error, size=len(resampled))
            resampled = [(t, y + jitter[i]) for i, (t, y) in enumerate(resampled)]
        boot_results.append(
            kernel_loess_simple(resampled, bandwidth_days, min_polls, t_values)
        )
    arr = np.array([[v if v is not None else np.nan for v in row]
                    for row in boot_results])
    lows, highs = [], []
    for col_idx in range(len(t_values)):
        col   = arr[:, col_idx]
        valid = col[~np.isnan(col)]
        if len(valid) < min_polls:
            lows.append(None); highs.append(None)
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

def generate_headline(polls, config, latest_smooth, subgroup_label=None):
    if not polls:
        return "", ""

    latest_poll = polls[-1]
    latest_date = latest_poll["date"].strftime("%-d %B %Y")
    pollster    = latest_poll["pollster"]

    party_vals = {
        p: latest_poll["values"].get(p)
        for p in [pt["name"] for pt in config["parties"] if pt["includeInLine"]]
        if latest_poll["values"].get(p) is not None
    }
    if not party_vals:
        return "", ""

    leader     = max(party_vals, key=party_vals.get)
    leader_val = party_vals[leader]

    ref          = config.get("referenceElection", {})
    ref_results  = ref.get("results", {})
    ref_label    = ref.get("label", "last election")
    ref_leader_val = ref_results.get(leader)
    change_str   = ""
    if ref_leader_val is not None:
        change    = round(leader_val - ref_leader_val, 1)
        direction = "up" if change > 0 else "down"
        change_str = f", {direction} {abs(change)} points since the {ref_label}"

    scope    = f" ({subgroup_label})" if subgroup_label else ""
    headline = f"{leader} lead{scope} at {leader_val}%{change_str}"

    intro = (
        f"Latest poll: {pollster} · {latest_date} · "
        + " · ".join(f"{p} {v}%" for p, v in sorted(party_vals.items(), key=lambda x: -x[1]))
    )

    return headline, intro

# ---------------------------------------------------------------------------
# Core build functions (operate on a filtered poll list)
# ---------------------------------------------------------------------------

def build_line_json(polls, config, subgroup_label=None, subgroup_cfg=None):
    global_bw        = config["smoothing"]["bandwidthDays"]
    global_min_polls = config["smoothing"]["minPollsInWindow"]
    n_boot           = config["smoothing"].get("bootstrapIterations", 200)
    global_moe       = config["smoothing"].get("marginOfError", 0)

    bw        = (subgroup_cfg or {}).get("bandwidthDays",    global_bw)
    min_polls = (subgroup_cfg or {}).get("minPollsInWindow", global_min_polls)
    moe       = (subgroup_cfg or {}).get("marginOfError",    global_moe)
    parties   = [p for p in config["parties"] if p["includeInLine"]]

    # Parse break dates — splits data into independent smoothed segments
    raw_breaks = config.get("breaks", [])
    break_days = sorted([
        days_since_epoch(datetime.strptime(b["date"].strip(), "%d/%m/%Y"))
        for b in raw_breaks
    ])

    def segment_pts(pts):
        """Split (t, y) points into segments at break boundaries.
        Always returns list of (seg_idx, pts) tuples."""
        if not break_days:
            return [(0, pts)]
        segments = []
        boundaries = [float("-inf")] + break_days + [float("inf")]
        for i in range(len(boundaries) - 1):
            seg = [(t, y) for t, y in pts if boundaries[i] <= t < boundaries[i+1]]
            if seg:
                segments.append((i, seg))
        return segments

    def segment_col_name(name, seg_idx):
        """Column name for a segment — plain name if no breaks, suffixed otherwise."""
        if not break_days:
            return name
        n = len(break_days)
        if seg_idx == 0:
            label = raw_breaks[0].get("label", "break")
            return f"{name} (pre-{label})"
        elif seg_idx >= n:
            label = raw_breaks[seg_idx - 1].get("label", "break")
            return f"{name} (post-{label})"
        else:
            label = raw_breaks[seg_idx].get("label", "break")
            return f"{name} (to {label})"

    series = {}
    for party in parties:
        name = party["name"]
        all_pts = [
            (days_since_epoch(p["date"]), p["values"][name])
            for p in polls
            if p["values"].get(name) is not None
        ]
        if not all_pts:
            continue

        for seg_idx, pts in segment_pts(all_pts):
            col_name = segment_col_name(name, seg_idx)

            # Per-segment bandwidth: segment 0 uses global, segment N uses break N-1's settings
            if seg_idx == 0:
                seg_bw        = bw
                seg_min_polls = min_polls
            else:
                seg_break = raw_breaks[seg_idx - 1]
                seg_bw        = seg_break.get("bandwidthDays",    bw)
                seg_min_polls = seg_break.get("minPollsInWindow", min_polls)

            smoothed = kernel_loess(pts, seg_bw, seg_min_polls, step_days=1)
            t_values = [s[0] for s in smoothed]

            print(f"    Bootstrapping CI for {col_name} (bandwidth {seg_bw}d, MoE ±{moe}pt, {n_boot} iterations)...")
            lows, highs = bootstrap_ci(pts, seg_bw, seg_min_polls, t_values, n_boot=n_boot, margin_of_error=moe)

            series[col_name] = [
                {
                    "t":      int(t),
                    "date":   epoch_to_date_str(t),
                    "value":  v,
                    "low":    lows[i],
                    "high":   highs[i],
                    "sparse": s,
                    "party":  name
                }
                for i, (t, v, s) in enumerate(smoothed)
            ]

    # Latest smoothed values — use the last segment per party (most recent)
    latest_smooth = {}
    for col_name, pts in series.items():
        non_null = [p for p in pts if p["value"] is not None]
        if non_null:
            # Use "party" field if present, else col_name (backwards compat)
            party_key = non_null[-1].get("party", col_name)
            # Only overwrite if this segment ends later
            existing = latest_smooth.get(party_key)
            if existing is None or non_null[-1]["t"] > existing["t"]:
                latest_smooth[party_key] = {"value": non_null[-1]["value"], "t": non_null[-1]["t"]}
    latest_smooth = {k: v["value"] for k, v in latest_smooth.items()}

    headline, intro = generate_headline(polls, config, latest_smooth, subgroup_label)
    latest_poll_date = polls[-1]["date"].strftime("%d-%b-%Y") if polls else ""

    raw_dots = [
        {
            "date":     p["date"].strftime("%Y-%m-%d"),
            "pollster": p["pollster"],
            "values":   {k: v for k, v in p["values"].items() if v is not None}
        }
        for p in polls
    ]

    return {
        "meta": {
            "trackerName":       config["trackerName"],
            "subgroup":          subgroup_label or "",
            "generated":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bandwidthDays":     bw,
            "minPollsInWindow":  min_polls,
            "bootstrapIterations": n_boot,
            "headline":          headline,
            "intro":             intro,
            "latestSmoothed":    latest_smooth,
            "latestPollDate":    latest_poll_date,
        },
        "series":   series,
        "rawPolls": raw_dots
    }

def build_bar_csv(polls, config, subgroup_cfg=None, line_json=None):
    if not polls:
        return ""

    # Party selection: subgroup override > global includeInBar
    bar_parties_override = (subgroup_cfg or {}).get("barParties")
    if bar_parties_override:
        party_lookup = {p["name"]: p for p in config["parties"]}
        parties = [party_lookup[name] for name in bar_parties_override if name in party_lookup]
    else:
        parties = [p for p in config["parties"] if p["includeInBar"]]

    include_other = (subgroup_cfg or {}).get("barIncludeOther",
                    config.get("barIncludeOther", False))

    # Per-subgroup reference election overrides global if present
    sg_ref      = (subgroup_cfg or {}).get("referenceElection", {})
    ref_results = sg_ref.get("results") or config.get("referenceElection", {}).get("results", {})
    ref_label   = sg_ref.get("label")   or config.get("referenceElection", {}).get("label", "Reference")

    # Use latest smoothed values if available, else fall back to raw polls
    if line_json is not None:
        latest = {name: val for name, val in line_json["meta"]["latestSmoothed"].items()}
    else:
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
        ref  = ref_results.get(name)
        if curr is None:
            continue
        if ref is not None:
            change     = round(curr - ref, 1)
            change_str = f"+{change}" if change >= 0 else str(change)
        else:
            change_str = "n/a"
            ref        = "n/a"
        lines.append(f"{name},{curr},{ref},{change_str}")

    # Other parties: 100 minus sum of bar parties only (not all parties)
    if include_other:
        curr_sum = sum(latest.get(p["name"], 0) or 0 for p in parties)
        curr_other = round(100 - curr_sum, 1)

        ref_sum = sum(ref_results.get(p["name"], 0) or 0 for p in parties
                      if ref_results.get(p["name"]) is not None)
        ref_other = round(100 - ref_sum, 1) if ref_sum > 0 else None

        if ref_other is not None:
            change     = round(curr_other - ref_other, 1)
            change_str = f"+{change}" if change >= 0 else str(change)
            lines.append(f"Other,{curr_other},{ref_other},{change_str}")
        else:
            lines.append(f"Other,{curr_other},n/a,n/a")

    return "\n".join(lines)

def build_line_csv(json_data, polls, hist_series=None, hist_polls=None):
    series      = json_data["series"]
    if not series:
        return ""

    hist_series = hist_series or {}
    hist_polls  = hist_polls  or {}

    # series keys may be suffixed (e.g. "Lab (pre-2024 election)")
    # party_names for poll dots uses the "party" field or falls back to key
    series_keys = list(series.keys())

    # Unique base party names (for poll dot columns)
    seen = []
    for key in series_keys:
        pts = series[key]
        base = pts[0].get("party", key) if pts else key
        if base not in seen:
            seen.append(base)
    party_names = seen

    hist_smooth_cols = sorted(set(
        k for k in hist_series
        if not k.endswith(" (low)") and not k.endswith(" (high)")
    ))

    daily = defaultdict(lambda: defaultdict(list))
    for poll in polls:
        date_str = poll["date"].strftime("%Y-%m-%d")
        for name in party_names:
            val = poll["values"].get(name)
            if val is not None:
                daily[date_str][name].append(val)

    for party, date_vals in hist_polls.items():
        for date_str, val in date_vals.items():
            daily[date_str][party].append(val)

    raw_dates = sorted(daily.keys())

    smooth_lookup = {}
    for key in series_keys:
        for pt in series.get(key, []):
            if pt["value"] is not None:
                smooth_lookup.setdefault(pt["date"], {})[key] = (
                    pt["value"], pt.get("low"), pt.get("high")
                )

    all_hist_dates  = set()
    for col_data in hist_series.values():
        all_hist_dates.update(col_data.keys())

    all_dates = sorted(set(smooth_lookup.keys()) | all_hist_dates | set(raw_dates))

    header = ["date"]
    for key in series_keys:
        header += [key, f"{key} (low)", f"{key} (high)"]
    for col in hist_smooth_cols:
        header += [col, f"{col} (low)", f"{col} (high)"]
    for name in party_names:
        header.append(f"{name} (poll)")
    lines = [",".join(header)]

    for date in all_dates:
        row = [date]
        for key in series_keys:
            entry = smooth_lookup.get(date, {}).get(key)
            if entry:
                v, lo, hi = entry
                row += [str(v),
                        "" if lo is None else str(lo),
                        "" if hi is None else str(hi)]
            else:
                row += ["", "", ""]
        for col in hist_smooth_cols:
            v  = hist_series.get(col, {}).get(date)
            lo = hist_series.get(f"{col} (low)", {}).get(date)
            hi = hist_series.get(f"{col} (high)", {}).get(date)
            row += ["" if v  is None else str(v),
                    "" if lo is None else str(lo),
                    "" if hi is None else str(hi)]
        for name in party_names:
            vals = daily.get(date, {}).get(name, [])
            avg  = round(sum(vals) / len(vals), 1) if vals else ""
            row.append("" if avg == "" else str(avg))
        lines.append(",".join(row))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Historical series loader
# ---------------------------------------------------------------------------

def load_historical_series(config, subgroup_value=None):
    hist_config = config.get("historicalSeries")
    if not hist_config:
        return {}, {}

    url            = hist_config["url"]
    suffix         = hist_config.get("labelSuffix", " (hist)")
    wanted_parties = hist_config.get("parties", [])
    match_subgroup = hist_config.get("matchSubgroup", False)

    # Resolve {subgroup} placeholder in URL
    if match_subgroup and subgroup_value is not None:
        slug = subgroup_slug(subgroup_value)
        url  = url.replace("{subgroup}", slug)
    elif match_subgroup and subgroup_value is None:
        # No subgroup provided but matchSubgroup requested — skip
        print("  Skipping historical series: matchSubgroup=true but no subgroup active")
        return {}, {}

    print(f"  Fetching historical series from: {url}")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.HTTPError as e:
        print(f"  WARNING: Could not fetch historical series ({e}), skipping")
        return {}, {}

    reader = csv.DictReader(io.StringIO(resp.text))
    rows   = list(reader)

    hist  = defaultdict(dict)
    polls = defaultdict(dict)

    for row in rows:
        date = row.get("date", "").strip()
        if not date:
            continue
        for party in wanted_parties:
            for src_key, dst_key in [
                (party,             f"{party}{suffix}"),
                (f"{party} (low)",  f"{party}{suffix} (low)"),
                (f"{party} (high)", f"{party}{suffix} (high)"),
            ]:
                if src_key in row and row[src_key].strip():
                    try:
                        hist[dst_key][date] = float(row[src_key])
                    except ValueError:
                        pass
            poll_key = f"{party} (poll)"
            if poll_key in row and row[poll_key].strip():
                try:
                    polls[party][date] = float(row[poll_key])
                except ValueError:
                    pass

    print(f"  Loaded historical series: {len(hist)} columns, {len(set(d for v in hist.values() for d in v))} dates")
    return dict(hist), dict(polls)


# ---------------------------------------------------------------------------
# Summary CSV builder
# ---------------------------------------------------------------------------

def build_summary_csv(subgroup_results, config, months_back=(2, 6)):
    """
    One row per subgroup, columns per party per timepoint.
    subgroup_results: list of (sg_label, line_json) tuples
    months_back: tuple of month offsets to include (default: 2 and 6)

    Output columns:
        subgroup, [Party] latest, [Party] Xm ago, [Party] Ym ago, ...
    """
    from datetime import datetime, timedelta

    if not subgroup_results:
        return ""

    # Collect all base party names that appear across any subgroup
    all_parties = []
    seen = set()
    for _, line_json in subgroup_results:
        for col_name in line_json["series"]:
            pts = line_json["series"][col_name]
            base = pts[0].get("party", col_name) if pts else col_name
            if base not in seen:
                seen.add(base)
                all_parties.append(base)

    # Only include parties that are includeInLine
    line_parties = [p["name"] for p in config["parties"] if p["includeInLine"]]
    parties = [p for p in all_parties if p in line_parties]

    # Reference dates
    now = datetime.utcnow()
    ref_dates = {m: now - timedelta(days=m * 30) for m in months_back}

    def find_value_at(series, party, target_dt):
        """Find smoothed value closest to target_dt for a given party."""
        target_t = (target_dt - datetime(1970, 1, 1)).days
        # Collect all segments for this party
        candidates = []
        for col_name, pts in series.items():
            base = pts[0].get("party", col_name) if pts else col_name
            if base != party:
                continue
            non_null = [(pt["t"], pt["value"]) for pt in pts if pt["value"] is not None]
            candidates.extend(non_null)
        if not candidates:
            return None
        # Find closest point to target
        closest = min(candidates, key=lambda x: abs(x[0] - target_t))
        # Only return if within 30 days of target
        if abs(closest[0] - target_t) <= 30:
            return closest[1]
        return None

    # Header
    header = ["subgroup"]
    for party in parties:
        header.append(f"{party} latest")
        for m in months_back:
            header.append(f"{party} {m}m ago")

    lines = [",".join(header)]

    for sg_label, line_json in subgroup_results:
        series   = line_json["series"]
        latest_s = line_json["meta"].get("latestSmoothed", {})
        row = [sg_label]
        for party in parties:
            # Latest smoothed value
            latest_val = latest_s.get(party)
            row.append("" if latest_val is None else str(latest_val))
            # Historical values
            for m in months_back:
                val = find_value_at(series, party, ref_dates[m])
                row.append("" if val is None else str(round(val, 1)))
        lines.append(",".join(row))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Datawrapper push
# ---------------------------------------------------------------------------

def push_to_datawrapper(json_data, bar_csv, polls, config,
                        hist_series=None, hist_polls=None,
                        line_id_override=None, bar_id_override=None):
    token = os.environ.get("DATAWRAPPER_TOKEN")
    if not token:
        print("Warning: DATAWRAPPER_TOKEN not set, skipping Datawrapper push")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    line_id = line_id_override or config["output"].get("datawrapperLineChartId")
    bar_id  = bar_id_override  or config["output"].get("datawrapperBarChartId")
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
        resp = requests.patch(
            f"https://api.datawrapper.de/v3/charts/{line_id}",
            headers=headers,
            json={"metadata": {"describe": {"byline": byline_html}}}
        )
        print(f"Line chart patch response: {resp.status_code}")
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
        resp = requests.patch(
            f"https://api.datawrapper.de/v3/charts/{bar_id}",
            headers=headers,
            json={"metadata": {"describe": {"byline": byline_html}}}
        )
        print(f"Bar chart patch response: {resp.status_code}")
        requests.post(
            f"https://api.datawrapper.de/v3/charts/{bar_id}/publish",
            headers=headers
        )
        print(f"Pushed and republished bar chart: {bar_id}")

# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------

def subgroup_slug(value):
    """Convert a subgroup value to a safe filename slug."""
    return value.lower().replace(" ", "-").replace("/", "-")

def get_output_paths(config, subgroup_value=None):
    """
    Return (line_json_path, bar_csv_path, line_csv_path, dw_line_id, dw_bar_id)
    for either the base tracker or a specific subgroup.
    """
    output = config.get("output", {})

    if subgroup_value is None:
        return (
            output.get("lineJsonPath", ""),
            output.get("barCsvPath", ""),
            output.get("lineCsvPath", ""),
            output.get("datawrapperLineChartId", ""),
            output.get("datawrapperBarChartId", ""),
        )

    slug = subgroup_slug(subgroup_value)
    subgroups_output = output.get("subgroups", {})
    sg_out = subgroups_output.get(subgroup_value, {})

    # Fall back to auto-generated paths if not explicitly configured
    base_line_json = output.get("lineJsonPath", "")
    base_bar_csv   = output.get("barCsvPath", "")
    base_line_csv  = output.get("lineCsvPath", "")

    def insert_slug(path, slug):
        root, ext = os.path.splitext(path)
        return f"{root}-{slug}{ext}"

    return (
        sg_out.get("lineJsonPath", insert_slug(base_line_json, slug) if base_line_json else ""),
        sg_out.get("barCsvPath",   insert_slug(base_bar_csv,   slug) if base_bar_csv   else ""),
        sg_out.get("lineCsvPath",  insert_slug(base_line_csv,  slug) if base_line_csv  else ""),
        sg_out.get("datawrapperLineChartId", ""),
        sg_out.get("datawrapperBarChartId",  ""),
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args   = parse_args()
    config = load_config(args.config)

    all_polls = fetch_polls(config)
    if not all_polls:
        print("No polls loaded, exiting")
        sys.exit(1)

    subgroups = config.get("subgroups")

    if subgroups:
        # --- Subgroup mode: run pipeline once per subgroup ---
        print(f"Subgroup mode: {len(subgroups)} subgroups")
        subgroup_results = []  # collect (label, line_json) for summary CSV
        for sg in subgroups:
            sg_value = sg["value"]
            sg_label = sg.get("name", sg_value)
            print(f"\n--- Subgroup: {sg_label} ---")

            polls = filter_polls_by_subgroup(all_polls, sg_value)
            if not polls:
                print(f"  No polls found for subgroup '{sg_value}', skipping")
                continue
            print(f"  {len(polls)} polls")

            # Load historical series for this subgroup
            hist_series, hist_polls = load_historical_series(config, subgroup_value=sg_value)

            print("  Building smoothed series...")
            line_json = build_line_json(polls, config, subgroup_label=sg_label, subgroup_cfg=sg)
            bar_csv   = build_bar_csv(polls, config, subgroup_cfg=sg, line_json=line_json)

            line_json_path, bar_csv_path, line_csv_path, dw_line_id, dw_bar_id = \
                get_output_paths(config, sg_value)

            def write(path, content):
                if not path or not content:
                    return
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(content)
                print(f"  Written: {path}")

            if line_json_path:
                os.makedirs(os.path.dirname(line_json_path), exist_ok=True)
                with open(line_json_path, "w") as f:
                    json.dump(line_json, f, indent=2)
                print(f"  Written: {line_json_path}")

            write(bar_csv_path, bar_csv)

            if line_csv_path:
                write(line_csv_path, build_line_csv(line_json, polls, hist_series, hist_polls))

            print(f"  Headline: {line_json['meta']['headline']}")
            subgroup_results.append((sg_label, line_json))

            if args.push_to_datawrapper:
                push_to_datawrapper(
                    line_json, bar_csv, polls, config,
                    hist_series=hist_series, hist_polls=hist_polls,
                    line_id_override=dw_line_id or None,
                    bar_id_override=dw_bar_id or None
                )

        # Write summary CSV after all subgroups processed
        summary_csv_path = config.get("output", {}).get("summaryCsvPath", "")
        if summary_csv_path and subgroup_results:
            summary_csv = build_summary_csv(subgroup_results, config)
            os.makedirs(os.path.dirname(summary_csv_path), exist_ok=True)
            with open(summary_csv_path, "w") as f:
                f.write(summary_csv)
            print(f"Written: {summary_csv_path}")

    else:
        # --- Standard mode: single series ---
        hist_series, hist_polls = load_historical_series(config, subgroup_value=None)
        print("Building smoothed series with confidence intervals...")
        line_json = build_line_json(all_polls, config)
        bar_csv   = build_bar_csv(all_polls, config, line_json=line_json)

        line_json_path, bar_csv_path, line_csv_path, _, _ = get_output_paths(config)

        def write(path, content):
            if not path or not content:
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            print(f"Written: {path}")

        if line_json_path:
            os.makedirs(os.path.dirname(line_json_path), exist_ok=True)
            with open(line_json_path, "w") as f:
                json.dump(line_json, f, indent=2)
            print(f"Written: {line_json_path}")

        write(bar_csv_path, bar_csv)

        if line_csv_path:
            write(line_csv_path, build_line_csv(line_json, all_polls, hist_series, hist_polls))

        print(f"\nHeadline: {line_json['meta']['headline']}")
        print(f"Intro:    {line_json['meta']['intro']}")

        if args.push_to_datawrapper:
            push_to_datawrapper(line_json, bar_csv, all_polls, config,
                                hist_series=hist_series, hist_polls=hist_polls)

if __name__ == "__main__":
    main()
