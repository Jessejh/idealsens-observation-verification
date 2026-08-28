# HANDOFF — curb/pavement review tool

Context document for continuing this project in Claude Code. Written after a
design session that produced the working pipeline in this repo.

---

## 1. What this is

A one-off tool for a city project. We drove a scooter with a GoPro HERO5 and a
phone-based tagging app around the city, stopping at each problem spot (bad
curb, damaged pavement, obstacles) and tagging it. The city now needs to review
that dataset, assign a severity of 1–3 to each observation, correct anything
mis-tagged, and add observations we missed.

Two halves:

- **This repo — the ingest pipeline.** Python, runs locally on the operator's
  machine. Reads GoPro footage, extracts evidence frames, builds playback
  proxies, pushes everything to Supabase.
- **A separate Lovable app — the review UI.** Maps, frame scrubber, grading
  panel. Reads from the same Supabase project. Spec in `LOVABLE_PROMPT.md`.

Explicitly a prototype. Not production. Single shared link, no user accounts,
permissive RLS. Ships once, gets thrown away, next project gets a new tool.

## 2. Repo layout question

**Pipeline and database migrations go in this repo together.** The pipeline
writes directly to these tables — a schema change that doesn't reach the
pipeline breaks ingest silently. Version them as one unit.

If you want Supabase's GitHub integration to auto-deploy schema changes, move
the SQL into `supabase/migrations/` with timestamped filenames. That works
fine in this repo. It's optional; running the SQL by hand in the dashboard is
perfectly adequate for a prototype.

**The Lovable frontend cannot live here.** Lovable manages its own GitHub repo
and pushes to it on its own schedule. Trying to merge them will cause pain.
Two repos, one Supabase project.

Suggested target layout:

```
.
├── curbtool/
│   ├── __init__.py
│   ├── gpmf.py          # GPMF/KLV parser + stop detection  (done, tested)
│   ├── pipeline.py      # extracted core logic              (TO BUILD)
│   ├── supabase_io.py   # REST + storage client             (TO BUILD)
│   └── gui.py           # Tkinter batch UI                  (TO BUILD)
├── ingest.py            # thin CLI wrapper over pipeline
├── supabase/migrations/ # or just schema.sql + spatial.sql at root
├── LOVABLE_PROMPT.md
├── README.md
└── .env.example
```

`.gitignore` must cover `.env` and `work/`. The `service_role` key must never
be committed — it bypasses RLS entirely.

## 3. Current state

`gpmf.py` — self-contained GPMF KLV parser. Extracts GPS5/GPS9 with SCAL
scaling, GPSU UTC stamps, fix quality and DOP. Also `detect_stops()`,
`stop_for_offset()`, `utc_to_offset()`, `offset_to_latlon()`. Unit-tested
against synthetic KLV payloads and a synthetic stop track. **Not yet tested
against a real GoPro file** — that's the first thing to verify.

`ingest.py` — working CLI with `ingest`, `backfill` and `track` subcommands.
Monolithic; needs decomposing before a GUI can drive it.

`schema.sql` / `spatial.sql` — tables, the `v_reviewed` export view, PostGIS
geography columns, and the `nearest_footage()` / `route_geojson()` functions.

`LOVABLE_PROMPT.md` — staged prompts for the review UI.

## 4. Design decisions worth not re-litigating

**Frames come from the HD file, video playback comes from a downscaled proxy.**
The campaign is ~80 GB of source. Uploading that would cost a workday and blow
through Supabase egress once officials start scrubbing. Instead: extract
high-quality JPEGs from HD locally (~2 GB total), transcode a 720p @ 2.5 Mbit/s
proxy for playback (~6 GB total), keep the HD on a drive.

**Observations are append-only. All human judgement goes in `reviews`.**
Never UPDATE an observation. Corrections, reclassifications and rejections are
new `reviews` rows. This preserves the ability to re-run ingest, audit what the
city changed, and measure reviewer agreement.

**Frame windows follow detected stops, not a fixed ±5 s.** The operator
stopped the scooter and framed each target on the camera screen before tagging.
So the target is visible for the whole stationary period, which `detect_stops()`
finds from GPS speed. `delta_s = 0` means the middle of the stop. Frame count
is capped at `MAX_FRAMES` so a long stop doesn't produce hundreds of images.

**GoPro telemetry for timing, phone GNSS for position.** GoPro telemetry lives
inside the video file, so the video↔time↔position mapping has zero clock-sync
error. But a HERO5 is a weak receiver. The phone is multi-constellation with
sensor fusion, so where a phone log exists we average its fixes across the stop
window and use that position instead, keeping the GoPro position alongside.
`gps_disagreement_m` between the two flags poor reception.

## 5. What to build

### 5a. Refactor first

Extract the core into `curbtool/pipeline.py` with a progress callback, so the
CLI and the GUI share one implementation:

