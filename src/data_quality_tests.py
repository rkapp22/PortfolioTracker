"""
Data quality tests for the portfolio pipeline.

Severity model:
  BLOCK  — abort pipeline (used for schema/structural failures)
  REJECT — drop the row, keep loading the rest; row goes to staging.rejected_rows
  WARN   — load everything, surface the concern to the user
  INFO   — informational counts, no action needed
"""

from __future__ import annotations
import datetime as dt
from dataclasses import dataclass
import pandas as pd
from sqlalchemy import text

# Required columns the Excel transactions sheet must have. Adjust to match
# the actual columns the project expects after reading the Excel.
REQUIRED_TX_COLUMNS = {"date", "ticker", "action", "quantity",
                       "price_per_unit", "currency", "gross_amount"}

REQUIRED_SECURITIES_COLUMNS = {"ticker", "name", "currency"}


@dataclass
class DqResult:
    check_name: str
    severity: str           # 'BLOCK' | 'REJECT' | 'WARN' | 'INFO'
    object_ref: str | None
    message: str
    row_count: int | None = None


class BlockingDqError(Exception):
    """Raised when a BLOCK-severity check fails."""


# --- BLOCK CHECKS -----------------------------------------------------------

def check_excel_schema(df_tx: pd.DataFrame, df_sec: pd.DataFrame) -> list[DqResult]:
    """Excel columns must match expected schema. BLOCK on failure."""
    results = []
    missing_tx = REQUIRED_TX_COLUMNS - set(df_tx.columns)
    missing_sec = REQUIRED_SECURITIES_COLUMNS - set(df_sec.columns)

    if missing_tx:
        msg = f"transactions sheet missing columns: {sorted(missing_tx)}"
        results.append(DqResult("excel.transactions.schema", "BLOCK",
                                "excel.transactions", msg))
    else:
        results.append(DqResult("excel.transactions.schema", "INFO",
                                "excel.transactions", "schema OK"))

    if missing_sec:
        msg = f"securities sheet missing columns: {sorted(missing_sec)}"
        results.append(DqResult("excel.securities.schema", "BLOCK",
                                "excel.securities", msg))
    else:
        results.append(DqResult("excel.securities.schema", "INFO",
                                "excel.securities", "schema OK"))
    return results


# --- REJECT CHECKS (row-level Excel) ----------------------------------------

def check_excel_transaction_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[DqResult]]:
    """
    Three obvious human-typo checks:
      1. quantity must be > 0
      2. price_per_unit must be > 0
      3. action must be BUY (then gross_amount < 0) or SELL (then gross_amount > 0)

    Returns (good_rows, bad_rows_with_reason_col, results).
    """
    df = df.copy().reset_index(drop=True)
    reasons = [""] * len(df)

    def add_reason(idx, text_):
        reasons[idx] = (reasons[idx] + "; " + text_).lstrip("; ") if reasons[idx] else text_

    for i, r in df.iterrows():
        # 1
        if pd.isna(r.get("quantity")) or r["quantity"] <= 0:
            add_reason(i, "quantity must be > 0")
        # 2
        if pd.isna(r.get("price_per_unit")) or r["price_per_unit"] <= 0:
            add_reason(i, "price_per_unit must be > 0")
        # 3
        act = r.get("action")
        amt = r.get("gross_amount")
        if act == "BUY" and not (pd.notna(amt) and amt < 0):
            add_reason(i, "BUY must have negative gross_amount")
        elif act == "SELL" and not (pd.notna(amt) and amt > 0):
            add_reason(i, "SELL must have positive gross_amount")
        elif act not in ("BUY", "SELL"):
            add_reason(i, f"action must be BUY or SELL, got '{act}'")

    bad_mask = pd.Series([bool(x) for x in reasons], index=df.index)
    good = df.loc[~bad_mask].copy()
    bad = df.loc[bad_mask].copy()
    bad["reject_reason"] = [reasons[i] for i in bad.index]

    results = [
        DqResult("excel.transactions.row_count", "INFO", "excel.transactions",
                 f"{len(df)} rows read", row_count=len(df)),
        DqResult("excel.transactions.rejected",
                 "REJECT" if len(bad) else "INFO",
                 "excel.transactions",
                 f"{len(bad)} rows rejected", row_count=len(bad)),
    ]
    return good, bad, results


# --- WARN CHECKS (API sanity) -----------------------------------------------

