"""Transform layer: staging -> dwh star schema.

Builds: dim_date, dim_security, fact_fx_rates, fact_daily_prices,
        fact_transactions, fact_dividends, fact_holdings (derived).

Run:  python src/transform.py
"""
import sys
import datetime as dt

import pandas as pd
from sqlalchemy import text

from config import BASE_CURRENCY
from db import get_engine, truncate

engine = get_engine()


def log(msg: str) -> None:
    print(f"[transform] {dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def date_key(d) -> int:
    d = pd.to_datetime(d)
    return d.year * 10000 + d.month * 100 + d.day


# --------------------------------------------------------------------------- #
# dim_date — spans all dates seen across transactions + prices
# --------------------------------------------------------------------------- #
def build_dim_date() -> None:
    spans = []
    for q in ["SELECT MIN(transaction_date) lo, MAX(transaction_date) hi FROM staging.transactions",
              "SELECT MIN(price_date) lo, MAX(price_date) hi FROM staging.daily_prices"]:
        r = pd.read_sql(q, engine).iloc[0]
        if pd.notna(r["lo"]):
            spans.append((pd.to_datetime(r["lo"]), pd.to_datetime(r["hi"])))
    lo = min(s[0] for s in spans)
    hi = max(s[1] for s in spans)

    days = pd.date_range(lo, hi, freq="D")
    df = pd.DataFrame({"full_date": days})
    df["date_key"] = df["full_date"].apply(date_key)
    df["year"] = df["full_date"].dt.year
    df["quarter"] = df["full_date"].dt.quarter
    df["month"] = df["full_date"].dt.month
    df["month_name"] = df["full_date"].dt.strftime("%B")
    df["day"] = df["full_date"].dt.day
    df["day_of_week"] = df["full_date"].dt.dayofweek + 1   # 1=Mon
    df["day_name"] = df["full_date"].dt.strftime("%A")
    df["week_of_year"] = df["full_date"].dt.isocalendar().week.astype(int)
    df["is_weekend"] = df["day_of_week"] >= 6
    df["is_trading_day"] = ~df["is_weekend"]
    df["full_date"] = df["full_date"].dt.date

    truncate("dwh", "dim_date")
    df.to_sql("dim_date", engine, schema="dwh", if_exists="append", index=False)
    log(f"dim_date: {len(df)} days")


# --------------------------------------------------------------------------- #
# dim_security
# --------------------------------------------------------------------------- #
def build_dim_security() -> None:
    sec = pd.read_sql("SELECT ticker, name, currency, country, sector FROM staging.securities", engine)
    sec["asset_class"] = "EQUITY"
    truncate("dwh", "dim_security")
    sec.to_sql("dim_security", engine, schema="dwh", if_exists="append", index=False)
    log(f"dim_security: {len(sec)} rows")


def security_key_map() -> dict[str, int]:
    m = pd.read_sql("SELECT security_key, ticker FROM dwh.dim_security", engine)
    return dict(zip(m["ticker"], m["security_key"]))


# --------------------------------------------------------------------------- #
# fact_fx_rates  (+ an in-memory lookup for converting other facts)
# --------------------------------------------------------------------------- #
def build_fact_fx_rates() -> pd.DataFrame:
    fx = pd.read_sql("SELECT rate_date, from_currency, to_currency, rate FROM staging.fx_rates", engine)
    truncate("dwh", "fact_fx_rates")
    if not fx.empty:
        out = fx.rename(columns={"rate": "rate_to_eur"})
        out["date_key"] = out["rate_date"].apply(date_key)
        out = out[["date_key", "from_currency", "to_currency", "rate_to_eur"]]
        out.to_sql("fact_fx_rates", engine, schema="dwh", if_exists="append", index=False)
        log(f"fact_fx_rates: {len(out)} rows")
    return fx


def fx_lookup(fx: pd.DataFrame):
    """Return f(date, currency)->rate_to_eur, forward-filled for weekends/holidays."""
    if fx.empty:
        return lambda d, c: 1.0
    fx = fx.copy()
    fx["rate_date"] = pd.to_datetime(fx["rate_date"])
    pivot = (fx.pivot_table(index="rate_date", columns="from_currency",
                            values="rate", aggfunc="last")
             .sort_index().ffill())

    def f(d, ccy):
        if ccy == BASE_CURRENCY:
            return 1.0
        d = pd.to_datetime(d)
        sub = pivot.loc[:d]
        if ccy in pivot.columns and not sub.empty and pd.notna(sub[ccy].iloc[-1]):
            return float(sub[ccy].iloc[-1])
        return 1.0  # fallback; flagged conceptually, fine for learning project
    return f


# --------------------------------------------------------------------------- #
# fact_daily_prices
# --------------------------------------------------------------------------- #
def build_fact_daily_prices(skmap, fxf) -> None:
    px = pd.read_sql("""
        SELECT p.price_date, p.ticker, p.open_price, p.high_price, p.low_price,
               p.close_price, p.volume, s.currency
        FROM staging.daily_prices p
        JOIN staging.securities s USING (ticker)
    """, engine)
    truncate("dwh", "fact_daily_prices")
    if px.empty:
        log("fact_daily_prices: no data")
        return
    px["date_key"] = px["price_date"].apply(date_key)
    px["security_key"] = px["ticker"].map(skmap)
    px["close_price_eur"] = px.apply(
        lambda r: round(r["close_price"] * fxf(r["price_date"], r["currency"]), 6), axis=1)
    out = px[["date_key", "security_key", "open_price", "high_price",
              "low_price", "close_price", "volume", "close_price_eur"]]
    out.to_sql("fact_daily_prices", engine, schema="dwh", if_exists="append", index=False)
    log(f"fact_daily_prices: {len(out)} rows")


# --------------------------------------------------------------------------- #
# fact_transactions
# --------------------------------------------------------------------------- #
def build_fact_transactions(skmap, fxf) -> pd.DataFrame:
    tx = pd.read_sql("""
        SELECT transaction_date, ticker, action, quantity, price_per_unit,
               currency, gross_amount, notes
        FROM staging.transactions
    """, engine)
    truncate("dwh", "fact_transactions")
    if tx.empty:
        log("fact_transactions: no data")
        return tx
    tx["date_key"] = tx["transaction_date"].apply(date_key)
    tx["security_key"] = tx["ticker"].map(skmap)
    tx["fx_rate_to_eur"] = tx.apply(lambda r: fxf(r["transaction_date"], r["currency"]), axis=1)
    tx["gross_amount_eur"] = (tx["gross_amount"] * tx["fx_rate_to_eur"]).round(2)
    out = tx[["date_key", "security_key", "action", "quantity", "price_per_unit",
              "currency", "gross_amount", "fx_rate_to_eur", "gross_amount_eur", "notes"]]
    out.to_sql("fact_transactions", engine, schema="dwh", if_exists="append", index=False)
    log(f"fact_transactions: {len(out)} rows")
    return tx


# --------------------------------------------------------------------------- #
# fact_dividends
# --------------------------------------------------------------------------- #
def build_fact_dividends(skmap, fxf) -> None:
    dv = pd.read_sql("""
        SELECT ex_date, ticker, dividend_per_share, currency
        FROM staging.dividends
    """, engine)
    truncate("dwh", "fact_dividends")
    if dv.empty:
        log("fact_dividends: no data")
        return
    dv["date_key"] = dv["ex_date"].apply(date_key)
    dv["security_key"] = dv["ticker"].map(skmap)
    dv["fx_rate_to_eur"] = dv.apply(lambda r: fxf(r["ex_date"], r["currency"]), axis=1)
    dv["dividend_per_share_eur"] = (dv["dividend_per_share"] * dv["fx_rate_to_eur"]).round(6)
    # collapse any dup (date, security) to satisfy the unique constraint
    dv = dv.drop_duplicates(subset=["date_key", "security_key"], keep="last")
    out = dv[["date_key", "security_key", "dividend_per_share", "currency",
              "fx_rate_to_eur", "dividend_per_share_eur"]]
    out.to_sql("fact_dividends", engine, schema="dwh", if_exists="append", index=False)
    log(f"fact_dividends: {len(out)} rows")


# --------------------------------------------------------------------------- #
# fact_holdings — the derived daily snapshot (the payoff table)
# --------------------------------------------------------------------------- #
def build_fact_holdings(tx: pd.DataFrame, skmap, fxf) -> None:
    truncate("dwh", "fact_holdings")
    if tx.empty:
        log("fact_holdings: no transactions")
        return

    prices = pd.read_sql("""
        SELECT p.price_date, p.ticker, p.close_price, s.currency
        FROM staging.daily_prices p JOIN staging.securities s USING (ticker)
    """, engine)
    if prices.empty:
        log("fact_holdings: no prices")
        return
    prices["price_date"] = pd.to_datetime(prices["price_date"])

    tx = tx.copy()
    tx["transaction_date"] = pd.to_datetime(tx["transaction_date"])
    tx["signed_qty"] = tx.apply(
        lambda r: r["quantity"] if r["action"] == "BUY" else -r["quantity"], axis=1)
    # signed cash basis in EUR: BUY increases cost basis, SELL reduces it
    tx["signed_cost_eur"] = tx.apply(
        lambda r: -r["gross_amount"] * fxf(r["transaction_date"], r["currency"]), axis=1)

    snapshots = []
    for ticker, g in tx.groupby("ticker"):
        g = g.sort_values("transaction_date")
        pr = prices[prices["ticker"] == ticker].sort_values("price_date")
        if pr.empty:
            continue
        # cumulative position + cost basis as step functions over trade dates
        cum_qty = g.set_index("transaction_date")["signed_qty"].cumsum()
        cum_cost = g.set_index("transaction_date")["signed_cost_eur"].cumsum()
        spine = pr[["price_date", "close_price", "currency"]].copy()
        spine["quantity_held"] = (cum_qty.reindex(spine["price_date"], method="ffill").values)
        spine["cost_basis_eur"] = (cum_cost.reindex(spine["price_date"], method="ffill").values)
        spine = spine.dropna(subset=["quantity_held"])
        spine = spine[spine["quantity_held"] != 0]   # only days actually held
        if spine.empty:
            continue
        spine["ticker"] = ticker
        snapshots.append(spine)

    if not snapshots:
        log("fact_holdings: nothing held")
        return

    h = pd.concat(snapshots, ignore_index=True)
    h["date_key"] = h["price_date"].apply(date_key)
    h["security_key"] = h["ticker"].map(skmap)
    h["market_value"] = (h["quantity_held"] * h["close_price"]).round(2)
    h["market_value_eur"] = h.apply(
        lambda r: round(r["quantity_held"] * r["close_price"]
                        * fxf(r["price_date"], r["currency"]), 2), axis=1)
    h["cost_basis_eur"] = h["cost_basis_eur"].round(2)
    out = h[["date_key", "security_key", "quantity_held", "close_price",
             "market_value", "market_value_eur", "cost_basis_eur"]]
    out.to_sql("fact_holdings", engine, schema="dwh", if_exists="append", index=False)
    log(f"fact_holdings: {len(out)} daily snapshot rows")


def main() -> int:
    build_dim_date()
    build_dim_security()
    skmap = security_key_map()
    fx = build_fact_fx_rates()
    fxf = fx_lookup(fx)

    build_fact_daily_prices(skmap, fxf)
    tx = build_fact_transactions(skmap, fxf)
    build_fact_dividends(skmap, fxf)
    build_fact_holdings(tx, skmap, fxf)
    log("Transform complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
