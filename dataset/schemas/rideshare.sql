-- rideshare: ride-hailing marketplace
-- Shape:    star (trips reference riders, drivers, vehicles and cities) plus a self-reference
--           (riders.referred_by)
-- Naming:   snake_case, unquoted
-- Flags:    native BOOLEAN; some are nullable (three-valued: true / false / unknown)
-- Types:    TIMESTAMPTZ instants (cities have their own time zone), DOUBLE PRECISION and REAL
--           measures, NUMERIC money
-- Generic:  id / name / city_id appear in several tables

CREATE TABLE cities (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    country      CHAR(2) NOT NULL,
    time_zone    TEXT NOT NULL,                         -- IANA name, e.g. 'Europe/Berlin'
    is_launched  BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE riders (
    id                BIGINT PRIMARY KEY,
    name              TEXT NOT NULL,
    email             TEXT UNIQUE,                       -- nullable
    city_id           INTEGER REFERENCES cities (id),    -- nullable: home city unknown
    signed_up_at      TIMESTAMPTZ NOT NULL,
    is_verified       BOOLEAN NOT NULL DEFAULT false,
    marketing_opt_in  BOOLEAN,                           -- null = never asked
    referred_by       BIGINT REFERENCES riders (id)      -- null = not referred
);

CREATE TABLE drivers (
    id            BIGINT PRIMARY KEY,
    name          TEXT NOT NULL,
    city_id       INTEGER NOT NULL REFERENCES cities (id),
    rating        REAL,                                  -- average rider score; null until rated
    is_active     BOOLEAN NOT NULL,
    onboarded_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE vehicles (
    id           INTEGER PRIMARY KEY,
    driver_id    BIGINT NOT NULL REFERENCES drivers (id),
    plate        TEXT NOT NULL UNIQUE,
    seats        SMALLINT NOT NULL,
    is_electric  BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE trips (
    id                BIGINT PRIMARY KEY,
    rider_id          BIGINT NOT NULL REFERENCES riders (id),
    driver_id         BIGINT REFERENCES drivers (id),    -- null until a driver accepts
    vehicle_id        INTEGER REFERENCES vehicles (id),  -- null until a driver accepts
    city_id           INTEGER NOT NULL REFERENCES cities (id),
    requested_at      TIMESTAMPTZ NOT NULL,
    started_at        TIMESTAMPTZ,                       -- null until pickup
    completed_at      TIMESTAMPTZ,                       -- null until drop-off
    distance_km       DOUBLE PRECISION,                  -- null until completed
    fare              NUMERIC(10, 2),                    -- null until completed
    surge_multiplier  REAL NOT NULL DEFAULT 1.0,
    is_cancelled      BOOLEAN NOT NULL DEFAULT false,
    is_shared         BOOLEAN NOT NULL DEFAULT false,
    promo_applied     BOOLEAN                            -- null for legacy trips
);

CREATE TABLE trip_ratings (
    trip_id       BIGINT PRIMARY KEY REFERENCES trips (id),
    rider_score   SMALLINT,                              -- 1-5, nullable: the rider skipped it
    driver_score  SMALLINT,                              -- 1-5, nullable: the driver skipped it
    comment       TEXT,                                  -- nullable
    rated_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE gps_pings (
    trip_id      BIGINT NOT NULL REFERENCES trips (id),
    recorded_at  TIMESTAMPTZ NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    speed_kmh    REAL,                                   -- nullable: no speed fix
    PRIMARY KEY (trip_id, recorded_at)
);
