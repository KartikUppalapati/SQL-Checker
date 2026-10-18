SCHEMA_ID = "sales_snowflake"

# Identifiers are unquoted PascalCase; PostgreSQL folds them to lowercase.
QUERIES = [
    ("What is total revenue (quantity times unit price) by product category name?",
     """SELECT cat.Name, SUM(f.Quantity * f.UnitPrice) AS Revenue
FROM FactOrderLine f
JOIN DimProduct p ON p.ProductId = f.ProductId
JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId
JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId
GROUP BY cat.CategoryId, cat.Name""",
     ["snowflake", "generic_names"]),

    ("What was revenue by sales region in calendar year 2024?",
     """SELECT r.Name, SUM(f.Quantity * f.UnitPrice) AS Revenue
FROM FactOrderLine f
JOIN DimCalendar d ON d.DateId = f.DateId
JOIN DimStore s ON s.StoreId = f.StoreId
JOIN DimCity ci ON ci.CityId = s.CityId
JOIN DimCountry co ON co.CountryId = ci.CountryId
JOIN DimRegion r ON r.RegionId = co.RegionId
WHERE d.CalendarYear = 2024
GROUP BY r.RegionId, r.Name""",
     ["snowflake", "generic_names"]),

    ("How many active stores are there in each country? Show the country name.",
     """SELECT co.Name, COUNT(*) AS Stores
FROM DimStore s
JOIN DimCity ci ON ci.CityId = s.CityId
JOIN DimCountry co ON co.CountryId = ci.CountryId
WHERE s.Status = 'active'
GROUP BY co.CountryId, co.Name""",
     ["snowflake", "flag_enum", "generic_names"]),

    ("Which brands have no country of origin recorded?",
     "SELECT Name FROM DimBrand WHERE CountryId IS NULL",
     ["nullable"]),

    ("How many distinct orders were placed at stores in each city? Show city name.",
     """SELECT ci.Name, COUNT(DISTINCT f.OrderId) AS Orders
FROM FactOrderLine f
JOIN DimStore s ON s.StoreId = f.StoreId
JOIN DimCity ci ON ci.CityId = s.CityId
GROUP BY ci.CityId, ci.Name""",
     ["snowflake", "generic_names"]),

    ("What is the average order value, i.e. total revenue divided by the number of distinct orders?",
     """SELECT SUM(Quantity * UnitPrice) / COUNT(DISTINCT OrderId) AS AvgOrderValue
FROM FactOrderLine""",
     []),

    ("Which product categories had no sales at all in calendar year 2023?",
     """SELECT cat.Name FROM DimCategory cat
WHERE NOT EXISTS (
    SELECT 1
    FROM FactOrderLine f
    JOIN DimCalendar d ON d.DateId = f.DateId
    JOIN DimProduct p ON p.ProductId = f.ProductId
    JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId
    WHERE sc.CategoryId = cat.CategoryId AND d.CalendarYear = 2023
)""",
     ["snowflake", "anti_join"]),

    ("List inactive products that still had sales in 2024.",
     """SELECT DISTINCT p.Name
FROM FactOrderLine f
JOIN DimProduct p ON p.ProductId = f.ProductId
JOIN DimCalendar d ON d.DateId = f.DateId
WHERE p.Status = 'inactive' AND d.CalendarYear = 2024""",
     ["snowflake", "flag_enum"]),

    ("For each region, which product category earned the most revenue? Return region name, category name and revenue.",
     """WITH rc AS (
    SELECT r.Name AS Region, cat.Name AS Category, SUM(f.Quantity * f.UnitPrice) AS Revenue,
           ROW_NUMBER() OVER (PARTITION BY r.RegionId ORDER BY SUM(f.Quantity * f.UnitPrice) DESC) AS rn
    FROM FactOrderLine f
    JOIN DimStore s ON s.StoreId = f.StoreId
    JOIN DimCity ci ON ci.CityId = s.CityId
    JOIN DimCountry co ON co.CountryId = ci.CountryId
    JOIN DimRegion r ON r.RegionId = co.RegionId
    JOIN DimProduct p ON p.ProductId = f.ProductId
    JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId
    JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId
    GROUP BY r.RegionId, r.Name, cat.CategoryId, cat.Name
)
SELECT Region, Category, Revenue FROM rc WHERE rn = 1""",
     ["snowflake", "cte", "generic_names"]),

    ("Show monthly revenue for stores in Germany during 2024, ordered by month.",
     """SELECT d.CalendarMonth, SUM(f.Quantity * f.UnitPrice) AS Revenue
FROM FactOrderLine f
JOIN DimCalendar d ON d.DateId = f.DateId
JOIN DimStore s ON s.StoreId = f.StoreId
JOIN DimCity ci ON ci.CityId = s.CityId
JOIN DimCountry co ON co.CountryId = ci.CountryId
WHERE co.Name = 'Germany' AND d.CalendarYear = 2024
GROUP BY d.CalendarMonth
ORDER BY d.CalendarMonth""",
     ["snowflake"]),

    ("Which customers have bought from stores in more than 3 different cities?",
     """SELECT c.Name
FROM FactOrderLine f
JOIN DimCustomer c ON c.CustomerId = f.CustomerId
JOIN DimStore s ON s.StoreId = f.StoreId
GROUP BY c.CustomerId, c.Name
HAVING COUNT(DISTINCT s.CityId) > 3""",
     ["snowflake", "having"]),

    ("What is total revenue after applying line discounts (DiscountPct is a percentage; missing means no discount)?",
     """SELECT SUM(Quantity * UnitPrice * (1 - COALESCE(DiscountPct, 0) / 100)) AS NetRevenue
FROM FactOrderLine""",
     ["nullable"]),

    ("For each product, show its name, its brand name and its subcategory name. Products without a brand should still appear.",
     """SELECT p.Name, b.Name AS Brand, sc.Name AS Subcategory
FROM DimProduct p
LEFT JOIN DimBrand b ON b.BrandId = p.BrandId
JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId""",
     ["snowflake", "outer_join", "generic_names"]),

    ("List stores opened before 2020 together with their city and country names.",
     """SELECT s.Name AS Store, ci.Name AS City, co.Name AS Country
FROM DimStore s
JOIN DimCity ci ON ci.CityId = s.CityId
JOIN DimCountry co ON co.CountryId = ci.CountryId
WHERE s.OpenedOn < DATE '2020-01-01'""",
     ["snowflake", "generic_names", "dates"]),

    ("How many units were sold of products whose brand comes from a different country than the store where they were sold?",
     """SELECT SUM(f.Quantity)
FROM FactOrderLine f
JOIN DimProduct p ON p.ProductId = f.ProductId
JOIN DimBrand b ON b.BrandId = p.BrandId
JOIN DimStore s ON s.StoreId = f.StoreId
JOIN DimCity ci ON ci.CityId = s.CityId
WHERE b.CountryId <> ci.CountryId""",
     ["snowflake", "nullable"]),

    ("How many order lines were guest checkouts (no customer) in each fiscal quarter of 2024?",
     """SELECT d.FiscalQuarter, COUNT(*)
FROM FactOrderLine f
JOIN DimCalendar d ON d.DateId = f.DateId
WHERE f.CustomerId IS NULL AND d.CalendarYear = 2024
GROUP BY d.FiscalQuarter""",
     ["nullable"]),

    ("How many customers are in each loyalty tier? Customers without a tier count under 'none'.",
     """SELECT COALESCE(LoyaltyTier, 'none') AS Tier, COUNT(*)
FROM DimCustomer
GROUP BY COALESCE(LoyaltyTier, 'none')""",
     ["nullable"]),

    ("Which subcategories have an average list price above 100? Show subcategory and category names.",
     """SELECT sc.Name, cat.Name
FROM DimProduct p
JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId
JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId
GROUP BY sc.SubcategoryId, sc.Name, cat.Name
HAVING AVG(p.ListPrice) > 100""",
     ["snowflake", "having", "generic_names"]),

    ("What are the 10 cities with the largest population that have at least one store? Show city name and population.",
     """SELECT ci.Name, ci.Population
FROM DimCity ci
WHERE ci.Population IS NOT NULL
  AND EXISTS (SELECT 1 FROM DimStore s WHERE s.CityId = ci.CityId)
ORDER BY ci.Population DESC
LIMIT 10""",
     ["top_k", "nullable"]),

    ("Which brands have no products in the catalog?",
     """SELECT b.Name
FROM DimBrand b
WHERE NOT EXISTS (SELECT 1 FROM DimProduct p WHERE p.BrandId = b.BrandId)""",
     ["anti_join", "nullable", "snowflake"]),

    ("List, in one column, the distinct names of every city that has a store and every country that has a store.",
     """SELECT ci.Name
FROM DimStore s
JOIN DimCity ci ON ci.CityId = s.CityId
UNION
SELECT co.Name
FROM DimStore s
JOIN DimCity ci ON ci.CityId = s.CityId
JOIN DimCountry co ON co.CountryId = ci.CountryId""",
     ["snowflake", "generic_names"]),

    ("Show revenue for each calendar year, followed by a grand-total row whose year is NULL.",
     """SELECT d.CalendarYear, SUM(f.Quantity * f.UnitPrice) AS Revenue
FROM FactOrderLine f
JOIN DimCalendar d ON d.DateId = f.DateId
GROUP BY d.CalendarYear
UNION ALL
SELECT NULL, SUM(Quantity * UnitPrice) FROM FactOrderLine""",
     ["snowflake"]),

    ("Count order lines by discount band: 'none' when there is no discount (missing or 0), 'small' below 10 percent, and 'large' at 10 percent or more.",
     """SELECT CASE WHEN COALESCE(DiscountPct, 0) = 0 THEN 'none'
            WHEN DiscountPct < 10 THEN 'small'
            ELSE 'large'
       END AS Band,
       COUNT(*)
FROM FactOrderLine
GROUP BY 1""",
     ['case', 'nullable']),

    ("For each calendar date with sales, show revenue and a centered moving average: the average daily revenue of that day and the two days with sales before and after it. Order by date.",
     """SELECT d.CalendarDate,
       SUM(f.Quantity * f.UnitPrice) AS Revenue,
       AVG(SUM(f.Quantity * f.UnitPrice)) OVER (ORDER BY d.CalendarDate ROWS BETWEEN 2 PRECEDING AND 2 FOLLOWING) AS Centered
FROM FactOrderLine f
JOIN DimCalendar d ON d.DateId = f.DateId
GROUP BY d.CalendarDate
ORDER BY d.CalendarDate""",
     ['window_frame']),
]
