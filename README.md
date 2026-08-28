# curbtool

Ingest pipeline for a city curb/pavement review project. We drove a scooter
with a GoPro HERO5 and a phone tagging app around the city, stopping at each
problem spot and tagging it. This tool turns that footage into a reviewable
dataset: evidence frames, playback proxies and rows in Supabase.

The review UI is a **separate Lovable app** against the same Supabase project —
see [`LOVABLE_PROMPT.md`](LOVABLE_PROMPT.md). It cannot live in this repo;
Lovable manages its own.

A prototype for one project. Single shared link, no accounts, permissive RLS.
Ships once, gets thrown away, the next campaign gets a new tool.

---

## Install

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

Python 3.10 or newer. **PyAV, never an ffmpeg binary** — AppLocker group policy
blocks executables on the operator's machine, and PyAV ships FFmpeg as linked
libraries, which is why this works at all. Keep that constraint in mind for
anything you add.

Tkinter is used for the GUI. It ships with python.org builds and with most
Windows Pythons; on Debian/Ubuntu it is `apt install python3-tk`.

## Set up the database

Run [`schema.sql`](schema.sql) then [`spatial.sql`](spatial.sql) in the
Supabase SQL editor. Or move them into `supabase/migrations/` with timestamped
filenames if you want the GitHub integration to deploy them — that works fine
here, and is optional.

`.env` needs the **`service_role`** key. It bypasses RLS. It is gitignored;
keep it that way, and never give it to Lovable — Lovable gets the anon key.

## Verify before batching

Clock offset is the most likely thing to be wrong. Check one file first:

```bash
python ingest.py track /media/gopro/GX010042.MP4
```

That prints the telemetry window, the detected stops and where they are. Then
ingest one file and open one observation in the review UI: the target should
sit near `delta_s = 0`. A systematic error is fixed for the whole campaign with
`--clock-offset`.

```bash
python ingest.py ingest /media/gopro/GX010042.MP4 \
    --campaign helsinki-2024 --observations tags.csv --gnss phone.csv
```

## Run the batch

```bash
python ingest.py ingest /media/gopro --campaign helsinki-2024 \
    --observations tags.csv --gnss phone.csv --save-settings
```

or open the GUI, which does the same thing with a file list, per-file progress
and a Cancel button:

```bash
python ingest.py gui
```

Pass the **same campaign-wide observation CSV to every run**. Each file matches
only what falls inside its own time window.

Re-running a completed file is a no-op. Use `--force` to redo one.

### The summary

Every run ends with a per-file table and, more importantly, this:

```
observations:     312 matched of 340 rows in the CSV
*** 28 observation(s) matched no file. They fell into a chapter gap or a
    GPS dropout, or the clock offset is wrong. ***
```

That last number is the one that matters. Twenty-eight observations vanishing
is invisible unless the tool says so.

---

## How it works

```
GoPro .MP4 ──┬─ GPMF telemetry ── stops ──┐
             │                            ├── matched observations ── frames ──┐
observation CSV ─── UTC timestamps ───────┘                                     │
                                                                                ├─ Supabase
phone GNSS CSV ─── averaged position ───────────────────────────────────────────┤
                                                                                │
             └─ 720p proxy ──────────────────────────────────────────────────────┘
```

| Module | What it does |
|---|---|
| `curbtool/gpmf.py` | GPMF KLV parser, GPS extraction, stop detection, time mapping |
| `curbtool/observations.py` | Observation and phone GNSS CSV loaders |
| `curbtool/media.py` | Frame extraction, proxy transcode, `.LRV` remux |
| `curbtool/supabase_io.py` | PostgREST rows, storage, resumable uploads |
| `curbtool/pipeline.py` | `ingest_file()` — one file, end to end |
| `curbtool/batch.py` | Run a list of files, surviving individual failures |
| `curbtool/gui.py` | Tkinter batch UI |
| `ingest.py` | CLI |

### Decisions worth not re-litigating

**Frames from HD, playback from a proxy.** The campaign is ~80 GB of source.
Uploading that would cost a workday and burn Supabase egress the moment
officials start scrubbing. Instead: high-quality JPEGs cut from the HD locally
(~2 GB), a 720p @ 2.5 Mbit/s proxy for playback (~6 GB), and the HD stays on a
drive.

**Observations are append-only. All human judgement goes in `reviews`.** Never
UPDATE an observation. Corrections, reclassifications and rejections are new
`reviews` rows. That preserves the ability to re-run ingest, audits what the
city changed, and makes reviewer agreement measurable.

**Frame windows follow detected stops, not a fixed ±5 s.** The operator stopped
the scooter and framed each target on the camera screen before tagging, so the
target is visible for the whole stationary period, which `detect_stops()` finds
from GPS speed. `delta_s = 0` is the middle of the stop. Frame count is capped
at `max_frames` so a long stop does not produce hundreds of images. Where a tag
falls outside every stop, the window falls back to ±5 s and `snapped` is false.

