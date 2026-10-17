"""Hand-written labeled predictions for sales_snowflake queries. See campus.py for the format."""
E, N = True, False

PAIRS = {
    1: [
        (E, "select cat.name, sum(f.quantity * f.unitprice) from factorderline f join dimproduct p on p.productid = f.productid join dimsubcategory sc on sc.subcategoryid = p.subcategoryid join dimcategory cat on cat.categoryid = sc.categoryid group by cat.name",
         "all-lowercase identifiers fold identically; category Name is UNIQUE NOT NULL"),
        (N, 'SELECT cat.Name, SUM(f.Quantity * f.UnitPrice) FROM "FactOrderLine" f JOIN DimProduct p ON p.ProductId = f.ProductId JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId GROUP BY cat.CategoryId, cat.Name',
         "INVALID: quoted \"FactOrderLine\" is case-sensitive; the table is factorderline"),
        (N, "SELECT cat.Name, SUM(f.Quantity * f.UnitPrice) FROM FactOrderLine f JOIN DimProduct p ON p.ProductId = f.ProductId JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId GROUP BY sc.SubcategoryId, cat.Name",
         "grouped at subcategory grain"),
    ],
    2: [
        (E, "SELECT r.Name, SUM(f.Quantity * f.UnitPrice) FROM DimRegion r JOIN DimCountry co ON co.RegionId = r.RegionId JOIN DimCity ci ON ci.CountryId = co.CountryId JOIN DimStore s ON s.CityId = ci.CityId JOIN FactOrderLine f ON f.StoreId = s.StoreId JOIN DimCalendar d ON d.DateId = f.DateId WHERE d.CalendarYear = 2024 GROUP BY r.Name",
         "joins walked from the other end; region Name is UNIQUE"),
        (N, "SELECT co.Name, SUM(f.Quantity * f.UnitPrice) FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE d.CalendarYear = 2024 GROUP BY co.CountryId, co.Name",
         "stops one hop short: per country, not per region"),
        (N, "SELECT r.Name, SUM(f.Quantity * f.UnitPrice) FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId JOIN DimRegion r ON r.RegionId = co.RegionId WHERE EXTRACT(YEAR FROM d.CalendarDate) = 2024 GROUP BY r.RegionId, r.Name",
         "CalendarYear and CalendarDate are independent columns; no constraint ties them together"),
    ],
    3: [
        (E, "SELECT co.Name, COUNT(s.StoreId) FROM DimCountry co JOIN DimCity ci ON ci.CountryId = co.CountryId JOIN DimStore s ON s.CityId = ci.CityId AND s.Status = 'active' GROUP BY co.Name",
         "filter in ON for an inner join; country Name is UNIQUE"),
        (E, "SELECT co.Name, COUNT(*) FROM DimStore s JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE s.Status <> 'inactive' GROUP BY co.CountryId, co.Name",
         "ActiveFlag has two labels and Status is NOT NULL"),
        (N, "SELECT co.Name, COUNT(*) FROM DimStore s JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE s.Status = 'Active' GROUP BY co.CountryId, co.Name",
         "'Active' is not a valid ActiveFlag label (runtime error)"),
        (N, "SELECT co.Name, COUNT(*) FROM DimStore s JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId GROUP BY co.CountryId, co.Name",
         "missing status filter"),
    ],
    4: [
        (E, "SELECT b.name FROM dimbrand b LEFT JOIN dimcountry c ON c.countryid = b.countryid WHERE c.countryid IS NULL",
         "FK guarantees a non-NULL CountryId always matches"),
        (N, "SELECT Name FROM DimBrand WHERE CountryId = NULL",
         "= NULL is never true"),
    ],
    5: [
        (E, "SELECT ci.Name, COUNT(DISTINCT f.OrderId) FROM DimCity ci JOIN DimStore s USING (CityId) JOIN FactOrderLine f USING (StoreId) GROUP BY ci.CityId, ci.Name",
         "USING joins"),
        (N, "SELECT ci.Name, COUNT(f.OrderId) FROM FactOrderLine f JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId GROUP BY ci.CityId, ci.Name",
         "counts order lines, not orders"),
        (N, "SELECT ci.Name, COUNT(DISTINCT f.OrderId) FROM FactOrderLine f JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId GROUP BY ci.Name",
         "same-named cities in different countries are merged"),
    ],
    6: [
        (E, "SELECT AVG(t) FROM (SELECT SUM(Quantity * UnitPrice) AS t FROM FactOrderLine GROUP BY OrderId) x",
         "average of per-order totals"),
        (N, "SELECT AVG(Quantity * UnitPrice) FROM FactOrderLine",
         "average line value, not order value"),
        (N, "SELECT SUM(Quantity * UnitPrice) / COUNT(*) FROM FactOrderLine",
         "divides by lines instead of distinct orders"),
    ],
    7: [
        (E, "SELECT Name FROM DimCategory EXCEPT SELECT cat.Name FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId JOIN DimProduct p ON p.ProductId = f.ProductId JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId WHERE d.CalendarYear = 2023",
         "EXCEPT is safe: category Name is UNIQUE NOT NULL"),
        (N, """SELECT cat.Name FROM DimCategory cat
LEFT JOIN DimSubcategory sc ON sc.CategoryId = cat.CategoryId
LEFT JOIN DimProduct p ON p.SubcategoryId = sc.SubcategoryId
LEFT JOIN FactOrderLine f ON f.ProductId = p.ProductId
LEFT JOIN DimCalendar d ON d.DateId = f.DateId
WHERE d.CalendarYear = 2023 AND f.OrderId IS NULL""",
         "year filter in WHERE contradicts the anti-join: always empty"),
    ],
    8: [
        (E, "SELECT DISTINCT Name FROM DimProduct p WHERE Status = 'inactive' AND ProductId IN (SELECT f.ProductId FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId WHERE d.CalendarYear = 2024)",
         "semi-join with DISTINCT"),
        (N, "SELECT p.Name FROM DimProduct p WHERE p.Status = 'inactive' AND EXISTS (SELECT 1 FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId WHERE f.ProductId = p.ProductId AND d.CalendarYear = 2024)",
         "no DISTINCT: same-named products repeat the name"),
        (N, "SELECT p.Name FROM FactOrderLine f JOIN DimProduct p ON p.ProductId = f.ProductId JOIN DimCalendar d ON d.DateId = f.DateId WHERE p.Status = 'inactive' AND d.CalendarYear = 2024",
         "one row per order line"),
    ],
    9: [
        (E, """SELECT DISTINCT ON (r.RegionId) r.Name, cat.Name, SUM(f.Quantity * f.UnitPrice)
FROM FactOrderLine f
JOIN DimStore s ON s.StoreId = f.StoreId
JOIN DimCity ci ON ci.CityId = s.CityId
JOIN DimCountry co ON co.CountryId = ci.CountryId
JOIN DimRegion r ON r.RegionId = co.RegionId
JOIN DimProduct p ON p.ProductId = f.ProductId
JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId
JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId
GROUP BY r.RegionId, r.Name, cat.CategoryId, cat.Name
ORDER BY r.RegionId, SUM(f.Quantity * f.UnitPrice) DESC""",
         "DISTINCT ON instead of ROW_NUMBER (target is unordered)"),
        (N, """WITH rc AS (
    SELECT r.Name AS Region, cat.Name AS Category, SUM(f.Quantity * f.UnitPrice) AS Revenue,
           RANK() OVER (PARTITION BY r.RegionId ORDER BY SUM(f.Quantity * f.UnitPrice) DESC) AS rk
    FROM FactOrderLine f
    JOIN DimStore s ON s.StoreId = f.StoreId
    JOIN DimCity ci ON ci.CityId = s.CityId
    JOIN DimCountry co ON co.CountryId = ci.CountryId
    JOIN DimRegion r ON r.RegionId = co.RegionId
    JOIN DimProduct p ON p.ProductId = f.ProductId
    JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId
    JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId
    GROUP BY r.RegionId, r.Name, cat.CategoryId, cat.Name)
SELECT Region, Category, Revenue FROM rc WHERE rk = 1""",
         "RANK returns all tied categories"),
    ],
    10: [
        (E, "select d.calendarmonth, sum(f.quantity * f.unitprice) from factorderline f join dimcalendar d on d.dateid = f.dateid join dimstore s on s.storeid = f.storeid join dimcity ci on ci.cityid = s.cityid join dimcountry co on co.countryid = ci.countryid where co.name = 'Germany' and d.calendaryear = 2024 group by 1 order by 1",
         "lowercase identifiers, ordinals"),
        (N, "SELECT d.CalendarMonth, SUM(f.Quantity * f.UnitPrice) FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE co.IsoCode = 'DE' AND d.CalendarYear = 2024 GROUP BY d.CalendarMonth ORDER BY d.CalendarMonth",
         "IsoCode and Name are independent columns; 'DE' is not guaranteed to be Germany"),
        (N, "SELECT d.CalendarMonth, SUM(f.Quantity * f.UnitPrice) AS r FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE co.Name = 'Germany' AND d.CalendarYear = 2024 GROUP BY d.CalendarMonth ORDER BY r DESC",
         "ordered by revenue"),
    ],
    11: [
        (E, "SELECT c.Name FROM FactOrderLine f JOIN DimCustomer c ON c.CustomerId = f.CustomerId JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId GROUP BY c.CustomerId, c.Name HAVING COUNT(DISTINCT ci.CityId) > 3",
         "extra hop to DimCity: CityId is the same key"),
        (N, "SELECT c.Name FROM FactOrderLine f JOIN DimCustomer c ON c.CustomerId = f.CustomerId GROUP BY c.CustomerId, c.Name HAVING COUNT(DISTINCT f.StoreId) > 3",
         "counts stores, not cities"),
        (N, "SELECT c.Name FROM FactOrderLine f JOIN DimCustomer c ON c.CustomerId = f.CustomerId JOIN DimStore s ON s.StoreId = f.StoreId GROUP BY c.CustomerId, c.Name HAVING COUNT(DISTINCT c.CityId) > 3",
         "uses the customer's home city (always at most 1)"),
    ],
    12: [
        (E, "SELECT SUM(Quantity * UnitPrice) - SUM(Quantity * UnitPrice * COALESCE(DiscountPct, 0) / 100) FROM FactOrderLine",
         "distributed: gross minus discount amount"),
        (N, "SELECT SUM(Quantity * UnitPrice * (1 - DiscountPct / 100)) FROM FactOrderLine",
         "lines without a discount drop out"),
        (N, "SELECT SUM(Quantity * UnitPrice * (1 - COALESCE(DiscountPct, 0))) FROM FactOrderLine",
         "percentage not divided by 100"),
    ],
    13: [
        (E, "SELECT p.Name, (SELECT b.Name FROM DimBrand b WHERE b.BrandId = p.BrandId), (SELECT sc.Name FROM DimSubcategory sc WHERE sc.SubcategoryId = p.SubcategoryId) FROM DimProduct p",
         "scalar subqueries; SubcategoryId is a NOT NULL FK"),
        (N, "SELECT p.Name, b.Name, sc.Name FROM DimProduct p JOIN DimBrand b ON b.BrandId = p.BrandId JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId",
         "inner join drops unbranded products"),
        (N, "SELECT p.Name, sc.Name, b.Name FROM DimProduct p LEFT JOIN DimBrand b ON b.BrandId = p.BrandId JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId",
         "brand and subcategory columns swapped"),
    ],
    14: [
        (E, "SELECT s.Name, ci.Name, co.Name FROM DimStore s JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE s.OpenedOn <= DATE '2019-12-31'",
         "for DATE values, <= Dec 31 equals < Jan 1"),
        (N, "SELECT s.Name, ci.Name, co.Name FROM DimStore s JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE s.OpenedOn < DATE '2020-01-01' OR s.OpenedOn IS NULL",
         "includes stores with unknown opening date"),
        (N, "SELECT s.Name, ci.Name, co.Name FROM DimStore s JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry co ON co.CountryId = ci.CountryId WHERE EXTRACT(YEAR FROM s.OpenedOn) <= 2020",
         "includes stores opened during 2020"),
    ],
    15: [
        (E, "SELECT SUM(f.Quantity) FROM FactOrderLine f JOIN DimProduct p ON p.ProductId = f.ProductId JOIN DimBrand b ON b.BrandId = p.BrandId JOIN DimCountry bc ON bc.CountryId = b.CountryId JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId JOIN DimCountry sc ON sc.CountryId = ci.CountryId WHERE bc.CountryId <> sc.CountryId",
         "extra FK-safe hops to DimCountry"),
        (N, "SELECT SUM(f.Quantity) FROM FactOrderLine f JOIN DimProduct p ON p.ProductId = f.ProductId JOIN DimBrand b ON b.BrandId = p.BrandId JOIN DimStore s ON s.StoreId = f.StoreId JOIN DimCity ci ON ci.CityId = s.CityId WHERE b.CountryId IS DISTINCT FROM ci.CountryId",
         "IS DISTINCT FROM includes brands with no country"),
    ],
    16: [
        (E, "SELECT d.FiscalQuarter, COUNT(*) FROM DimCalendar d JOIN FactOrderLine f ON f.DateId = d.DateId WHERE d.CalendarYear = 2024 AND f.CustomerId IS NULL GROUP BY 1",
         "reordered"),
        (N, "SELECT d.FiscalQuarter, COUNT(f.CustomerId) FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId WHERE f.CustomerId IS NULL AND d.CalendarYear = 2024 GROUP BY d.FiscalQuarter",
         "COUNT of the NULL column is always 0"),
        (N, "SELECT d.FiscalQuarter, COUNT(*) FILTER (WHERE f.CustomerId IS NULL) FROM FactOrderLine f JOIN DimCalendar d ON d.DateId = f.DateId WHERE d.CalendarYear = 2024 GROUP BY d.FiscalQuarter",
         "quarters with sales but no guest checkouts appear with 0"),
    ],
    17: [
        (E, "SELECT COALESCE(loyaltytier, 'none') AS t, COUNT(customerid) FROM dimcustomer GROUP BY t",
         "GROUP BY output alias"),
        (N, "SELECT LoyaltyTier, COUNT(*) FROM DimCustomer GROUP BY LoyaltyTier",
         "NULL instead of 'none'"),
    ],
    18: [
        (E, "SELECT sc.Name, cat.Name FROM DimProduct p JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId GROUP BY sc.SubcategoryId, sc.Name, cat.CategoryId, cat.Name HAVING AVG(p.ListPrice) > 100",
         "extra grouping key is functionally determined"),
        (N, "SELECT sc.Name, cat.Name FROM DimProduct p JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId WHERE p.ListPrice > 100 GROUP BY sc.SubcategoryId, sc.Name, cat.Name",
         "row filter instead of average filter"),
        (N, "SELECT sc.Name, cat.Name FROM DimProduct p JOIN DimSubcategory sc ON sc.SubcategoryId = p.SubcategoryId JOIN DimCategory cat ON cat.CategoryId = sc.CategoryId GROUP BY sc.SubcategoryId, sc.Name, cat.Name HAVING AVG(p.ListPrice) >= 100",
         ">= includes average exactly 100"),
    ],
    19: [
        (E, "SELECT Name, Population FROM DimCity WHERE CityId IN (SELECT CityId FROM DimStore) AND Population IS NOT NULL ORDER BY 2 DESC LIMIT 10",
         "IN semi-join, ordinal ORDER BY"),
        (N, "SELECT ci.Name, ci.Population FROM DimCity ci WHERE EXISTS (SELECT 1 FROM DimStore s WHERE s.CityId = ci.CityId) ORDER BY ci.Population DESC LIMIT 10",
         "PostgreSQL sorts NULLs first in DESC order, so cities with unknown population take the top spots"),
        (N, "SELECT ci.Name, ci.Population FROM DimCity ci WHERE EXISTS (SELECT 1 FROM DimStore s WHERE s.CityId = ci.CityId) ORDER BY ci.Population DESC NULLS LAST LIMIT 10",
         "NULL-population cities still fill the list when fewer than 10 have a population"),
    ],
}
