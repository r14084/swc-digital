#!/usr/bin/env python3
"""Print one Codex ChatGPT weekly usage percentage for a local profile.

The local profile's OAuth access token is read only in process memory. The
script prints just the weekly percentage and never logs credentials or the full
account response.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_PROFILE_ROOT = Path(
    os.environ.get("CODEX_PROFILE_ROOT", Path.home() / ".codex-profiles")
)
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Codex weekly usage.")
    parser.add_argument("--profile", required=True, help="Codex profile directory name")
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=DEFAULT_PROFILE_ROOT,
        help="directory containing Codex profiles (default: CODEX_PROFILE_ROOT or ~/.codex-profiles)",
    )
    return parser.parse_args()


def access_token(profile: str, profile_root: Path = DEFAULT_PROFILE_ROOT) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", profile):
        raise RuntimeError("invalid Codex profile name")
    path = profile_root.expanduser() / profile / ".codex" / "auth.json"
    try:
        with path.open() as handle:
            token = json.load(handle)["tokens"]["access_token"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Codex OAuth credentials are unavailable") from exc
    if not isinstance(token, str) or not token:
        raise RuntimeError("Codex OAuth access token is unavailable")
    return token


def weekly_usage(profile: str, profile_root: Path = DEFAULT_PROFILE_ROOT) -> dict[str, int]:
    request = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {access_token(profile, profile_root)}", "User-Agent": "codex-cli"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        raise RuntimeError("Codex usage request failed") from exc
    try:
        window = data["rate_limit"]["primary_window"]
        value = window["used_percent"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Codex weekly usage is unavailable") from exc
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 <= round(value) <= 100
    ):
        raise RuntimeError("Codex weekly usage is invalid")
    result = {"percent": round(value)}
    reset_seconds = window.get("reset_after_seconds") if isinstance(window, dict) else None
    if (
        isinstance(reset_seconds, (int, float))
        and not isinstance(reset_seconds, bool)
        and math.isfinite(reset_seconds)
        and reset_seconds >= 0
    ):
        result["reset_minutes"] = int(min(reset_seconds // 60, 65535))
    return result


def main() -> int:
    args = parse_args()
    try:
        print(json.dumps(weekly_usage(args.profile, args.profile_root)))
    except RuntimeError as exc:
        print(f"codex_usage: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
