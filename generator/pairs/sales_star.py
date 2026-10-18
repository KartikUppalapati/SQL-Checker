"""Hand-written labeled predictions for sales_star queries. See campus.py for the format."""
E, N = True, False

PAIRS = {
    1: [
        (E, "SELECT d.year, SUM(f.unit_price * f.quantity) FROM dim_date d JOIN fact_sales f ON f.date_key = d.date_key GROUP BY d.year ORDER BY 1",
         "commuted product, ordinal ORDER BY"),
        (N, "SELECT d.year, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year",
         "target is ordered by year; prediction is unordered"),
        (N, "SELECT d.year, SUM(f.quantity) * SUM(f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year ORDER BY d.year",
         "product of sums is not sum of products"),
        (E, "SELECT d.year, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year ORDER BY d.year ASC NULLS LAST",
         "year is NOT NULL"),
        (N, "SELECT EXTRACT(YEAR FROM d.full_date), SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY 1 ORDER BY 1",
         "year and full_date are independent columns; no constraint ties them together"),
        (E, "SELECT d.year, SUM(f.quantity * f.unit_price) AS revenue FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year ORDER BY 1",
         "ORDER BY position 1 is d.year"),
        (N, "SELECT d.year, SUM(f.quantity * f.unit_price) AS revenue FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year ORDER BY 3",
         "INVALID: ORDER BY position 3 is not in the select list"),
    ],
    2: [
        (E, "SELECT st.region, SUM(fs.quantity * fs.unit_price) FROM dim_store st JOIN fact_sales fs ON fs.store_key = st.store_key JOIN dim_date dd ON dd.date_key = fs.date_key WHERE dd.quarter = 4 AND dd.year = 2024 GROUP BY st.region",
         "aliases and join order"),
        (N, "SELECT s.region, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.year = 2024 AND d.month IN (10, 11, 12) GROUP BY s.region",
         "quarter and month are independent columns; no constraint ties them together"),
        (N, "SELECT s.region, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.year = 2024 OR d.quarter = 4 GROUP BY s.region",
         "OR instead of AND"),
        (E, "SELECT s.region, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE (d.year, d.quarter) = (2024, 4) GROUP BY s.region",
         "row-value comparison"),
    ],
    3: [
        (E, "SELECT p.name, SUM(f.quantity) FROM dim_product p JOIN fact_sales f ON f.product_key = p.product_key JOIN dim_date d ON d.date_key = f.date_key AND d.year = 2023 GROUP BY p.product_key, p.name ORDER BY SUM(f.quantity) DESC LIMIT 5",
         "filter moved into the join condition"),
        (N, "SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.name ORDER BY units DESC LIMIT 5",
         "products with the same name are merged"),
        (N, "SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.product_key, p.name ORDER BY SUM(f.quantity * f.unit_price) DESC LIMIT 5",
         "ranked by revenue instead of units"),
        (N, "SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.product_key, p.name LIMIT 5",
         "LIMIT without ORDER BY returns 5 arbitrary products"),
        (E, "SELECT p.name, SUM(f.quantity) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.product_key, p.name ORDER BY SUM(f.quantity) DESC LIMIT 5",
         "ORDER BY the aggregate instead of its alias"),
        (E, "SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.product_key, p.name ORDER BY units DESC NULLS LAST LIMIT 5",
         "SUM of a NOT NULL column over a non-empty group is never NULL"),
        (N, "SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.product_key, p.name ORDER BY units DESC FETCH FIRST 5 ROWS WITH TIES",
         "WITH TIES returns extra rows when fifth place is tied"),
    ],
    4: [
        (E, "SELECT d.is_weekend, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d USING (date_key) GROUP BY 1",
         "USING join, ordinal GROUP BY"),
        (N, "SELECT CASE WHEN d.is_weekend = 1 THEN 'weekend' ELSE 'weekday' END, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY 1",
         "text labels instead of the flag values"),
        (N, "SELECT d.day_of_week, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.day_of_week",
         "per weekday, not weekend vs weekday"),
        (E, "SELECT d.is_weekend, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY GROUPING SETS ((d.is_weekend))",
         "a single grouping set is a plain GROUP BY"),
        (N, "SELECT d.is_weekend, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY ROLLUP (d.is_weekend)",
         "ROLLUP adds a grand-total row"),
        (E, "SELECT x.is_weekend, SUM(x.rev) FROM (SELECT d.is_weekend, f.quantity * f.unit_price AS rev FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key) x GROUP BY x.is_weekend",
         "derived table computes the line amount"),
        (N, "SELECT d.is_weekend, SUM(f.quantity * f.unit_price) AS revenue FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.is_weekend HAVING revenue > 0",
         "INVALID: an output alias cannot be used in HAVING"),
    ],
    5: [
        (E, "SELECT SUM(quantity * unit_price) - COALESCE(SUM(discount_amount), 0) FROM fact_sales",
         "linearity of SUM; both are NULL on an empty table"),
        (N, "SELECT SUM(quantity * unit_price - discount_amount) FROM fact_sales",
         "rows with a NULL discount drop out of the sum entirely"),
        (N, "SELECT SUM(quantity * (unit_price - COALESCE(discount_amount, 0))) FROM fact_sales",
         "applies the discount per unit instead of per sale"),
        (N, "SELECT SUM(quantity * unit_price) - SUM(discount_amount) FROM fact_sales",
         "if no sale has a discount, SUM(discount_amount) is NULL and so is the result"),
        (E, "SELECT SUM(quantity * unit_price - CASE WHEN discount_amount IS NULL THEN 0 ELSE discount_amount END) FROM fact_sales",
         "CASE spelled-out COALESCE"),
    ],
    6: [
        (E, "SELECT COUNT(*) - COUNT(customer_key) FROM fact_sales",
         "all rows minus rows with a customer"),
        (N, "SELECT COUNT(customer_key) FROM fact_sales WHERE customer_key IS NULL",
         "COUNT(col) skips the NULLs being counted: always 0"),
        (N, "SELECT SUM(CASE WHEN customer_key IS NULL THEN 1 ELSE 0 END) FROM fact_sales",
         "returns NULL instead of 0 on an empty fact table"),
        (E, "SELECT COUNT(*) FROM fact_sales WHERE customer_key IS NULL LIMIT 1",
         "an aggregate without GROUP BY returns exactly one row, so LIMIT 1 changes nothing"),
        (E, "SELECT COUNT(*) FILTER (WHERE customer_key IS NULL) FROM fact_sales",
         "FILTER instead of WHERE; both return 0 on an empty table"),
        (E, "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_customer c ON c.customer_key = f.customer_key WHERE c.name IS NULL",
         "a non-NULL customer_key always finds its customer (FK) and name is NOT NULL, so c.name is NULL exactly for anonymous sales"),
    ],
    7: [
        (E, "SELECT DISTINCT c.name FROM dim_customer c WHERE EXISTS (SELECT 1 FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key WHERE f.customer_key = c.customer_key AND s.city <> c.city)",
         "EXISTS form, still DISTINCT on name"),
        (N, "SELECT c.name FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key JOIN dim_store s ON s.store_key = f.store_key WHERE c.city <> s.city",
         "missing DISTINCT"),
        (N, "SELECT DISTINCT c.name FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key JOIN dim_store s ON s.store_key = f.store_key WHERE c.city IS DISTINCT FROM s.city",
         "IS DISTINCT FROM also matches customers with a NULL city"),
        (E, "SELECT DISTINCT c.name FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key JOIN dim_store s ON s.store_key = f.store_key WHERE NOT (c.city = s.city)",
         "NOT (a = b) is a <> b, NULL included"),
        (N, "SELECT DISTINCT c.name FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key JOIN dim_store s ON s.store_key = f.store_key WHERE c.city <> s.city OR c.city IS NULL",
         "also returns customers with no city on file"),
        (N, "SELECT DISTINCT c.name FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key JOIN dim_store s ON s.store_key = f.store_key WHERE lower(c.city) <> lower(s.city)",
         "cities that differ only in case no longer count as different"),
    ],
    8: [
        (E, "SELECT d.month, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE p.category = 'Electronics' AND d.year = 2024 GROUP BY 1 ORDER BY 1",
         "ordinals"),
        (N, "SELECT d.month, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_product p ON p.product_key = f.product_key WHERE d.year = 2024 AND p.category ILIKE 'electronics' GROUP BY d.month ORDER BY d.month",
         "case-insensitive category match"),
        (N, "SELECT d.month, SUM(f.quantity * f.unit_price) AS r FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_product p ON p.product_key = f.product_key WHERE d.year = 2024 AND p.category = 'Electronics' GROUP BY d.month ORDER BY r DESC",
         "ordered by revenue instead of month"),
        (N, "SELECT d.month, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_product p ON p.product_key = f.product_key WHERE d.full_date >= DATE '2024-01-01' AND d.full_date < DATE '2025-01-01' AND p.category = 'Electronics' GROUP BY d.month ORDER BY d.month",
         "full_date and year are independent columns; no constraint ties them together"),
        (E, "SELECT d.month, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 AND f.product_key IN (SELECT product_key FROM dim_product WHERE category = 'Electronics') GROUP BY d.month ORDER BY d.month",
         "semi-join against the product dimension"),
    ],
    9: [
        (E, "SELECT s.name, AVG(f.quantity) FROM dim_store s JOIN fact_sales f USING (store_key) GROUP BY s.store_key, s.name",
         "USING join"),
        (N, "SELECT s.name, AVG(f.quantity) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY s.name",
         "stores sharing a name are merged"),
        (N, "SELECT s.name, SUM(f.quantity) / COUNT(*) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY s.store_key, s.name",
         "integer division truncates the average"),
        (N, "SELECT s.name, AVG(f.quantity)::int FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY s.store_key, s.name",
         "rounds the average to an integer"),
        (N, "SELECT s.name, AVG(f.quantity) AS avg_qty FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY f.store_key",
         "INVALID: s.name must appear in GROUP BY; f.store_key is not the dim_store primary key"),
    ],
    10: [
        (E, "SELECT p.category, SUM(f.is_promo) * 100.0 / COUNT(*) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "multiplication commuted, still numeric before the division"),
        (N, "SELECT p.category, 100 * SUM(f.is_promo) / COUNT(*) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "integer division"),
        (N, "SELECT p.category, 100.0 * COUNT(*) FILTER (WHERE f.is_promo = 1) / COUNT(*) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "CHECK constraints are not part of the equivalence definition: a value outside the CHECK list would make these differ"),
        (N, "SELECT p.category, 100.0 * SUM(f.is_promo = 1) / COUNT(*) AS promo_pct FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "INVALID: sum(boolean) does not exist in PostgreSQL"),
    ],
    11: [
        (E, "SELECT sku FROM dim_product WHERE product_key NOT IN (SELECT product_key FROM fact_sales)",
         "NOT IN is safe: fact_sales.product_key is NOT NULL"),
        (E, "SELECT p.sku FROM dim_product p LEFT JOIN fact_sales f ON f.product_key = p.product_key WHERE f.sales_key IS NULL",
         "LEFT JOIN anti-join on the fact PK"),
        (N, "SELECT sku FROM dim_product WHERE is_discontinued = 1",
         "discontinued is not the same as never sold"),
        (E, "SELECT sku FROM dim_product EXCEPT SELECT p.sku FROM dim_product p JOIN fact_sales f ON f.product_key = p.product_key",
         "EXCEPT is safe: sku is UNIQUE NOT NULL"),
        (N, "SELECT sku FROM dim_product WHERE product_key NOT IN (SELECT product_key FROM fact_sales WHERE is_return = 0)",
         "products that only appear in returns count as never sold"),
    ],
    12: [
        (E, "SELECT c.is_loyalty_member, AVG(f.unit_price * f.quantity) FROM dim_customer c JOIN fact_sales f ON c.customer_key = f.customer_key GROUP BY c.is_loyalty_member",
         "join order"),
        (N, "SELECT c.is_loyalty_member, AVG(f.quantity * f.unit_price) FROM fact_sales f LEFT JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.is_loyalty_member",
         "anonymous sales form an extra NULL group"),
        (N, "SELECT c.is_loyalty_member, AVG(t.spend) FROM dim_customer c JOIN (SELECT customer_key, SUM(quantity * unit_price) AS spend FROM fact_sales GROUP BY customer_key) t ON t.customer_key = c.customer_key GROUP BY c.is_loyalty_member",
         "average per customer instead of per sale"),
        (E, "SELECT c.is_loyalty_member, AVG(f.quantity * f.unit_price) FROM fact_sales f LEFT JOIN dim_customer c ON c.customer_key = f.customer_key WHERE f.customer_key IS NOT NULL GROUP BY c.is_loyalty_member",
         "once anonymous sales are dropped, every sale finds its customer through the FK, so the LEFT JOIN is an inner join"),
        (N, "SELECT c.is_loyalty_member, AVG(f.quantity * f.unit_price) AS avg_sale FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key WHERE c.is_loyalty_member GROUP BY c.is_loyalty_member",
         "INVALID: argument of WHERE must be boolean, not smallint"),
    ],
    13: [
        (E, """SELECT s.name, SUM(f.quantity * f.unit_price)
FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key
WHERE d.year = 2024
GROUP BY s.store_key, s.name
HAVING SUM(f.quantity * f.unit_price) > (
    SELECT AVG(t.rev) FROM (
        SELECT SUM(f2.quantity * f2.unit_price) AS rev
        FROM fact_sales f2 JOIN dim_date d2 ON d2.date_key = f2.date_key
        WHERE d2.year = 2024 GROUP BY f2.store_key) t)""",
         "HAVING against an inline average-of-store-totals"),
        (N, """WITH store_rev AS (
    SELECT f.store_key, SUM(f.quantity * f.unit_price) AS revenue
    FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key
    WHERE d.year = 2024 GROUP BY f.store_key)
SELECT s.name, r.revenue FROM store_rev r JOIN dim_store s ON s.store_key = r.store_key
WHERE r.revenue > (SELECT AVG(quantity * unit_price) FROM fact_sales)""",
         "compares to the average sale, not the average store total"),
        (N, """WITH store_rev AS (
    SELECT f.store_key, SUM(f.quantity * f.unit_price) AS revenue
    FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key
    WHERE d.year = 2024 GROUP BY f.store_key)
SELECT s.name, r.revenue FROM store_rev r JOIN dim_store s ON s.store_key = r.store_key
WHERE r.revenue >= (SELECT AVG(revenue) FROM store_rev)""",
         ">= includes stores exactly at the average"),
        (E, """WITH store_rev AS (
    SELECT f.store_key, SUM(f.quantity * f.unit_price) AS revenue
    FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key
    WHERE d.year = 2024 GROUP BY f.store_key)
SELECT s.name, r.revenue FROM (SELECT store_key, revenue, AVG(revenue) OVER () AS avg_rev FROM store_rev) r JOIN dim_store s ON s.store_key = r.store_key
WHERE r.revenue > r.avg_rev""",
         "average over a window instead of a scalar subquery"),
        (N, """WITH store_rev AS (
    SELECT f.store_key, SUM(f.quantity * f.unit_price) AS revenue
    FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key
    WHERE d.year = 2024 GROUP BY f.store_key)
SELECT s.name, r.revenue FROM store_rev r JOIN dim_store s ON s.store_key = r.store_key
WHERE r.revenue > (SELECT AVG(COALESCE(r2.revenue, 0)) FROM dim_store s2 LEFT JOIN store_rev r2 ON r2.store_key = s2.store_key)""",
         "stores without 2024 sales count as 0 and pull the average down"),
    ],
    14: [
        (E, """WITH y AS (
    SELECT d.year, SUM(f.quantity * f.unit_price) AS rev
    FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year)
SELECT year, rev, rev - LAG(rev) OVER (ORDER BY year) FROM y ORDER BY year""",
         "CTE then window"),
        (N, """SELECT d.year, SUM(f.quantity * f.unit_price),
       SUM(f.quantity * f.unit_price) - LAG(SUM(f.quantity * f.unit_price)) OVER (ORDER BY d.year DESC)
FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year ORDER BY d.year""",
         "window ordered descending: compares to the following year"),
        (N, """SELECT d.year, SUM(f.quantity * f.unit_price),
       SUM(f.quantity * f.unit_price) - LAG(SUM(f.quantity * f.unit_price)) OVER (ORDER BY d.year)
FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year""",
         "target is ordered; prediction is not"),
        (E, """WITH y AS (
    SELECT d.year, SUM(f.quantity * f.unit_price) AS rev
    FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year)
SELECT year, rev, rev - LAG(rev, 1) OVER (ORDER BY year) FROM y ORDER BY year""",
         "explicit LAG offset of 1"),
        (N, """WITH y AS (
    SELECT d.year, SUM(f.quantity * f.unit_price) AS rev
    FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year)
SELECT year, rev, rev - LAG(rev, 1, 0) OVER (ORDER BY year) FROM y ORDER BY year""",
         "default 0: the first year's change is its full revenue instead of NULL"),
        (N, "SELECT d.year, SUM(f.quantity * f.unit_price) AS revenue, revenue - LAG(revenue) OVER (ORDER BY d.year) AS change FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.year ORDER BY d.year",
         "INVALID: an output alias is not visible in the same select list (column revenue does not exist)"),
    ],
    15: [
        (E, "SELECT s.country, COUNT(DISTINCT c.customer_key) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key LEFT JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY s.country",
         "NULL customers are not counted either way"),
        (N, "SELECT s.country, COUNT(f.customer_key) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY s.country",
         "counts sales with a customer, not distinct customers"),
        (N, "SELECT c.country, COUNT(DISTINCT f.customer_key) FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.country",
         "generic column: customer's country instead of store's country"),
        (E, "SELECT t.country, COUNT(t.customer_key) FROM (SELECT DISTINCT s.country, f.customer_key FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key) t GROUP BY t.country",
         "count over distinct (country, customer) pairs; the NULL customer is not counted"),
        (N, "SELECT t.country, COUNT(*) FROM (SELECT DISTINCT s.country, f.customer_key FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key) t GROUP BY t.country",
         "anonymous sales count as one extra customer"),
        (E, "SELECT country, COUNT(DISTINCT customer_key) AS customers FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY country",
         "country exists only in dim_store and customer_key only in fact_sales, so neither needs a qualifier"),
        (N, "SELECT s.country, COUNT(DISTINCT customer_key) AS customers FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY s.country",
         "INVALID: customer_key is ambiguous once dim_customer is joined"),
    ],
    16: [
        (E, "SELECT p.brand, AVG(f.is_return) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.brand",
         "AVG over SMALLINT already returns numeric; the cast is redundant"),
        (N, "SELECT p.brand, SUM(f.is_return) / COUNT(*) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.brand",
         "integer division"),
        (N, "SELECT p.brand, AVG(f.is_return::numeric) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key WHERE p.brand IS NOT NULL GROUP BY p.brand",
         "drops the no-brand group"),
        (N, "SELECT COALESCE(p.brand, 'Unknown'), AVG(f.is_return::numeric) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY 1",
         "labels the no-brand group 'Unknown' and merges it with a real brand of that name"),
        (N, "SELECT p.brand, AVG(f.is_return = 1) AS return_rate FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.brand",
         "INVALID: avg(boolean) does not exist in PostgreSQL"),
    ],
    17: [
        (E, "SELECT COUNT(*) FROM fact_sales WHERE date_key IN (SELECT date_key FROM dim_date WHERE is_holiday = 1) AND store_key IN (SELECT store_key FROM dim_store WHERE is_flagship = 1)",
         "semi-joins against dimension keys"),
        (N, "SELECT COUNT(*) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.is_holiday = 1 OR s.is_flagship = 1",
         "OR instead of AND"),
        (N, "SELECT COUNT(DISTINCT f.date_key) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.is_holiday = 1 AND s.is_flagship = 1",
         "counts days, not sales"),
        (N, "SELECT SUM(1) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.is_holiday = 1 AND s.is_flagship = 1",
         "SUM(1) is NULL instead of 0 when there are no such sales"),
        (N, "SELECT COUNT(*) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.is_holiday = 1 AND s.is_flagship",
         "INVALID: argument of AND must be boolean, not smallint"),
    ],
    18: [
        (E, """WITH t AS (SELECT customer_key, SUM(quantity * unit_price) AS total FROM fact_sales GROUP BY customer_key)
SELECT c.name, t.total FROM t JOIN dim_customer c ON c.customer_key = t.customer_key WHERE t.total > 10000 ORDER BY t.total DESC""",
         "pre-aggregate in a CTE; the NULL-customer group is dropped by the join"),
        (N, "SELECT c.name, SUM(f.quantity * f.unit_price) AS spend FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key WHERE f.quantity * f.unit_price > 10000 GROUP BY c.customer_key, c.name ORDER BY spend DESC",
         "filters individual sales instead of totals"),
        (N, "SELECT c.name, SUM(f.quantity * f.unit_price) AS spend FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.name HAVING SUM(f.quantity * f.unit_price) > 10000 ORDER BY spend DESC",
         "customers sharing a name are merged"),
        (N, "SELECT c.name, SUM(f.quantity * f.unit_price) AS spend FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.customer_key, c.name HAVING SUM(f.quantity * f.unit_price) >= 10000 ORDER BY spend DESC",
         ">= includes customers who spent exactly 10000"),
        (E, "SELECT c.name, SUM(f.quantity * f.unit_price) AS spend FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.customer_key, c.name HAVING SUM(f.quantity * f.unit_price) > 1e4 ORDER BY spend DESC",
         "1e4 is 10000"),
    ],
    19: [
        (E, "SELECT p.category, SUM(f.quantity * f.unit_price) - SUM(f.quantity * p.unit_cost) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key WHERE p.unit_cost IS NOT NULL GROUP BY p.category",
         "difference of sums; all terms non-NULL after the filter"),
        (N, "SELECT p.category, SUM(f.quantity * (f.unit_price - p.unit_cost)) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "without the filter, categories whose products all lack a cost appear with NULL"),
        (N, "SELECT p.category, SUM(f.unit_price - p.unit_cost) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key WHERE p.unit_cost IS NOT NULL GROUP BY p.category",
         "ignores quantity"),
        (E, "SELECT p.category, SUM(f.quantity * f.unit_price - f.quantity * p.unit_cost) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key WHERE p.unit_cost IS NOT NULL GROUP BY p.category",
         "multiplication distributed over the difference"),
        (N, "SELECT p.category, SUM(f.quantity * (f.unit_price - COALESCE(p.unit_cost, 0))) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "products without a cost count as pure margin"),
    ],
    20: [
        (E, "SELECT DISTINCT s.name FROM dim_store s WHERE EXISTS (SELECT 1 FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE f.store_key = s.store_key AND p.is_discontinued = 1 AND d.year = 2024)",
         "EXISTS with DISTINCT"),
        (N, "SELECT s.name FROM dim_store s WHERE EXISTS (SELECT 1 FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE f.store_key = s.store_key AND p.is_discontinued = 1 AND d.year = 2024)",
         "without DISTINCT, two stores with the same name produce the name twice"),
        (N, "SELECT DISTINCT s.name FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE p.is_discontinued = 0 AND d.year = 2024",
         "active products instead of discontinued"),
        (E, "SELECT s.name FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE p.is_discontinued = 1 AND d.year = 2024 GROUP BY s.name",
         "GROUP BY name is DISTINCT name"),
        (N, "SELECT s.name FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE p.is_discontinued = 1 AND d.year = 2024 GROUP BY s.store_key, s.name",
         "one row per store, so a name shared by several stores repeats"),
    ],
    21: [
        (E, "SELECT name FROM dim_customer WHERE customer_key NOT IN (SELECT customer_key FROM fact_sales WHERE customer_key IS NOT NULL)",
         "NOT IN with NULLs filtered out"),
        (E, "SELECT c.name FROM dim_customer c LEFT JOIN fact_sales f ON f.customer_key = c.customer_key WHERE f.sales_key IS NULL",
         "LEFT JOIN anti-join on the sales primary key"),
        (N, "SELECT name FROM dim_customer WHERE customer_key NOT IN (SELECT customer_key FROM fact_sales)",
         "NOT IN over a nullable column: one anonymous sale makes the result empty"),
        (N, "SELECT name FROM dim_customer EXCEPT SELECT c.name FROM dim_customer c JOIN fact_sales f ON f.customer_key = c.customer_key",
         "EXCEPT on names: customer name is not unique, and duplicates are removed"),
        (N, "SELECT c.name FROM dim_customer c LEFT JOIN fact_sales f ON f.customer_key = c.customer_key WHERE f.customer_key = NULL",
         "= NULL is never true"),
        (E, "SELECT name FROM dim_customer WHERE customer_key <> ALL (SELECT customer_key FROM fact_sales WHERE customer_key IS NOT NULL)",
         "<> ALL is NOT IN; NULLs are filtered out"),
    ],
    22: [
        (N, "SELECT f.product_key FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 UNION ALL SELECT product_key FROM dim_product WHERE is_discontinued = 1",
         "UNION ALL returns one row per 2024 sale and repeats discontinued products that also sold"),
        (E, "SELECT p.product_key FROM dim_product p WHERE p.is_discontinued = 1 OR EXISTS (SELECT 1 FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE f.product_key = p.product_key AND d.year = 2024)",
         "fact_sales.product_key is a NOT NULL FK to the product primary key"),
        (N, "SELECT f.product_key FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 INTERSECT SELECT product_key FROM dim_product WHERE is_discontinued = 1",
         "INTERSECT: only discontinued products sold in 2024"),
        (N, "SELECT f.product_key FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year >= 2024 UNION SELECT product_key FROM dim_product WHERE is_discontinued = 1",
         "includes sales after 2024"),
        (N, "SELECT f.product_key FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 UNION SELECT sku FROM dim_product WHERE is_discontinued = 1",
         "INVALID: UNION types integer and character varying cannot be matched"),
    ],
    23: [
        (E, "SELECT COUNT(*) FILTER (WHERE discount_amount IS NOT NULL), COUNT(*) FROM fact_sales",
         "COUNT(col) counts the non-NULL values"),
        (E, "SELECT COUNT(discount_amount), COUNT(sales_key) FROM fact_sales",
         "COUNT of the primary key equals COUNT(*)"),
        (N, "SELECT SUM(CASE WHEN discount_amount IS NOT NULL THEN 1 ELSE 0 END), COUNT(*) FROM fact_sales",
         "on an empty fact table SUM returns NULL where COUNT returns 0"),
        (N, "SELECT COUNT(*), COUNT(*) FROM fact_sales",
         "COUNT(*) counts sales without a discount too"),
        (N, "SELECT COUNT(*), COUNT(discount_amount) FROM fact_sales",
         "columns swapped"),
        (N, "SELECT COUNT(*), (SELECT COUNT(*) FROM fact_sales) FROM fact_sales WHERE discount_amount > 0",
         "a recorded discount of 0 is not counted"),
        (N, "SELECT SUM(CASE WHEN discount_amount IS NOT NULL THEN 1 ELSE 0 END) AS discounted, COUNT(*) AS total FROM fact_sales",
         "on an empty table SUM returns NULL where COUNT returns 0"),
        (E, "SELECT COUNT(*) FILTER (WHERE discount_amount IS NOT NULL) AS discounted, COUNT(*) AS total FROM fact_sales",
         "a filtered COUNT(*) counts the rows with a discount"),
    ],
    24: [
        (E, "SELECT d.month, SUM(f.quantity * f.unit_price), SUM(SUM(f.quantity * f.unit_price)) OVER (ORDER BY d.month) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 GROUP BY d.month ORDER BY d.month",
         "the default RANGE frame equals ROWS here: each month is one row, so no row has peers"),
        (E, "SELECT d.month, SUM(f.quantity * f.unit_price), SUM(SUM(f.quantity * f.unit_price)) OVER (ORDER BY d.month ROWS UNBOUNDED PRECEDING) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 GROUP BY d.month ORDER BY d.month",
         "a frame with only a start ends at CURRENT ROW"),
        (N, "SELECT d.month, SUM(f.quantity * f.unit_price), SUM(SUM(f.quantity * f.unit_price)) OVER () FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 GROUP BY d.month ORDER BY d.month",
         "the full-year total on every row"),
        (N, "SELECT d.month, SUM(f.quantity * f.unit_price), SUM(SUM(f.quantity * f.unit_price)) OVER (ORDER BY d.month ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 GROUP BY d.month ORDER BY d.month",
         "two-month sum instead of year to date"),
    ],
    25: [
        (E, """SELECT store, product, units FROM (
    SELECT s.name AS store, p.name AS product, SUM(f.quantity) AS units,
           ROW_NUMBER() OVER (PARTITION BY s.store_key ORDER BY SUM(f.quantity) DESC, p.product_key) AS rn
    FROM fact_sales f
    JOIN dim_store s ON s.store_key = f.store_key
    JOIN dim_product p ON p.product_key = f.product_key
    GROUP BY s.store_key, s.name, p.product_key, p.name) x
WHERE rn <= 3""",
         "ROW_NUMBER per store; stores without sales are dropped by both"),
        (E, """SELECT store, product, units FROM (
    SELECT s.name AS store, p.name AS product, SUM(f.quantity) AS units,
           RANK() OVER (PARTITION BY s.store_key ORDER BY SUM(f.quantity) DESC, p.product_key) AS rn
    FROM fact_sales f
    JOIN dim_store s ON s.store_key = f.store_key
    JOIN dim_product p ON p.product_key = f.product_key
    GROUP BY s.store_key, s.name, p.product_key, p.name) x
WHERE rn <= 3""",
         "RANK equals ROW_NUMBER: product_key is unique within a store, so there are no ties"),
        (N, """SELECT s.name, t.name, t.units FROM dim_store s
CROSS JOIN LATERAL (
    SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key
    WHERE f.store_key = s.store_key GROUP BY p.product_key, p.name ORDER BY units DESC LIMIT 3) t""",
         "missing tie-break: ties at third place can select different products"),
        (N, """SELECT s.name, t.name, t.units FROM dim_store s
LEFT JOIN LATERAL (
    SELECT p.product_key, p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key
    WHERE f.store_key = s.store_key GROUP BY p.product_key, p.name ORDER BY units DESC, p.product_key LIMIT 3) t ON TRUE""",
         "LEFT JOIN LATERAL keeps stores without sales, with NULLs"),
    ],
    26: [
        (N, "SELECT p.category, mode() WITHIN GROUP (ORDER BY f.quantity DESC) AS common_qty FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "mode picks the first of the equally common values in sort order: DESC takes the largest"),
        (E, "SELECT DISTINCT ON (p.category) p.category, f.quantity AS common_qty FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category, f.quantity ORDER BY p.category, COUNT(*) DESC, f.quantity",
         "count each quantity per category and keep the most frequent, smallest first"),
        (N, "SELECT p.category, MAX(f.quantity) AS common_qty FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "the largest quantity, not the most common"),
        (N, "SELECT p.category, percentile_disc(0.5) WITHIN GROUP (ORDER BY f.quantity) AS common_qty FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "the median, not the mode"),
        (E, "SELECT p.category, mode() WITHIN GROUP (ORDER BY f.quantity ASC) AS common_qty FROM dim_product p JOIN fact_sales f ON f.product_key = p.product_key GROUP BY p.category",
         "join order and an explicit ASC"),
    ],
    27: [
        (E, "SELECT f.product_key, COALESCE(SUM(f.quantity) FILTER (WHERE d.year = 2023), 0) AS units_2023, COALESCE(SUM(f.quantity) FILTER (WHERE d.year = 2024), 0) AS units_2024 FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year IN (2023, 2024) GROUP BY f.product_key",
         "conditional aggregation over the two years gives one row per product sold in either"),
        (N, "SELECT f.product_key, COALESCE(SUM(f.quantity) FILTER (WHERE d.year = 2023), 0) AS units_2023, COALESCE(SUM(f.quantity) FILTER (WHERE d.year = 2024), 0) AS units_2024 FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY f.product_key",
         "without the year filter, products sold only in other years appear with 0 and 0"),
        (N, "SELECT a.product_key, COALESCE(a.units, 0) AS units_2023, COALESCE(b.units, 0) AS units_2024 FROM (SELECT f.product_key, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY f.product_key) a LEFT JOIN (SELECT f.product_key, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 GROUP BY f.product_key) b ON b.product_key = a.product_key",
         "a LEFT JOIN drops products sold only in 2024"),
        (N, "SELECT COALESCE(a.product_key, b.product_key) AS product_key, a.units AS units_2023, b.units AS units_2024 FROM (SELECT f.product_key, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY f.product_key) a FULL JOIN (SELECT f.product_key, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 GROUP BY f.product_key) b ON b.product_key = a.product_key",
         "NULL instead of 0 for the missing year"),
        (E, "SELECT product_key, COALESCE(a.units, 0) AS units_2023, COALESCE(b.units, 0) AS units_2024 FROM (SELECT f.product_key, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY f.product_key) a FULL JOIN (SELECT f.product_key, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2024 GROUP BY f.product_key) b USING (product_key)",
         "FULL JOIN USING merges product_key as COALESCE(a.product_key, b.product_key)"),
    ],
}
