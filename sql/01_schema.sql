-- =============================================================================
-- Investment Portfolio Tracker — warehouse schema
-- Auto-executed by PostgreSQL on FIRST container boot
-- (mounted into /docker-entrypoint-initdb.d/). Runs only when the data volume
-- is empty; to re-run, `docker compose down -v` then `up` again.
-- =============================================================================
-- Two layers:
--   staging : raw landing zone, loaded as-is from APIs + Excel (the "bronze")
--   dwh     : modeled star schema for Power BI (the "gold")
-- Money/quantities use NUMERIC (exact) — never float for financial values.
-- snake_case, unquoted identifiers (Postgres folds to lowercase).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS dwh;


-- #############################################################################
-- STAGING LAYER  — raw, append/replace on each ingest. Minimal constraints.
-- #############################################################################

-- Raw transactions as read from the investment Excel --------------------------
CREATE TABLE staging.transactions (
    transaction_date  DATE,
    ticker            VARCHAR(20),
    action            VARCHAR(10),
    quantity          NUMERIC(18,6),
    price_per_unit    NUMERIC(18,6),
    currency          CHAR(3),
    gross_amount      NUMERIC(18,2),
    notes             VARCHAR(255),
    loaded_at         TIMESTAMPTZ DEFAULT now()
);

-- Raw securities as read from the Excel "securities" sheet --------------------
CREATE TABLE staging.securities (
    ticker            VARCHAR(20),
    name              VARCHAR(120),
    currency          CHAR(3),
    country           CHAR(2),
    sector            VARCHAR(60),
    loaded_at         TIMESTAMPTZ DEFAULT now()
);

-- Raw daily prices from the price API (yfinance) ------------------------------
CREATE TABLE staging.daily_prices (
    price_date        DATE,
    ticker            VARCHAR(20),
    open_price        NUMERIC(18,6),
    high_price        NUMERIC(18,6),
    low_price         NUMERIC(18,6),
    close_price       NUMERIC(18,6),
    volume            BIGINT,
    loaded_at         TIMESTAMPTZ DEFAULT now()
);

-- Raw FX rates from the FX API (Frankfurter) ----------------------------------
CREATE TABLE staging.fx_rates (
    rate_date         DATE,
    from_currency     CHAR(3),
    to_currency       CHAR(3),
    rate              NUMERIC(18,8),
    loaded_at         TIMESTAMPTZ DEFAULT now()
);

-- Raw dividends from the dividend API (yfinance) ------------------------------
CREATE TABLE staging.dividends (
    ex_date              DATE,
    ticker               VARCHAR(20),
    dividend_per_share   NUMERIC(18,6),
    currency             CHAR(3),
    loaded_at            TIMESTAMPTZ DEFAULT now()
);


-- #############################################################################
-- DWH LAYER  — modeled star schema. This is what Power BI connects to.
-- #############################################################################

-- ---- DIMENSIONS -------------------------------------------------------------

CREATE TABLE dwh.dim_date (
    date_key        INTEGER     PRIMARY KEY,          -- YYYYMMDD
    full_date       DATE        NOT NULL UNIQUE,
    year            SMALLINT    NOT NULL,
    quarter         SMALLINT    NOT NULL,
    month           SMALLINT    NOT NULL,
    month_name      VARCHAR(9)  NOT NULL,
    day             SMALLINT    NOT NULL,
    day_of_week     SMALLINT    NOT NULL,             -- 1=Mon..7=Sun
    day_name        VARCHAR(9)  NOT NULL,
    week_of_year    SMALLINT    NOT NULL,
    is_weekend      BOOLEAN     NOT NULL,
    is_trading_day  BOOLEAN     NOT NULL DEFAULT TRUE
);

CREATE TABLE dwh.dim_security (
    security_key    SERIAL       PRIMARY KEY,
    ticker          VARCHAR(20)  NOT NULL UNIQUE,     -- natural key & API symbol e.g. 'TKM1T.TL'
    name            VARCHAR(120) NOT NULL,
    currency        CHAR(3)      NOT NULL,
    country         CHAR(2),
    sector          VARCHAR(60),
    asset_class     VARCHAR(20)  NOT NULL DEFAULT 'EQUITY'
);

-- ---- FACTS ------------------------------------------------------------------

