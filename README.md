# benma — Britain Elects New Model Army

Poll trackers: long-format poll data in, smoothed lines and website pages out.

## How data flows

```
parser repo (private)                      this repo
raw poll tables → master archive  ──push──▶ data/<tracker>.csv
                                                │  nightly, 06:00 UTC
                                                ▼
                         pipeline/run_all.py  (smooth.py per tracker, in parallel)
                                                │
                    trackers/<id>/data/*.csv + *.json   ← the published CSVs
                    trackers/index.csv                  ← list of every file, with URLs
                                                │
                         pipeline/export_page.py, articles.py → site/  (GitHub Pages)
```

`data/*.csv` is written by the parser repo's "Publish tracker data to benma" workflow. Don't edit by hand.

## The files you'll want

| What | Where |
|---|---|
| All smoothing settings (bandwidth, min polls, margin of error, local-linear, max gap) | `config/smoothing.json` |
| Every published CSV, listed with links | `trackers/index.csv` |
| Per tracker outputs | `trackers/<id>/data/line[-<subsample>].csv`, `bar[-<subsample>].csv`, `summary.csv`, plus `.json` |
| Tracker definition (parties, colours, output paths) | `trackers/<id>/config.json` |
| Leaders page definition | `pages/leaders.json` |

## Changing smoothing

Edit `config/smoothing.json` and run the **Nightly** workflow (or **Run single tracker**).
Per tracker: `bandwidthDays`, `minPollsInWindow`, `marginOfError`, and `subgroups` overrides keyed by lower-case subsample (`overall`, `65+`). Top-level `defaults` set `localLinear` and `maxGapDays`.
Set `"enabled": false` on a tracker to stop rebuilding it; `"name"` labels it in the index.

## Adding a tracker

1. Parser repo: add it to `master/tracker_rules.json`.
2. Run **Create new tracker** here (folder name + option names). It writes the config and a smoothing entry.
3. Run **Nightly**.

## Running locally

```
pip install requests numpy
python pipeline/run_all.py --only vi_westminster_24      # one or more trackers
python pipeline/index_trackers.py                         # rebuild trackers/index.csv
```

## Folder names

Leadership trackers are `ldr_<surname>_all` (the "all" is historic: it is the folder, not a filter), except `ldr_cameron`, `ldr_corbyn` and `ldr_miliband`.
