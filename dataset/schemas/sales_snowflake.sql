-- sales_snowflake: retail data warehouse, snowflake schema
-- Shape:    snowflake (fact table with normalized, multi-hop dimension hierarchies:
--           Product -> Subcategory -> Category, Store -> City -> Country -> Region,
--           Brand -> Country)
-- Naming:   PascalCase, UNquoted -- PostgreSQL folds these to lowercase, so DimProduct,
--           dimproduct and DIMPRODUCT all name the same table
-- Flags:    PostgreSQL ENUM type ActiveFlag ('active'/'inactive')
-- Generic:  Name appears in nearly every dimension

CREATE TYPE ActiveFlag AS ENUM ('active', 'inactive');

CREATE TABLE DimRegion (
    RegionId  INTEGER PRIMARY KEY,
    Name      VARCHAR(50) NOT NULL UNIQUE
);

CREATE TABLE DimCountry (
    CountryId  INTEGER PRIMARY KEY,
    Name       VARCHAR(100) NOT NULL UNIQUE,
    IsoCode    CHAR(2) NOT NULL UNIQUE,
    RegionId   INTEGER NOT NULL REFERENCES DimRegion (RegionId)
);

CREATE TABLE DimCity (
    CityId      INTEGER PRIMARY KEY,
    Name        VARCHAR(100) NOT NULL,
    CountryId   INTEGER NOT NULL REFERENCES DimCountry (CountryId),
    Population  INTEGER                                            -- nullable
);

CREATE TABLE DimStore (
    StoreId   INTEGER PRIMARY KEY,
    Name      VARCHAR(100) NOT NULL,
    CityId    INTEGER NOT NULL REFERENCES DimCity (CityId),
    OpenedOn  DATE,                                                 -- nullable
    Status    ActiveFlag NOT NULL DEFAULT 'active'
);

CREATE TABLE DimCategory (
    CategoryId  INTEGER PRIMARY KEY,
    Name        VARCHAR(50) NOT NULL UNIQUE
);

CREATE TABLE DimSubcategory (
    SubcategoryId  INTEGER PRIMARY KEY,
    Name           VARCHAR(50) NOT NULL,
    CategoryId     INTEGER NOT NULL REFERENCES DimCategory (CategoryId)
);

CREATE TABLE DimBrand (
    BrandId    INTEGER PRIMARY KEY,
    Name       VARCHAR(100) NOT NULL UNIQUE,
    CountryId  INTEGER REFERENCES DimCountry (CountryId)              -- nullable
);

CREATE TABLE DimProduct (
    ProductId      INTEGER PRIMARY KEY,
    Name           VARCHAR(200) NOT NULL,
    SubcategoryId  INTEGER NOT NULL REFERENCES DimSubcategory (SubcategoryId),
    BrandId        INTEGER REFERENCES DimBrand (BrandId),             -- nullable (generic)
    ListPrice      NUMERIC(10, 2) NOT NULL,
    Status         ActiveFlag NOT NULL DEFAULT 'active'
);

CREATE TABLE DimCustomer (
    CustomerId   INTEGER PRIMARY KEY,
    Name         VARCHAR(200) NOT NULL,
    CityId       INTEGER REFERENCES DimCity (CityId),                 -- nullable
    LoyaltyTier  VARCHAR(10),                                        -- nullable
    Status       ActiveFlag NOT NULL DEFAULT 'active'
);

CREATE TABLE DimCalendar (
    DateId         INTEGER PRIMARY KEY,                               -- yyyymmdd
    CalendarDate   DATE NOT NULL UNIQUE,
    CalendarYear   SMALLINT NOT NULL,
    CalendarMonth  SMALLINT NOT NULL,
    FiscalQuarter  SMALLINT NOT NULL
);

CREATE TABLE FactOrderLine (
    OrderId      BIGINT NOT NULL,
    LineNumber   SMALLINT NOT NULL,
    DateId       INTEGER NOT NULL REFERENCES DimCalendar (DateId),
    ProductId    INTEGER NOT NULL REFERENCES DimProduct (ProductId),
    StoreId      INTEGER NOT NULL REFERENCES DimStore (StoreId),
    CustomerId   INTEGER REFERENCES DimCustomer (CustomerId),         -- null = guest checkout
    Quantity     INTEGER NOT NULL,
    UnitPrice    NUMERIC(10, 2) NOT NULL,
    DiscountPct  NUMERIC(5, 2),                                      -- nullable
    PRIMARY KEY (OrderId, LineNumber)
);
