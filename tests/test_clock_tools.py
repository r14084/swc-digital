from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import clockctl  # noqa: E402
import clock_gui  # noqa: E402
import clock_service  # noqa: E402
import crypto_market  # noqa: E402
import usage_collector  # noqa: E402


def config() -> dict[str, object]:
    return {
        "collector": {},
        "sources": {},
    }


class CollectorStateTests(unittest.TestCase):
    def test_percentage_parser_rejects_boolean_and_fractional_values(self) -> None:
        for output in ('{"percent":true}', '{"percent":42.5}'):
            with self.subTest(output=output):
                with self.assertRaises(usage_collector.CollectorError):
                    usage_collector.parse_usage(output, "test")

    def test_partial_claude_baseline_survives_the_next_rotation(self) -> None:
        state = {
            "claude_values": [42, None, None],
            "next_claude_index": 1,
            "claude_reset_epochs": [None, None, None],
            "last_snapshot": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state))
            loaded = usage_collector.load_state(config(), path)
        self.assertEqual(loaded["claude_values"], [42, None, None])
        self.assertEqual(loaded["next_claude_index"], 1)

    def test_cache_keeps_the_previous_values_for_trend_arrows(self) -> None:
        state = {
            "last_snapshot": {
                "values": [1, 2, 3, 4, 5, 6],
            }
        }
        usage_collector.cache_snapshot(
            state,
            [6, 5, 4, 3, 2, 1],
            None,
            [-1] * 6,
            None,
        )
        self.assertEqual(
            state["last_snapshot"]["previous_values"],
            [1, 2, 3, 4, 5, 6],
        )

    def test_publish_rejects_an_empty_serial_response(self) -> None:
        with mock.patch.object(usage_collector.clockctl, "transact", return_value=""):
            with self.assertRaisesRegex(
                usage_collector.CollectorError, "did not respond"
            ):
                usage_collector.publish(config(), [1, 2, 3, 4, 5, 6], None)

    def test_publish_can_use_the_persistent_service_transport(self) -> None:
        transport = mock.Mock(return_value="OK usage updated")
        usage_collector.publish(
            config(), [1, 2, 3, 4, 5, 6], None, transact_fn=transport
        )
        commands = transport.call_args.args[0]
        self.assertTrue(commands[0].startswith("SET TIME "))
        self.assertEqual(commands[1], "USAGE 1 2 3 4 5 6")


