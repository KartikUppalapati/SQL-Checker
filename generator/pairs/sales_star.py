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
    ],
    2: [
        (E, "SELECT st.region, SUM(fs.quantity * fs.unit_price) FROM dim_store st JOIN fact_sales fs ON fs.store_key = st.store_key JOIN dim_date dd ON dd.date_key = fs.date_key WHERE dd.quarter = 4 AND dd.year = 2024 GROUP BY st.region",
         "aliases and join order"),
        (N, "SELECT s.region, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.year = 2024 AND d.month IN (10, 11, 12) GROUP BY s.region",
         "quarter and month are independent columns; no constraint ties them together"),
        (N, "SELECT s.region, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.year = 2024 OR d.quarter = 4 GROUP BY s.region",
         "OR instead of AND"),
    ],
    3: [
        (E, "SELECT p.name, SUM(f.quantity) FROM dim_product p JOIN fact_sales f ON f.product_key = p.product_key JOIN dim_date d ON d.date_key = f.date_key AND d.year = 2023 GROUP BY p.product_key, p.name ORDER BY SUM(f.quantity) DESC LIMIT 5",
         "filter moved into the join condition"),
        (N, "SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.name ORDER BY units DESC LIMIT 5",
         "products with the same name are merged"),
        (N, "SELECT p.name, SUM(f.quantity) AS units FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE d.year = 2023 GROUP BY p.product_key, p.name ORDER BY SUM(f.quantity * f.unit_price) DESC LIMIT 5",
         "ranked by revenue instead of units"),
    ],
    4: [
        (E, "SELECT d.is_weekend, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d USING (date_key) GROUP BY 1",
         "USING join, ordinal GROUP BY"),
        (N, "SELECT CASE WHEN d.is_weekend = 1 THEN 'weekend' ELSE 'weekday' END, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY 1",
         "text labels instead of the flag values"),
        (N, "SELECT d.day_of_week, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key GROUP BY d.day_of_week",
         "per weekday, not weekend vs weekday"),
    ],
    5: [
        (E, "SELECT SUM(quantity * unit_price) - COALESCE(SUM(discount_amount), 0) FROM fact_sales",
         "linearity of SUM; both are NULL on an empty table"),
        (N, "SELECT SUM(quantity * unit_price - discount_amount) FROM fact_sales",
         "rows with a NULL discount drop out of the sum entirely"),
        (N, "SELECT SUM(quantity * (unit_price - COALESCE(discount_amount, 0))) FROM fact_sales",
         "applies the discount per unit instead of per sale"),
    ],
    6: [
        (E, "SELECT COUNT(*) - COUNT(customer_key) FROM fact_sales",
         "all rows minus rows with a customer"),
        (N, "SELECT COUNT(customer_key) FROM fact_sales WHERE customer_key IS NULL",
         "COUNT(col) skips the NULLs being counted: always 0"),
        (N, "SELECT SUM(CASE WHEN customer_key IS NULL THEN 1 ELSE 0 END) FROM fact_sales",
         "returns NULL instead of 0 on an empty fact table"),
    ],
    7: [
        (E, "SELECT DISTINCT c.name FROM dim_customer c WHERE EXISTS (SELECT 1 FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key WHERE f.customer_key = c.customer_key AND s.city <> c.city)",
         "EXISTS form, still DISTINCT on name"),
        (N, "SELECT c.name FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key JOIN dim_store s ON s.store_key = f.store_key WHERE c.city <> s.city",
         "missing DISTINCT"),
        (N, "SELECT DISTINCT c.name FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key JOIN dim_store s ON s.store_key = f.store_key WHERE c.city IS DISTINCT FROM s.city",
         "IS DISTINCT FROM also matches customers with a NULL city"),
    ],
    8: [
        (E, "SELECT d.month, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE p.category = 'Electronics' AND d.year = 2024 GROUP BY 1 ORDER BY 1",
         "ordinals"),
        (N, "SELECT d.month, SUM(f.quantity * f.unit_price) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_product p ON p.product_key = f.product_key WHERE d.year = 2024 AND p.category ILIKE 'electronics' GROUP BY d.month ORDER BY d.month",
         "case-insensitive category match"),
        (N, "SELECT d.month, SUM(f.quantity * f.unit_price) AS r FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_product p ON p.product_key = f.product_key WHERE d.year = 2024 AND p.category = 'Electronics' GROUP BY d.month ORDER BY r DESC",
         "ordered by revenue instead of month"),
    ],
    9: [
        (E, "SELECT s.name, AVG(f.quantity) FROM dim_store s JOIN fact_sales f USING (store_key) GROUP BY s.store_key, s.name",
         "USING join"),
        (N, "SELECT s.name, AVG(f.quantity) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY s.name",
         "stores sharing a name are merged"),
        (N, "SELECT s.name, SUM(f.quantity) / COUNT(*) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY s.store_key, s.name",
         "integer division truncates the average"),
    ],
    10: [
        (E, "SELECT p.category, SUM(f.is_promo) * 100.0 / COUNT(*) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "multiplication commuted, still numeric before the division"),
        (N, "SELECT p.category, 100 * SUM(f.is_promo) / COUNT(*) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "integer division"),
    ],
    11: [
        (E, "SELECT sku FROM dim_product WHERE product_key NOT IN (SELECT product_key FROM fact_sales)",
         "NOT IN is safe: fact_sales.product_key is NOT NULL"),
        (E, "SELECT p.sku FROM dim_product p LEFT JOIN fact_sales f ON f.product_key = p.product_key WHERE f.sales_key IS NULL",
         "LEFT JOIN anti-join on the fact PK"),
        (N, "SELECT sku FROM dim_product WHERE is_discontinued = 1",
         "discontinued is not the same as never sold"),
    ],
    12: [
        (E, "SELECT c.is_loyalty_member, AVG(f.unit_price * f.quantity) FROM dim_customer c JOIN fact_sales f ON c.customer_key = f.customer_key GROUP BY c.is_loyalty_member",
         "join order"),
        (N, "SELECT c.is_loyalty_member, AVG(f.quantity * f.unit_price) FROM fact_sales f LEFT JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.is_loyalty_member",
         "anonymous sales form an extra NULL group"),
        (N, "SELECT c.is_loyalty_member, AVG(t.spend) FROM dim_customer c JOIN (SELECT customer_key, SUM(quantity * unit_price) AS spend FROM fact_sales GROUP BY customer_key) t ON t.customer_key = c.customer_key GROUP BY c.is_loyalty_member",
         "average per customer instead of per sale"),
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
    ],
    15: [
        (E, "SELECT s.country, COUNT(DISTINCT c.customer_key) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key LEFT JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY s.country",
         "NULL customers are not counted either way"),
        (N, "SELECT s.country, COUNT(f.customer_key) FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key GROUP BY s.country",
         "counts sales with a customer, not distinct customers"),
        (N, "SELECT c.country, COUNT(DISTINCT f.customer_key) FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.country",
         "generic column: customer's country instead of store's country"),
    ],
    16: [
        (E, "SELECT p.brand, AVG(f.is_return) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.brand",
         "AVG over SMALLINT already returns numeric; the cast is redundant"),
        (N, "SELECT p.brand, SUM(f.is_return) / COUNT(*) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.brand",
         "integer division"),
        (N, "SELECT p.brand, AVG(f.is_return::numeric) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key WHERE p.brand IS NOT NULL GROUP BY p.brand",
         "drops the no-brand group"),
    ],
    17: [
        (E, "SELECT COUNT(*) FROM fact_sales WHERE date_key IN (SELECT date_key FROM dim_date WHERE is_holiday = 1) AND store_key IN (SELECT store_key FROM dim_store WHERE is_flagship = 1)",
         "semi-joins against dimension keys"),
        (N, "SELECT COUNT(*) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.is_holiday = 1 OR s.is_flagship = 1",
         "OR instead of AND"),
        (N, "SELECT COUNT(DISTINCT f.date_key) FROM fact_sales f JOIN dim_date d ON d.date_key = f.date_key JOIN dim_store s ON s.store_key = f.store_key WHERE d.is_holiday = 1 AND s.is_flagship = 1",
         "counts days, not sales"),
    ],
    18: [
        (E, """WITH t AS (SELECT customer_key, SUM(quantity * unit_price) AS total FROM fact_sales GROUP BY customer_key)
SELECT c.name, t.total FROM t JOIN dim_customer c ON c.customer_key = t.customer_key WHERE t.total > 10000 ORDER BY t.total DESC""",
         "pre-aggregate in a CTE; the NULL-customer group is dropped by the join"),
        (N, "SELECT c.name, SUM(f.quantity * f.unit_price) AS spend FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key WHERE f.quantity * f.unit_price > 10000 GROUP BY c.customer_key, c.name ORDER BY spend DESC",
         "filters individual sales instead of totals"),
        (N, "SELECT c.name, SUM(f.quantity * f.unit_price) AS spend FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key GROUP BY c.name HAVING SUM(f.quantity * f.unit_price) > 10000 ORDER BY spend DESC",
         "customers sharing a name are merged"),
    ],
    19: [
        (E, "SELECT p.category, SUM(f.quantity * f.unit_price) - SUM(f.quantity * p.unit_cost) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key WHERE p.unit_cost IS NOT NULL GROUP BY p.category",
         "difference of sums; all terms non-NULL after the filter"),
        (N, "SELECT p.category, SUM(f.quantity * (f.unit_price - p.unit_cost)) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key GROUP BY p.category",
         "without the filter, categories whose products all lack a cost appear with NULL"),
        (N, "SELECT p.category, SUM(f.unit_price - p.unit_cost) FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key WHERE p.unit_cost IS NOT NULL GROUP BY p.category",
         "ignores quantity"),
    ],
    20: [
        (E, "SELECT DISTINCT s.name FROM dim_store s WHERE EXISTS (SELECT 1 FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE f.store_key = s.store_key AND p.is_discontinued = 1 AND d.year = 2024)",
         "EXISTS with DISTINCT"),
        (N, "SELECT s.name FROM dim_store s WHERE EXISTS (SELECT 1 FROM fact_sales f JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE f.store_key = s.store_key AND p.is_discontinued = 1 AND d.year = 2024)",
         "without DISTINCT, two stores with the same name produce the name twice"),
        (N, "SELECT DISTINCT s.name FROM fact_sales f JOIN dim_store s ON s.store_key = f.store_key JOIN dim_product p ON p.product_key = f.product_key JOIN dim_date d ON d.date_key = f.date_key WHERE p.is_discontinued = 0 AND d.year = 2024",
         "active products instead of discontinued"),
    ],
}
