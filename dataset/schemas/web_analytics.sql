-- web_analytics: clickstream warehouse, star schema with two fact tables
-- Shape:    star (factPageView and factOrder share conformed dimensions dimDate / dimVisitor /
--           dimCampaign)
-- Naming:   camelCase, double-quoted (case-sensitive; must be quoted in queries), fact/dim prefixes
-- Flags:    SMALLINT 0/1 with CHECK constraints
-- Generic:  "dateKey" / "visitorKey" / "campaignKey" appear in both fact tables

CREATE TABLE "dimDate" (
    "dateKey"       INTEGER PRIMARY KEY,                    -- yyyymmdd
    "calendarDate"  DATE NOT NULL UNIQUE,
    "year"          SMALLINT NOT NULL,
    "month"         SMALLINT NOT NULL,
    "dayOfWeek"     SMALLINT NOT NULL,                      -- 0 = Sunday
    "isWeekend"     SMALLINT NOT NULL CHECK ("isWeekend" IN (0, 1))
);

CREATE TABLE "dimPage" (
    "pageKey"        INTEGER PRIMARY KEY,
    "url"            TEXT NOT NULL UNIQUE,
    "title"          TEXT,                                  -- nullable
    "section"        VARCHAR(50) NOT NULL,
    "isLandingPage"  SMALLINT NOT NULL DEFAULT 0 CHECK ("isLandingPage" IN (0, 1))
);

CREATE TABLE "dimVisitor" (
    "visitorKey"    BIGINT PRIMARY KEY,
    "firstSeenAt"   TIMESTAMP NOT NULL,
    "country"       CHAR(2),                                -- nullable (geo lookup failed)
    "deviceType"    VARCHAR(20) NOT NULL,                   -- 'desktop' / 'mobile' / 'tablet'
    "isRegistered"  SMALLINT NOT NULL DEFAULT 0 CHECK ("isRegistered" IN (0, 1))
);

CREATE TABLE "dimCampaign" (
    "campaignKey"  INTEGER PRIMARY KEY,
    "name"         VARCHAR(100) NOT NULL,
    "channel"      VARCHAR(30) NOT NULL,                    -- 'email' / 'search' / 'social' ...
    "budget"       NUMERIC(12, 2),                          -- nullable
    "startDate"    DATE NOT NULL,
    "endDate"      DATE                                     -- null = open-ended
);

CREATE TABLE "factPageView" (
    "viewId"       BIGINT PRIMARY KEY,
    "dateKey"      INTEGER NOT NULL REFERENCES "dimDate" ("dateKey"),
    "pageKey"      INTEGER NOT NULL REFERENCES "dimPage" ("pageKey"),
    "visitorKey"   BIGINT NOT NULL REFERENCES "dimVisitor" ("visitorKey"),
    "campaignKey"  INTEGER REFERENCES "dimCampaign" ("campaignKey"),   -- null = organic
    "sessionId"    VARCHAR(64) NOT NULL,
    "durationMs"   INTEGER,                                            -- null = not measured
    "isBounce"     SMALLINT NOT NULL CHECK ("isBounce" IN (0, 1))
);

CREATE TABLE "factOrder" (
    "orderId"       BIGINT PRIMARY KEY,
    "dateKey"       INTEGER NOT NULL REFERENCES "dimDate" ("dateKey"),
    "visitorKey"    BIGINT NOT NULL REFERENCES "dimVisitor" ("visitorKey"),
    "campaignKey"   INTEGER REFERENCES "dimCampaign" ("campaignKey"),  -- null = direct
    "revenue"       NUMERIC(12, 2) NOT NULL,
    "itemCount"     INTEGER NOT NULL,
    "isFirstOrder"  SMALLINT NOT NULL CHECK ("isFirstOrder" IN (0, 1))
);
