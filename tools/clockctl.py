#!/usr/bin/env python3
"""USB command-line client for the custom ESP8266 clock firmware."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
from pathlib import Path
import socket
import sys
import time

import serial
from serial.tools import list_ports

import crypto_market


DEFAULT_PORT = "auto"
CH340_USB_IDS = {(0x1A86, 0x7523)}
DEFAULT_STATE = Path(__file__).with_name("usage-collector-state.json")
DEFAULT_SOCKET = (
    Path.home() / "Library" / "Application Support" / "PHUDClock" / "clock.sock"
)
MAX_PROXY_RESPONSE_BYTES = 64 * 1024
GALLERY_BYTES = 240 * 240 * 2


class ClockControlError(RuntimeError):
    """The local clock service or USB protocol rejected an operation."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure the Smart Weather Clock over USB serial."
    )
    parser.add_argument(
        "--port", default=DEFAULT_PORT, help="serial device path or auto"
    )
    parser.add_argument("--baud", default=115200, type=int, help="serial baud rate")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("status", help="read the current clock configuration")
    sub.add_parser("find-port", help="print the detected clock serial device")
    sub.add_parser("help", help="list commands implemented by the firmware")
    sub.add_parser("show", help="redraw the dashboard")
    sub.add_parser("test", help="show the four-colour LCD test pattern")
    screen_parser = sub.add_parser("screen", help="switch the visible screen")
    screen_parser.add_argument(
        "mode", choices=("face", "usage", "btc", "clock", "thai")
    )
    face_parser = sub.add_parser("face", help="show the animated LED eyes")
    face_parser.add_argument(
        "mood",
        choices=(
            "auto",
            "neutral",
            "happy",
            "focus",
            "curious",
            "sleepy",
            "alert",
            "celebrate",
        ),
    )
    remind_parser = sub.add_parser("remind", help="manage persistent daily reminders")
    remind_sub = remind_parser.add_subparsers(dest="remind_action", required=True)
    remind_set = remind_sub.add_parser("set", help="set one reminder slot")
    remind_set.add_argument("slot", type=reminder_slot)
    remind_set.add_argument("time", type=reminder_time, metavar="HH:MM")
    remind_set.add_argument("label", help="1-20 printable ASCII characters")
    remind_del = remind_sub.add_parser("del", help="delete one reminder slot")
    remind_del.add_argument("slot", type=reminder_slot)
    remind_sub.add_parser("list", help="list all reminder slots")
    sub.add_parser("save", help="persist the current settings")
    sub.add_parser("reboot", help="restart the clock")
    sub.add_parser("sync-time", help="set the clock time from this computer")
    sub.add_parser("shell", help="keep one USB session open for multiple commands")

    usage_parser = sub.add_parser(
        "usage", help="update Claude and Codex weekly usage percentages"
    )
    usage_parser.add_argument("claude_1", type=percent)
    usage_parser.add_argument("claude_2", type=percent)
    usage_parser.add_argument("claude_3", type=percent)
    usage_parser.add_argument("codex_1", type=percent)
    usage_parser.add_argument("codex_2", type=percent)
    usage_parser.add_argument("codex_3", type=percent)
    usage_parser.add_argument(
        "--active", choices=("c1", "c2", "c3", "x1", "x2", "x3", "off"),
        help="mark the active account in the same USB session",
    )

    set_parser = sub.add_parser("set", help="change one setting")
    set_parser.add_argument(
        "key",
        choices=("title", "city", "brightness", "rotation", "blinvert", "accent", "time"),
    )
    set_parser.add_argument("value", nargs="+", help="new setting value")

    send_parser = sub.add_parser("send", help="send one raw firmware command")
    send_parser.add_argument("command", nargs="+", help="raw command text")
    return parser.parse_args()


