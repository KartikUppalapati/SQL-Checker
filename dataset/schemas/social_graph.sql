-- social_graph: microblogging network
-- Shape:    self-referential many-to-many (follows: users <-> users), self-referential reply
--           threads (posts.replyToPostId -> posts), many-to-many (posts <-> hashTags)
-- Naming:   camelCase, double-quoted (case-sensitive; must be quoted in queries)
-- Flags:    VARCHAR 'on'/'off' with CHECK constraints
-- Generic:  "id" / "userId" / "createdAt" appear in several tables

CREATE TABLE "users" (
    "id"                  BIGINT PRIMARY KEY,
    "userName"            VARCHAR(50) NOT NULL UNIQUE,
    "displayName"         VARCHAR(100),                             -- nullable
    "countryCode"         CHAR(2),                                  -- nullable
    "createdAt"           TIMESTAMP NOT NULL,
    "emailNotifications"  VARCHAR(3) NOT NULL DEFAULT 'on'  CHECK ("emailNotifications" IN ('on', 'off')),
    "privateProfile"      VARCHAR(3) NOT NULL DEFAULT 'off' CHECK ("privateProfile" IN ('on', 'off'))
);

CREATE TABLE "follows" (
    "followerId"  BIGINT NOT NULL REFERENCES "users" ("id"),
    "followeeId"  BIGINT NOT NULL REFERENCES "users" ("id"),
    "createdAt"   TIMESTAMP NOT NULL,
    "muted"       VARCHAR(3) NOT NULL DEFAULT 'off' CHECK ("muted" IN ('on', 'off')),
    PRIMARY KEY ("followerId", "followeeId")
);

CREATE TABLE "posts" (
    "id"               BIGINT PRIMARY KEY,
    "userId"           BIGINT NOT NULL REFERENCES "users" ("id"),
    "body"             TEXT NOT NULL,
    "replyToPostId"    BIGINT REFERENCES "posts" ("id"),            -- null for top-level posts
    "createdAt"        TIMESTAMP NOT NULL,
    "editedAt"         TIMESTAMP,                                   -- null if never edited
    "commentsEnabled"  VARCHAR(3) NOT NULL DEFAULT 'on' CHECK ("commentsEnabled" IN ('on', 'off'))
);

CREATE TABLE "postLikes" (
    "userId"     BIGINT NOT NULL REFERENCES "users" ("id"),
    "postId"     BIGINT NOT NULL REFERENCES "posts" ("id"),
    "createdAt"  TIMESTAMP NOT NULL,
    PRIMARY KEY ("userId", "postId")
);

CREATE TABLE "hashTags" (
    "id"       INTEGER PRIMARY KEY,
    "tagName"  VARCHAR(50) NOT NULL UNIQUE
);

CREATE TABLE "postHashTags" (
    "postId"     BIGINT NOT NULL REFERENCES "posts" ("id"),
    "hashTagId"  INTEGER NOT NULL REFERENCES "hashTags" ("id"),
    PRIMARY KEY ("postId", "hashTagId")
);
