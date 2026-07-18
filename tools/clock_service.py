#!/usr/bin/env python3
"""Own one persistent USB session and drive context-aware Face emotions.

The service is the only normal owner of the CH340 serial device. Local CLI and
GUI processes proxy bounded requests through a user-only Unix socket, avoiding
the board reset that would otherwise happen on every control action.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import serial

import clockctl
import usage_collector


DEFAULT_CONFIG = Path(__file__).with_name("usage-collector.toml")
MAX_REQUEST_BYTES = 256 * 1024
IDLE_TIME_PATTERN = re.compile(rb'"HIDIdleTime"\s*=\s*(\d+)')


class ClockServiceError(RuntimeError):
    """The persistent controller could not safely perform an operation."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the persistent USB clock and Face emotion service."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--socket", type=Path, default=clockctl.DEFAULT_SOCKET)
    return parser.parse_args()


def integer_setting(
    values: dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    value = values.get(name, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ClockServiceError(f"emotion.{name} must be {minimum}-{maximum}")
    return value


def service_settings(config: dict[str, Any]) -> dict[str, int | str]:
    collector = config["collector"]
    port = collector.get("port", clockctl.DEFAULT_PORT)
    baud = collector.get("baud", 115200)
    interval = collector.get("interval_seconds", 300)
    if not isinstance(port, str) or not port:
        raise ClockServiceError("collector.port must be non-empty text")
    if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
        raise ClockServiceError("collector.baud must be a positive integer")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 30:
        raise ClockServiceError("collector.interval_seconds must be at least 30")

    emotion = config.get("emotion", {})
    if not isinstance(emotion, dict):
        raise ClockServiceError("[emotion] must be a table")
    poll = integer_setting(emotion, "poll_seconds", 5, 2, 60)
    curious = integer_setting(emotion, "curious_after_seconds", 300, 30, 3600)
    sleepy = integer_setting(emotion, "sleepy_after_seconds", 1800, 60, 86400)
    happy = integer_setting(emotion, "return_happy_seconds", 30, 5, 300)
    if sleepy <= curious:
        raise ClockServiceError(
            "emotion.sleepy_after_seconds must exceed curious_after_seconds"
        )
    return {
        "port": port,
        "baud": baud,
        "usage_interval": interval,
        "poll_seconds": poll,
        "curious_after": curious,
        "sleepy_after": sleepy,
        "return_happy": happy,
    }


def parse_idle_seconds(output: bytes) -> float:
    match = IDLE_TIME_PATTERN.search(output)
    if not match:
        raise ClockServiceError("macOS idle time is unavailable")
    return int(match.group(1)) / 1_000_000_000


def mac_idle_seconds() -> float:
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-c", "IOHIDSystem", "-d", "1"],
            check=True,
            capture_output=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClockServiceError("could not read macOS idle time") from exc
    return parse_idle_seconds(result.stdout)


class EmotionEngine:
    """Map Mac activity to a stable mood with a short return greeting."""

    def __init__(self, curious_after: int, sleepy_after: int, return_happy: int):
        self.curious_after = curious_after
        self.sleepy_after = sleepy_after
        self.return_happy = return_happy
        self.previous_idle: float | None = None
        self.happy_until = 0.0

    def mood(self, idle_seconds: float, now: float) -> str:
        if idle_seconds < 0:
            raise ClockServiceError("idle time cannot be negative")
        returned = (
            self.previous_idle is not None
            and self.previous_idle >= self.curious_after
            and idle_seconds < self.curious_after
        )
        if returned:
            self.happy_until = now + self.return_happy
        self.previous_idle = idle_seconds

        if idle_seconds >= self.sleepy_after:
            self.happy_until = 0.0
            return "sleepy"
        if idle_seconds >= self.curious_after:
            self.happy_until = 0.0
            return "curious"
        if now < self.happy_until:
            return "happy"
        return "focus"


class SerialBroker:
    """Serialize all access to one CH340 connection and reconnect once safely."""

    def __init__(
        self, port: str, baud: int, restore_commands: Callable[[], list[str]]
    ) -> None:
        self.port = port
        self.baud = baud
        self.restore_commands = restore_commands
        self.connection: serial.Serial | None = None
        self.lock = threading.RLock()

    def _close_locked(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    def _open_locked(self) -> None:
        self._close_locked()
        self.connection = clockctl.open_connection(self.port, self.baud)
        restore = self.restore_commands()
        if restore:
            response = clockctl.transact_connection(self.connection, restore)
            if not response or any(
                line.startswith("ERR") for line in response.splitlines()
            ):
                self._close_locked()
                raise ClockServiceError("clock rejected cached state restoration")

    def start(self) -> None:
        with self.lock:
            self._open_locked()

    def _run_with_reconnect(self, operation: Callable[[serial.Serial], str]) -> str:
        with self.lock:
            if self.connection is None:
                self._open_locked()
            assert self.connection is not None
            try:
                return operation(self.connection)
            except (serial.SerialException, OSError):
                self._open_locked()
                assert self.connection is not None
                return operation(self.connection)

    def transact(self, commands: list[str]) -> str:
        return self._run_with_reconnect(
            lambda connection: clockctl.transact_connection(connection, commands)
        )

    def gallery_upload(
        self, restore_commands: list[str], slot: int, raw: bytes
    ) -> str:
        return self._run_with_reconnect(
            lambda connection: clockctl.gallery_upload_connection(
                connection, restore_commands, slot, raw
            )
        )

    def close(self) -> None:
        with self.lock:
            self._close_locked()


def request_payload(broker: SerialBroker, request: object) -> str:
    if not isinstance(request, dict):
        raise ClockServiceError("request must be an object")
    operation = request.get("operation")
    commands = request.get("commands")
    if not isinstance(commands, list):
        raise ClockServiceError("commands must be a list")
    clockctl.validate_commands(commands)
    if operation == "commands":
        return broker.transact(commands)
    if operation != "gallery_upload":
        raise ClockServiceError("unknown service operation")
    slot = request.get("slot")
    encoded = request.get("data")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < 7:
        raise ClockServiceError("gallery slot must be 0-6")
    if not isinstance(encoded, str):
        raise ClockServiceError("gallery data must be base64 text")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ClockServiceError("gallery data is not valid base64") from exc
    if len(raw) != clockctl.GALLERY_BYTES:
        raise ClockServiceError("gallery data must contain 115200 bytes")
    return broker.gallery_upload(commands, slot, raw)


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            result = {"error": "request is missing or too large"}
        else:
            try:
                request = json.loads(raw)
                response = request_payload(self.server.broker, request)  # type: ignore[attr-defined]
                result = {"response": response}
            except (
                ClockServiceError,
                clockctl.ClockControlError,
                json.JSONDecodeError,
                serial.SerialException,
                OSError,
            ) as exc:
                result = {"error": str(exc) or exc.__class__.__name__}
        try:
            self.wfile.write(
                (json.dumps(result, separators=(",", ":")) + "\n").encode()
            )
        except (BrokenPipeError, ConnectionResetError):
            pass


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, broker: SerialBroker):
        self.broker = broker
        super().__init__(str(path), ControlHandler)


def prepare_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid():
        raise ClockServiceError("clock service directory is not owned by this user")
    os.chmod(path.parent, 0o700)
    if not path.exists():
        return
    details = path.lstat()
    if details.st_uid != os.getuid() or not stat.S_ISSOCK(details.st_mode):
        raise ClockServiceError("refusing to replace an unsafe clock service path")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, socket.timeout):
        path.unlink()
    except OSError as exc:
        raise ClockServiceError("could not inspect existing clock service") from exc
    else:
        raise ClockServiceError("clock service is already running")
    finally:
        probe.close()


def log(message: str, error: bool = False) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    stream = sys.stderr if error else sys.stdout
    print(f"{stamp} clock service: {message}", file=stream, flush=True)


def usage_worker(
    stop: threading.Event,
    config: dict[str, Any],
    config_path: Path,
    interval: int,
    broker: SerialBroker,
) -> None:
    while not stop.is_set():
        try:
            usage_collector.run_once(config, config_path, broker.transact)
        except (usage_collector.CollectorError, serial.SerialException, OSError) as exc:
            log(f"usage update failed: {exc.__class__.__name__}", error=True)
        stop.wait(interval)


def run_service(config: dict[str, Any], config_path: Path, socket_path: Path) -> None:
    settings = service_settings(config)
    state_path = usage_collector.state_path(config, config_path)
    active_emotion: dict[str, str | None] = {"mood": None}

    def restore_commands() -> list[str]:
        commands = clockctl.cached_display_commands(state_path)
        if active_emotion["mood"]:
            commands.append(f"EMOTION {active_emotion['mood'].upper()}")
        return commands

    broker = SerialBroker(
        str(settings["port"]), int(settings["baud"]), restore_commands
    )
    stop = threading.Event()

    def stop_service(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    prepare_socket(socket_path)
    server: ControlServer | None = None
    server_thread: threading.Thread | None = None
    worker: threading.Thread | None = None
    try:
        broker.start()
        server = ControlServer(socket_path, broker)
        os.chmod(socket_path, 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        worker = threading.Thread(
            target=usage_worker,
            args=(
                stop,
                config,
                config_path,
                int(settings["usage_interval"]),
                broker,
            ),
            daemon=True,
        )
        worker.start()
        engine = EmotionEngine(
            int(settings["curious_after"]),
            int(settings["sleepy_after"]),
            int(settings["return_happy"]),
        )
        idle_error_logged = False
        log("persistent USB controller started")
        while not stop.is_set():
            try:
                idle = mac_idle_seconds()
                mood = engine.mood(idle, time.monotonic())
                if mood != active_emotion["mood"]:
                    response = broker.transact([f"EMOTION {mood.upper()}"])
                    if "OK emotion updated" not in response:
                        raise ClockServiceError("firmware rejected automatic emotion")
                    active_emotion["mood"] = mood
                    log(f"emotion {mood}")
                idle_error_logged = False
            except (ClockServiceError, serial.SerialException, OSError) as exc:
                if not idle_error_logged:
                    log(f"emotion update failed: {exc.__class__.__name__}", error=True)
                    idle_error_logged = True
            stop.wait(int(settings["poll_seconds"]))
    finally:
        stop.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2)
        broker.close()
        try:
            if socket_path.exists() and socket_path.lstat().st_uid == os.getuid():
                socket_path.unlink()
        except OSError:
            pass
        log("persistent USB controller stopped")


def main() -> int:
    args = parse_args()
    try:
        config = usage_collector.load_config(args.config)
        run_service(config, args.config, args.socket)
    except (ClockServiceError, usage_collector.CollectorError) as exc:
        log(str(exc), error=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