def percent(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("percentage must be between 0 and 100")
    return parsed


def reminder_slot(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 7:
        raise argparse.ArgumentTypeError("reminder slot must be between 0 and 7")
    return parsed


def reminder_time(value: str) -> str:
    try:
        parsed = dt.datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("reminder time must be HH:MM") from exc
    return parsed.strftime("%H:%M")


def cached_display_commands(state_path: Path = DEFAULT_STATE) -> list[str]:
    """Restore the last complete display snapshot without polling providers."""
    commands = [f"SET TIME {dt.datetime.now().astimezone():%H:%M}"]
    try:
        snapshot = json.loads(state_path.read_text()).get("last_snapshot")
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return commands
    if not isinstance(snapshot, dict):
        return commands
    values = snapshot.get("values")
    reset_epochs = snapshot.get("reset_epochs")
    active = snapshot.get("active")
    if (
        not isinstance(values, list)
        or len(values) != 6
        or any(
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100
            for value in values
        )
        or not isinstance(reset_epochs, list)
        or len(reset_epochs) != 6
        or any(
            value is not None
            and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            )
            for value in reset_epochs
        )
        or active is not None
        and active not in {"c1", "c2", "c3", "x1", "x2", "x3", "off"}
    ):
        return commands
    now = dt.datetime.now().timestamp()
    resets = [
        max(0, min(65535, math.ceil((epoch - now) / 60)))
        if isinstance(epoch, (int, float)) and epoch > now else -1
        for epoch in reset_epochs
    ]
    previous = snapshot.get("previous_values")
    if (
        not isinstance(previous, list)
        or len(previous) != 6
        or any(
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100
            for value in previous
        )
    ):
        previous = None
    usage_values = values + previous if previous is not None else values
    commands.append("USAGE " + " ".join(map(str, usage_values)))
    commands.append("RESETS " + " ".join(map(str, resets)))
    if active:
        commands.append(f"ACTIVE {active.upper()}")
    btc = crypto_market.validate_snapshot(snapshot.get("btc"))
    if btc is not None:
        commands.append(crypto_market.command(btc))
    return commands


def commands_for(args: argparse.Namespace) -> list[str]:
    if args.action == "status":
        return ["STATUS"]
    if args.action == "help":
        return ["HELP"]
    if args.action == "show":
        return ["SHOW"]
    if args.action == "test":
        return ["TEST"]
    if args.action == "screen":
        # Restore all RAM-only display data after the USB reset, then switch.
        return cached_display_commands() + [f"SCREEN {args.mode.upper()}"]
    if args.action == "face":
        # FACE selects the persistent Face screen and its manual/automatic mood.
        return cached_display_commands() + [f"FACE {args.mood.upper()}"]
    if args.action == "remind":
        restore = cached_display_commands()
        if args.remind_action == "list":
            return restore + ["REMIND LIST"]
        if args.remind_action == "del":
            return restore + [f"REMIND DEL {args.slot}"]
        try:
            args.label.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("reminder label must use printable ASCII") from exc
        if not 1 <= len(args.label) <= 20 or not args.label.isprintable():
            raise ValueError("reminder label must be 1-20 printable ASCII characters")
        return restore + [f"REMIND SET {args.slot} {args.time} {args.label}"]
    if args.action == "save":
        return ["SAVE"]
    if args.action == "reboot":
        return ["REBOOT"]
    if args.action == "sync-time":
        return [f"SET TIME {dt.datetime.now().astimezone():%H:%M}"]
    if args.action == "usage":
        values = (
            args.claude_1,
            args.claude_2,
            args.claude_3,
            args.codex_1,
            args.codex_2,
            args.codex_3,
        )
        # Opening the CH340 serial device resets this clock and clears its
        # RAM-only wall time. Restore it before publishing the fresh snapshot.
        commands = [
            f"SET TIME {dt.datetime.now().astimezone():%H:%M}",
            "USAGE " + " ".join(map(str, values)),
        ]
        if args.active:
            commands.append(f"ACTIVE {args.active.upper()}")
        return commands
    if args.action == "set":
        # A new process resets this CH340-connected board on open. Persist the
        # setting before closing so it survives the next CLI invocation.
        return [f"SET {args.key.upper()} {' '.join(args.value)}", "SAVE"]
    if args.action == "send":
        return [" ".join(args.command)]
    raise AssertionError(f"unhandled action: {args.action}")


def resolve_port(port: str) -> str:
    """Resolve ``auto`` to the only connected CH340 clock serial device."""
    if port != "auto":
        return port
    candidates = sorted(
        info.device
        for info in list_ports.comports()
        if (info.vid, info.pid) in CH340_USB_IDS
        and info.device.startswith("/dev/cu.")
    )
    if not candidates:
        raise ClockControlError("USB clock not found (no CH340 serial device connected)")
    if len(candidates) > 1:
        devices = ", ".join(candidates)
        raise ClockControlError(
            f"multiple CH340 serial devices found: {devices}; use --port explicitly"
        )
    return candidates[0]


def open_connection(port: str, baud: int) -> serial.Serial:
    # Configure modem-control lines before opening the device. This CH340
    # revision wires DTR/RTS to auto-reset; opening first and releasing later
    # creates a reset pulse and loses the clock's RAM-only wall time.
    connection = serial.Serial()
    connection.port = resolve_port(port)
    connection.baudrate = baud
    connection.timeout = 0.15
    connection.dtr = False
    connection.rts = False
    connection.open()
    # The port-open reset is hardware behavior on this board. Wait for setup()
    # and the display initialization to finish, then discard its startup lines.
    time.sleep(1.4)
    connection.reset_input_buffer()
    return connection


def read_response(connection: serial.Serial, long_response: bool = False) -> str:
    deadline = time.monotonic() + (1.2 if long_response else 0.7)
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        chunk = connection.read(4096)
        if chunk:
            chunks.append(chunk)
            deadline = time.monotonic() + 0.18
    return b"".join(chunks).decode("utf-8", errors="replace").strip()


def validate_commands(commands: list[str]) -> None:
    if not commands or len(commands) > 32:
        raise ClockControlError("a request must contain 1-32 commands")
    if any(
        not isinstance(command, str)
        or not 1 <= len(command) <= 255
        or "\n" in command
        or "\r" in command
        for command in commands
    ):
        raise ClockControlError("commands must be bounded single lines")


def transact_connection(connection: serial.Serial, commands: list[str]) -> str:
    validate_commands(commands)
    responses: list[str] = []
    for command in commands:
        connection.write((command + "\n").encode("utf-8"))
        connection.flush()
        response = read_response(connection, command == "HELP")
        if response:
            responses.append(response)
            if any(line.startswith("ERR") for line in response.splitlines()):
                break
    return "\n".join(responses)


def proxy_request(payload: dict[str, object], timeout: float = 5.0) -> str:
    request = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(DEFAULT_SOCKET))
        client.sendall(request)
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_PROXY_RESPONSE_BYTES:
                raise ClockControlError("clock service response is too large")
    except OSError as exc:
        raise ClockControlError("clock service is unavailable") from exc
    finally:
        client.close()
    try:
        decoded = json.loads(bytes(response).split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClockControlError("clock service returned an invalid response") from exc
    if not isinstance(decoded, dict):
        raise ClockControlError("clock service returned an invalid response")
    error = decoded.get("error")
    if isinstance(error, str) and error:
        raise ClockControlError(error)
    result = decoded.get("response")
    if not isinstance(result, str):
        raise ClockControlError("clock service returned an invalid response")
    return result


def transact(port: str, baud: int, commands: list[str]) -> str:
    validate_commands(commands)
    if DEFAULT_SOCKET.exists():
        return proxy_request({"operation": "commands", "commands": commands})
    connection = open_connection(port, baud)
    try:
        return transact_connection(connection, commands)
    finally:
        connection.close()


def fnv1a(data: bytes) -> int:
    value = 2166136261
    for byte in data:
        value = (value ^ byte) * 16777619 & 0xFFFFFFFF
    return value


def gallery_upload_connection(
    connection: serial.Serial, restore_commands: list[str], slot: int, raw: bytes
) -> str:
    if not 0 <= slot < 7 or len(raw) != GALLERY_BYTES:
        raise ClockControlError("gallery upload needs slot 0-6 and 115200 bytes")
    validate_commands(restore_commands)
    responses: list[str] = []
    for command in restore_commands:
        connection.write((command + "\n").encode())
        connection.flush()
        response = read_response(connection)
        if not response or any(line.startswith("ERR") for line in response.splitlines()):
            raise ClockControlError("clock display state could not be restored before upload")
        responses.append(response)
    connection.write(f"GALLERY BEGIN {slot} {len(raw)}\n".encode())
    connection.flush()
    response = read_response(connection)
    if "OK gallery ready" not in response:
        raise ClockControlError("clock could not prepare gallery storage")
    responses.append(response)
    connection.write(raw)
    connection.flush()
    response = read_response(connection, True)
    if "OK gallery data received" not in response:
        raise ClockControlError("gallery data transfer failed")
    responses.append(response)
    connection.write(f"GALLERY END {fnv1a(raw):08X}\n".encode())
    connection.flush()
    response = read_response(connection)
    if "OK gallery uploaded" not in response:
        raise ClockControlError("clock rejected the gallery upload")
    responses.append(response)
    return "\n".join(responses)


def gallery_upload(
    port: str, baud: int, restore_commands: list[str], slot: int, raw: bytes
) -> str:
    if DEFAULT_SOCKET.exists():
        return proxy_request(
            {
                "operation": "gallery_upload",
                "commands": restore_commands,
                "slot": slot,
                "data": base64.b64encode(raw).decode("ascii"),
            },
            timeout=30.0,
        )
    connection = open_connection(port, baud)
    try:
        return gallery_upload_connection(connection, restore_commands, slot, raw)
    finally:
        connection.close()


def interactive_shell(port: str, baud: int) -> int:
    if DEFAULT_SOCKET.exists():
        print("Clock service shell. Type firmware commands, or EXIT to close.")
        while True:
            try:
                command = input("clock> ").strip()
            except EOFError:
                print()
                return 0
            if not command:
                continue
            if command.upper() in {"EXIT", "QUIT"}:
                return 0
            try:
                response = transact(port, baud, [command])
            except ClockControlError as exc:
                print(f"clockctl: {exc}", file=sys.stderr)
                continue
            print(response or "(no response)")
    try:
        connection = open_connection(port, baud)
    except (serial.SerialException, ClockControlError) as exc:
        print(f"clockctl: {exc}", file=sys.stderr)
        return 2
    print("USB clock shell. Type firmware commands, or EXIT to close.")
    try:
        while True:
            try:
                command = input("clock> ").strip()
            except EOFError:
                print()
                return 0
            if not command:
                continue
            if command.upper() in {"EXIT", "QUIT"}:
                return 0
            connection.write((command + "\n").encode("utf-8"))
            connection.flush()
            response = read_response(connection, command.upper() == "HELP")
            print(response or "(no response)")
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    if args.action == "find-port":
        try:
            print(resolve_port(args.port))
        except ClockControlError as exc:
            print(f"clockctl: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.action == "shell":
        return interactive_shell(args.port, args.baud)
    try:
        commands = commands_for(args)
    except ValueError as exc:
        print(f"clockctl: {exc}", file=sys.stderr)
        return 2
    try:
        response = transact(args.port, args.baud, commands)
    except (serial.SerialException, ClockControlError) as exc:
        print(f"clockctl: {exc}", file=sys.stderr)
        return 2

    if response:
        print(response)
    else:
        print("clockctl: no response from clock", file=sys.stderr)
        return 3
    return 1 if any(line.startswith("ERR") for line in response.splitlines()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
