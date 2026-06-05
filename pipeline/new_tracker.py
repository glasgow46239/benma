#!/usr/bin/env python3
"""
new_tracker.py — interactive setup script for a new poll tracker
-----------------------------------------------------------------
Creates a new tracker folder, config.json, and index.html,
and optionally creates Datawrapper charts via the API.

Usage:
    python pipeline/new_tracker.py
    python pipeline/new_tracker.py --datawrapper-token YOUR_TOKEN
    DATAWRAPPER_TOKEN=xxx python pipeline/new_tracker.py

Dependencies:
    pip install requests
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ask(prompt, default=None, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default:
            return default
        if not val and required:
            print("  (required)")
            continue
        return val or ""

def ask_yn(prompt, default="y"):
    val = input(f"{prompt} [{'Y/n' if default=='y' else 'y/N'}]: ").strip().lower()
    if not val:
        return default == "y"
    return val.startswith("y")

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

# ---------------------------------------------------------------------------
# Datawrapper
# ---------------------------------------------------------------------------

DW_BASE = "https://api.datawrapper.de/v3"

def dw_create_chart(token, chart_type, title):
    resp = requests.post(
        f"{DW_BASE}/charts",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"type": chart_type, "title": title}
    )
    if resp.status_code not in (200, 201):
        print(f"  WARNING: Could not create chart ({resp.status_code}): {resp.text}")
        return None
    return resp.json().get("id")

def dw_set_byline(token, chart_id, byline):
    requests.patch(
        f"{DW_BASE}/charts/{chart_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"metadata": {"describe": {"byline": byline}}}
    )

# ---------------------------------------------------------------------------
# Party input
# ---------------------------------------------------------------------------

KNOWN_COLORS = {
    "con":    "#0087DC",
    "lab":    "#E4003B",
    "ldem":   "#FAA61A",
    "ld":     "#FAA61A",
    "pc":     "#005B54",
    "snp":    "#FDF38E",
    "grn":    "#00B140",
    "green":  "#00B140",
    "reform": "#12B6CF",
    "ukip":   "#6D3177",
    "dup":    "#D46A4C",
    "sf":     "#326760",
    "sdlp":   "#2AA82C",
    "uup":    "#48A5EE",
    "apni":   "#F6CB2F",
}

def get_parties():
    print("\nEnter parties (one per line).")
    print("Format: Name:ColumnIndex:HexColor")
    print("Column index is 0-based from the CSV (0=date, 1=pollster, 2=#1, 3=#2, ...)")
    print("Color is optional if the party name is recognised (Con, Lab, LDem, PC, SNP, Grn, Reform, UKIP).")
    print("Flags: add '+line' or '+bar' to include/exclude from charts (default: both).")
    print("Example:  Lab:3:#E4003B+line+bar")
    print("Example:  UKIP:5  (color auto-detected, included in both)")
    print("Blank line to finish.\n")

    parties = []
    while True:
        raw = input("  Party: ").strip()
        if not raw:
            if not parties:
                print("  (need at least one party)")
                continue
            break

        parts = raw.split(":")
        if len(parts) < 2:
            print("  Format: Name:ColumnIndex[:Color]")
            continue

        name = parts[0].strip()
        try:
            col = int(parts[1].strip())
        except ValueError:
            print("  Column index must be a number")
            continue

        # Color
        color_part = parts[2].strip() if len(parts) > 2 else ""
        # Strip any flags from color
        color_part = color_part.split("+")[0].strip()
        if not color_part:
            color = KNOWN_COLORS.get(name.lower())
            if not color:
                color = input(f"  Color for {name} (hex): ").strip()
        else:
            color = color_part

        # Flags
        include_line = "+line" in raw.lower() or "+bar" not in raw.lower()
        include_bar  = "+bar"  in raw.lower() or "+line" not in raw.lower()

        parties.append({
            "name": name,
            "col": col,
            "color": color,
            "includeInLine": include_line,
            "includeInBar": include_bar
        })
        print(f"  Added: {name} (col {col}, {color}, line={include_line}, bar={include_bar})")

    return parties

# ---------------------------------------------------------------------------
# Reference election
# ---------------------------------------------------------------------------

def get_reference_election(parties):
    print("\nReference election (shown in bar chart as comparison baseline).")
    label = ask("Election label", default="2024 general election")
    date  = ask("Election date (DD/MM/YYYY)", default="04/07/2024")

    bar_parties = [p for p in parties if p["includeInBar"]]
    results = {}
    print("Enter vote share (%) for each party, or blank to skip:")
    for p in bar_parties:
        val = input(f"  {p['name']}: ").strip()
        if val:
            try:
                results[p["name"]] = float(val)
            except ValueError:
                print("  (skipped — not a number)")

    return {"label": label, "date": date, "results": results}

# ---------------------------------------------------------------------------
# Build config
# ---------------------------------------------------------------------------

def build_config(folder_name, tracker_name, data_url, parties, ref_election,
                 bw, min_polls, n_boot, line_id, bar_id):
    return {
        "trackerName": tracker_name,
        "dataUrl": data_url,
        "smoothing": {
            "bandwidthDays": bw,
            "minPollsInWindow": min_polls,
            "bootstrapIterations": n_boot
        },
        "skipPollsterPatterns": ["GE result", "result"],
        "parties": parties,
        "referenceElection": ref_election,
        "output": {
            "lineJsonPath": f"trackers/{folder_name}/data/line.json",
            "barCsvPath":   f"trackers/{folder_name}/data/bar.csv",
            "lineCsvPath":  f"trackers/{folder_name}/data/line.csv",
            "datawrapperLineChartId": line_id or "",
            "datawrapperBarChartId":  bar_id  or "",
        }
    }

# ---------------------------------------------------------------------------
# Scaffold files
# ---------------------------------------------------------------------------

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{tracker_name} — explorer</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #f7f6f2; --surface: #ffffff; --border: rgba(0,0,0,0.1);
    --text: #1a1a1a; --muted: #666; --radius: 6px;
    --font: 'DM Sans', system-ui, sans-serif;
    --font-mono: 'DM Mono', monospace;
  }}
  body {{ font-family: var(--font); background: var(--bg); color: var(--text);
          min-height: 100vh; padding: 1.5rem; }}
  header {{ display: flex; align-items: flex-start; justify-content: space-between;
            flex-wrap: wrap; gap: 1rem; margin-bottom: 1.5rem; }}
  .tracker-name {{ font-size: 11px; font-weight: 500; letter-spacing: 0.08em;
                   text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }}
  h1 {{ font-size: 22px; font-weight: 600; line-height: 1.2; }}
  .headline-sub {{ font-size: 13px; color: var(--muted); margin-top: 4px;
                   font-family: var(--font-mono); }}
  .share-btn {{ display: flex; align-items: center; gap: 6px; font-size: 12px;
                background: var(--surface); border: 1px solid var(--border);
                border-radius: var(--radius); padding: 6px 12px; cursor: pointer;
                color: var(--muted); white-space: nowrap; transition: color 0.15s; }}
  .share-btn:hover {{ color: var(--text); }}
  .controls-bar {{ display: flex; flex-wrap: wrap; gap: 1.5rem; align-items: center;
                   background: var(--surface); border: 1px solid var(--border);
                   border-radius: var(--radius); padding: 0.75rem 1rem;
                   margin-bottom: 1rem; font-size: 13px; color: var(--muted); }}
  .ctrl-group {{ display: flex; align-items: center; gap: 8px; }}
  .ctrl-group label {{ font-weight: 500; color: var(--text); }}
  input[type=range] {{ -webkit-appearance: none; width: 110px; height: 4px;
                       background: #ddd; border-radius: 2px; outline: none; }}
  input[type=range]::-webkit-slider-thumb {{ -webkit-appearance: none; width: 14px;
    height: 14px; border-radius: 50%; background: var(--text); cursor: pointer; }}
  .ctrl-val {{ font-family: var(--font-mono); font-size: 12px; min-width: 36px;
               color: var(--text); font-weight: 500; }}
  .ctrl-divider {{ width: 1px; height: 20px; background: var(--border); }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 1rem; }}
  .legend-pill {{ display: flex; align-items: center; gap: 5px; font-size: 12px;
                  font-weight: 500; padding: 4px 10px; border-radius: 20px;
                  border: 1.5px solid transparent; cursor: pointer;
                  transition: opacity 0.15s; user-select: none; }}
  .legend-pill.hidden-party {{ opacity: 0.3; }}
  .legend-dot {{ width: 7px; height: 7px; border-radius: 50%; }}
  .chart-wrap {{ background: var(--surface); border: 1px solid var(--border);
                 border-radius: var(--radius); padding: 1rem; position: relative; }}
  .status {{ font-size: 12px; color: var(--muted); margin-top: 0.75rem;
             font-family: var(--font-mono); display: flex; gap: 1rem; flex-wrap: wrap; }}
  .status-dot {{ display: inline-block; width: 6px; height: 6px; border-radius: 50%;
                 background: #22c55e; margin-right: 4px; vertical-align: middle; }}
  .toast {{ position: fixed; bottom: 1.5rem; right: 1.5rem; background: var(--text);
            color: #fff; font-size: 13px; padding: 8px 16px; border-radius: var(--radius);
            opacity: 0; pointer-events: none; transition: opacity 0.2s; }}
  .toast.show {{ opacity: 1; }}
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&family=DM+Mono&display=swap" rel="stylesheet">
</head>
<body>
<header>
  <div>
    <div class="tracker-name" id="tracker-name">Loading…</div>
    <h1 id="headline">{tracker_name}</h1>
    <div class="headline-sub" id="intro-line"></div>
  </div>
  <button class="share-btn" id="share-btn">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
      <circle cx="12" cy="3" r="1.5"/><circle cx="4" cy="8" r="1.5"/><circle cx="12" cy="13" r="1.5"/>
      <line x1="5.4" y1="7.1" x2="10.6" y2="4"/><line x1="5.4" y1="8.9" x2="10.6" y2="12"/>
    </svg>
    Share view
  </button>
</header>
<div class="controls-bar">
  <div class="ctrl-group">
    <label for="bw-slider">Bandwidth</label>
    <input type="range" id="bw-slider" min="30" max="400" step="10" value="120">
    <span class="ctrl-val"><span id="bw-val">120</span>d</span>
  </div>
  <div class="ctrl-divider"></div>
  <div class="ctrl-group">
    <label for="mp-slider">Min polls</label>
    <input type="range" id="mp-slider" min="2" max="10" step="1" value="4">
    <span class="ctrl-val" id="mp-val">4</span>
  </div>
  <div class="ctrl-divider"></div>
  <label class="ctrl-group" style="cursor:pointer">
    <input type="checkbox" id="show-dots" checked>
    <span>Show poll dots</span>
  </label>
  <label class="ctrl-group" style="cursor:pointer">
    <input type="checkbox" id="show-sparse" checked>
    <span>Show sparse zones</span>
  </label>
</div>
<div class="legend" id="legend"></div>
<div class="chart-wrap">
  <div style="position:relative; height:440px;">
    <canvas id="chart-canvas"></canvas>
  </div>
</div>
<div class="status">
  <span><span class="status-dot"></span><span id="status-text">Loading data…</span></span>
  <span id="status-polls"></span>
  <span id="status-params"></span>
</div>
<div class="toast" id="toast">Link copied to clipboard</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const CONFIG_URL = "config.json";
let config = null, allPolls = [], hiddenParties = new Set(), chart = null;

function readUrlParams() {{
  const p = new URLSearchParams(location.search);
  return {{
    bw: p.has("bw") ? +p.get("bw") : null,
    minPolls: p.has("mp") ? +p.get("mp") : null,
    showDots: p.has("dots") ? p.get("dots") !== "0" : null,
    hidden: p.has("hidden") ? p.get("hidden").split(",").filter(Boolean) : []
  }};
}}

function writeUrlParams() {{
  const p = new URLSearchParams();
  p.set("bw", document.getElementById("bw-slider").value);
  p.set("mp", document.getElementById("mp-slider").value);
  p.set("dots", document.getElementById("show-dots").checked ? "1" : "0");
  if (hiddenParties.size) p.set("hidden", [...hiddenParties].join(","));
  history.replaceState(null, "", "?" + p.toString());
}}

function parseDate(s) {{ const [d,m,y] = s.trim().split("/"); return new Date(+y,+m-1,+d); }}

function shouldSkip(pollster, patterns) {{
  return patterns.some(pat => pollster.toLowerCase().includes(pat.toLowerCase()));
}}

async function fetchPolls(cfg) {{
  const resp = await fetch(cfg.dataUrl);
  const text = await resp.text();
  const lines = text.trim().split("\\n");
  let headerIdx = 0;
  for (let i = 0; i < lines.length; i++) {{
    if (lines[i].toLowerCase().startsWith("date")) {{ headerIdx = i; break; }}
  }}
  const skipPatterns = cfg.skipPollsterPatterns || [];
  const polls = [];
  for (const line of lines.slice(headerIdx + 1)) {{
    const cols = line.split(",");
    if (!cols[0] || !cols[0].trim()) continue;
    const date = parseDate(cols[0]);
    if (isNaN(date)) continue;
    const pollster = (cols[1] || "").trim();
    if (shouldSkip(pollster, skipPatterns)) continue;
    const entry = {{ date, ts: date.getTime(), pollster, values: {{}} }};
    for (const party of cfg.parties) {{
      const raw = (cols[party.col] || "").trim();
      entry.values[party.name] = raw !== "" && raw !== "0" ? +raw : null;
    }}
    polls.push(entry);
  }}
  polls.sort((a,b) => a.ts - b.ts);
  return polls;
}}

function tricube(u) {{ const a = Math.abs(u); if (a >= 1) return 0; return Math.pow(1-Math.pow(a,3),3); }}

function kernelLoess(pts, bwMs, minPolls, nSteps=300) {{
  if (pts.length < 2) return [];
  const tMin = pts[0][0], tMax = pts[pts.length-1][0];
  const results = [];
  for (let i = 0; i <= nSteps; i++) {{
    const t = tMin + (tMax-tMin)*(i/nSteps);
    const nearby = pts.filter(([tx]) => Math.abs(tx-t) <= bwMs);
    if (nearby.length < minPolls) {{ results.push({{t, y:null, sparse:false}}); continue; }}
    const wider = pts.filter(([tx]) => Math.abs(tx-t) <= bwMs*1.8);
    const sparse = wider.length < minPolls*2;
    let wSum=0, wySum=0;
    for (const [tx,y] of nearby) {{ const w=tricube((tx-t)/bwMs); wSum+=w; wySum+=w*y; }}
    results.push({{t, y: wSum>0 ? Math.round(wySum/wSum*10)/10 : null, sparse}});
  }}
  return results;
}}

function buildLegend(cfg) {{
  const el = document.getElementById("legend");
  el.innerHTML = "";
  for (const party of cfg.parties) {{
    if (!party.includeInLine) continue;
    const pill = document.createElement("div");
    pill.className = "legend-pill" + (hiddenParties.has(party.name) ? " hidden-party" : "");
    pill.style.borderColor = party.color + "55";
    pill.style.background = party.color + "12";
    pill.innerHTML = `<span class="legend-dot" style="background:${{party.color}}"></span>${{party.name}}`;
    pill.addEventListener("click", () => {{
      if (hiddenParties.has(party.name)) hiddenParties.delete(party.name);
      else hiddenParties.add(party.name);
      pill.classList.toggle("hidden-party");
      writeUrlParams(); rebuildChart();
    }});
    el.appendChild(pill);
  }}
}}

function getParams() {{
  return {{
    bw: +document.getElementById("bw-slider").value * 86400000,
    minPolls: +document.getElementById("mp-slider").value,
    showDots: document.getElementById("show-dots").checked,
    showSparse: document.getElementById("show-sparse").checked,
  }};
}}

function buildDatasets(cfg, polls, params) {{
  const {{ bw, minPolls, showDots, showSparse }} = params;
  const datasets = [];
  for (const party of cfg.parties) {{
    if (!party.includeInLine) continue;
    const hidden = hiddenParties.has(party.name);
    const pts = polls.filter(p => p.values[party.name] != null).map(p => [p.ts, p.values[party.name]]);
    if (!pts.length) continue;
    const smooth = kernelLoess(pts, bw, minPolls);
    datasets.push({{
      label: party.name, data: smooth.map(s => ({{x:s.t, y:s.y}})),
      borderColor: party.color, borderWidth: 2, pointRadius: 0,
      tension: 0.4, fill: false, spanGaps: false, hidden, partyName: party.name,
      segment: {{
        borderDash: ctx => {{ const s=smooth[ctx.p0DataIndex]; return s&&s.sparse&&showSparse?[5,4]:[]; }},
        borderColor: ctx => {{ const s=smooth[ctx.p0DataIndex]; return s&&s.sparse&&showSparse?party.color+"77":party.color; }}
      }}
    }});
    if (showDots) {{
      datasets.push({{
        label: party.name+"_dots", data: pts.map(([x,y])=>({{x,y}})),
        borderColor: party.color, backgroundColor: party.color+"30",
        borderWidth: 1.5, pointRadius: 3.5, pointHoverRadius: 5,
        showLine: false, hidden, partyName: party.name
      }});
    }}
  }}
  return datasets;
}}

function updateStatus(polls, params) {{
  const bwDays = params.bw/86400000;
  document.getElementById("status-polls").textContent = `${{polls.length}} polls`;
  document.getElementById("status-params").textContent = `bandwidth ${{bwDays}}d · min polls ${{params.minPolls}}`;
  if (polls.length) {{
    const latest = polls[polls.length-1];
    document.getElementById("status-text").textContent =
      `Latest: ${{latest.pollster}}, ${{latest.date.toLocaleDateString("en-GB",{{day:"numeric",month:"short",year:"numeric"}})}}`;
  }}
}}

function rebuildChart() {{
  if (!config || !allPolls.length) return;
  const params = getParams();
  const datasets = buildDatasets(config, allPolls, params);
  updateStatus(allPolls, params);
  writeUrlParams();
  if (chart) {{ chart.data.datasets = datasets; chart.update("none"); return; }}
  const ctx = document.getElementById("chart-canvas").getContext("2d");
  chart = new Chart(ctx, {{
    type: "line", data: {{ datasets }},
    options: {{
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: {{ mode: "index", intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            title: items => new Date(items[0].parsed.x).toLocaleDateString("en-GB",{{day:"numeric",month:"short",year:"numeric"}}),
            label: item => item.dataset.label.endsWith("_dots")||item.parsed.y==null ? null : ` ${{item.dataset.partyName}}: ${{item.parsed.y.toFixed(1)}}%`
          }},
          filter: item => !item.dataset.label.endsWith("_dots") && item.parsed.y !== null
        }}
      }},
      scales: {{
        x: {{ type:"linear",
          min: new Date(2010,0,1).getTime(), max: new Date(2027,0,1).getTime(),
          ticks: {{ callback: v => new Date(v).getFullYear(), maxTicksLimit:18, color:"#999", font:{{size:11}} }},
          grid: {{ color:"rgba(0,0,0,0.05)" }}
        }},
        y: {{ min:0, max:60,
          ticks: {{ callback: v => v+"%", color:"#999", font:{{size:11}} }},
          grid: {{ color:"rgba(0,0,0,0.05)" }}
        }}
      }}
    }}
  }});
}}

document.getElementById("share-btn").addEventListener("click", () => {{
  writeUrlParams();
  navigator.clipboard.writeText(location.href).then(() => {{
    const t = document.getElementById("toast");
    t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 2000);
  }});
}});
document.getElementById("bw-slider").addEventListener("input", function() {{
  document.getElementById("bw-val").textContent = this.value; rebuildChart();
}});
document.getElementById("mp-slider").addEventListener("input", function() {{
  document.getElementById("mp-val").textContent = this.value; rebuildChart();
}});
document.getElementById("show-dots").addEventListener("change", rebuildChart);
document.getElementById("show-sparse").addEventListener("change", rebuildChart);

async function boot() {{
  try {{
    config = await (await fetch(CONFIG_URL)).json();
    document.getElementById("tracker-name").textContent = config.trackerName;
    document.title = config.trackerName + " — explorer";
    const urlParams = readUrlParams();
    const bw = urlParams.bw ?? config.smoothing.bandwidthDays;
    const mp = urlParams.minPolls ?? config.smoothing.minPollsInWindow;
    document.getElementById("bw-slider").value = bw;
    document.getElementById("bw-val").textContent = bw;
    document.getElementById("mp-slider").value = mp;
    document.getElementById("mp-val").textContent = mp;
    if (urlParams.showDots !== null) document.getElementById("show-dots").checked = urlParams.showDots;
    hiddenParties = new Set(urlParams.hidden);
    allPolls = await fetchPolls(config);
    buildLegend(config);
    rebuildChart();
    if (allPolls.length) {{
      const latest = allPolls[allPolls.length-1];
      const partyVals = config.parties
        .filter(p => p.includeInLine && latest.values[p.name] != null)
        .sort((a,b) => latest.values[b.name]-latest.values[a.name]);
      if (partyVals.length) {{
        document.getElementById("headline").textContent = `${{partyVals[0].name}} lead in ${{config.trackerName}}`;
        document.getElementById("intro-line").textContent =
          `Latest: ${{latest.pollster}} · ` + partyVals.map(p=>`${{p.name}} ${{latest.values[p.name]}}%`).join(" · ");
      }}
    }}
  }} catch(err) {{
    console.error(err);
    document.getElementById("status-text").textContent = "Error loading data — check console";
  }}
}}
boot();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Create a new poll tracker")
    parser.add_argument("--datawrapper-token", default=os.environ.get("DATAWRAPPER_TOKEN", ""),
                        help="Datawrapper API token (or set DATAWRAPPER_TOKEN env var)")
    parser.add_argument("--trackers-dir", default="trackers",
                        help="Root trackers directory (default: trackers)")
    args = parser.parse_args()

    token = args.datawrapper_token
    trackers_dir = Path(args.trackers_dir)

    print("=" * 60)
    print("  New poll tracker setup")
    print("=" * 60)

    # Basic info
    tracker_name = ask("\nTracker name (e.g. 'Welsh Senedd VI')")
    folder_name  = ask("Folder name", default=slug(tracker_name))
    data_url     = ask("Google Sheets CSV URL")

    # Check folder doesn't already exist
    tracker_dir = trackers_dir / folder_name
    if tracker_dir.exists():
        if not ask_yn(f"\nFolder trackers/{folder_name} already exists. Overwrite?", default="n"):
            print("Aborted.")
            sys.exit(0)

    # Smoothing defaults
    print("\nSmoothing defaults (can be adjusted in config.json later):")
    bw       = int(ask("  Bandwidth (days)", default="120"))
    min_polls = int(ask("  Min polls in window", default="4"))
    n_boot   = int(ask("  Bootstrap iterations for CI", default="200"))

    # Parties
    parties = get_parties()

    # Reference election
    ref_election = get_reference_election(parties)

    # Datawrapper
    line_id, bar_id = "", ""
    use_dw = ask_yn("\nCreate Datawrapper charts automatically?", default="y" if token else "n")

    if use_dw:
        if not token:
            token = ask("Datawrapper API token")
        if token:
            print(f"\nCreating line chart '{tracker_name}'...")
            line_id = dw_create_chart(token, "d3-lines", tracker_name)
            print(f"  Line chart ID: {line_id}" if line_id else "  Failed to create line chart")

            print(f"Creating bar chart '{tracker_name} — change since {ref_election['label']}'...")
            bar_id = dw_create_chart(token, "d3-bars-split",
                                     f"{tracker_name} — change since {ref_election['label']}")
            print(f"  Bar chart ID: {bar_id}" if bar_id else "  Failed to create bar chart")
        else:
            print("  No token provided, skipping Datawrapper chart creation.")
    else:
        line_id = ask("Datawrapper line chart ID (or blank)", required=False)
        bar_id  = ask("Datawrapper bar chart ID (or blank)",  required=False)

    # Build config
    config = build_config(folder_name, tracker_name, data_url, parties, ref_election,
                          bw, min_polls, n_boot, line_id, bar_id)

    # Scaffold files
    tracker_dir.mkdir(parents=True, exist_ok=True)
    (tracker_dir / "data").mkdir(exist_ok=True)

    config_path = tracker_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\nWritten: {config_path}")

    index_path = tracker_dir / "index.html"
    with open(index_path, "w") as f:
        f.write(INDEX_TEMPLATE.format(tracker_name=tracker_name))
    print(f"Written: {index_path}")

    # Summary
    print("\n" + "=" * 60)
    print("  Tracker created successfully!")
    print("=" * 60)
    print(f"\n  Folder:     trackers/{folder_name}/")
    print(f"  Config:     trackers/{folder_name}/config.json")
    print(f"  Explorer:   trackers/{folder_name}/index.html")
    if line_id:
        print(f"  DW line:    https://app.datawrapper.de/chart/{line_id}/visualize")
    if bar_id:
        print(f"  DW bar:     https://app.datawrapper.de/chart/{bar_id}/visualize")
    print(f"\n  Next steps:")
    print(f"  1. git add trackers/{folder_name}/ && git commit -m 'add {folder_name} tracker'")
    print(f"  2. git push")
    print(f"  3. Run the pipeline: python pipeline/smooth.py --config trackers/{folder_name}/config.json --push-to-datawrapper")
    print(f"  4. Style your Datawrapper charts")
    print()

if __name__ == "__main__":
    main()
