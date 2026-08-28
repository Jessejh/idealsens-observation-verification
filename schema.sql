-- curbtool schema — curb/pavement review prototype.
--
-- Run this in the Supabase SQL editor, or move it into supabase/migrations/
-- with a timestamped name if you want the GitHub integration to deploy it.
-- Then run spatial.sql.
--
-- Two rules shape everything below:
--
--   1. Observations are append-only. Ingest writes them; nothing else ever
--      UPDATEs one. Every piece of human judgement — a severity, a correction,
--      a rejection — is a new row in `reviews`. That keeps ingest re-runnable,
--      keeps an audit trail of what the city changed, and makes reviewer
--      agreement measurable.
--
--   2. Ingest is idempotent. IDs are derived (uuid5) from the campaign, the
--      filename and the file size, so re-running a file updates the same rows
--      instead of duplicating them — and reviews stay attached across a re-run.
--
-- This is a prototype: single shared link, no accounts, permissive RLS. See
-- the bottom of this file.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- sessions — one row per video chapter
-- ---------------------------------------------------------------------------
-- A HERO5 splits recording at 4 GB, roughly 19 minutes at 1080p30, so a
-- campaign is many chapters and each becomes its own session.

create table if not exists sessions (
  id                 uuid primary key,
  campaign           text        not null,
  filename           text        not null,
  file_size          bigint      not null,
  device             text,
  duration_s         double precision,
  started_utc        timestamptz,
  ended_utc          timestamptz,
  clock_offset_s     double precision not null default 0,

  proxy_path         text,
  proxy_url          text,
  proxy_bytes        bigint,
  proxy_source       text,          -- hd | lrv

  observation_count  integer     not null default 0,
  frame_count        integer     not null default 0,
  stop_count         integer     not null default 0,
  snapped_count      integer     not null default 0,

  ingest_status      text        not null default 'pending'
                       check (ingest_status in ('pending','complete','failed')),
  ingest_error       text,
  ingested_at        timestamptz,
  created_at         timestamptz not null default now(),

  unique (campaign, filename, file_size)
);

create index if not exists sessions_campaign_idx on sessions (campaign);
create index if not exists sessions_time_idx     on sessions (started_utc);

-- ---------------------------------------------------------------------------
-- observations — one row per tagged problem spot
-- ---------------------------------------------------------------------------
-- Positions are kept twice on purpose. GoPro telemetry gives the timing, since
-- it lives inside the video file and so has zero clock-sync error against the
-- footage. But a HERO5 is a weak receiver; the phone is multi-constellation
-- with sensor fusion. Where a phone log exists its fixes are averaged across
-- the stop and used as the position, with the camera's kept alongside.
-- gps_disagreement_m between the two is the flag for poor reception.

create table if not exists observations (
  id                  uuid primary key,
  session_id          uuid references sessions(id) on delete cascade,
  campaign            text        not null,
  external_id         text,                      -- id from the tagging app
  source              text        not null default 'ingest'
                        check (source in ('ingest','review')),

  observed_utc        timestamptz not null,
  video_offset_s      double precision,

  -- Frame windows follow the detected stop, not a fixed span either side of
  -- the tag: the operator stopped and framed each target on the camera screen
  -- before tagging, so the target is visible for the whole stationary period.
  stop_index          integer,
  stop_start_s        double precision,
  stop_end_s          double precision,
  snapped             boolean     not null default false,

  category            text,
  note                text,

  lat                 double precision,          -- best available position
  lon                 double precision,
  position_source     text check (position_source in ('phone','gopro','tag')),
  gopro_lat           double precision,
  gopro_lon           double precision,
  phone_lat           double precision,
  phone_lon           double precision,
  phone_fix_count     integer,
  gps_disagreement_m  double precision,
  gps_dop             double precision,
  gps_fix             integer,

  created_at          timestamptz not null default now()
);

create index if not exists observations_session_idx  on observations (session_id);
create index if not exists observations_campaign_idx on observations (campaign);
create index if not exists observations_time_idx     on observations (observed_utc);

-- ---------------------------------------------------------------------------
-- frames — evidence stills cut from the HD original
-- ---------------------------------------------------------------------------
-- delta_s is relative to the middle of the stop, so delta_s = 0 is the moment
-- the operator was most likely framing the target. The review UI opens there.

