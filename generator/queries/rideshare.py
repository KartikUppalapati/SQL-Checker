SCHEMA_ID = "rideshare"

# (question, target_sql, extra_tags)
QUERIES = [

    ('List the names of verified riders.',
     """SELECT name FROM riders WHERE is_verified""",
     ['flag_bool']),

    ('Which riders have not opted in to marketing emails? Include riders who were never asked.',
     """SELECT id, name FROM riders WHERE marketing_opt_in IS NOT TRUE""",
     ['flag_bool', 'nullable', 'three_valued']),

    ('How many riders were never asked about marketing emails?',
     """SELECT COUNT(*) FROM riders WHERE marketing_opt_in IS NULL""",
     ['flag_bool', 'nullable', 'three_valued']),

    ('For each city with drivers, say whether all of its drivers are active.',
     """SELECT c.name, bool_and(d.is_active) AS all_active
FROM cities c
JOIN drivers d ON d.city_id = c.id
GROUP BY c.id, c.name""",
     ['flag_bool', 'bool_agg']),

    ('How many shared rides were requested in each city that has trips?',
     """SELECT city_id, COUNT(*) FILTER (WHERE is_shared) AS shared_trips
FROM trips
GROUP BY city_id""",
     ['flag_bool']),

    ("For each trip, show its id and the local calendar date, in the time zone of the trip's city, on which it was requested.",
     """SELECT t.id, (t.requested_at AT TIME ZONE c.time_zone)::date AS local_date
FROM trips t
JOIN cities c ON c.id = t.city_id""",
     ['timestamptz', 'time_zone']),

    ("How many trips were requested on 2024-03-01, as a calendar day in the session's time zone?",
     """SELECT COUNT(*) FROM trips
WHERE requested_at >= '2024-03-01' AND requested_at < '2024-03-02'""",
     ['timestamptz', 'range']),

    ('What is the average pickup-to-drop-off time in minutes of the trips in each city, over trips with both times recorded?',
     """SELECT city_id, AVG(EXTRACT(EPOCH FROM completed_at - started_at) / 60) AS avg_minutes
FROM trips
WHERE started_at IS NOT NULL AND completed_at IS NOT NULL
GROUP BY city_id""",
     ['timestamptz', 'interval', 'nullable']),

    ('What is the total distance driven by each driver on completed trips?',
     """SELECT driver_id, SUM(distance_km) AS km
FROM trips
WHERE completed_at IS NOT NULL AND driver_id IS NOT NULL
GROUP BY driver_id""",
     ['float', 'nullable']),

    ('Which drivers have a rating of at least 4.5?',
     """SELECT name FROM drivers WHERE rating >= 4.5""",
     ['float', 'nullable']),

    ('What is the average distance of the trips in each city, rounded to one decimal place? Only trips with a recorded distance count.',
     """SELECT city_id, ROUND(AVG(distance_km)::numeric, 1) AS avg_km
FROM trips
WHERE distance_km IS NOT NULL
GROUP BY city_id""",
     ['float', 'nullable']),

    ('For each city with completed trips, what share of its completed trips used a promo? Trips whose promo status is unknown count as no promo.',
     """SELECT city_id, AVG(CASE WHEN promo_applied THEN 1.0 ELSE 0.0 END) AS promo_share
FROM trips
WHERE completed_at IS NOT NULL
GROUP BY city_id""",
     ['flag_bool', 'nullable', 'three_valued', 'case']),

    ("For each GPS ping, show the trip, the time, and the change in latitude since the trip's previous ping.",
     """SELECT trip_id, recorded_at, lat - LAG(lat) OVER (PARTITION BY trip_id ORDER BY recorded_at) AS dlat
FROM gps_pings""",
     ['float', 'window']),

    ('List the riders who were referred by a verified rider.',
     """SELECT r.name
FROM riders r
JOIN riders ref ON ref.id = r.referred_by
WHERE ref.is_verified""",
     ['flag_bool', 'self_join']),

    ('Find completed, non-cancelled trips that used a promo; an unknown promo status does not count.',
     """SELECT id FROM trips
WHERE NOT is_cancelled AND completed_at IS NOT NULL AND promo_applied""",
     ['flag_bool', 'nullable', 'three_valued']),

    ('For each rider with trips, say whether any of their trips was a shared ride.',
     """SELECT rider_id, bool_or(is_shared) AS ever_shared
FROM trips
GROUP BY rider_id""",
     ['flag_bool', 'bool_agg']),

    ('Which trips were requested after 09:00 UTC on 2024-06-01?',
     """SELECT id FROM trips WHERE requested_at > TIMESTAMPTZ '2024-06-01 09:00:00+00'""",
     ['timestamptz', 'time_zone']),

    ('How many trips were requested in each hour of the day, measured in UTC?',
     """SELECT EXTRACT(HOUR FROM requested_at AT TIME ZONE 'UTC') AS hour, COUNT(*) AS trips
FROM trips
GROUP BY EXTRACT(HOUR FROM requested_at AT TIME ZONE 'UTC')""",
     ['timestamptz', 'time_zone']),

    ('Which trips took more than 30 minutes from pickup to drop-off?',
     """SELECT id FROM trips WHERE completed_at - started_at > INTERVAL '30 minutes'""",
     ['timestamptz', 'interval', 'nullable']),

    ('What is the latest recorded position (latitude and longitude) of each trip that has GPS pings?',
     """SELECT DISTINCT ON (trip_id) trip_id, lat, lon
FROM gps_pings
ORDER BY trip_id, recorded_at DESC""",
     ['distinct_on', 'float']),

    ('What is the average fare of completed trips with a vehicle, split by whether the vehicle is electric?',
     """SELECT v.is_electric, AVG(t.fare) AS avg_fare
FROM trips t
JOIN vehicles v ON v.id = t.vehicle_id
WHERE t.completed_at IS NOT NULL
GROUP BY v.is_electric""",
     ['flag_bool']),

    ('Which drivers have never had a cancelled trip? Drivers without trips count.',
     """SELECT d.name FROM drivers d
WHERE NOT EXISTS (SELECT 1 FROM trips t WHERE t.driver_id = d.id AND t.is_cancelled)""",
     ['flag_bool', 'anti_join']),
]
