# LOVABLE_PROMPT.md — the review UI

Staged prompts for building the review app in Lovable. It reads the same
Supabase project this pipeline writes to.

**Give Lovable the anon key, never the `service_role` key.** The service key
bypasses RLS entirely and belongs only on the operator's machine.

Build it in the order below. Each stage is a separate prompt: paste one, get it
working, then paste the next. Trying to describe the whole app in one prompt
produces something that looks right and works nowhere.

---

## What you are building, in one paragraph

City officials review a dataset of curb and pavement problems collected by
driving a scooter with a GoPro and a phone tagging app around the city. For
each observation they assign a severity of 1–3, correct anything mis-tagged,
reject false positives, and add spots the drive missed. It is a prototype for
one project: a single shared link, no accounts, and it gets thrown away when
the project ends.

---

## The data

Read these. Do not create tables — the pipeline owns the schema.

| Table / view | What it is |
|---|---|
| `v_reviewed` | **Start here.** One row per observation with its latest review already joined: `severity`, `review_status`, `final_category`, `is_reviewed`, `frame_count`, `cover_frame_url`, plus position and video timing. |
| `observations` | Raw observations. Append-only. |
| `frames` | Evidence stills per observation. `delta_s` is seconds from the middle of the stop; `public_url` is directly usable in an `<img>`. |
| `sessions` | One row per video chapter, with `proxy_url` for playback and `duration_s`. |
| `reviews` | Every piece of human judgement. **Insert only.** |
| `track_points` | The driven route. |

Helper functions, callable via `supabase.rpc(...)`:

- `route_geojson(in_campaign text)` — the driven route as a FeatureCollection.
- `observations_geojson(in_campaign text)` — observations as points, carrying severity.
- `nearest_footage(in_lat, in_lon, in_radius_m, in_limit, in_campaign)` — the
  closest places the scooter passed a point, each with a `session_id` and an
  `offset_s`. This is how a reviewer adds an observation the drive missed.

### The one rule that matters

**Never UPDATE an observation. Never DELETE one.** Every correction, severity,
rejection and reclassification is an INSERT into `reviews`. Changing a
category means inserting a row with `status = 'reclassified'` and
`corrected_category` set. This is what lets the pipeline re-run without
destroying the city's work, and what lets us audit what changed.

`v_reviewed` always shows the most recent review, so the UI can read it as if
observations were editable while the history stays intact.

---

## Stage 1 — the list

> Build a single-page app on Supabase (project URL and anon key in env vars)
> that lists rows from the view `v_reviewed`, newest `observed_utc` first.
>
> Show a table with: the `cover_frame_url` as a small thumbnail, `final_category`,
> `observed_utc`, `filename`, `severity` (or "ungraded"), and `review_status`.
>
> Above the table show three counts: total observations, how many have a
> severity, and how many are still ungraded.
>
> Add filters: campaign, category, ungraded-only, and a severity picker.
> Persist filters in the URL query string so a reviewer can share a link to
> exactly what they are looking at.
>
> No login. No sign-up screen. Anyone with the link can use it.

Check before moving on: the counts are right, and the ungraded filter is the
default view.

## Stage 2 — the detail panel and the frame scrubber

> Clicking a row opens a detail panel.
>
> Load that observation's rows from `frames`, ordered by `delta_s`. Show the
> frame with `delta_s` closest to zero first — that is the middle of the stop,
> where the operator was framing the target.
>
> Put a slider under the image spanning the available frames, labelled in
> seconds from the middle of the stop ("−2.0 s", "0 s", "+2.0 s"). Left and
> right arrow keys step frames. Preload the neighbouring images so scrubbing
> does not flicker.
>
> Beside the image show: tagged category, the operator's note, `observed_utc`,
> the source filename with `video_offset_s`, and the number of frames.
>
> If `gps_disagreement_m` is above 25, show a small warning that the phone and
> the camera disagree about the position by that many metres, so the map pin
> may be off.

## Stage 3 — grading

> Add a grading panel to the detail view.
>
> Severity is three large buttons: 1, 2, 3. Below them a status select
> (confirmed, reclassified, rejected, duplicate, needs revisit), an optional
> corrected category, an optional note, and a reviewer name that is remembered
> in localStorage between sessions.
>
> Saving INSERTs a row into `reviews`. It must never UPDATE or DELETE an
> observation — corrections are new review rows. After saving, refresh that row
> from `v_reviewed`.
>
> Keyboard: 1, 2 and 3 set severity, R marks rejected, and Enter saves and
> moves to the next ungraded observation. Grading several hundred observations
> by mouse alone is the difference between an afternoon and a week.
>
> Show the previous reviews for an observation underneath, oldest first, so a
> reviewer can see that someone already looked at it and what they said.

## Stage 4 — the map

> Add a map view alongside the list.
>
> Draw the driven route from `route_geojson(campaign)` as a thin line, and
> observations from `observations_geojson(campaign)` as pins coloured by
> severity: grey for ungraded, then green, amber, red for 1, 2, 3. Rejected
> observations get a hollow pin.
>
> Clicking a pin opens the same detail panel as the list. Selecting a row in
> the list pans the map to it. Keep list and map in sync in both directions.

## Stage 5 — video playback

> In the detail panel add a "Play video" tab that plays `sessions.proxy_url`
> for that observation's session, seeking to `video_offset_s` on open.
>
> The proxy is a downscaled copy — the HD footage stays on a drive and is
> never uploaded. Do not try to load anything else.
>
> Add buttons to jump to the start and end of the stop (`stop_start_s`,
> `stop_end_s`).

## Stage 6 — adding what the drive missed

> Add an "Add observation" mode. The reviewer clicks a point on the map, and
> the app calls `nearest_footage(lat, lon, 40, 5, campaign)` to find where the
> scooter passed nearby.
>
> Show the candidates with their distance in metres, and let the reviewer pick
> one to preview the video at that offset.
>
> On save, INSERT into `observations` with `source = 'review'`, the chosen
> position, and the picked `session_id` and `video_offset_s`. Then INSERT the
> grading as a normal row in `reviews`.
>
> `source = 'review'` is required — RLS rejects the insert without it, and it
> is what keeps reviewer-added observations distinct from the pipeline's own.

## Stage 7 — export

> Add an Export button that downloads `v_reviewed` for the current campaign as
> CSV, respecting the active filters, with a filename that includes the
> campaign and today's date.
>
> Include the columns an engineer needs to act on a finding: id, category,
> final category, severity, review status, reviewer, note, lat, lon,
> observed_utc, filename, video_offset_s, and cover_frame_url.

---

## Things to get right

**Ungraded is the default view.** The job is to grade everything; the app
should open on what is left to do, and the count should visibly fall.

**Do not paginate at 10 rows.** Several hundred observations is the whole
dataset. Virtualise a long list rather than making reviewers click through
thirty pages.

**Frames come from storage URLs, not the database.** `public_url` is directly
usable. Do not try to download and re-host them.

**Severity is human judgement.** Do not add a model, a heuristic, or a
"suggested severity". The entire point of the review step is that the city
decides.

**No auth.** No accounts, no roles, no invitations. One shared link. If you
find yourself building a login screen, stop.