```python
@dataclass
class Progress:
    file: str
    stage: str        # track | match | stops | frames | proxy | upload | rows
    current: int
    total: int
    message: str = ""

def ingest_file(job: IngestJob, on_progress: Callable[[Progress], None],
                should_cancel: Callable[[], bool]) -> IngestResult: ...
```

Every long loop must call `on_progress` and check `should_cancel`. Frame
extraction and transcoding are the two slow stages and need real per-item
progress, not a spinner.

### 5b. Deterministic session IDs

Current behaviour: re-running a file without `--session-id` generates a fresh
UUID and duplicates everything. Unacceptable for a batch GUI where a partial
run gets retried.

Derive the session ID as `uuid5(NAMESPACE_URL, f"{campaign}/{filename}/{size}")`.
Re-running a file then upserts the session, and ingest becomes idempotent:
delete the session's child rows and re-insert, or skip if already complete.

Add a resume check: before processing, query whether that session already has
observations and frames, and skip unless `--force`.

### 5c. Tkinter batch GUI

Tkinter specifically, not PySide or Electron. It's in the standard library, so
there's no installer and no executable — this machine has AppLocker group
policy restrictions that already forced the pipeline off ffmpeg binaries and
onto PyAV. Keep the same constraint in mind for anything you add.

Layout:

- **File list.** Multi-select file dialog, or drag-and-drop a folder. One row
  per video showing filename, duration, status (queued / running / done /
  failed / skipped), and a per-file progress bar. Auto-detect the matching
  `.LRV` and show whether one was found.
- **Settings panel.** Observation CSV, phone GNSS CSV (optional), campaign
  name, clock offset, stop speed threshold, minimum stop duration, frame
  width, proxy bitrate, and an upload on/off toggle. Persist these to
  `~/.curbtool.json` so they survive restarts — retyping paths for 17 files is
  the fastest way to make the tool annoying.
- **Log pane.** Timestamped lines, same content the CLI prints to stderr.
- **Controls.** Start, Cancel, and Open work folder. Cancel must actually stop
  between items, not just set a flag nobody reads.

Threading: work runs in a `threading.Thread`, progress arrives on the main
thread via `queue.Queue` polled with `root.after(100, ...)`. Never touch
Tkinter widgets from the worker thread. Encoding is CPU-bound and holds the
GIL in PyAV, so process one file at a time; parallelism here buys little and
costs a lot of complexity.

Failures must not abort the batch. Catch per file, mark it failed, keep going,
and show a summary at the end.

*(You may already have a frame-extraction GUI in the idealsens work — if its
threading and progress patterns are sound, reuse them rather than inventing
new ones.)*

### 5d. Resumable uploads

Proxies will be several hundred MB each. The current code does a single
streaming POST, so a dropped connection restarts the whole file. Switch the
proxy upload to Supabase's TUS resumable endpoint at
`/storage/v1/upload/resumable`, chunked at 6 MB, with progress reported per
chunk. Frames are small enough to keep on the standard endpoint.

### 5e. A batch summary worth reading

At the end, print and save: per file — observations matched, observations out
of range, stop snap ratio, frames extracted, proxy size. Across the batch —
total matched versus rows in the observation CSV.

**That last number is the important one.** If the CSV has 340 rows and only
312 matched across all files, 28 observations vanished into chapter gaps or
GPS dropouts, and nobody will notice unless the tool says so.

## 6. Gotchas

**Clock offset is the most likely thing to be wrong.** Run one file, open one
observation, check the target sits near `delta_s = 0`. A systematic error is
fixed for the whole campaign with `--clock-offset`. Verify before batching 17
files.

**GoPro GPS cold start.** Until the camera gets a fix it falls back to its
internal RTC, which can be minutes off. The parser drops samples with
`fix < 2`, so a bad section produces unmatched observations rather than
silently wrong ones. Surface the unmatched count prominently.

**HERO5 chapters at 4 GB**, roughly 19 minutes at 1080p30, so expect ~17 files
for this campaign. Each becomes its own session row. Pass the same
campaign-wide observation CSV to every run; each file matches only what falls
inside its own time window.

**PyAV, never an ffmpeg binary.** AppLocker blocks executables on this machine.
PyAV ships FFmpeg as linked libraries, which is why it works.

**`add_stream(template=...)`** for stream-copy remuxing needs a reasonably
modern PyAV. Pin it in `requirements.txt` and note the version that worked.

## 7. Done when

- One command from the GUI processes a folder of GoPro files end to end.
- Re-running a completed file is a no-op unless forced.
- Cancelling mid-batch stops cleanly and leaves the database consistent.
- A dropped connection during a proxy upload resumes rather than restarting.
- The summary reports total matched versus total CSV rows.

## 8. Out of scope

Auth and user accounts. Real RLS policies. Automatic severity classification —
severity is the city's judgement and the whole point of the review step is that
they make it. Any attempt to generalise this beyond the current project; the
next campaign gets a new tool.