class CachedDisplayTests(unittest.TestCase):
    def test_restore_includes_previous_values_and_rounds_reset_up(self) -> None:
        snapshot = {
            "last_snapshot": {
                "values": [10, 20, 30, 40, 50, 60],
                "previous_values": [9, 21, 30, 39, 50, 61],
                "reset_epochs": [time.time() + 1] + [None] * 5,
                "active": "c1",
                "btc": None,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(snapshot))
            commands = clockctl.cached_display_commands(path)
        self.assertIn(
            "USAGE 10 20 30 40 50 60 9 21 30 39 50 61",
            commands,
        )
        self.assertIn("RESETS 1 -1 -1 -1 -1 -1", commands)

    def test_face_command_restores_cached_data_before_selecting_mood(self) -> None:
        args = SimpleNamespace(action="face", mood="happy")
        with mock.patch.object(
            clockctl, "cached_display_commands", return_value=["SET TIME 12:34"]
        ):
            commands = clockctl.commands_for(args)
        self.assertEqual(commands, ["SET TIME 12:34", "FACE HAPPY"])

    def test_face_is_a_supported_screen_mode(self) -> None:
        args = SimpleNamespace(action="screen", mode="face")
        with mock.patch.object(
            clockctl, "cached_display_commands", return_value=["SET TIME 12:34"]
        ):
            commands = clockctl.commands_for(args)
        self.assertEqual(commands, ["SET TIME 12:34", "SCREEN FACE"])


class CryptoValidationTests(unittest.TestCase):
    def test_valid_snapshot_is_normalized(self) -> None:
        candles = "3264003C" * crypto_market.CANDLES
        snapshot = crypto_market.validate_snapshot(
            {"price_cents": 1, "change_bps": 0, "candles": candles.lower()}
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["candles"], candles)

    def test_snapshot_rejects_ohlc_outside_its_range(self) -> None:
        invalid = "6564003C" * crypto_market.CANDLES
        self.assertIsNone(
            crypto_market.validate_snapshot(
                {"price_cents": 1, "change_bps": 0, "candles": invalid}
            )
        )

    def test_snapshot_rejects_boolean_numbers(self) -> None:
        candles = "3264003C" * crypto_market.CANDLES
        self.assertIsNone(
            crypto_market.validate_snapshot(
                {"price_cents": True, "change_bps": 0, "candles": candles}
            )
        )


class GuiRenderingTests(unittest.TestCase):
    def test_every_form_contains_the_csrf_token(self) -> None:
        page = clock_gui.render_page("Ready", [("", "")] * 8, "test-token").decode()
        self.assertGreater(page.count("<form"), 0)
        self.assertEqual(page.count("<form"), page.count('name="csrf_token"'))
        self.assertNotIn("__REMINDERS__", page)
        for mood in clock_gui.FACE_MOODS:
            self.assertIn(f'value="{mood}"', page)

    def test_face_control_sends_one_validated_mood(self) -> None:
        with mock.patch.object(clock_gui, "clock_commands") as commands:
            message = clock_gui.set_face_screen({"mood": ["curious"]})
        commands.assert_called_once_with(["FACE CURIOUS"], restore_display=True)
        self.assertIn("curious", message)

    def test_face_control_rejects_unknown_mood(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "valid Face mood"):
            clock_gui.set_face_screen({"mood": ["unknown"]})


class PersistentServiceTests(unittest.TestCase):
    def test_auto_port_follows_the_only_connected_ch340(self) -> None:
        ports = [
            SimpleNamespace(
                device="/dev/cu.usbserial-10", vid=0x1A86, pid=0x7523
            ),
            SimpleNamespace(device="/dev/cu.debug-console", vid=None, pid=None),
        ]
        with mock.patch.object(clockctl.list_ports, "comports", return_value=ports):
            self.assertEqual(clockctl.resolve_port("auto"), "/dev/cu.usbserial-10")

    def test_auto_port_refuses_to_guess_between_two_ch340_devices(self) -> None:
        ports = [
            SimpleNamespace(device=f"/dev/cu.usbserial-{suffix}", vid=0x1A86, pid=0x7523)
            for suffix in ("10", "20")
        ]
        with mock.patch.object(clockctl.list_ports, "comports", return_value=ports):
            with self.assertRaisesRegex(clockctl.ClockControlError, "multiple CH340"):
                clockctl.resolve_port("auto")

    def test_explicit_port_bypasses_discovery(self) -> None:
        with mock.patch.object(clockctl.list_ports, "comports") as comports:
            self.assertEqual(
                clockctl.resolve_port("/dev/cu.usbserial-custom"),
                "/dev/cu.usbserial-custom",
            )
        comports.assert_not_called()

    def test_find_port_prints_without_opening_the_clock(self) -> None:
        args = SimpleNamespace(action="find-port", port="auto")
        with (
            mock.patch.object(clockctl, "parse_args", return_value=args),
            mock.patch.object(
                clockctl, "resolve_port", return_value="/dev/cu.usbserial-10"
            ),
            mock.patch.object(clockctl, "open_connection") as open_connection,
            mock.patch("builtins.print") as print_output,
        ):
            self.assertEqual(clockctl.main(), 0)
        print_output.assert_called_once_with("/dev/cu.usbserial-10")
        open_connection.assert_not_called()

    def test_idle_parser_converts_nanoseconds(self) -> None:
        output = b'| |   "HIDIdleTime" = 12500000000\n'
        self.assertEqual(clock_service.parse_idle_seconds(output), 12.5)

    def test_emotion_engine_greets_after_away_then_returns_to_focus(self) -> None:
        engine = clock_service.EmotionEngine(300, 1800, 30)
        self.assertEqual(engine.mood(10, 0), "focus")
        self.assertEqual(engine.mood(400, 10), "curious")
        self.assertEqual(engine.mood(1, 20), "happy")
        self.assertEqual(engine.mood(1, 49), "happy")
        self.assertEqual(engine.mood(1, 50), "focus")
        self.assertEqual(engine.mood(1800, 60), "sleepy")

    def test_emotion_thresholds_must_be_ordered(self) -> None:
        values = config()
        values["emotion"] = {
            "curious_after_seconds": 300,
            "sleepy_after_seconds": 300,
        }
        with self.assertRaisesRegex(clock_service.ClockServiceError, "must exceed"):
            clock_service.service_settings(values)

    def test_command_proxy_validates_and_forwards_bounded_commands(self) -> None:
        broker = mock.Mock()
        broker.transact.return_value = "OK emotion updated"
        response = clock_service.request_payload(
            broker,
            {"operation": "commands", "commands": ["EMOTION FOCUS"]},
        )
        self.assertEqual(response, "OK emotion updated")
        broker.transact.assert_called_once_with(["EMOTION FOCUS"])

    def test_command_proxy_rejects_embedded_newline(self) -> None:
        with self.assertRaisesRegex(clockctl.ClockControlError, "single lines"):
            clock_service.request_payload(
                mock.Mock(),
                {"operation": "commands", "commands": ["STATUS\nREBOOT"]},
            )

    def test_cli_uses_the_local_service_socket(self) -> None:
        broker = mock.Mock()
        broker.transact.return_value = "STATUS screen=face"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clock.sock"
            server = clock_service.ControlServer(path, broker)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(clockctl, "DEFAULT_SOCKET", path):
                    response = clockctl.transact("unused", 115200, ["STATUS"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertEqual(response, "STATUS screen=face")
        broker.transact.assert_called_once_with(["STATUS"])

    def test_gallery_upload_uses_the_same_service_socket(self) -> None:
        broker = mock.Mock()
        broker.gallery_upload.return_value = "OK gallery uploaded"
        raw = bytes(clockctl.GALLERY_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clock.sock"
            server = clock_service.ControlServer(path, broker)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(clockctl, "DEFAULT_SOCKET", path):
                    response = clockctl.gallery_upload(
                        "unused", 115200, ["SET TIME 12:34"], 2, raw
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertEqual(response, "OK gallery uploaded")
        broker.gallery_upload.assert_called_once_with(
            ["SET TIME 12:34"], 2, raw
        )


if __name__ == "__main__":
    unittest.main()
