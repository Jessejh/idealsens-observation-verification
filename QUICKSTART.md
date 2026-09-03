# Quickstart — cut frames from one video and see if they match

Goal: half an hour, one chapter, no database, and an answer to the only
question that matters yet — **does the frame show the thing the operator
tagged?**

Nothing here uploads anything or writes to Supabase.

---

## 1. Install

Python 3.10 or newer, from python.org.

```
py -m pip install --user -r requirements.txt
py run_tests.py
```

You want `core: PASS`. If `gui: SKIPPED`, Tkinter is missing — it doesn't
matter, the browser UI doesn't use it.

> On a machine with AppLocker, don't make a virtual environment first: it puts
> a `python.exe` inside your profile and the policy blocks running it. The
> `--user` install above sidesteps that.

## 2. Copy some footage off the card

One or two chapters is plenty. Put them in a folder of their own, keeping any
`.LRV` companions beside them.

**Pick chapters that overlap the drive.** The observations cover these windows
(UTC — subtract nothing, add 3 h for Estonian local time):

| Date | UTC window | Observations |
|---|---|---|
| 2026-08-26 | 08:47 – 09:08 | 20 |
| 2026-08-26 | 09:23 – 12:02 | 171 |
| 2026-08-26 | 12:47 – 13:18 | 21 |
| 2026-08-26 | 13:18 – 13:35 | 17 |
| 2026-08-26 | 13:36 – 15:16 | 123 |
| 2026-08-27 | 08:11 – 09:47 | 95 |
| 2026-08-27 | 12:34 – 15:14 | 190 |

The 09:23–12:02 block on the 26th is the densest — start there.

## 3. Do the clocks agree?

```
py ingest.py timecheck D:\footage ^
   --observations data\parnu-observations-2026-08-26_27.csv
```

Reads telemetry only. Decodes nothing, writes nothing, takes seconds.

What you're looking for:

- **PLACE** — "agree on place to within N m". Kilometres means wrong CSV for
  this footage.
- **camera TZ** — probably `UTC+3`. That's your camera's timezone setting
  showing up in the container stamp. Expected, harmless, ignored by the tool.
- **VERDICT** — "no whole-hour shift does better" is the pass.

## 4. Will everything match?

```
py ingest.py check D:\footage ^
   --observations data\parnu-observations-2026-08-26_27.csv
```

Per chapter: its UTC window, how many observations fall in it, and what
percentage landed during a detected stop. A low stop percentage means stop
detection needs tuning before the frames will be any good.

## 5. Cut the frames

```
py ingest.py ingest D:\footage\GX010042.MP4 ^
   --campaign parnu-2026 ^
   --observations data\parnu-observations-2026-08-26_27.csv ^
   --gnss data\parnu-observations-2026-08-26_27.csv ^
   --no-upload
```

Video is off by default, so this only cuts the stills — about a minute a
chapter. `--gnss` with the same file is deliberate: it carries a timestamp, coordinates
and an accuracy per row, which is exactly a phone position log — and at a
median 3.3 m it beats the camera's own fix.

## 6. Look at the pictures

```
explorer work\parnu-2026\GX010042\frames
```

One folder per observation. The middle frame of each set is the middle of the
stop, which is where the operator was framing the target.

**This is the whole point.** Open a dozen. If they show broken kerbs, potholes
and lampposts in the way, the pipeline works and you can run the rest. If they
show the road ahead, stop and tune:

| What you see | Try |
|---|---|
| Frames slightly early or late | `--clock-offset 2` (seconds, ± ) |
| Frames while still moving | `--stop-speed 1.2` (raise it — GPS is noisy at rest) |
| Stops being missed entirely | `--stop-min-duration 2` (lower it) |
| Too few frames per spot | `--max-frames 15` |
| Want them sharper | `--frame-width 1920` |
| Curious where the time goes | `py tools\bench.py D:\footage\GX010042.MP4` |

Re-run the same command with `--force` after each change.

---

## Prefer buttons?

```
py ingest.py web
```

Opens a page in your browser with Check and Start as buttons, a settings panel,
a live log, and any failure shown as a numbered code with what it means and
what to do. Nothing leaves your machine.

## When something fails

Errors carry a code — `E102`, `E201`, `E303`. Each says what it means and what
to do next. The ranges are in README.md; the full table is in
`curbtool/errors.py`.

## If you do want video later

```
py ingest.py backfill D:\footage --campaign parnu-2026 ^
   --observations data\parnu-observations-2026-08-26_27.csv ^
   --proxy-source lrv
```

`lrv` uses the copy the camera already wrote — seconds a chapter. It reuses the
frames on disk and leaves grading untouched. Only reach for `--proxy-source hd`
if a reviewer says the low-resolution version is genuinely too coarse; that is
10–20 minutes a chapter. Run `tools/bench.py --proxy` first to see what it
would actually cost on your machine.

## Not yet

Supabase, video, the review UI. None of it matters until step 6 looks right.
