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

## First: do the clocks agree?

Run this whenever the timestamps come from a new app or a new camera. It reads
telemetry and the CSV, decodes nothing and writes nothing.

```bash
python ingest.py timecheck /media/gopro --observations tags.csv
```

It answers the question from both sides.

**The camera cannot have the wrong timezone.** GoPro telemetry takes its time
from the GPS satellites, so GPSU (GPS5) and the per-sample stamps in GPS9 are
UTC no matter what the camera's clock or timezone is set to. The trap is the
MP4 container's `creation_time`, which GoPro writes in the camera's *local*
time while labelling it with a "Z". `timecheck` shows both and reports the gap
— that gap is the camera's timezone setting, it is expected to be non-zero, and
the pipeline never reads that field.

**The export is checked against itself.** Where it states the same instant more
than once — local, UTC, epoch — the offsets between those columns prove the
zone from the file, with nothing assumed. The column actually used is scored
and named, and a column that looks like local wall-clock time is ranked last.

Then it counts how many observations land inside the footage as things stand,
and whether any whole-hour shift would do materially better. If one would, that
is local time being read as UTC. It also compares where the export says it was
against where the camera was: kilometres apart means the wrong CSV is paired
with the wrong footage.

If the export only gives local time, name its zone rather than computing an
offset by hand — a named zone gets summer time right, a fixed offset cannot:

```bash
python ingest.py check /media/gopro --observations tags.csv \
    --timezone Europe/Tallinn
```

On Windows this needs the `tzdata` package (`pip install tzdata`); the tool
says so if it is missing.

## Check before you ingest

`check` matches the whole campaign against every chapter without decoding a
single frame or writing anything. It takes about as long as reading the files
off the drive, and it is the only cheap way to discover that the clock offset
is wrong.

```bash
python ingest.py check /media/gopro --observations tags.csv
```

It answers three questions: does every observation land inside some chapter,
does it land during a stop where the target is actually framed, and if not
would a different `--clock-offset` fix it. It only suggests a shift when most
of the campaign is unmatched — one stray tag four hours out is a stray tag, not
a broken clock, and shifting the campaign to accommodate it would break
everything that currently works.

Fix whatever it reports, then run it again until it says `READY`.

## Then prove it on one file

Numbers matching is not the same as frames showing the problem. Ingest one
file with no video and no upload, and **look at the JPEGs**:

```bash
python ingest.py ingest /media/gopro/GX010042.MP4 --campaign helsinki-2024 \
    --observations tags.csv --no-proxy --no-upload
open work/helsinki-2024/GX010042/frames/
```

The frame nearest `delta_s = 0` should show the curb or the pothole the
operator was pointing at. If it shows the road ahead, or the operator's shoe,
stop and tune before doing this seventeen times.

## Run the batch

```bash
python ingest.py ingest /media/gopro --campaign helsinki-2024 \
    --observations tags.csv --gnss phone.csv --save-settings
```

or open the UI, which does the same thing with buttons:

```bash
python ingest.py web      # browser UI (recommended)
python ingest.py gui      # desktop window, needs Tkinter
```

`web` starts a small server on this machine and opens a page in your browser.
**Check** runs the dry run, **Start** runs the ingest, **Cancel** stops between
files, and any failure appears as a numbered code with what it means and what
to do about it. Nothing leaves the machine: the server binds to 127.0.0.1, every
request must carry a token minted at startup, non-loopback Host headers are
refused, and the `service_role` key is never sent to the page.

Pass the **same campaign-wide observation CSV to every run**. Each file matches
only what falls inside its own time window.

Re-running a completed file is a no-op. Use `--force` to redo one.

**Consider `--no-proxy` for the first full pass.** Transcoding is the most
expensive thing this pipeline does — tens of minutes and several hundred
megabytes per chapter — and nobody knows yet whether reviewers need video at
all. Frames-only gets the city grading the same day. Add video later, without
redoing anything and without disturbing grading already done:

```bash
python ingest.py backfill /media/gopro --campaign helsinki-2024 \
    --observations tags.csv --proxy-source lrv
```

`backfill` reuses the frames already in the work folder, builds the proxies and
fills in each session's `proxy_url`. Derived IDs mean observations are updated
rather than replaced, so reviews stay attached.

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
| `curbtool/verify.py` | `check` — dry-run the matching, decode nothing |
| `curbtool/timecheck.py` | `timecheck` — prove the clocks and places agree |
| `curbtool/batch.py` | Run a list of files, surviving individual failures |
| `curbtool/gui.py` | Tkinter batch UI |
| `curbtool/webui.py` | Local server behind the browser UI |
| `curbtool/web/index.html` | The browser UI itself |
| `curbtool/errors.py` | Error codes: what failed, what it means, what to do |
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
| `timezone` | `--timezone` | — | IANA zone for timestamps with no zone of their own. |
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
| `proxy_source` | `--proxy-source`, `--no-proxy` | `hd` | `none` skips video entirely; `hd` transcodes; `lrv`/`auto` remux the `.LRV` companion. |
| `work_dir` | `--work-dir` | `work` | Frames, proxies, summaries. Gitignored. |
| `upload` | `--no-upload` | on | Off processes locally without touching Supabase. |

Rows with no flag are edited in `~/.curbtool.json` (or the GUI, where they
appear in the settings panel) rather than on the command line.

### CSV columns

An export that carries a per-observation id is used as-is. One that carries
only a **session id** shared across rows is not: an identifier that repeats is
not an identifier, and observation IDs derive from it, so the tool falls back to
row numbers and says so. Getting this wrong merges hundreds of observations onto
one database row.

Where several time columns exist they are scored, the winner is named, and a
column that looks like local wall-clock time is ranked last. `observation_type`
becomes the category and `label` the note, so filtering works on the stable key
rather than a sentence.

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

184 tests: the pipeline, the export loader, the timezone audit, the web UI's
HTTP surface and guards, and the desktop window. The GPMF parser runs against synthetic KLV
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

## Error codes

Every failure the operator can see has a stable code, a plain-language meaning
and a next action — `E102` beats "telemetry track has no GPS fixes at quality
>= 2" when you are holding a camera card. The UI shows them in full; the codes
are defined in `curbtool/errors.py`.

| Range | Area |
|---|---|
| `E0xx` | Setup and configuration |
| `E1xx` | Telemetry — the GoPro's own GPS track |
| `E2xx` | Inputs — the observation CSV and phone log |
| `E3xx` | Media — decoding, frames, proxies |
| `E4xx` | Supabase — storage and database |
| `E9xx` | Unclassified; the raw error is always kept |

Codes are stable once published: one may end up in an email to whoever picks
this up next.

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
