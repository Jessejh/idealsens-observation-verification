# data

The campaign this tool was built for.

## `parnu-observations-2026-08-26_27.csv`

637 observations tagged in Pärnu on 26–27 August 2026, exported from the
phone tagging app. This is input data, not code — the pipeline reads it at
runtime from the path in Settings. Nothing from it is compiled into the tool.

### The columns

| Column | What it is |
|---|---|
| `observation_type` | The machine key (`poor_surface`, `difficult_curb`, …). Becomes the observation's category. |
| `label` | The human sentence for the same thing. Becomes the note. |
| `time_local_eest` | Local Estonian time. **Not used** — ranked last, because reading it as UTC would put every frame three hours out. |
| `time_utc` | ISO 8601 with a `Z`. **This is the column the tool uses.** |
| `ts_utc_ms` | Epoch milliseconds. Agrees with `time_utc` to the second on every row. |
| `lat`, `lon` | Phone position at the moment of tagging. |
| `accuracy_m` | Phone GPS accuracy — median 3.3 m, better than the camera's. |
| `session_id` | Shared by every row in one phone session. **Never an observation identifier** — it repeats, so the loader rejects it and uses row numbers. |
| `device_id` | The phone. |

### It verifies its own timezone

The same instant appears three ways, and they agree exactly:

```
time_utc vs ts_utc_ms:       identical
time_utc vs time_local_eest: -3.00 h    (EEST = UTC+3, all 637 rows)
```

So there is nothing to assume about the export's zone. Confirm it yourself:

```bash
python ingest.py timecheck --observations data/parnu-observations-2026-08-26_27.csv
```

### Which footage covers what

Chapters have to overlap one of these windows to match anything. All times UTC.

| Date | From | To | Observations | Session |
|---|---|---|---|---|
| 2026-08-26 | 08:47:53 | 09:07:54 | 20 | `826f022d…` |
| 2026-08-26 | 09:22:57 | 12:01:38 | 171 | `665f30ee…` |
| 2026-08-26 | 12:47:02 | 13:17:51 | 21 | `1b22ec77…` |
| 2026-08-26 | 13:18:27 | 13:34:46 | 17 | *(blank)* |
| 2026-08-26 | 13:36:22 | 15:16:16 | 123 | `93970c1a…` |
| 2026-08-27 | 08:10:57 | 09:46:55 | 95 | `914181dd…` |
| 2026-08-27 | 12:34:04 | 15:13:34 | 190 | `c80873cd…` |

Counts by type: 347 poor_surface, 123 difficult_curb, 68 tree_roots, 57 lamppost, 42 raised_manhole.

Seventeen rows have a blank `session_id`. Harmless: it is never used as an
identity, so nothing depends on it. Every other column is complete on all
637 rows.
