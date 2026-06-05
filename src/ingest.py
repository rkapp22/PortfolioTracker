"""Ingest layer: read the Excel + public APIs, land raw data in `staging`.

Sources (all keyless):
  - Excel              : transactions + securities sheets
  - yfinance           : daily prices + dividends (single batched call)
  - Frankfurter API    : FX rates to EUR  (https://www.frankfurter.dev)

Run:  python src/ingest.py
"""
import sys
import datetime as dt

import pandas as pd
import requests
import yfinance as yf

from config import EXCEL_PATH, BASE_CURRENCY
from db import get_engine, truncate
from data_quality_tests import (
    check_excel_schema, check_excel_transaction_rows,
    check_api_prices_sanity,
    write_dq_results, write_rejected_rows,
    BlockingDqError,
)

engine = get_engine()
FRANKFURTER = "https://api.frankfurter.dev/v1"

# yfinance -> snake_case DB schema. Module-level so prices + dividends share it.
RENAME_MAP = {
    "Date": "price_date",
    "Open": "open_price",
    "High": "high_price",
    "Low": "low_price",
    "Close": "close_price",
    "Volume": "volume",
    "Dividends": "dividends",
}
PRICE_REQUIRED = ("price_date", "ticker")


def log(msg: str) -> None:
    print(f"[ingest] {dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def _staging_columns(table: str) -> list[str]:
    """Lowercase column names that exist in staging.<table>."""
    q = (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='staging' AND table_name=%(t)s"
    )
    return (
        pd.read_sql(q, engine, params={"t": table})["column_name"]
        .str.lower()
        .tolist()
    )


def _filter_to_existing(df: pd.DataFrame, table: str,
                        required: tuple[str, ...]) -> pd.DataFrame | None:
    """Keep only columns that exist in staging.<table>. Abort if required missing."""
    df.columns = [c.lower() for c in df.columns]
    existing = _staging_columns(table)
    allowed = [c for c in df.columns if c in existing]
    dropped = [c for c in df.columns if c not in existing]
    if dropped:
        log(f"  dropping columns not in staging.{table}: {dropped}")
    missing = [c for c in required if c not in allowed]
    if missing:
        log(f"  ERROR: required columns missing from {table}: {missing}. Aborting write.")
        return None
    return df[allowed]


# --------------------------------------------------------------------------- #
# 1. Excel -> staging.transactions, staging.securities
# --------------------------------------------------------------------------- #
def ingest_excel(run_id: dt.datetime, dq_results: list) -> tuple[list[str], pd.Timestamp]:
    log(f"Reading Excel: {EXCEL_PATH}")
    sheets = pd.read_excel(EXCEL_PATH, sheet_name=None)

    df_sec = sheets["securities"].copy()
    df_sec.columns = [c.strip().lower() for c in df_sec.columns]

    df_tx = sheets["transactions"].copy()
    df_tx.columns = [c.strip().lower() for c in df_tx.columns]

    # DQ: schema check — BLOCK on failure (runs before any staging write)
    schema_results = check_excel_schema(df_tx, df_sec)
    dq_results.extend(schema_results)
    if any(r.severity == "BLOCK" for r in schema_results):
        write_dq_results(engine, run_id, dq_results)
        raise BlockingDqError("Excel schema check failed — see staging.dq_results")

    # DQ: row-level checks — REJECT bad rows, keep the rest
    good_tx, bad_tx, row_results = check_excel_transaction_rows(df_tx)
    dq_results.extend(row_results)
    write_rejected_rows(engine, run_id, "excel.transactions", bad_tx)

    # Load securities
    truncate("staging", "securities")
    df_sec.to_sql("securities", engine, schema="staging",
                  if_exists="append", index=False)
    log(f"  loaded {len(df_sec)} securities")

    # Load good transactions (rename date -> transaction_date, keep known columns)
    good_tx = good_tx.rename(columns={"date": "transaction_date"})
    keep = ["transaction_date", "ticker", "action", "quantity",
            "price_per_unit", "currency", "gross_amount", "notes"]
    good_tx = good_tx[[c for c in keep if c in good_tx.columns]]
    truncate("staging", "transactions")
    good_tx.to_sql("transactions", engine, schema="staging",
                   if_exists="append", index=False)
    log(f"  loaded {len(good_tx)} transactions")

    tickers = df_sec["ticker"].dropna().unique().tolist()
    earliest = pd.to_datetime(good_tx["transaction_date"]).min()
    return tickers, earliest


# --------------------------------------------------------------------------- #
# 2. yfinance -> staging.daily_prices  +  staging.dividends
# --------------------------------------------------------------------------- #
def _fetch_yf_bulk(tickers: list[str], start: pd.Timestamp) -> pd.DataFrame | None:
    """One batched, threaded yfinance call. Returns a MultiIndex DataFrame."""
    if not tickers:
        return None
    raw = yf.download(
        tickers=tickers,
        start=start.date(),
        auto_adjust=False,   # keep raw Close; expose Adj Close
        actions=True,        # include Dividends + Stock Splits
        group_by="ticker",
        threads=True,
        progress=False,
    )
    if raw is None or raw.empty:
        return None
    return raw


def _slice_ticker(raw: pd.DataFrame, tk: str) -> pd.DataFrame:
    """Pull a single-ticker frame out of yf.download's MultiIndex result."""
    if isinstance(raw.columns, pd.MultiIndex):
        if tk not in raw.columns.get_level_values(0):
            return pd.DataFrame()
        return raw[tk].dropna(how="all")
    # Single-ticker case: yfinance returns a flat frame.
    return raw.dropna(how="all")


def _normalize_prices(hist: pd.DataFrame, tk: str) -> pd.DataFrame:
    """Reset index, rename to snake_case, attach ticker, normalize date.

    yfinance returns the date index under various names depending on version
    and call shape: 'Date', 'Datetime', 'date', or unnamed (-> 'index').
    We force a known name BEFORE reset_index() to make rename deterministic.
    """
    hist = hist.copy()
    hist.index.name = "price_date"          # force a known index name
    hist = hist.reset_index()

    # Now apply the column rename for OHLCV / actions columns.
    hist = hist.rename(columns={c: RENAME_MAP.get(c, c) for c in hist.columns})

    if "price_date" not in hist.columns:
        raise KeyError(
            f"[{tk}] expected 'price_date' after reset_index; "
            f"got columns: {list(hist.columns)}"
        )

    hist["price_date"] = pd.to_datetime(hist["price_date"]).dt.date
    hist["ticker"] = tk
    return hist


def ingest_prices_and_dividends(
    tickers: list[str],
    start: pd.Timestamp,
    securities_ccy: dict[str, str],
    dq_results: list,
) -> None:
    """Fetch OHLCV + actions in ONE yfinance call, then split into two tables."""
    log(f"Fetching daily prices + dividends for {len(tickers)} tickers from {start.date()}")
    raw = _fetch_yf_bulk(tickers, start)

    truncate("staging", "daily_prices")
    truncate("staging", "dividends")

    if raw is None:
        log("  WARNING: yfinance returned no data")
        return

    price_frames: list[pd.DataFrame] = []
    div_frames: list[pd.DataFrame] = []

    for tk in tickers:
        hist = _slice_ticker(raw, tk)
        if hist.empty:
            log(f"  WARNING: no data for {tk}")
            continue

        norm = _normalize_prices(hist, tk)
        price_frames.append(norm)
        log(f"  {tk}: {len(norm)} price rows")

        # Carve dividends out of the same frame — no second API call.
        if "dividends" in norm.columns:
            divs = norm.loc[norm["dividends"].fillna(0) > 0,
                            ["price_date", "ticker", "dividends"]].copy()
            if not divs.empty:
                divs = divs.rename(columns={
                    "price_date": "ex_date",
                    "dividends": "dividend_per_share",
                })
                divs["currency"] = securities_ccy.get(tk, BASE_CURRENCY)
                div_frames.append(divs)
                log(f"  {tk}: {len(divs)} dividend events")

    # ----- write prices ----------------------------------------------------- #
    all_prices = pd.concat(price_frames, ignore_index=True) if price_frames else pd.DataFrame()
    dq_results.extend(check_api_prices_sanity(all_prices))

    if not all_prices.empty:
        df = _filter_to_existing(all_prices, "daily_prices", PRICE_REQUIRED)
        if df is not None:
            df.to_sql(
                "daily_prices", engine, schema="staging",
                if_exists="append", index=False,
                method="multi", chunksize=10_000,
            )
            log(f"  wrote {len(df)} rows to staging.daily_prices")

    # ----- write dividends -------------------------------------------------- #
    if div_frames:
        ddf = pd.concat(div_frames, ignore_index=True)
        ddf.to_sql(
            "dividends", engine, schema="staging",
            if_exists="append", index=False,
            method="multi", chunksize=10_000,
        )
        log(f"  wrote {len(ddf)} rows to staging.dividends")


# --------------------------------------------------------------------------- #
# 3. Frankfurter -> staging.fx_rates
# --------------------------------------------------------------------------- #
def ingest_fx(currencies: set[str], start: pd.Timestamp) -> None:
    foreign = {c for c in currencies if c and c != BASE_CURRENCY}
    truncate("staging", "fx_rates")
    if not foreign:
        log("No non-EUR currencies; skipping FX.")
        return

    log(f"Fetching FX {foreign} -> {BASE_CURRENCY} from {start.date()}")
    url = (f"{FRANKFURTER}/{start.date()}.."
           f"?base={BASE_CURRENCY}&symbols={','.join(sorted(foreign))}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for date_str, rates in payload.get("rates", {}).items():
        for ccy, eur_to_ccy in rates.items():
            # API gives 1 EUR = eur_to_ccy CCY; we want 1 CCY = ? EUR
            rows.append({
                "rate_date": pd.to_datetime(date_str).date(),
                "from_currency": ccy,
                "to_currency": BASE_CURRENCY,
                "rate": round(1.0 / eur_to_ccy, 8),
            })

    if rows:
        pd.DataFrame(rows).to_sql("fx_rates", engine, schema="staging",
                                  if_exists="append", index=False)
        log(f"  loaded {len(rows)} FX rows")


def main(run_id: dt.datetime = None) -> tuple[int, list]:
    if run_id is None:
        run_id = dt.datetime.now()
    dq_results = []

    tickers, earliest = ingest_excel(run_id, dq_results)
    sec = pd.read_sql("SELECT ticker, currency FROM staging.securities", engine)
    ccy_map = dict(zip(sec["ticker"], sec["currency"]))
    currencies = set(sec["currency"].dropna())

    ingest_prices_and_dividends(tickers, earliest, ccy_map, dq_results)
    ingest_fx(currencies, earliest)

    write_dq_results(engine, run_id, dq_results)
    log("Ingest complete.")
    return 0, dq_results


if __name__ == "__main__":
    sys.exit(main()[0])