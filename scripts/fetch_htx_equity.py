#!/usr/bin/env python3
"""Fetch HTX coin-margined perpetual contract equity and update CSV."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOST = "api.hbdm.com"
BASE_URL = f"https://{HOST}"
TIMEZONE = ZoneInfo("Asia/Shanghai")
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "htx_equity.csv"
CSV_HEADER = ["date", "total_equity", "trx_balance", "trx_liquidation_price", "trx_price"]
LEGACY_CSV_HEADER = ["date", "total_equity", "trx_balance", "trx_liquidation_price"]
DEFAULT_VALUATION_ASSET = "USDT"
# Coin-margined swap docs list USD but not USDT; USD valuation is USDT-equivalent.
VALUATION_ASSET_FALLBACKS = ("USDT", "USD")


class HtxApiError(Exception):
    """Raised when HTX API returns an error response."""


def create_signature(
    method: str,
    path: str,
    params: dict[str, str],
    secret_key: str,
) -> str:
    sorted_params = sorted(params.items())
    encoded_params = urllib.parse.urlencode(sorted_params)
    payload = "\n".join([method, HOST, path, encoded_params])
    digest = hmac.new(
        secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def post_private(
    path: str,
    access_key: str,
    secret_key: str,
    body: dict | None = None,
) -> dict:
    if body is None:
        body = {}

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    params = {
        "AccessKeyId": access_key,
        "SignatureMethod": "HmacSHA256",
        "SignatureVersion": "2",
        "Timestamp": timestamp,
    }
    params["Signature"] = create_signature("POST", path, params, secret_key)

    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}{path}?{query}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "storage-htx-equity-script/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HtxApiError(f"HTTP {exc.code} for {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HtxApiError(f"Network error for {path}: {exc}") from exc

    if payload.get("status") != "ok":
        raise HtxApiError(f"API error for {path}: {payload}")

    return payload


def get_public(path: str, params: dict[str, str | int] | None = None) -> dict:
    query = urllib.parse.urlencode(params or {})
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"

    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "storage-htx-equity-script/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HtxApiError(f"HTTP {exc.code} for {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HtxApiError(f"Network error for {path}: {exc}") from exc

    if payload.get("status") != "ok":
        raise HtxApiError(f"API error for {path}: {payload}")

    return payload


def fetch_total_equity(access_key: str, secret_key: str) -> float:
    preferred_asset = os.environ.get("HTX_VALUATION_ASSET", DEFAULT_VALUATION_ASSET).upper()
    assets_to_try = [preferred_asset]
    for fallback in VALUATION_ASSET_FALLBACKS:
        if fallback not in assets_to_try:
            assets_to_try.append(fallback)

    last_error: HtxApiError | None = None
    for valuation_asset in assets_to_try:
        try:
            payload = post_private(
                "/swap-api/v1/swap_balance_valuation",
                access_key,
                secret_key,
                {"valuation_asset": valuation_asset},
            )
        except HtxApiError as exc:
            last_error = exc
            continue

        for item in payload.get("data", []):
            asset = item.get("valuation_asset", "").upper()
            if asset in {valuation_asset, preferred_asset, "USDT", "USD"}:
                return float(item["balance"])

        data = payload.get("data", [])
        if data:
            return float(data[0]["balance"])

        last_error = HtxApiError(
            f"No valuation data returned for valuation_asset={valuation_asset}"
        )

    if last_error is not None:
        raise last_error
    raise HtxApiError("No valuation data returned from HTX")


def fetch_trx_account(access_key: str, secret_key: str) -> tuple[float, float | None]:
    """Return (margin_balance, liquidation_price) for TRX-USD.

    liquidation_price comes from swap_account_info (not swap_position_info).
    It is null when there is no open position.
    """
    payload = post_private(
        "/swap-api/v1/swap_account_info",
        access_key,
        secret_key,
        {"contract_code": "TRX-USD"},
    )

    for item in payload.get("data", []):
        if item.get("symbol", "").upper() != "TRX":
            continue

        balance = float(item.get("margin_balance") or 0)
        liq_raw = item.get("liquidation_price")
        if liq_raw is None or liq_raw == "":
            return balance, None
        return balance, float(liq_raw)

    return 0.0, None


def fetch_current_trx_price() -> float:
    payload = get_public(
        "/swap-ex/market/detail/merged",
        {"contract_code": "TRX-USD"},
    )
    tick = payload.get("tick") or {}
    price = tick.get("close")
    if price in (None, ""):
        raise HtxApiError("No current TRX price returned from HTX")
    return float(price)


def fetch_historical_trx_price(date: str) -> float:
    target = datetime.strptime(date, "%Y%m%d").replace(
        hour=22,
        minute=0,
        second=0,
        tzinfo=TIMEZONE,
    )
    target_ts = int(target.timestamp())
    payload = get_public(
        "/swap-ex/market/history/kline",
        {
            "contract_code": "TRX-USD",
            "period": "1min",
            "from": target_ts - 600,
            "to": target_ts + 600,
        },
    )
    candles = payload.get("data") or []
    if not candles:
        raise HtxApiError(f"No historical TRX price returned for {date}")

    candle = min(
        candles,
        key=lambda item: abs(int(item.get("id", 0)) - target_ts),
    )
    price = candle.get("close")
    if price in (None, ""):
        raise HtxApiError(f"No historical TRX price returned for {date}")
    return float(price)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if fieldnames not in (CSV_HEADER, LEGACY_CSV_HEADER):
            raise HtxApiError(
                f"Unexpected CSV header in {path}: expected {CSV_HEADER}, "
                f"got {reader.fieldnames}"
            )
        rows = []
        for row in reader:
            normalized = {column: row.get(column, "") for column in CSV_HEADER}
            rows.append(normalized)
        return rows


def write_csv_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def upsert_today_row(
    rows: list[dict[str, str]],
    date: str,
    total_equity: float,
    trx_balance: float,
    trx_liquidation_price: float | None,
    trx_price: float,
) -> tuple[list[dict[str, str]], str]:
    row = {
        "date": date,
        "total_equity": str(round(total_equity)),
        "trx_balance": str(round(trx_balance)),
        "trx_liquidation_price": f"{trx_liquidation_price:.2f}" if trx_liquidation_price is not None else "",
        "trx_price": f"{trx_price:.6f}",
    }

    for index, existing in enumerate(rows):
        if existing["date"] == date:
            rows[index] = row
            return rows, "updated"

    rows.append(row)
    return rows, "appended"


def backfill_missing_trx_prices(rows: list[dict[str, str]]) -> bool:
    updated = False
    for row in rows:
        if row.get("trx_price") not in (None, ""):
            continue
        try:
            row["trx_price"] = f"{fetch_historical_trx_price(row['date']):.6f}"
        except HtxApiError as exc:
            print(
                f"Warning: unable to backfill TRX price for {row['date']}: {exc}",
                file=sys.stderr,
            )
            continue
        updated = True
    return updated


def main() -> int:
    access_key = os.environ.get("HTX_AK", "").strip()
    secret_key = os.environ.get("HTX_SK", "").strip()
    if not access_key or not secret_key:
        print("HTX_AK and HTX_SK environment variables are required.", file=sys.stderr)
        return 1

    today = datetime.now(TIMEZONE).strftime("%Y%m%d")

    try:
        total_equity = fetch_total_equity(access_key, secret_key)
        trx_balance, trx_liquidation_price = fetch_trx_account(access_key, secret_key)
        trx_price = fetch_current_trx_price()
    except HtxApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    rows = read_csv_rows(CSV_PATH)
    backfilled = backfill_missing_trx_prices(rows)
    rows, action = upsert_today_row(
        rows,
        today,
        total_equity,
        trx_balance,
        trx_liquidation_price,
        trx_price,
    )
    write_csv_rows(CSV_PATH, rows)

    liq_display = f"{trx_liquidation_price:.2f}" if trx_liquidation_price is not None else "N/A"
    price_display = f"{trx_price:.6f}"
    print(
        f"{action.capitalize()} {CSV_PATH}: date={today}, "
        f"total_equity={round(total_equity)}, trx_balance={round(trx_balance)}, "
        f"trx_liquidation_price={liq_display}, trx_price={price_display}, "
        f"history_backfilled={'yes' if backfilled else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
