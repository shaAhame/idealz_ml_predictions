"""Load DAILY aggregates (one row per sale_date x location x category).

Two sources, same output contract:
  * supabase: the `v_daily_sales` view (aggregated in Postgres -> small payload),
    fetched in date windows with limit/offset pagination inside each window.
  * csv:      a CSV export of the Google Sheet (same headers as the sheet) or of
    `sales_transactions`; aggregated here exactly like the view does. Used for
    local development, tests and CI.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from ml.config import Settings
from ml.supabase_client import SupabaseClient, client_from_settings

log = logging.getLogger(__name__)

SERIES_KEYS: list[str] = ["location", "category"]  # CUSTOMIZE: the forecast entity
DAILY_COLUMNS: list[str] = ["sale_date", *SERIES_KEYS, "revenue", "profit", "units", "bills"]
DAILY_VIEW = "v_daily_sales"

# sheet header (normalised) -> transaction column. Mirrors COLUMN_MAP in the Apps Script.
SHEET_HEADER_MAP: dict[str, str] = {
    "date": "sale_date", "location": "location", "category": "category", "bill no": "bill_no",
    "imei": "imei", "brand/item": "item", "model": "model", "capacity": "capacity",
    "colour": "colour", "sales rep": "sales_rep", "revenue": "revenue", "cost": "cost",
}


class DataError(RuntimeError):
    """Input data is missing, malformed or stale."""


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
def load_daily_sales(
    settings: Settings,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    client: SupabaseClient | None = None,
) -> pd.DataFrame:
    if settings.data_source == "csv":
        daily = aggregate_daily(load_transactions_csv(settings.local_data_path))
    else:
        own_client = client is None
        client = client or client_from_settings(settings)
        try:
            daily = _load_daily_from_supabase(client, start=start, end=end, page_size=settings.supabase_page_size)
        finally:
            if own_client:
                client.close()
    daily = normalise_daily(daily)
    if start is not None:
        daily = daily[daily["sale_date"] >= pd.Timestamp(start)]
    if end is not None:
        daily = daily[daily["sale_date"] <= pd.Timestamp(end)]
    log.info("daily sales loaded", extra={"rows": len(daily), "source": settings.data_source,
                                          "series": int(daily.groupby(SERIES_KEYS).ngroups) if len(daily) else 0})
    return daily.reset_index(drop=True)


def check_freshness(daily: pd.DataFrame, cutoff: dt.date, stale_days: int) -> dt.date:
    """Raise if the newest sale is older than `stale_days` before the cutoff.
    Zero-filling would otherwise silently turn a broken sync into 'no sales'."""
    if daily.empty:
        raise DataError("No sales data available.")
    newest = daily["sale_date"].max().date()
    if (cutoff - newest).days > stale_days:
        raise DataError(
            f"Data is stale: newest sale_date is {newest}, cutoff is {cutoff} "
            f"(> {stale_days} days). Check the Google Sheets sync before training/forecasting."
        )
    return newest


# ----------------------------------------------------------------------------
# Supabase
# ----------------------------------------------------------------------------
def _load_daily_from_supabase(
    client: SupabaseClient,
    *,
    start: dt.date | None,
    end: dt.date | None,
    page_size: int,
    window_days: int = 92,
) -> pd.DataFrame:
    if start is None:
        first = _first_date(client)
        if first is None:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        start = first
    end = end or dt.date.today() + dt.timedelta(days=1)

    frames: list[pd.DataFrame] = []
    w_start = start
    while w_start <= end:
        w_end = min(end, w_start + dt.timedelta(days=window_days - 1))
        rows = client.select_all(
            DAILY_VIEW,
            select=",".join(DAILY_COLUMNS),
            filters=[("sale_date", f"gte.{w_start.isoformat()}"), ("sale_date", f"lte.{w_end.isoformat()}")],
            order="sale_date.asc,location.asc,category.asc",  # total order -> stable pagination
            page_size=page_size,
        )
        if rows:
            frames.append(pd.DataFrame(rows))
        w_start = w_end + dt.timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=DAILY_COLUMNS)


def _first_date(client: SupabaseClient) -> dt.date | None:
    page = next(client.select_pages(DAILY_VIEW, select="sale_date", order="sale_date.asc", page_size=1), [])
    return dt.date.fromisoformat(page[0]["sale_date"]) if page else None


# ----------------------------------------------------------------------------
# CSV (sheet export) — cleaning mirrors apps_script/sync_to_supabase.gs
# ----------------------------------------------------------------------------
def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", str(h)).strip().lower().rstrip(".")


def _to_number(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    txt = s.astype("string").str.strip()
    neg = txt.str.match(r"^\(.*\)$").fillna(False)
    txt = (txt.str.replace(r"^\(|\)$", "", regex=True)
              .str.replace(r"(?i)(LKR|Rs\.?|USD|\$)", "", regex=True)
              .str.replace(r"[,\s]", "", regex=True)
              .replace({"": pd.NA, "-": pd.NA}))
    out = pd.to_numeric(txt, errors="coerce").astype(float)
    return out.where(~neg, -out)


def _to_date(s: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(s, format="ISO8601", errors="coerce")
    fallback = pd.to_datetime(s[parsed.isna()], dayfirst=True, format="mixed", errors="coerce")
    return parsed.fillna(fallback).dt.normalize()


def load_transactions_csv(path: Path) -> pd.DataFrame:
    if not Path(path).exists():
        raise DataError(f"CSV not found: {path}. Generate one with `python scripts/make_sample_data.py`.")
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    cols = {c: SHEET_HEADER_MAP.get(_norm_header(c), _norm_header(c).replace(" ", "_")) for c in raw.columns}
    tx = raw.rename(columns=cols)
    missing = {"sale_date", "location", "category", "revenue"} - set(tx.columns)
    if missing:
        raise DataError(f"CSV {path} is missing columns: {sorted(missing)}")

    tx["sale_date"] = _to_date(tx["sale_date"].replace("", pd.NA))
    tx["revenue"] = _to_number(tx["revenue"])
    tx["cost"] = _to_number(tx["cost"]) if "cost" in tx else np.nan
    for c in ("location", "category"):
        tx[c] = tx[c].astype("string").str.replace(r"\s+", " ", regex=True).str.strip().str.upper()
    tx["bill_no"] = (tx["bill_no"].astype("string").str.strip().replace("", pd.NA) if "bill_no" in tx
                     else pd.Series(pd.NA, index=tx.index, dtype="string"))  # optional

    valid = (tx["sale_date"].notna() & tx["revenue"].notna()
             & tx["location"].fillna("").ne("") & tx["category"].fillna("").ne(""))
    if (~valid).any():
        log.warning("dropping invalid csv rows", extra={"invalid_rows": int((~valid).sum())})
    if "deleted_at" in tx:
        valid &= tx["deleted_at"].fillna("").eq("")
    return tx.loc[valid].reset_index(drop=True)


def aggregate_daily(tx: pd.DataFrame) -> pd.DataFrame:
    """Same semantics as the `v_daily_sales` view."""
    tx = tx.assign(profit=(tx["revenue"] - tx["cost"]).fillna(0.0))
    return (tx.groupby(["sale_date", *SERIES_KEYS], observed=True)
              .agg(revenue=("revenue", "sum"), profit=("profit", "sum"),
                   units=("revenue", "size"), bills=("bill_no", "nunique"))
              .reset_index())


def normalise_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in DAILY_COLUMNS}).astype(
            {"sale_date": "datetime64[ns]", "location": "object", "category": "object"})
    out = df.loc[:, DAILY_COLUMNS].copy()
    out["sale_date"] = pd.to_datetime(out["sale_date"]).dt.normalize().astype("datetime64[ns]")
    for c in SERIES_KEYS:
        out[c] = out[c].astype(str).str.strip().str.upper()
    for c in ("revenue", "profit", "units", "bills"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0).astype(float)
    # The view already aggregates; re-aggregating guards against case/whitespace variants.
    out = out.groupby(["sale_date", *SERIES_KEYS], as_index=False)[["revenue", "profit", "units", "bills"]].sum()
    return out.sort_values(["sale_date", *SERIES_KEYS]).reset_index(drop=True)