create table if not exists frames (
  id             uuid primary key,
  observation_id uuid not null references observations(id) on delete cascade,
  session_id     uuid references sessions(id) on delete cascade,
  seq            integer not null,
  delta_s        double precision not null,
  offset_s       double precision not null,
  storage_path   text    not null,
  public_url     text,
  width          integer,
  height         integer,
  bytes          integer,
  created_at     timestamptz not null default now(),

  unique (observation_id, seq)
);

create index if not exists frames_observation_idx on frames (observation_id, seq);
create index if not exists frames_session_idx     on frames (session_id);

-- ---------------------------------------------------------------------------
-- track_points — the driven route, for the map
-- ---------------------------------------------------------------------------

create table if not exists track_points (
  id         bigserial primary key,
  session_id uuid   not null references sessions(id) on delete cascade,
  seq        integer not null,
  offset_s   double precision not null,
  utc        timestamptz,
  lat        double precision not null,
  lon        double precision not null,
  speed_mps  double precision,

  unique (session_id, seq)
);

create index if not exists track_points_session_idx on track_points (session_id, seq);

-- ---------------------------------------------------------------------------
-- reviews — every piece of human judgement, append-only
-- ---------------------------------------------------------------------------
-- Severity is the city's call and nothing else writes it. Correcting a
-- mis-tagged observation means inserting a 'reclassified' review with
-- corrected_category, never editing the observation.

create table if not exists reviews (
  id                 uuid primary key default gen_random_uuid(),
  observation_id     uuid not null references observations(id) on delete cascade,

  severity           smallint check (severity between 1 and 3),
  status             text not null default 'confirmed'
                       check (status in ('confirmed','reclassified','rejected',
                                         'duplicate','needs_revisit')),
  corrected_category text,
  note               text,
  reviewer_name      text,

  created_at         timestamptz not null default now()
);

create index if not exists reviews_observation_idx on reviews (observation_id, created_at desc);

-- ---------------------------------------------------------------------------
-- v_reviewed — the export view
-- ---------------------------------------------------------------------------
-- One row per observation carrying its most recent review, whether or not it
-- has one. This is what the city exports at the end of the project, and what
-- the review UI reads for its "still to grade" list (severity is null).

create or replace view v_reviewed as
select
  o.id,
  o.campaign,
  o.session_id,
  s.filename,
  o.external_id,
  o.source,
  o.observed_utc,
  o.video_offset_s,
  o.stop_index,
  o.stop_start_s,
  o.stop_end_s,
  o.snapped,
  o.category                              as tagged_category,
  o.note                                  as tagged_note,
  o.lat,
  o.lon,
  o.position_source,
  o.gps_disagreement_m,
  o.gps_dop,

  r.severity,
  r.status                                as review_status,
  coalesce(r.corrected_category, o.category) as final_category,
  r.note                                  as review_note,
  r.reviewer_name,
  r.created_at                            as reviewed_at,
  (r.id is not null)                      as is_reviewed,

  (select count(*) from frames f where f.observation_id = o.id) as frame_count,
  (select f.public_url
     from frames f
    where f.observation_id = o.id
    order by abs(f.delta_s), f.seq
    limit 1)                              as cover_frame_url
from observations o
left join sessions s on s.id = o.session_id
left join lateral (
  select rr.*
    from reviews rr
   where rr.observation_id = o.id
   order by rr.created_at desc, rr.id desc
   limit 1
) r on true;

-- ---------------------------------------------------------------------------
-- RLS — prototype only
-- ---------------------------------------------------------------------------
-- No accounts, one shared link. The anon key may read everything and write
-- reviews (that is the whole job of the review UI); it may not touch the rows
-- ingest owns. The pipeline uses the service_role key, which bypasses all of
-- this. Do not carry these policies into anything that outlives the project.

alter table sessions     enable row level security;
alter table observations enable row level security;
alter table frames       enable row level security;
alter table track_points enable row level security;
alter table reviews      enable row level security;

do $$
declare t text;
begin
  foreach t in array array['sessions','observations','frames','track_points','reviews'] loop
    execute format('drop policy if exists %I on %I', t || '_anon_read', t);
    execute format(
      'create policy %I on %I for select to anon, authenticated using (true)',
      t || '_anon_read', t);
  end loop;
end $$;

drop policy if exists reviews_anon_insert on reviews;
create policy reviews_anon_insert on reviews
  for insert to anon, authenticated with check (true);

-- Reviewers may add observations the drive missed; those are marked
-- source = 'review' so ingest never mistakes one for its own row.
drop policy if exists observations_anon_insert on observations;
create policy observations_anon_insert on observations
  for insert to anon, authenticated with check (source = 'review');
