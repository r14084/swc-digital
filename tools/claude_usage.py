#!/usr/bin/env python3
"""Print one Claude Code usage percentage without exposing OAuth credentials.

The keychain source performs a read-only request with the Claude Code OAuth
token held only in process memory. The cache source reads Claude Code's local
usage snapshot and is useful for profiles whose credentials are not available
through the shared macOS keychain.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pwd
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
WINDOW_KEYS = {"session": "five_hour", "weekly": "seven_day"}
MACOS_USER_HOME = pwd.getpwuid(os.getuid()).pw_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Claude Code usage percentage.")
    parser.add_argument("--window", choices=WINDOW_KEYS, default="session")
    parser.add_argument(
        "--keychain-service", default=KEYCHAIN_SERVICE,
        help="macOS Keychain service name (default: Claude Code-credentials)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--keychain", action="store_true", help="use Claude Code OAuth")
    source.add_argument(
        "--config-dir", type=Path, help="read cached usage from a Claude config directory"
    )
    return parser.parse_args()


def keychain_token(service: str) -> str:
    try:
        raw = subprocess.check_output(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            text=True,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "HOME": MACOS_USER_HOME},
        )
        token = json.loads(raw)["claudeAiOauth"]["accessToken"]
    except (KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Claude Code OAuth credentials are unavailable") from exc
    if not isinstance(token, str) or not token:
        raise RuntimeError("Claude Code OAuth access token is unavailable")
    return token


def live_usage(service: str) -> dict[str, object]:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {keychain_token(service)}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code-usage-collector/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        raise RuntimeError("Claude usage request failed") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Claude usage response is invalid")
    return result


def cached_usage(config_dir: Path) -> dict[str, object]:
    try:
        with (config_dir / ".claude.json").open() as handle:
            config = json.load(handle)
        result = config["cachedUsageUtilization"]["utilization"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Claude usage cache is unavailable") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Claude usage cache is invalid")
    return result


def percent(usage: dict[str, object], window: str) -> int:
    record = usage.get(WINDOW_KEYS[window])
    raw = record.get("utilization") if isinstance(record, dict) else None
    if (
        not isinstance(raw, (int, float))
        or isinstance(raw, bool)
        or not math.isfinite(raw)
    ):
        raise RuntimeError(f"Claude {window} usage is unavailable")
    value = round(raw)
    if not 0 <= value <= 100:
        raise RuntimeError("Claude usage percentage is out of range")
    return value


def reset_minutes(usage: dict[str, object], window: str) -> int | None:
    """Minutes until the window resets, from resets_at; None when unavailable."""
    record = usage.get(WINDOW_KEYS[window])
    if not isinstance(record, dict):
        return None
    resets_at = record.get("resets_at")
    try:
        if isinstance(resets_at, str):
            moment = dt.datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        elif (
            isinstance(resets_at, (int, float))
            and not isinstance(resets_at, bool)
            and math.isfinite(resets_at)
        ):
            moment = dt.datetime.fromtimestamp(resets_at, dt.timezone.utc)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    remaining = (moment - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60
    if remaining < 0:
        return None
    return int(min(remaining, 65535))


def main() -> int:
    args = parse_args()
    try:
        usage = live_usage(args.keychain_service) if args.keychain else cached_usage(args.config_dir)
        result: dict[str, int] = {"percent": percent(usage, args.window)}
        reset = reset_minutes(usage, args.window)
        if reset is not None:
            result["reset_minutes"] = reset
        print(json.dumps(result))
    except RuntimeError as exc:
        print(f"claude_usage: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
