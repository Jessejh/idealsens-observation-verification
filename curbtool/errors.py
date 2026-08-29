"""Stable error codes for everything the pipeline can fail at.

Every failure the operator can see gets a code, a plain-language meaning and a
concrete next action. The point is not tidiness: it is that "GX010045.MP4:
telemetry track has no GPS fixes at quality >= 2" means nothing to someone
holding a camera card, whereas "E102 — the camera never got a satellite lock
for this chapter; expected near the start of a drive" tells them whether to
worry.

Codes are stable. Once published, a code keeps its meaning — it may end up in
an email to whoever comes after this project.

    E0xx  setup and configuration
    E1xx  telemetry (the GoPro's own GPS track)
    E2xx  inputs (the observation CSV, the phone log)
    E3xx  media (decoding, frames, proxies)
    E4xx  Supabase (storage and database)
    E9xx  anything unclassified
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    code: str
    title: str
    meaning: str
    fix: str
    fatal: bool = True      # False = the batch can carry on past it


def _c(code: str, title: str, meaning: str, fix: str, fatal: bool = True) -> ErrorCode:
    return ErrorCode(code, title, meaning, fix, fatal)


CODES: dict[str, ErrorCode] = {code.code: code for code in [
    # -- setup ------------------------------------------------------------
    _c("E001", "No campaign name",
       "The campaign name is part of every identifier written to the database.",
       "Type a campaign name in Settings. Pick one and never change it — changing "
       "it later creates a second, parallel copy of the whole campaign."),
    _c("E002", "Supabase is not configured",
       "Uploading is switched on, but no project URL and service_role key were found.",
       "Fill in SUPABASE_URL and SUPABASE_SERVICE_KEY in the .env file next to "
       "ingest.py, or switch off \"Upload to Supabase\" and work locally."),
    _c("E003", "No video files",
       "The folder holds no .MP4 files.",
       "Check the path. GoPro chapters are named GX01nnnn.MP4; the .LRV files "
       "beside them are companions, not chapters."),
    _c("E004", "Cannot write to the work folder",
       "Frames and proxies could not be written to disk.",
       "Check the work folder path is somewhere you can write, and that the drive "
       "is not full. A campaign needs roughly 3 GB for frames alone."),

    # -- telemetry --------------------------------------------------------
    _c("E101", "No telemetry track in this file",
       "The file has no GPMF metadata track, so there is no way to know when or "
       "where any of it was recorded.",
       "Confirm this is original camera footage. Anything re-encoded by an editor "
       "or a phone app has had the telemetry stripped out.",
       fatal=False),
    _c("E102", "The camera never got a satellite lock",
       "Every GPS sample in this chapter is below usable fix quality, so its "
       "timestamps come from the camera's internal clock and can be minutes wrong.",
       "Normal for the first chapter of a drive, while the receiver is still "
       "acquiring. The chapter is skipped rather than matched wrongly; tags "
       "inside it will show as unmatched.",
       fatal=False),
    _c("E110", "Nothing matched — the clock offset looks wrong",
       "The observations do not fall inside any chapter's time window, and "
       "shifting them by a whole number of hours would fix most of them.",
       "Apply the suggested clock offset in Settings and check again. The tagging "
       "app most likely exported local time rather than UTC."),
    _c("E111", "Some observations matched no chapter",
       "These tags fall outside every chapter's time window.",
       "They fell into a gap between recordings, or into a stretch where the "
       "camera had no satellite lock. Open them in the CSV and decide whether "
       "they are real losses.",
       fatal=False),

    # -- inputs -----------------------------------------------------------
    _c("E201", "The observation CSV could not be read",
       "The file is missing, empty, or not a CSV.",
       "Check the path in Settings points at the export from the tagging app."),
    _c("E202", "No timestamp column in the observation CSV",
       "Without timestamps there is no way to match a tag to a moment of footage.",
       "Open the CSV and check it has a time column. Recognised names include "
       "utc, timestamp, time, datetime and aikaleima."),
    _c("E203", "No usable rows in the observation CSV",
       "A timestamp column was found, but no row's timestamp could be parsed.",
       "Check the timestamp format. ISO 8601 (2024-06-01T08:30:15Z) and epoch "
       "seconds both work."),
    _c("E204", "The phone GNSS log could not be read",
       "The optional phone position log is missing a timestamp or coordinates.",
       "Either fix the columns or clear the field — the phone log is optional, "
       "and without it the camera's own positions are used."),

    # -- media ------------------------------------------------------------
    _c("E301", "The video file could not be opened",
       "The container is unreadable — usually a truncated or corrupted file.",
       "Check the file copied off the card completely. Compare its size against "
       "the card.",
       fatal=False),
    _c("E302", "Frame extraction failed",
       "The file opened but decoding stopped part way through.",
       "Usually damage partway into the file. Try playing it; if a player also "
       "stops at the same point, the source is damaged.",
       fatal=False),
    _c("E303", "Proxy transcode failed",
       "Re-encoding the playback copy did not finish.",
       "Check for free disk space first. Frames are unaffected — you can run with "
       "video switched off and add it later.",
       fatal=False),
    _c("E304", "The .LRV companion could not be remuxed",
       "The camera's own low-resolution copy could not be repackaged.",
       "Set \"Proxy from\" to hd to transcode from the full file instead.",
       fatal=False),

    # -- Supabase ---------------------------------------------------------
    _c("E401", "Supabase rejected the credentials",
       "The project URL or the service_role key is wrong.",
       "Copy both again from the Supabase dashboard under Settings → API. The key "
       "needed is service_role, not anon."),
    _c("E402", "Could not reach Supabase",
       "The request did not complete after several retries.",
       "Check the network. Uploads resume from where they stopped, so nothing is "
       "lost by trying again later."),
    _c("E403", "Supabase rejected a row",
       "The database refused what the pipeline wrote, usually a schema mismatch.",
       "Run schema.sql and spatial.sql in the SQL editor. This is what happens "
       "when the tables are older than the pipeline."),
    _c("E404", "A file upload failed",
       "Frames or a proxy could not be stored.",
       "Check the frames and proxies buckets exist and the project is not out of "
       "storage quota.",
       fatal=False),

    # -- catch-all --------------------------------------------------------
    _c("E900", "Unexpected failure",
       "Something failed that the tool does not have a specific code for.",
       "The detail line below is the raw error. Keep it — it is what makes the "
       "cause findable.",
       fatal=False),
]}


def get(code: str) -> ErrorCode:
    return CODES.get(code, CODES["E900"])


def classify(exc: BaseException) -> ErrorCode:
    """Map an exception onto a code."""
    code = classify_text(type(exc).__name__, str(exc))
    if code is not None:
        return code
    if isinstance(exc, PermissionError):
        return get("E004")
    if isinstance(exc, OSError):
        if getattr(exc, "errno", None) == 28:
            return get("E004")
    return get("E900")


def classify_text(name: str, message: str) -> ErrorCode | None:
    """Map an exception's class name and message onto a code.

    Split out from :func:`classify` so a failure recorded as plain text — one
    that crossed a thread boundary as a string — can still be classified
    without resurrecting the exception object.

    Type first, then the message for sub-cases, because several distinct
    operator-facing situations share one exception class.
    """
    text = message.lower()

    if name == "GpmfError":
        if "no gps fixes" in text or "satellite" in text:
            return get("E102")
        return get("E101")

    if name == "ObservationError":
        if "phone gnss" in text:
            return get("E204")
        if "no timestamp column" in text:
            return get("E202")
        if "none had a readable timestamp" in text or "no usable fixes" in text:
            return get("E203")
        return get("E201")

    if name == "MediaError":
        if "remux" in text:
            return get("E304")
        if "transcode" in text:
            return get("E303")
        if "frame extraction" in text:
            return get("E302")
        return get("E301")

    if name == "SupabaseError":
        if "must both be set" in text or "not configured" in text:
            return get("E002")
        if "401" in text or "403" in text or "invalid" in text and "key" in text:
            return get("E401")
        if "failed after" in text or "timed out" in text or "connection" in text:
            return get("E402")
        if "upload" in text or "storage" in text:
            return get("E404")
        return get("E403")

    if name == "PipelineError":
        return get("E301") if "does not exist" in text else get("E900")

    if name in ("OSError", "PermissionError", "IOError"):
        return get("E004")
    if "no space left" in text:
        return get("E004")

    return None


def describe(exc: BaseException) -> dict:
    """A JSON-ready description of a failure: the code plus the raw detail."""
    code = classify(exc)
    return {
        "code": code.code,
        "title": code.title,
        "meaning": code.meaning,
        "fix": code.fix,
        "fatal": code.fatal,
        "detail": f"{type(exc).__name__}: {exc}",
    }
