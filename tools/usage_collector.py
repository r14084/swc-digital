#!/usr/bin/env python3
"""Poll six local usage providers and publish one USB clock snapshot.

Provider commands are local executables configured in TOML. Each must print one
percentage (for example ``42`` or ``42%``), or JSON with a ``percent`` field.
No provider stdout, credentials, or session data is logged or sent to the clock.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

import serial

import clockctl
import crypto_market


SOURCE_NAMES = (
    "claude_1",
    "claude_2",
    "claude_3",
    "codex_1",
    "codex_2",
    "codex_3",
)
CLAUDE_NAMES = SOURCE_NAMES[:3]
CODEX_NAMES = SOURCE_NAMES[3:]
ACTIVE_VALUES = {"c1", "c2", "c3", "x1", "x2", "x3", "off"}
DEFAULT_CONFIG = Path(__file__).with_name("usage-collector.toml")


class CollectorError(RuntimeError):
    """A configured provider could not supply a valid percentage."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll six local usage providers and update the USB clock."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--publish-cache",
        action="store_true",
        help="publish cached Claude values while refreshing Codex only",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="override the configured poll interval in seconds (minimum: 30)",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise CollectorError(f"configuration not found: {path}") from exc
    except OSError as exc:
        raise CollectorError(f"could not read configuration: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CollectorError(f"invalid TOML in {path}: {exc}") from exc

    if not isinstance(config.get("collector"), dict):
        raise CollectorError("missing [collector] configuration")
    if not isinstance(config.get("sources"), dict):
        raise CollectorError("missing [sources] configuration")
    return config


def configured_command(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise CollectorError(f"{name}.command must be a non-empty list of strings")
    return list(value)


def run_command(command: list[str], name: str) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise CollectorError(f"{name}: command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(f"{name}: command timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise CollectorError(f"{name}: command failed (exit {exc.returncode})") from exc
    except UnicodeError as exc:
        raise CollectorError(f"{name}: command output is not valid text") from exc
    return result.stdout.strip()


def parse_usage(output: str, name: str) -> tuple[int, int | None]:
    """Return (percent, minutes-until-reset or None) from provider output."""
    value: object = output
    reset: object = None
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        for key in ("percent", "usage_percent", "used_percent"):
            if key in decoded:
                value = decoded[key]
                break
        else:
            raise CollectorError(f"{name}: JSON output needs a percent field")
        reset = decoded.get("reset_minutes")
    elif decoded is not None:
        value = decoded

    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
    if isinstance(value, bool):
        raise CollectorError(f"{name}: output is not an integer percentage")
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise CollectorError(f"{name}: output is not an integer percentage")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CollectorError(f"{name}: output is not an integer percentage") from exc
    if not 0 <= number <= 100:
        raise CollectorError(f"{name}: percentage must be 0-100")

    reset_minutes: int | None = None
    if (
        isinstance(reset, (int, float))
        and not isinstance(reset, bool)
        and math.isfinite(reset)
        and 0 <= reset <= 65535
    ):
        reset_minutes = int(reset)
    return number, reset_minutes


def parse_percent(output: str, name: str) -> int:
    return parse_usage(output, name)[0]


def state_path(config: dict[str, Any], config_path: Path) -> Path:
    configured = config["collector"].get("state_path")
    if configured is None:
        return config_path.with_name("usage-collector-state.json")
    if not isinstance(configured, str) or not configured:
        raise CollectorError("collector.state_path must be a non-empty path")
    result = Path(configured).expanduser()
    return result if result.is_absolute() else config_path.parent / result


def initial_claude_values(config: dict[str, Any]) -> list[int | None]:
    values = config["collector"].get("claude_initial_percentages")
    if values is None:
        return [None, None, None]
    if not isinstance(values, list) or len(values) != len(CLAUDE_NAMES):
        raise CollectorError("collector.claude_initial_percentages needs C1, C2, C3")
    result: list[int | None] = []
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
            raise CollectorError("collector.claude_initial_percentages must be 0-100")
        result.append(value)
    return result


def load_state(config: dict[str, Any], path: Path) -> dict[str, Any]:
    state = {
        "claude_values": initial_claude_values(config),
        "next_claude_index": 0,
        "claude_reset_epochs": [None, None, None],
        "last_snapshot": None,
    }
    try:
        loaded = json.loads(path.read_text())
    except FileNotFoundError:
        return state
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError(f"could not read collector state: {path}") from exc
    if not isinstance(loaded, dict):
        raise CollectorError("collector state is invalid")
    values = loaded.get("claude_values")
    next_index = loaded.get("next_claude_index")
    if (
        not isinstance(values, list)
        or len(values) != len(CLAUDE_NAMES)
        or any(
            value is not None
            and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 100
            )
            for value in values
        )
        or not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or not 0 <= next_index < len(CLAUDE_NAMES)
    ):
        raise CollectorError("collector state is invalid")
    resets = loaded.get("claude_reset_epochs")
    if (
        isinstance(resets, list)
        and len(resets) == len(CLAUDE_NAMES)
        and all(
            value is None
            or (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
            for value in resets
        )
    ):
        state["claude_reset_epochs"] = resets
    state["claude_values"] = values
    state["next_claude_index"] = next_index
    snapshot = loaded.get("last_snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, dict):
            raise CollectorError("collector state snapshot is invalid")
        snapshot_values = snapshot.get("values")
        snapshot_resets = snapshot.get("reset_epochs")
        snapshot_active = snapshot.get("active")
        snapshot_btc = snapshot.get("btc")
        snapshot_previous = snapshot.get("previous_values")
        if (
            not isinstance(snapshot_values, list)
            or len(snapshot_values) != len(SOURCE_NAMES)
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 100
                for value in snapshot_values
            )
            or not isinstance(snapshot_resets, list)
            or len(snapshot_resets) != len(SOURCE_NAMES)
            or any(
                value is not None
                and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                )
                for value in snapshot_resets
            )
            or snapshot_active is not None
            and snapshot_active not in ACTIVE_VALUES
        ):
            raise CollectorError("collector state snapshot is invalid")
        if snapshot_previous is not None and (
            not isinstance(snapshot_previous, list)
            or len(snapshot_previous) != len(SOURCE_NAMES)
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 100
                for value in snapshot_previous
            )
        ):
            raise CollectorError("collector state snapshot history is invalid")
        state["last_snapshot"] = {
            "values": snapshot_values,
            "previous_values": snapshot_previous,
            "reset_epochs": snapshot_resets,
            "active": snapshot_active,
            "btc": crypto_market.validate_snapshot(snapshot_btc),
        }
    return state


def cache_snapshot(
    state: dict[str, Any], values: list[int], active: str | None, resets: list[int],
    btc: dict[str, Any] | None,
) -> None:
    now = time.time()
    previous_snapshot = state.get("last_snapshot")
    previous_values = (
        list(previous_snapshot["values"])
        if isinstance(previous_snapshot, dict)
        and isinstance(previous_snapshot.get("values"), list)
        and len(previous_snapshot["values"]) == len(SOURCE_NAMES)
        else None
    )
    state["last_snapshot"] = {
        "values": list(values),
        "previous_values": previous_values,
        "reset_epochs": [now + value * 60 if value >= 0 else None for value in resets],
        "active": active,
        "btc": crypto_market.validate_snapshot(btc),
    }


def collect_crypto(state: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return crypto_market.fetch_btc_usd()
    except crypto_market.CryptoMarketError:
        snapshot = state.get("last_snapshot")
        if isinstance(snapshot, dict):
            return crypto_market.validate_snapshot(snapshot.get("btc"))
        return None


def claude_reset_minutes(state: dict[str, Any]) -> list[int]:
    """Minutes until each cached Claude account resets; -1 when unknown/past."""
    now = time.time()
    minutes: list[int] = []
    for epoch in state["claude_reset_epochs"]:
        if isinstance(epoch, (int, float)) and epoch > now:
            minutes.append(min(65535, math.ceil((epoch - now) / 60)))
        else:
            minutes.append(-1)
    return minutes


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except OSError as exc:
        raise CollectorError(f"could not save collector state: {path}") from exc


def collect_codex(config: dict[str, Any]) -> tuple[list[int], list[int]]:
    """Refresh all Codex percentages; returns (percentages, reset minutes)."""
    sources = config["sources"]
    values: list[int] = []
    resets: list[int] = []
    for name in CODEX_NAMES:
        source = sources.get(name)
        if not isinstance(source, dict):
            raise CollectorError(f"missing [sources.{name}]")
        command = configured_command(source.get("command"), name)
        percent_value, reset_minutes = parse_usage(run_command(command, name), name)
        values.append(percent_value)
        resets.append(reset_minutes if reset_minutes is not None else -1)
    return values, resets


def collect_snapshot(
    config: dict[str, Any], state: dict[str, Any]
) -> tuple[list[int] | None, str | None, str, list[int]]:
    sources = config["sources"]
    index = state["next_claude_index"]
    claude_name = CLAUDE_NAMES[index]
    source = sources.get(claude_name)
    if not isinstance(source, dict):
        raise CollectorError(f"missing [sources.{claude_name}]")
    command = configured_command(source.get("command"), claude_name)
    refreshed = True
    try:
        percent_value, reset_minutes = parse_usage(
            run_command(command, claude_name), claude_name
        )
    except CollectorError:
        # A Claude rate limit must not make the display's sync health stale.
        # Keep the last trusted C1-C3 snapshot and still refresh Codex.
        refreshed = False
    else:
        state["claude_values"][index] = percent_value
        state["claude_reset_epochs"][index] = (
            time.time() + reset_minutes * 60 if reset_minutes is not None else None
        )
    # Always advance the rotation. A persistently failing account must not
    # starve the other two; it simply retries on its next turn (~15 minutes).
    state["next_claude_index"] = (index + 1) % len(CLAUDE_NAMES)

    label = f"{claude_name} {'refreshed' if refreshed else 'cached'}"
    if any(value is None for value in state["claude_values"]):
        return None, None, label, []

    codex_values, codex_resets = collect_codex(config)
    values = list(state["claude_values"]) + codex_values
    resets = claude_reset_minutes(state) + codex_resets

    active = config["collector"].get("active_command")
    if active is None:
        return values, None, label, resets
    active_output = run_command(configured_command(active, "collector.active_command"), "active")
    active_value = active_output.strip().lower()
    if active_value not in ACTIVE_VALUES:
        raise CollectorError("active: output must be c1-c3, x1-x3, or off")
    return values, active_value, label, resets


def collect_active(config: dict[str, Any]) -> str | None:
    active = config["collector"].get("active_command")
    if active is None:
        return None
    active_output = run_command(configured_command(active, "collector.active_command"), "active")
    active_value = active_output.strip().lower()
    if active_value not in ACTIVE_VALUES:
        raise CollectorError("active: output must be c1-c3, x1-x3, or off")
    return active_value


def publish(
    config: dict[str, Any],
    values: list[int],
    active: str | None,
    resets: list[int] | None = None,
    btc: dict[str, Any] | None = None,
    previous_values: list[int] | None = None,
    transact_fn: Callable[[list[str]], str] | None = None,
) -> None:
    collector = config["collector"]
    port = collector.get("port", clockctl.DEFAULT_PORT)
    baud = collector.get("baud", 115200)
    if (
        not isinstance(port, str)
        or not port
        or not isinstance(baud, int)
        or isinstance(baud, bool)
        or baud <= 0
    ):
        raise CollectorError("collector.port must be text and collector.baud a positive integer")

    if (
        len(values) != len(SOURCE_NAMES)
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 100
            for value in values
        )
    ):
        raise CollectorError("usage snapshot is invalid")
    usage_values = list(values)
    if previous_values is not None:
        if (
            len(previous_values) != len(SOURCE_NAMES)
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 100
                for value in previous_values
            )
        ):
            raise CollectorError("previous usage snapshot is invalid")
        usage_values.extend(previous_values)
    commands = [
        time.strftime("SET TIME %H:%M"),
        "USAGE " + " ".join(map(str, usage_values)),
    ]
    if resets and len(resets) == len(SOURCE_NAMES):
        commands.append("RESETS " + " ".join(map(str, resets)))
    if active:
        commands.append(f"ACTIVE {active.upper()}")
    if btc is not None:
        commands.append(crypto_market.command(btc))
    response = (
        transact_fn(commands)
        if transact_fn is not None
        else clockctl.transact(port, baud, commands)
    )
    if not response:
        raise CollectorError("clock did not respond to the usage snapshot")
    if any(line.startswith("ERR") for line in response.splitlines()):
        raise CollectorError("clock rejected the usage snapshot")


