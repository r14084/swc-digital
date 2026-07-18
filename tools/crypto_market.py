#!/usr/bin/env python3
"""Fetch and compact public BTC-USD hourly candles for the USB clock."""

from __future__ import annotations

import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from typing import Any


CANDLES = 24
API_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


class CryptoMarketError(RuntimeError):
    """Public market data was missing or invalid."""


def validate_snapshot(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    price_cents = value.get("price_cents")
    change_bps = value.get("change_bps")
    candles = value.get("candles")
    if (
        not isinstance(price_cents, int)
        or isinstance(price_cents, bool)
        or not 0 < price_cents <= 0xFFFFFFFF
        or not isinstance(change_bps, int)
        or isinstance(change_bps, bool)
        or not -32768 <= change_bps <= 32767
        or not isinstance(candles, str)
        or len(candles) != CANDLES * 8
    ):
        return None
    try:
        raw = bytes.fromhex(candles)
    except ValueError:
        return None
    if len(raw) != CANDLES * 4 or any(value > 100 for value in raw):
        return None
    for offset in range(0, len(raw), 4):
        opened, high, low, closed = raw[offset:offset + 4]
        if low > high or not low <= opened <= high or not low <= closed <= high:
            return None
    return {
        "price_cents": price_cents,
        "change_bps": change_bps,
        "candles": candles.upper(),
    }


def command(snapshot: dict[str, Any]) -> str:
    checked = validate_snapshot(snapshot)
    if checked is None:
        raise CryptoMarketError("cached BTC-USD snapshot is invalid")
    return (
        f"BTC {checked['price_cents']} {checked['change_bps']} "
        f"{checked['candles']}"
    )


def fetch_btc_usd(timeout: float = 12.0) -> dict[str, Any]:
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(hours=26)
    query = urllib.parse.urlencode(
        {
            "granularity": 3600,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    )
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "PHUD-USB-Clock/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CryptoMarketError("BTC-USD market data request failed") from exc
    if not isinstance(payload, list):
        raise CryptoMarketError("BTC-USD market data response is invalid")

    parsed: list[tuple[int, float, float, float, float]] = []
    try:
        for row in payload:
            if not isinstance(row, list) or len(row) < 5:
                continue
            timestamp = int(row[0])
            low, high, opened, closed = map(float, row[1:5])
            if (
                not all(math.isfinite(value) for value in (low, high, opened, closed))
                or min(low, high, opened, closed) <= 0
                or max(low, high, opened, closed) > 0xFFFFFFFF / 100
                or low > high
                or not low <= opened <= high
                or not low <= closed <= high
            ):
                continue
            parsed.append((timestamp, low, high, opened, closed))
    except (TypeError, ValueError, OverflowError) as exc:
        raise CryptoMarketError("BTC-USD candle values are invalid") from exc
    parsed.sort(key=lambda candle: candle[0])
    if len(parsed) < CANDLES:
        raise CryptoMarketError("fewer than 24 BTC-USD hourly candles returned")
    selected = parsed[-CANDLES:]

    chart_low = min(candle[1] for candle in selected)
    chart_high = max(candle[2] for candle in selected)
    spread = chart_high - chart_low
    if spread <= 0:
        raise CryptoMarketError("BTC-USD candle range is empty")

    compact = bytearray()
    for _, low, high, opened, closed in selected:
        for price in (opened, high, low, closed):
            compact.append(max(0, min(100, round((price - chart_low) * 100 / spread))))

    current = selected[-1][4]
    previous = selected[0][3]
    snapshot = {
        "price_cents": round(current * 100),
        "change_bps": max(-32768, min(32767, round((current / previous - 1) * 10000))),
        "candles": compact.hex().upper(),
    }
    checked = validate_snapshot(snapshot)
    if checked is None:
        raise CryptoMarketError("BTC-USD compact snapshot is invalid")
    return checked


if __name__ == "__main__":
    snapshot = fetch_btc_usd()
    print(f"BTC-USD ${snapshot['price_cents'] / 100:,.2f}")