def check_api_prices_sanity(df: pd.DataFrame) -> list[DqResult]:
    """
    OHLC integrity + extreme moves. WARN only, never blocks.
    Expects columns: ticker, price_date, open_price, high_price, low_price,
                     close_price, volume
    """
    results = []
    if df.empty:
        results.append(DqResult("api.prices.empty", "WARN", "api.prices",
                                "no price rows received from API", row_count=0))
        return results

    # OHLC integrity
    bad_ohlc = df[
        (df["high_price"] < df["low_price"]) |
        (df["high_price"] < df["open_price"]) |
        (df["high_price"] < df["close_price"]) |
        (df["close_price"] <= 0)
    ]
    results.append(DqResult(
        "api.prices.ohlc_integrity",
        "WARN" if len(bad_ohlc) else "INFO",
        "api.prices",
        f"{len(bad_ohlc)} rows fail OHLC integrity (high>=low,open,close; close>0)",
        row_count=len(bad_ohlc),
    ))

    # Extreme moves: >30% day-over-day per ticker
    df2 = df.sort_values(["ticker", "price_date"]).copy()
    df2["prev_close"] = df2.groupby("ticker")["close_price"].shift(1)
    df2["pct_change"] = (df2["close_price"] / df2["prev_close"]) - 1
    extreme = df2[df2["pct_change"].abs() > 0.30].dropna(subset=["pct_change"])
    results.append(DqResult(
        "api.prices.extreme_moves",
        "WARN" if len(extreme) else "INFO",
        "api.prices",
        f"{len(extreme)} day-over-day moves exceed ±30%",
        row_count=len(extreme),
    ))

    return results


# --- WARN CHECKS (post-transform, in SQL) -----------------------------------

OVERSELL_SQL = """
WITH signed AS (
    SELECT transaction_date, ticker,
           CASE action WHEN 'BUY' THEN quantity ELSE -quantity END AS qty
    FROM staging.transactions
),
running AS (
    SELECT transaction_date, ticker,
           SUM(qty) OVER (PARTITION BY ticker ORDER BY transaction_date
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS pos
    FROM signed
)
SELECT ticker, transaction_date, pos
FROM running
WHERE pos < 0
ORDER BY ticker, transaction_date;
"""

def check_oversell(engine) -> list[DqResult]:
    """Cumulative position must never go negative. WARN — user fixes Excel."""
    df = pd.read_sql(text(OVERSELL_SQL), engine)
    if df.empty:
        return [DqResult("excel.transactions.oversell", "INFO",
                         "excel.transactions",
                         "no oversell violations", row_count=0)]
    msg = "; ".join(
        f"{r.ticker} went negative on {r.transaction_date} (pos={r.pos})"
        for r in df.itertuples()
    )
    return [DqResult("excel.transactions.oversell", "WARN",
                     "excel.transactions",
                     f"{len(df)} oversell event(s): {msg}",
                     row_count=len(df))]


# --- PERSISTENCE -----------------------------------------------------------

def write_dq_results(engine, run_id: dt.datetime, results: list[DqResult]) -> None:
    if not results:
        return
    rows = [{
        "run_id": run_id,
        "check_name": r.check_name,
        "severity": r.severity,
        "object_ref": r.object_ref,
        "message": r.message,
        "row_count": r.row_count,
    } for r in results]
    pd.DataFrame(rows).to_sql("dq_results", engine, schema="staging",
                              if_exists="append", index=False)


def write_rejected_rows(engine, run_id: dt.datetime, source: str,
                        df_bad: pd.DataFrame) -> None:
    if df_bad.empty:
        return
    # Move 'reject_reason' out of the row payload and into its own column
    reasons = df_bad["reject_reason"].tolist()
    payload = df_bad.drop(columns=["reject_reason"])
    # Serialize each row to JSON
    json_rows = payload.astype(object).where(pd.notna(payload), None) \
                       .to_dict(orient="records")

    import json as _json
    rows = [{
        "run_id": run_id,
        "source": source,
        "reason": reasons[i],
        "row_data": _json.dumps(json_rows[i], default=str),
    } for i in range(len(json_rows))]

    pd.DataFrame(rows).to_sql("rejected_rows", engine, schema="staging",
                              if_exists="append", index=False)


# --- TERMINAL SUMMARY ------------------------------------------------------

def print_summary(results: list[DqResult]) -> None:
    by_sev = {"BLOCK": [], "REJECT": [], "WARN": [], "INFO": []}
    for r in results:
        by_sev.setdefault(r.severity, []).append(r)

    print("")
    print("=" * 60)
    print("DATA QUALITY SUMMARY")
    print("=" * 60)
    for sev in ("BLOCK", "REJECT", "WARN", "INFO"):
        items = by_sev.get(sev, [])
        if not items:
            continue
        print(f"\n[{sev}] {len(items)} check(s):")
        for r in items:
            tag = f"({r.row_count})" if r.row_count is not None else ""
            print(f"  - {r.check_name} {tag}: {r.message}")
    print("\nDetails: SELECT * FROM staging.dq_results ORDER BY run_id DESC;")
    if any(r.severity == "REJECT" for r in results):
        print("Quarantined rows: SELECT * FROM staging.rejected_rows ORDER BY run_id DESC;")
    print("=" * 60)
