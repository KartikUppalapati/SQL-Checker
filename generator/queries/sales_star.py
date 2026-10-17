SCHEMA_ID = "sales_star"

QUERIES = [
    ("What was total revenue (quantity times unit price) for each year? Order by year.",
     """SELECT d.year, SUM(f.quantity * f.unit_price) AS revenue
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.year
ORDER BY d.year""",
     ["star"]),

    ("What was revenue by store region in Q4 of 2024?",
     """SELECT s.region, SUM(f.quantity * f.unit_price) AS revenue
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
JOIN dim_store s ON s.store_key = f.store_key
WHERE d.year = 2024 AND d.quarter = 4
GROUP BY s.region""",
     ["star"]),

    ("Which 5 products sold the most units in 2023? Show product name and units.",
     """SELECT p.name, SUM(f.quantity) AS units
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
JOIN dim_date d ON d.date_key = f.date_key
WHERE d.year = 2023
GROUP BY p.product_key, p.name
ORDER BY units DESC
LIMIT 5""",
     ["star", "top_k"]),

    ("Compare total revenue on weekends versus weekdays.",
     """SELECT d.is_weekend, SUM(f.quantity * f.unit_price) AS revenue
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.is_weekend""",
     ["star", "flag_int"]),

    ("What is total net revenue after subtracting discounts? Sales without a discount have no deduction.",
     """SELECT SUM(quantity * unit_price - COALESCE(discount_amount, 0)) AS net_revenue
FROM fact_sales""",
     ["nullable"]),

    ("How many sales were made to anonymous (unknown) customers?",
     "SELECT COUNT(*) FROM fact_sales WHERE customer_key IS NULL",
     ["nullable"]),

    ("List the names of customers who made a purchase in a store located in a different city from their own home city.",
     """SELECT DISTINCT c.name
FROM fact_sales f
JOIN dim_customer c ON c.customer_key = f.customer_key
JOIN dim_store s ON s.store_key = f.store_key
WHERE c.city <> s.city""",
     ["star", "generic_names", "nullable"]),

    ("Show monthly revenue for the Electronics category during 2024, ordered by month.",
     """SELECT d.month, SUM(f.quantity * f.unit_price) AS revenue
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
JOIN dim_product p ON p.product_key = f.product_key
WHERE d.year = 2024 AND p.category = 'Electronics'
GROUP BY d.month
ORDER BY d.month""",
     ["star"]),

    ("What is the average quantity per sale at each store? Show store name.",
     """SELECT s.name, AVG(f.quantity) AS avg_qty
FROM fact_sales f
JOIN dim_store s ON s.store_key = f.store_key
GROUP BY s.store_key, s.name""",
     ["star", "generic_names"]),

    ("For each product category, what percentage of sales were promotional?",
     """SELECT p.category, 100.0 * SUM(f.is_promo) / COUNT(*) AS promo_pct
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
GROUP BY p.category""",
     ["star", "flag_int", "integer_division"]),

    ("Which products have never been sold? Return their SKUs.",
     """SELECT p.sku FROM dim_product p
WHERE NOT EXISTS (SELECT 1 FROM fact_sales f WHERE f.product_key = p.product_key)""",
     ["anti_join"]),

    ("Compare average sale amount (quantity times unit price) between loyalty members and non-members. Anonymous sales are excluded.",
     """SELECT c.is_loyalty_member, AVG(f.quantity * f.unit_price) AS avg_sale
FROM fact_sales f
JOIN dim_customer c ON c.customer_key = f.customer_key
GROUP BY c.is_loyalty_member""",
     ["star", "flag_int"]),

    ("Which stores had 2024 revenue above the average 2024 revenue across stores?",
     """WITH store_rev AS (
    SELECT f.store_key, SUM(f.quantity * f.unit_price) AS revenue
    FROM fact_sales f
    JOIN dim_date d ON d.date_key = f.date_key
    WHERE d.year = 2024
    GROUP BY f.store_key
)
SELECT s.name, r.revenue
FROM store_rev r
JOIN dim_store s ON s.store_key = r.store_key
WHERE r.revenue > (SELECT AVG(revenue) FROM store_rev)""",
     ["star", "cte"]),

    ("For each year, show revenue and the change in revenue from the previous year.",
     """SELECT d.year,
       SUM(f.quantity * f.unit_price) AS revenue,
       SUM(f.quantity * f.unit_price) - LAG(SUM(f.quantity * f.unit_price)) OVER (ORDER BY d.year) AS change
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.year
ORDER BY d.year""",
     ["star"]),

    ("How many distinct customers bought in stores of each country?",
     """SELECT s.country, COUNT(DISTINCT f.customer_key) AS customers
FROM fact_sales f
JOIN dim_store s ON s.store_key = f.store_key
GROUP BY s.country""",
     ["star", "generic_names"]),

    ("What fraction of sales are returns for each brand? Products without a brand form their own group.",
     """SELECT p.brand, AVG(f.is_return::numeric) AS return_rate
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
GROUP BY p.brand""",
     ["star", "flag_int", "nullable"]),

    ("How many sales happened on holidays at flagship stores?",
     """SELECT COUNT(*)
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
JOIN dim_store s ON s.store_key = f.store_key
WHERE d.is_holiday = 1 AND s.is_flagship = 1""",
     ["star", "flag_int"]),

    ("Which customers spent more than 10000 in total? Show name and total spend, largest first.",
     """SELECT c.name, SUM(f.quantity * f.unit_price) AS spend
FROM fact_sales f
JOIN dim_customer c ON c.customer_key = f.customer_key
GROUP BY c.customer_key, c.name
HAVING SUM(f.quantity * f.unit_price) > 10000
ORDER BY spend DESC""",
     ["star", "having"]),

    ("What is the gross margin (revenue minus quantity times unit cost) per category, for products with a known unit cost?",
     """SELECT p.category, SUM(f.quantity * (f.unit_price - p.unit_cost)) AS margin
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
WHERE p.unit_cost IS NOT NULL
GROUP BY p.category""",
     ["star", "nullable"]),

    ("List store names that sold discontinued products in 2024.",
     """SELECT DISTINCT s.name
FROM fact_sales f
JOIN dim_store s ON s.store_key = f.store_key
JOIN dim_product p ON p.product_key = f.product_key
JOIN dim_date d ON d.date_key = f.date_key
WHERE p.is_discontinued = 1 AND d.year = 2024""",
     ["star", "flag_int", "generic_names"]),
]
