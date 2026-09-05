-- curbtool spatial layer — PostGIS geography, indexes and map helpers.
-- Run after schema.sql.
--
-- Latitude and longitude stay as plain columns in schema.sql: the pipeline
-- writes those, and they are what a CSV export needs. The geography columns
-- here are generated from them, so there is exactly one source of truth and
-- nothing to keep in sync.

create extension if not exists postgis;

-- ---------------------------------------------------------------------------
-- Generated geography columns
-- ---------------------------------------------------------------------------

alter table observations
  add column if not exists geog geography(Point, 4326)
  generated always as (
    case when lat is not null and lon is not null
         then st_setsrid(st_makepoint(lon, lat), 4326)::geography
    end
  ) stored;

alter table track_points
  add column if not exists geog geography(Point, 4326)
  generated always as (
    st_setsrid(st_makepoint(lon, lat), 4326)::geography
  ) stored;

create index if not exists observations_geog_idx on observations using gist (geog);
create index if not exists track_points_geog_idx on track_points using gist (geog);

-- ---------------------------------------------------------------------------
-- nearest_footage — "what did we drive past here?"
-- ---------------------------------------------------------------------------
-- Given a point the reviewer clicked on the map, find the closest places the
-- scooter actually passed, so they can open the video there. This is what
-- makes adding a missed observation possible: the reviewer drops a pin, and
-- this returns the file and the second to look at.

create or replace function nearest_footage(
  in_lat        double precision,
  in_lon        double precision,
  in_radius_m   double precision default 40,
  in_limit      integer default 5,
  in_campaign   text default null
)
returns table (
  session_id  uuid,
  filename    text,
  campaign    text,
  offset_s    double precision,
  utc         timestamptz,
  lat         double precision,
  lon         double precision,
  distance_m  double precision
)
language sql
stable
as $$
  with target as (
    select st_setsrid(st_makepoint(in_lon, in_lat), 4326)::geography as g
  ),
  -- Rank points within each session so one pass of the same street does not
  -- return forty near-identical rows a metre apart.
  ranked as (
    select
      tp.session_id,
      tp.offset_s,
      tp.utc,
      tp.lat,
      tp.lon,
      st_distance(tp.geog, target.g) as distance_m,
      row_number() over (
        partition by tp.session_id
        order by st_distance(tp.geog, target.g)
      ) as rank_in_session
    from track_points tp
    cross join target
    join sessions s on s.id = tp.session_id
    where st_dwithin(tp.geog, target.g, in_radius_m)
      and (in_campaign is null or s.campaign = in_campaign)
  )
  select
    r.session_id,
    s.filename,
    s.campaign,
    r.offset_s,
    r.utc,
    r.lat,
    r.lon,
    r.distance_m
  from ranked r
  join sessions s on s.id = r.session_id
  where r.rank_in_session = 1
  order by r.distance_m
  limit in_limit;
$$;

-- ---------------------------------------------------------------------------
-- route_geojson — the driven route as one GeoJSON FeatureCollection
-- ---------------------------------------------------------------------------
-- One LineString per session, ready to hand straight to the map layer. Pass a
-- campaign to get the whole drive, or a session to get one chapter.

create or replace function route_geojson(
  in_campaign   text default null,
  in_session_id uuid default null
)
returns jsonb
language sql
stable
as $$
  with lines as (
    select
      s.id,
      s.filename,
      s.campaign,
      s.started_utc,
      st_makeline(tp.geog::geometry order by tp.seq) as line
    from sessions s
    join track_points tp on tp.session_id = s.id
    where (in_campaign is null   or s.campaign = in_campaign)
      and (in_session_id is null or s.id = in_session_id)
    group by s.id, s.filename, s.campaign, s.started_utc
    -- st_makeline needs two points; a session with one fix is not a route.
    having count(tp.id) > 1
  )
  select coalesce(
    jsonb_build_object(
      'type', 'FeatureCollection',
      'features', jsonb_agg(
        jsonb_build_object(
          'type', 'Feature',
          'geometry', st_asgeojson(line)::jsonb,
          'properties', jsonb_build_object(
            'session_id',  id,
            'filename',    filename,
            'campaign',    campaign,
            'started_utc', started_utc
          )
        )
      )
    ),
    jsonb_build_object('type', 'FeatureCollection', 'features', '[]'::jsonb)
  )
  from lines;
$$;

-- ---------------------------------------------------------------------------
-- observations_geojson — graded observations for the map
-- ---------------------------------------------------------------------------
-- Carries the current severity, so the map can colour pins by grade and show
-- at a glance how much of the campaign is still ungraded.

create or replace function observations_geojson(
  in_campaign text default null
)
returns jsonb
language sql
stable
as $$
  select coalesce(
    jsonb_build_object(
      'type', 'FeatureCollection',
      'features', jsonb_agg(
        jsonb_build_object(
          'type', 'Feature',
          'geometry', jsonb_build_object(
            'type', 'Point',
            'coordinates', jsonb_build_array(v.lon, v.lat)
          ),
          'properties', to_jsonb(v) - 'lat' - 'lon'
        )
      )
    ),
    jsonb_build_object('type', 'FeatureCollection', 'features', '[]'::jsonb)
  )
  from v_reviewed v
  where v.lat is not null
    and v.lon is not null
    and (in_campaign is null or v.campaign = in_campaign);
$$;

grant execute on function nearest_footage(double precision, double precision,
                                          double precision, integer, text)
  to anon, authenticated;
grant execute on function route_geojson(text, uuid)        to anon, authenticated;
grant execute on function observations_geojson(text)       to anon, authenticated;
