# config/smoothing.json

Every smoothing setting lives here, one entry per tracker (`trackers/<id>`):

| key | meaning |
|---|---|
| `bandwidthDays` | smoothing window (days). Longer = smoother, more lag |
| `minPollsInWindow` | polls needed in the window before a point is drawn |
| `marginOfError` | extra noise (points) added in the bootstrap band |
| `subgroups` | per-subgroup overrides keyed by lowercase value (`overall`, `65+`, `scotland`...). Unlisted subgroups inherit the tracker-wide value |
| `localLinear` | sloped local fit; removes lag at the latest edge (default on) |
| `maxGapDays` | draw nothing where the polls either side of a date are further apart than this (default 45) |

`defaults` apply to every tracker; any tracker entry may override any key (e.g. `"localLinear": false`).
Tracker `config.json` files no longer carry bandwidth/min-poll values.
