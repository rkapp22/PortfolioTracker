"""Ingest layer: read the Excel + public APIs, land raw data in `staging`.

Sources (all keyless):
  - Excel              : transactions + securities sheets
  - yfinance           : daily prices + dividends
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

engine = get_engine()
FRANKFURTER = "https://api.frankfurter.dev/v1"


def log(msg: str) -> None:
    print(f"[ingest] {dt.datetime.now():%H:%M:%S} {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 1. Excel -> staging.transactions, staging.securities
# --------------------------------------------------------------------------- #
def ingest_excel() -> tuple[list[str], pd.Timestamp]:
    log(f"Reading Excel: {EXCEL_PATH}")
    sheets = pd.read_excel(EXCEL_PATH, sheet_name=None)

    securities = sheets["securities"].copy()
    securities.columns = [c.strip().lower() for c in securities.columns]
    truncate("staging", "securities")
    securities.to_sql("securities", engine, schema="staging",
                      if_exists="append", index=False)
    log(f"  loaded {len(securities)} securities")

    tx = sheets["transactions"].copy()
    tx.columns = [c.strip().lower() for c in tx.columns]
    tx = tx.rename(columns={"date": "transaction_date"})
    # keep only the columns staging.transactions expects
    keep = ["transaction_date", "ticker", "action", "quantity",
            "price_per_unit", "currency", "gross_amount", "notes"]
    tx = tx[[c for c in keep if c in tx.columns]]
    truncate("staging", "transactions")
    tx.to_sql("transactions", engine, schema="staging",
              if_exists="append", index=False)
    log(f"  loaded {len(tx)} transactions")

    tickers = securities["ticker"].dropna().unique().tolist()
    earliest = pd.to_datetime(tx["transaction_date"]).min()
    return tickers, earliest


# --------------------------------------------------------------------------- #
# 2. yfinance -> staging.daily_prices
# --------------------------------------------------------------------------- #
def ingest_prices(tickers: list[str], start: pd.Timestamp) -> None:
    log(f"Fetching daily prices for {len(tickers)} tickers from {start.date()}")
    frames = []
    for tk in tickers:
        try:
            hist = yf.Ticker(tk).history(start=start.date(), auto_adjust=False)
            if hist.empty:
                log(f"  WARNING: no price data for {tk}")
                continue
            hist = hist.reset_index()[
                ["Date", "Open", "High", "Low", "Close", "Volume"]
            ]
            hist.columns = ["price_date", "open_price", "high_price",
                            "low_price", "close_price", "volume"]
            hist["price_date"] = pd.to_datetime(hist["price_date"]).dt.date
            hist["ticker"] = tk
            frames.append(hist)
            log(f"  {tk}: {len(hist)} rows")
        except Exception as e:  # noqa: BLE001 - keep ingest resilient per ticker
            log(f"  ERROR fetching {tk}: {e}")

    truncate("staging", "daily_prices")
    if frames:
        pd.concat(frames, ignore_index=True).to_sql(
            "daily_prices", engine, schema="staging",
            if_exists="append", index=False)


# --------------------------------------------------------------------------- #
# 3. yfinance -> staging.dividends
# --------------------------------------------------------------------------- #
def ingest_dividends(tickers: list[str], securities_ccy: dict[str, str]) -> None:
    log("Fetching dividends")
    frames = []
    for tk in tickers:
        try:
            div = yf.Ticker(tk).dividends  # Series indexed by ex-date
            if div is None or div.empty:
                continue
            d = div.reset_index()
            d.columns = ["ex_date", "dividend_per_share"]
            d["ex_date"] = pd.to_datetime(d["ex_date"]).dt.date
            d["ticker"] = tk
            d["currency"] = securities_ccy.get(tk, BASE_CURRENCY)
            frames.append(d)
            log(f"  {tk}: {len(d)} dividend events")
        except Exception as e:  # noqa: BLE001
            log(f"  ERROR dividends {tk}: {e}")

    truncate("staging", "dividends")
    if frames:
        pd.concat(frames, ignore_index=True).to_sql(
            "dividends", engine, schema="staging",
            if_exists="append", index=False)


# --------------------------------------------------------------------------- #
# 4. Frankfurter -> staging.fx_rates
# --------------------------------------------------------------------------- #
def ingest_fx(currencies: set[str], start: pd.Timestamp) -> None:
    foreign = {c for c in currencies if c and c != BASE_CURRENCY}
    if not foreign:
        log("No non-EUR currencies; skipping FX.")
        truncate("staging", "fx_rates")
        return

    log(f"Fetching FX {foreign} -> {BASE_CURRENCY} from {start.date()}")
    # Frankfurter: base=EUR by default; ask for the foreign symbols, then invert.
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

    truncate("staging", "fx_rates")
    if rows:
        pd.DataFrame(rows).to_sql("fx_rates", engine, schema="staging",
                                  if_exists="append", index=False)
        log(f"  loaded {len(rows)} FX rows")


def main() -> int:
    tickers, earliest = ingest_excel()
    sec = pd.read_sql("SELECT ticker, currency FROM staging.securities", engine)
    ccy_map = dict(zip(sec["ticker"], sec["currency"]))
    currencies = set(sec["currency"].dropna())

    ingest_prices(tickers, earliest)
    ingest_dividends(tickers, ccy_map)
    ingest_fx(currencies, earliest)
    log("Ingest complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