def log(message: str, error: bool = False) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} usage collector: {message}",
          file=sys.stderr if error else sys.stdout, flush=True)


def run_once(
    config: dict[str, Any], config_path: Path,
    transact_fn: Callable[[list[str]], str] | None = None,
) -> None:
    path = state_path(config, config_path)
    state = load_state(config, path)
    values, active, claude_name, resets = collect_snapshot(config, state)
    btc = collect_crypto(state)
    if values is not None:
        cache_snapshot(state, values, active, resets, btc)
    save_state(path, state)
    if values is None:
        raise CollectorError("Claude baseline is incomplete; waiting for the next rotation")
    previous = state["last_snapshot"].get("previous_values")
    publish(config, values, active, resets, btc, previous, transact_fn)
    log(f"clock updated ({claude_name})")


def publish_cached_claude(
    config: dict[str, Any], config_path: Path,
    transact_fn: Callable[[list[str]], str] | None = None,
) -> None:
    path = state_path(config, config_path)
    state = load_state(config, path)
    claude_values = state["claude_values"]
    if any(value is None for value in claude_values):
        raise CollectorError("Claude cache is not ready yet")
    codex_values, codex_resets = collect_codex(config)
    values = list(claude_values) + codex_values
    active = collect_active(config)
    resets = claude_reset_minutes(state) + codex_resets
    btc = collect_crypto(state)
    cache_snapshot(state, values, active, resets, btc)
    save_state(path, state)
    previous = state["last_snapshot"].get("previous_values")
    publish(config, values, active, resets, btc, previous, transact_fn)
    log("clock updated (cached Claude values)")


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        interval = (
            args.interval
            if args.interval is not None
            else config["collector"].get("interval_seconds", 300)
        )
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 30:
            raise CollectorError("interval must be an integer of at least 30 seconds")
        if args.publish_cache:
            publish_cached_claude(config, args.config)
            return 0
        while True:
            try:
                run_once(config, args.config)
            except CollectorError as exc:
                # Do not publish partial/guessed data. The clock's sync indicator
                # will become stale and red until a complete future poll succeeds.
                log(str(exc), error=True)
                if args.once:
                    return 2
            except (serial.SerialException, OSError) as exc:
                # A busy or briefly missing USB port (for example while the GUI
                # holds it) must not kill the polling loop.
                log(f"clock unreachable: {exc.__class__.__name__}", error=True)
                if args.once:
                    return 2
            if args.once:
                return 0
            time.sleep(interval)
    except CollectorError as exc:
        log(str(exc), error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
