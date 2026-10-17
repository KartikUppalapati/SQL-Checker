-- sales_star: retail data warehouse, star schema
-- Shape:    star (one fact table, denormalized single-hop dimensions; category/region are
--           plain text columns on the dimensions)
-- Naming:   snake_case, unquoted, fact_/dim_ prefixes, *_key surrogate keys
-- Flags:    SMALLINT 0/1 with CHECK constraints
-- Generic:  name / city / country appear in several dimensions

CREATE TABLE dim_date (
    date_key     INTEGER PRIMARY KEY,                -- yyyymmdd
    full_date    DATE NOT NULL UNIQUE,
    year         SMALLINT NOT NULL,
    quarter      SMALLINT NOT NULL,
    month        SMALLINT NOT NULL,
    month_name   VARCHAR(10) NOT NULL,
    day_of_week  SMALLINT NOT NULL,                  -- 0 = Sunday
    is_weekend   SMALLINT NOT NULL CHECK (is_weekend IN (0, 1)),
    is_holiday   SMALLINT NOT NULL CHECK (is_holiday IN (0, 1))
);

CREATE TABLE dim_product (
    product_key      INTEGER PRIMARY KEY,
    sku              VARCHAR(30) NOT NULL UNIQUE,
    name             VARCHAR(200) NOT NULL,
    category         VARCHAR(50) NOT NULL,
    subcategory      VARCHAR(50),                    -- nullable
    brand            VARCHAR(50),                    -- nullable (store brand / unknown)
    unit_cost        NUMERIC(10, 2),                 -- nullable
    is_discontinued  SMALLINT NOT NULL DEFAULT 0 CHECK (is_discontinued IN (0, 1))
);

CREATE TABLE dim_store (
    store_key    INTEGER PRIMARY KEY,
    name         VARCHAR(100) NOT NULL,
    city         VARCHAR(100) NOT NULL,
    region       VARCHAR(50) NOT NULL,
    country      VARCHAR(50) NOT NULL,
    square_feet  INTEGER,                            -- nullable
    opened_date  DATE,                               -- nullable
    is_flagship  SMALLINT NOT NULL DEFAULT 0 CHECK (is_flagship IN (0, 1))
);

CREATE TABLE dim_customer (
    customer_key       INTEGER PRIMARY KEY,
    name               VARCHAR(200) NOT NULL,
    segment            VARCHAR(20) NOT NULL,         -- 'consumer' / 'business'
    city               VARCHAR(100),                 -- nullable
    country            VARCHAR(50),                  -- nullable
    birth_year         INTEGER,                      -- nullable
    is_loyalty_member  SMALLINT NOT NULL DEFAULT 0 CHECK (is_loyalty_member IN (0, 1))
);

CREATE TABLE fact_sales (
    sales_key        BIGINT PRIMARY KEY,
    date_key         INTEGER NOT NULL REFERENCES dim_date (date_key),
    product_key      INTEGER NOT NULL REFERENCES dim_product (product_key),
    store_key        INTEGER NOT NULL REFERENCES dim_store (store_key),
    customer_key     INTEGER REFERENCES dim_customer (customer_key),   -- null = anonymous sale
    quantity         INTEGER NOT NULL,
    unit_price       NUMERIC(10, 2) NOT NULL,
    discount_amount  NUMERIC(10, 2),                                   -- null = no discount
    is_promo         SMALLINT NOT NULL DEFAULT 0 CHECK (is_promo IN (0, 1)),
    is_return        SMALLINT NOT NULL DEFAULT 0 CHECK (is_return IN (0, 1))
);