**GoPro telemetry for timing, phone GNSS for position.** GoPro telemetry lives
inside the video file, so video↔time↔position has zero clock-sync error. But a
HERO5 is a weak receiver; the phone is multi-constellation with sensor fusion.
Where a phone log exists, its fixes are averaged across the stop and used as the
position, with the GoPro's kept alongside. `gps_disagreement_m` flags poor
reception.

**Session IDs are derived, not generated.** `uuid5(NAMESPACE_URL,
"{campaign}/{filename}/{size}")`, and observation IDs derive from the session.
Re-running upserts the same rows rather than duplicating everything, and
reviews stay attached across a re-ingest. Changing the campaign name changes
every ID, so pick one and keep it.

---

## Settings

Persisted to `~/.curbtool.json`, shared by the CLI and the GUI. Command-line
flags win over the file. `python ingest.py settings` prints the current set;
`--save-settings` on an ingest writes them back.

| Setting | Flag | Default | Notes |
|---|---|---|---|
| `campaign` | `--campaign` | — | Required. Part of the derived session ID. |
| `observations_csv` | `--observations` | — | Campaign-wide, passed to every file. |
| `gnss_csv` | `--gnss` | — | Optional phone GNSS log. |
| `clock_offset_s` | `--clock-offset` | 0 | Added to every observation timestamp. |
| `stop_speed_mps` | `--stop-speed` | 0.7 | At or below this, counted as stopped. |
| `stop_min_duration_s` | `--stop-min-duration` | 3.0 | Shortest run that counts as a stop. |
| `stop_tolerance_s` | — | 2.0 | How far outside a stop a tag may fall and still snap to it. |
| `fallback_window_s` | — | 5.0 | Half-window used when a tag snaps to no stop. |
| `frame_width` | `--frame-width` | 1280 | Extracted frame width. |
| `frame_quality` | — | 88 | JPEG quality for extracted frames. |
| `max_frames` | `--max-frames` | 9 | Cap per observation. |
| `frame_interval_s` | `--frame-interval` | 1.0 | Spacing within a stop window. |
| `proxy_height` | `--proxy-height` | 720 | |
| `proxy_bitrate_kbps` | `--proxy-bitrate` | 2500 | |
| `proxy_source` | `--proxy-source` | `hd` | `hd` transcodes; `lrv`/`auto` remux the companion. |
| `work_dir` | `--work-dir` | `work` | Frames, proxies, summaries. Gitignored. |
| `upload` | `--no-upload` | on | Off processes locally without touching Supabase. |

Rows with no flag are edited in `~/.curbtool.json` (or the GUI, where they
appear in the settings panel) rather than on the command line.

### CSV columns

Both loaders detect columns from aliases, so most exports work untouched:
timestamps (`utc`, `timestamp`, `time`, `aikaleima`…), coordinates (`lat`,
`latitude`, `y`…), category, note and id. Semicolon delimiters and decimal
commas are handled. Timestamps may be ISO 8601 (with or without a zone) or
epoch seconds/milliseconds; **a timestamp with no zone is read as UTC**, so if
the tagging app exported local time the whole campaign is off by a constant —
which is what `--clock-offset` fixes, and what the summary's hint will point at.

---

## Tests

```bash
python run_tests.py
```

98 core tests plus 17 GUI tests. The GPMF parser runs against synthetic KLV
built to the camera's own layout; the pipeline's end-to-end tests do real
decoding, real transcoding and real HTTP against an in-process Supabase,
including a dropped connection mid-upload and a restart; the GUI tests drive
the real widget tree, a full batch, a cancel and a corrupt file.

The GUI tests get their own process — Tk keeps process-global state and aborts
if any of it is finalised on a worker thread, which the threaded tests would
otherwise trigger. `run_tests.py` handles that. On a headless machine use
`xvfb-run -a python run_tests.py`.

**Not yet verified against a real GoPro file.** Everything below the payload
level is exercised against synthetic KLV written to the documented layout, and
the media paths run against generated clips, but no HERO5 footage has been
through this. That is the first thing to do — see *Verify before batching*.

---

## Gotchas

**GoPro GPS cold start.** Until the camera gets a fix it falls back to its
internal RTC, which can be minutes off. The parser drops samples below fix
quality 2, so a bad section produces unmatched observations rather than
silently wrong ones. Watch the unmatched count.

**HERO5 chapters at 4 GB**, roughly 19 minutes at 1080p30, so expect ~17 files
per campaign. Each becomes its own session row.

**Proxy transcoding is the slow part**, several minutes per chapter. Where a
`.LRV` companion is good enough, `--proxy-source lrv` stream-copies it in
seconds instead.

**Interrupting is safe.** Ctrl-C (or Cancel in the GUI) stops after the current
step. The session row is only marked `complete` once everything has landed, so
an interrupted file is picked up again on the next run, and a part-uploaded
proxy resumes rather than restarting.

---

## Out of scope

Auth and user accounts. Real RLS policies. Automatic severity classification —
severity is the city's judgement, and the whole point of the review step is
that they make it. Any attempt to generalise this beyond the current project.
