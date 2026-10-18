SCHEMA_ID = "web_analytics"

QUERIES = [
    ("How many page views did each site section get?",
     """SELECT p."section", COUNT(*) AS views
FROM "factPageView" v
JOIN "dimPage" p ON p."pageKey" = v."pageKey"
GROUP BY p."section\"""",
     ["star"]),

    ("What is the bounce rate (percentage of page views that bounced) for each device type?",
     """SELECT vi."deviceType", 100.0 * SUM(v."isBounce") / COUNT(*) AS bounce_pct
FROM "factPageView" v
JOIN "dimVisitor" vi ON vi."visitorKey" = v."visitorKey"
GROUP BY vi."deviceType\"""",
     ["star", "flag_int", "integer_division"]),

    ("What is the average view duration in seconds (with fractions) for each page URL, ignoring unmeasured views?",
     """SELECT p."url", AVG(v."durationMs" / 1000.0) AS avg_seconds
FROM "factPageView" v
JOIN "dimPage" p ON p."pageKey" = v."pageKey"
WHERE v."durationMs" IS NOT NULL
GROUP BY p."url\"""",
     ["star", "integer_division", "nullable"]),

    ("How many distinct sessions were there on each day of March 2024?",
     """SELECT d."calendarDate", COUNT(DISTINCT v."sessionId") AS sessions
FROM "factPageView" v
JOIN "dimDate" d ON d."dateKey" = v."dateKey"
WHERE d."year" = 2024 AND d."month" = 3
GROUP BY d."calendarDate\"""",
     ["star", "dates"]),

    ("What is total order revenue by campaign channel? Orders with no campaign should be reported under channel 'direct'.",
     """SELECT COALESCE(c."channel", 'direct') AS channel, SUM(o."revenue") AS revenue
FROM "factOrder" o
LEFT JOIN "dimCampaign" c ON c."campaignKey" = o."campaignKey"
GROUP BY COALESCE(c."channel", 'direct')""",
     ["star", "outer_join", "nullable"]),

    ("Which visitors viewed pages but never placed an order? Return visitor keys.",
     """SELECT DISTINCT v."visitorKey" FROM "factPageView" v
WHERE NOT EXISTS (SELECT 1 FROM "factOrder" o WHERE o."visitorKey" = v."visitorKey")""",
     ["anti_join"]),

    ("Which campaigns drove no page views at all? Give campaign names.",
     """SELECT c."name" FROM "dimCampaign" c
LEFT JOIN "factPageView" v ON v."campaignKey" = c."campaignKey"
WHERE v."viewId" IS NULL""",
     ["anti_join", "outer_join"]),

    ("What are the top 10 landing pages by number of weekend page views?",
     """SELECT p."url", COUNT(*) AS views
FROM "factPageView" v
JOIN "dimPage" p ON p."pageKey" = v."pageKey"
JOIN "dimDate" d ON d."dateKey" = v."dateKey"
WHERE p."isLandingPage" = 1 AND d."isWeekend" = 1
GROUP BY p."url"
ORDER BY views DESC
LIMIT 10""",
     ["star", "flag_int", "top_k"]),

    ("How many orders came from visitors in each country? Visitors with unknown country are grouped together.",
     """SELECT vi."country", COUNT(*) AS orders
FROM "factOrder" o
JOIN "dimVisitor" vi ON vi."visitorKey" = o."visitorKey"
GROUP BY vi."country\"""",
     ["star", "nullable"]),

    ("For each campaign with a budget, what is the ratio of attributed order revenue to budget?",
     """SELECT c."name", SUM(o."revenue") / c."budget" AS roi
FROM "dimCampaign" c
JOIN "factOrder" o ON o."campaignKey" = c."campaignKey"
WHERE c."budget" IS NOT NULL
GROUP BY c."campaignKey", c."name", c."budget\"""",
     ["star", "nullable"]),

    ("Show daily page views along with a 7-day trailing moving average of views, ordered by date.",
     """SELECT d."calendarDate", COUNT(*) AS views,
       AVG(COUNT(*)) OVER (ORDER BY d."calendarDate" ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS views_7d_avg
FROM "factPageView" v
JOIN "dimDate" d ON d."dateKey" = v."dateKey"
GROUP BY d."calendarDate"
ORDER BY d."calendarDate\"""",
     ["star"]),

    ("Compare average order revenue between registered and unregistered visitors.",
     """SELECT vi."isRegistered", AVG(o."revenue")
FROM "factOrder" o
JOIN "dimVisitor" vi ON vi."visitorKey" = o."visitorKey"
GROUP BY vi."isRegistered\"""",
     ["star", "flag_int"]),

    ("Which page URLs have no title?",
     """SELECT "url" FROM "dimPage" WHERE "title" IS NULL""",
     ["nullable"]),

    ("For each month of 2024, what share of revenue came from first orders?",
     """SELECT d."month",
       SUM(CASE WHEN o."isFirstOrder" = 1 THEN o."revenue" ELSE 0 END) / SUM(o."revenue") AS first_order_share
FROM "factOrder" o
JOIN "dimDate" d ON d."dateKey" = o."dateKey"
WHERE d."year" = 2024
GROUP BY d."month\"""",
     ["star", "flag_int", "case"]),

    ("How many distinct visitors placed their first order through an email campaign?",
     """SELECT COUNT(DISTINCT o."visitorKey")
FROM "factOrder" o
JOIN "dimCampaign" c ON c."campaignKey" = o."campaignKey"
WHERE o."isFirstOrder" = 1 AND c."channel" = 'email'""",
     ["star", "flag_int"]),

    ("What is the average number of items per order on weekdays?",
     """SELECT AVG(o."itemCount")
FROM "factOrder" o
JOIN "dimDate" d ON d."dateKey" = o."dateKey"
WHERE d."isWeekend" = 0""",
     ["star", "flag_int"]),

    ("List campaigns that are still running (no end date) and started in 2024, newest first.",
     """SELECT "name", "startDate" FROM "dimCampaign"
WHERE "endDate" IS NULL AND "startDate" >= DATE '2024-01-01' AND "startDate" < DATE '2025-01-01'
ORDER BY "startDate" DESC""",
     ["nullable", "dates"]),

    ("How many page views per visitor were there on average, per device type? Use whole-number division.",
     """SELECT vi."deviceType", COUNT(*) / COUNT(DISTINCT v."visitorKey") AS views_per_visitor
FROM "factPageView" v
JOIN "dimVisitor" vi ON vi."visitorKey" = v."visitorKey"
GROUP BY vi."deviceType\"""",
     ["star", "integer_division"]),

    ("Which sections had more page views from organic traffic (no campaign) than from campaign traffic?",
     """SELECT p."section"
FROM "factPageView" v
JOIN "dimPage" p ON p."pageKey" = v."pageKey"
GROUP BY p."section"
HAVING COUNT(*) FILTER (WHERE v."campaignKey" IS NULL) > COUNT(*) FILTER (WHERE v."campaignKey" IS NOT NULL)""",
     ["star", "having", "nullable"]),

    ("Which pages have never been viewed? Return their URLs.",
     """SELECT pg."url"
FROM "dimPage" pg
WHERE NOT EXISTS (SELECT 1 FROM "factPageView" v WHERE v."pageKey" = pg."pageKey")""",
     ["anti_join", "star"]),

    ("List the keys of visitors who were active in 2024, either by viewing a page or by placing an order.",
     """SELECT v."visitorKey"
FROM "factPageView" v
JOIN "dimDate" d ON d."dateKey" = v."dateKey"
WHERE d."year" = 2024
UNION
SELECT o."visitorKey"
FROM "factOrder" o
JOIN "dimDate" d ON d."dateKey" = o."dateKey"
WHERE d."year" = 2024""",
     ["star"]),

    ("Which campaigns have no attributed orders? Give campaign names.",
     """SELECT c."name"
FROM "dimCampaign" c
WHERE NOT EXISTS (SELECT 1 FROM "factOrder" o WHERE o."campaignKey" = c."campaignKey")""",
     ["anti_join", "nullable", "star"]),

    ("Bucket page views by measured duration: 'quick' under 10 seconds, 'engaged' at 10 seconds or more, and 'unmeasured' when no duration was recorded. Show the bucket and the number of views.",
     """SELECT CASE WHEN "durationMs" IS NULL THEN 'unmeasured'
            WHEN "durationMs" < 10000 THEN 'quick'
            ELSE 'engaged'
       END AS bucket,
       COUNT(*) AS views
FROM "factPageView"
GROUP BY 1""",
     ['case', 'nullable']),

    ("For each visitor who placed an order, show their first order: visitor key, order id and revenue. 'First' means the earliest order date; break ties by the lowest order id.",
     '''SELECT DISTINCT ON (o."visitorKey") o."visitorKey", o."orderId", o."revenue"
FROM "factOrder" o
JOIN "dimDate" d ON d."dateKey" = o."dateKey"
ORDER BY o."visitorKey", d."calendarDate", o."orderId"''',
     ['distinct_on']),
]
