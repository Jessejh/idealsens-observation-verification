"""curbtool — ingest pipeline for the curb/pavement review project.

Reads GoPro footage, matches phone-tagged observations against the camera's
own telemetry, extracts evidence frames, builds playback proxies and pushes
everything to Supabase. The review UI is a separate Lovable app against the
same Supabase project; see LOVABLE_PROMPT.md.
"""

__version__ = "0.1.0"