CREATE TABLE dwh.fact_transactions (
    transaction_key   SERIAL        PRIMARY KEY,
    date_key          INTEGER       NOT NULL REFERENCES dwh.dim_date(date_key),
    security_key      INTEGER       NOT NULL REFERENCES dwh.dim_security(security_key),
    action            VARCHAR(4)    NOT NULL CHECK (action IN ('BUY','SELL')),
    quantity          NUMERIC(18,6) NOT NULL CHECK (quantity > 0),
    price_per_unit    NUMERIC(18,6) NOT NULL CHECK (price_per_unit >= 0),
    currency          CHAR(3)       NOT NULL,
    gross_amount      NUMERIC(18,2) NOT NULL,         -- native, signed (BUY<0, SELL>0)
    fx_rate_to_eur    NUMERIC(18,8) NOT NULL,
    gross_amount_eur  NUMERIC(18,2) NOT NULL,
    notes             VARCHAR(255)
);
CREATE INDEX ix_fact_transactions_date     ON dwh.fact_transactions(date_key);
CREATE INDEX ix_fact_transactions_security ON dwh.fact_transactions(security_key);

CREATE TABLE dwh.fact_daily_prices (
    date_key          INTEGER       NOT NULL REFERENCES dwh.dim_date(date_key),
    security_key      INTEGER       NOT NULL REFERENCES dwh.dim_security(security_key),
    open_price        NUMERIC(18,6),
    high_price        NUMERIC(18,6),
    low_price         NUMERIC(18,6),
    close_price       NUMERIC(18,6) NOT NULL,
    volume            BIGINT,
    close_price_eur   NUMERIC(18,6) NOT NULL,
    PRIMARY KEY (date_key, security_key)
);
CREATE INDEX ix_fact_daily_prices_security ON dwh.fact_daily_prices(security_key);

CREATE TABLE dwh.fact_fx_rates (
    date_key          INTEGER       NOT NULL REFERENCES dwh.dim_date(date_key),
    from_currency     CHAR(3)       NOT NULL,
    to_currency       CHAR(3)       NOT NULL DEFAULT 'EUR',
    rate_to_eur       NUMERIC(18,8) NOT NULL CHECK (rate_to_eur > 0),
    PRIMARY KEY (date_key, from_currency, to_currency)
);
CREATE INDEX ix_fact_fx_rates_currency ON dwh.fact_fx_rates(from_currency);

CREATE TABLE dwh.fact_dividends (
    dividend_key            SERIAL        PRIMARY KEY,
    date_key                INTEGER       NOT NULL REFERENCES dwh.dim_date(date_key),  -- ex-date
    security_key            INTEGER       NOT NULL REFERENCES dwh.dim_security(security_key),
    dividend_per_share      NUMERIC(18,6) NOT NULL CHECK (dividend_per_share >= 0),
    currency                CHAR(3)       NOT NULL,
    fx_rate_to_eur          NUMERIC(18,8) NOT NULL,
    dividend_per_share_eur  NUMERIC(18,6) NOT NULL,
    UNIQUE (date_key, security_key)
);
CREATE INDEX ix_fact_dividends_security ON dwh.fact_dividends(security_key);

CREATE TABLE dwh.fact_holdings (
    date_key            INTEGER       NOT NULL REFERENCES dwh.dim_date(date_key),
    security_key        INTEGER       NOT NULL REFERENCES dwh.dim_security(security_key),
    quantity_held       NUMERIC(18,6) NOT NULL,
    close_price         NUMERIC(18,6),
    market_value        NUMERIC(18,2),
    market_value_eur    NUMERIC(18,2) NOT NULL,
    cost_basis_eur      NUMERIC(18,2) NOT NULL,
    unrealized_pnl_eur  NUMERIC(18,2) GENERATED ALWAYS AS (market_value_eur - cost_basis_eur) STORED,
    PRIMARY KEY (date_key, security_key)
);
CREATE INDEX ix_fact_holdings_security ON dwh.fact_holdings(security_key);

-- =============================================================================
-- Confirmation notice in the Postgres init log
-- =============================================================================
DO $$
BEGIN
    RAISE NOTICE 'Portfolio schema initialised: staging + dwh layers created.';
END $$;
