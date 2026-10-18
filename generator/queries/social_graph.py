SCHEMA_ID = "social_graph"

QUERIES = [
    ("What are the usernames of everyone who follows the user 'alice'?",
     """SELECT u."userName"
FROM "follows" f
JOIN "users" u ON u."id" = f."followerId"
JOIN "users" a ON a."id" = f."followeeId"
WHERE a."userName" = 'alice'""",
     ["self_m2m", "self_join"]),

    ("List every pair of users who follow each other, showing each pair once (smaller user id first).",
     """SELECT f1."followerId", f1."followeeId"
FROM "follows" f1
JOIN "follows" f2 ON f2."followerId" = f1."followeeId" AND f2."followeeId" = f1."followerId"
WHERE f1."followerId" < f1."followeeId\"""",
     ["self_m2m", "self_join"]),

    ("How many followers does each user have? Include users with no followers and sort by follower count descending.",
     """SELECT u."userName", COUNT(f."followerId") AS followers
FROM "users" u
LEFT JOIN "follows" f ON f."followeeId" = u."id"
GROUP BY u."id", u."userName"
ORDER BY followers DESC""",
     ["outer_join", "self_m2m"]),

    ("Which users does 'alice' follow that 'bob' does not follow? Return user ids.",
     """SELECT f."followeeId" FROM "follows" f
JOIN "users" u ON u."id" = f."followerId"
WHERE u."userName" = 'alice'
EXCEPT
SELECT f."followeeId" FROM "follows" f
JOIN "users" u ON u."id" = f."followerId"
WHERE u."userName" = 'bob'""",
     ["set_op", "self_m2m"]),

    ("What are the 10 most-liked posts? Show post id and like count, ties broken by post id.",
     """SELECT p."id", COUNT(*) AS likes
FROM "posts" p
JOIN "postLikes" l ON l."postId" = p."id"
GROUP BY p."id"
ORDER BY likes DESC, p."id"
LIMIT 10""",
     ["top_k"]),

    ("For every reply post, show the reply's id and the username of the author of the post it replies to.",
     """SELECT r."id", u."userName"
FROM "posts" r
JOIN "posts" parent ON parent."id" = r."replyToPostId"
JOIN "users" u ON u."id" = parent."userId\"""",
     ["self_join", "generic_names"]),

    ("Which users have turned email notifications off?",
     """SELECT "userName" FROM "users" WHERE "emailNotifications" = 'off'""",
     ["flag_onoff"]),

    ("Show every user's display name, falling back to their username when no display name is set.",
     """SELECT COALESCE("displayName", "userName") AS shown_name FROM "users\"""",
     ["nullable"]),

    ("What are the 5 most used hashtags and how many posts use each?",
     """SELECT h."tagName", COUNT(*) AS uses
FROM "hashTags" h
JOIN "postHashTags" ph ON ph."hashTagId" = h."id"
GROUP BY h."id", h."tagName"
ORDER BY uses DESC
LIMIT 5""",
     ["m2m", "top_k"]),

    ("Which users have never written a post?",
     """SELECT u."userName" FROM "users" u
LEFT JOIN "posts" p ON p."userId" = u."id"
WHERE p."id" IS NULL""",
     ["anti_join", "outer_join"]),

    ("How many posts did each user publish in each month of 2024? Return user id, month (truncated timestamp) and count.",
     """SELECT "userId", date_trunc('month', "createdAt") AS month, COUNT(*) AS posts
FROM "posts"
WHERE "createdAt" >= TIMESTAMP '2024-01-01' AND "createdAt" < TIMESTAMP '2025-01-01'
GROUP BY "userId", date_trunc('month', "createdAt")""",
     ["dates"]),

    ("Suggest accounts for 'alice' to follow: users followed by someone alice follows, excluding alice herself and anyone she already follows. Return distinct user ids.",
     """SELECT DISTINCT f2."followeeId"
FROM "users" a
JOIN "follows" f1 ON f1."followerId" = a."id"
JOIN "follows" f2 ON f2."followerId" = f1."followeeId"
WHERE a."userName" = 'alice'
  AND f2."followeeId" <> a."id"
  AND NOT EXISTS (SELECT 1 FROM "follows" f3
                  WHERE f3."followerId" = a."id" AND f3."followeeId" = f2."followeeId")""",
     ["self_m2m", "self_join", "anti_join"]),

    ("What is the average number of likes per post, counting posts with zero likes?",
     """SELECT AVG(cnt) FROM (
    SELECT p."id", COUNT(l."userId") AS cnt
    FROM "posts" p LEFT JOIN "postLikes" l ON l."postId" = p."id"
    GROUP BY p."id"
) t""",
     ["outer_join", "derived_table"]),

    ("How many posts have been edited?",
     """SELECT COUNT("editedAt") FROM "posts\"""",
     ["nullable"]),

    ("Which users with private profiles have more than 100 followers?",
     """SELECT u."userName"
FROM "users" u
JOIN "follows" f ON f."followeeId" = u."id"
WHERE u."privateProfile" = 'on'
GROUP BY u."id", u."userName"
HAVING COUNT(*) > 100""",
     ["flag_onoff", "having"]),

    ("For each user who has posted, return the id and body of their most recent post.",
     """SELECT DISTINCT ON ("userId") "userId", "id", "body"
FROM "posts"
ORDER BY "userId", "createdAt" DESC""",
     ["distinct_on"]),

    ("How many top-level posts (not replies) have comments disabled?",
     """SELECT COUNT(*) FROM "posts"
WHERE "replyToPostId" IS NULL AND "commentsEnabled" = 'off'""",
     ["flag_onoff", "nullable"]),

    ("List users who liked their own posts, with the number of such self-likes.",
     """SELECT u."userName", COUNT(*) AS self_likes
FROM "postLikes" l
JOIN "posts" p ON p."id" = l."postId"
JOIN "users" u ON u."id" = l."userId"
WHERE p."userId" = l."userId"
GROUP BY u."id", u."userName\"""",
     ["generic_names"]),

    ("How many users signed up from each country? Users without a country code are grouped together.",
     """SELECT "countryCode", COUNT(*) FROM "users" GROUP BY "countryCode\"""",
     ["nullable"]),

    ("Which posts have not received any replies? Return post ids.",
     """SELECT p."id"
FROM "posts" p
WHERE NOT EXISTS (SELECT 1 FROM "posts" r WHERE r."replyToPostId" = p."id")""",
     ["anti_join", "nullable", "self_join"]),

    ("List the ids of users who interacted with post 42, either by liking it or by replying to it.",
     """SELECT "userId" FROM "postLikes" WHERE "postId" = 42
UNION
SELECT "userId" FROM "posts" WHERE "replyToPostId" = 42""",
     []),

    ("List every username with its country code, showing 'unknown' when the country code is missing.",
     """SELECT "userName", COALESCE("countryCode", 'unknown') AS country FROM "users\"""",
     ["nullable"]),

    ("Which posts mention '#postgres' in any capitalization? Show the post id and the body length in characters.",
     """SELECT "id", length("body") AS "bodyLength"
FROM "posts"
WHERE "body" ILIKE '%#postgres%'""",
     ['string_func']),

    ("For each user, show their username and the bodies of their three most recent top-level posts (not replies). Break ties by the highest post id.",
     """SELECT u."userName", p."body"
FROM "users" u
CROSS JOIN LATERAL (
    SELECT "body" FROM "posts"
    WHERE "userId" = u."id" AND "replyToPostId" IS NULL
    ORDER BY "createdAt" DESC, "id" DESC
    LIMIT 3
) p""",
     ['lateral', 'top_k']),
]
