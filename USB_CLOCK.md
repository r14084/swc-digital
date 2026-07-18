# USB Clock firmware

This project contains a USB-only firmware target for the ESP8266 Smart Weather
Clock connected through a CH340 USB-to-serial bridge. Normal CLI, GUI, and
service operation auto-detects the clock, so moving it to another USB port does
not require changing `/dev/cu.usbserial-*` paths. It does not start Wi-Fi.

To print the current device path without opening or resetting the clock:

```sh
uv run --with pyserial tools/clockctl.py find-port
```

## Build

```sh
uvx --from platformio platformio run -e clock_usb
```

The firmware image is written to `.pio/build/clock_usb/firmware.bin`.

## Configure over USB

Run the CLI through `uv` so its `pyserial` dependency stays isolated:

```sh
uv run --with pyserial tools/clockctl.py status
uv run --with pyserial tools/clockctl.py sync-time
uv run --with pyserial tools/clockctl.py set title "PHUD CLOCK"
uv run --with pyserial tools/clockctl.py set city "BANGKOK"
uv run --with pyserial tools/clockctl.py set accent "#36D6C4"
uv run --with pyserial tools/clockctl.py set brightness 70
uv run --with pyserial tools/clockctl.py usage 42 57 18 31 64 9
uv run --with pyserial tools/clockctl.py save
```

Settings are staged in RAM and redraw immediately. Run `save` to persist them.
The `clockctl.py set` command automatically sends `SAVE` in the same USB session.
Raw serial commands remain staged until `SAVE` is sent.

## Account usage screen

The six bars use one full-width column: Claude `C1`–`C3`, then weekly Codex
`X1`–`X3`. The upper recommendation is split into two columns, one for Claude
and one for Codex. It also shows sync health and a per-update trend arrow. The
clock receives only six percentages, never account credentials.

Push a complete snapshot with:

```sh
uv run --with pyserial tools/clockctl.py usage \
  <claude-1-pct> <claude-2-pct> <claude-3-pct> \
  <codex-1-weekly-pct> <codex-2-weekly-pct> <codex-3-weekly-pct> \
  --active c2
```

All values must be integers from 0 to 100. Usage is RAM-only (like the clock
time) and is deliberately not written to flash. A `usage` update redraws only
the bar area, so a host-side polling job does not make the screen blink.
`clockctl.py usage` restores the RAM-only time in that same USB session before
it sends the snapshot, because opening this board's serial port resets it.
The collector also sends the preceding complete snapshot as optional protocol
history, so the trend arrows still work even though every USB-open reset clears
the ESP8266's RAM.

Mark the account currently in use with `--active c1` through `--active x3`
(or `--active off`). The highlighted dot is RAM-only and must be sent with the
usage snapshot because opening a new USB session resets the board.

Bars turn amber at 70% and red at 90%. The raw `RESETS` command supplies six
minutes-until-quota-reset values (`-1` = unknown) in the same account order as
`USAGE`; the dashboard then shows a live countdown on the sync line for the
most relevant account per provider (the active account, otherwise the fullest
one). Reset times are RAM-only like usage; the collector sends them with every
snapshot and `STATUS` reports them as `reset_minutes=`.

```sh
uv run --with pyserial tools/clockctl.py usage 42 57 18 31 64 9 --active c2
```

## Local control panel

Run a small browser GUI on this Mac (it listens only on `127.0.0.1`):

```sh
uv run --with pyserial --with pillow tools/clock_gui.py
```

It opens `http://127.0.0.1:8765` and provides an LED Face controller plus buttons
for the Usage dashboard, BTC/USD, a large Clock view, a Thai font test showing
`สวัสดี`, brightness, an LCD test pattern, and a seven-slot digital gallery.
Gallery uploads are centre-cropped to 240 × 240, then stored only in
the clock's local flash. The slideshow cycles through populated slots every
10 seconds by default (adjustable from 3 to 300 seconds). The Usage button uses
the cached Claude values and refreshes Codex only, so it does not force a Claude
request or trigger its rate limit. With the persistent service, the Clock view
stays selected through usage updates; a device/service reconnect returns it to
the non-persistent default unless another screen is selected afterward.

The control panel is organized into four task tabs: **Home** for screen changes,
**Reminders** for eight daily alert cards, **Gallery** for uploads and slideshow
control, and **Settings** for brightness and the LCD test. It remembers the last
open tab after a form action and uses a single result banner for clear feedback.
With V2-A installed, every control action—including binary Gallery upload—uses
the persistent service's user-only Unix socket, so it does not reopen the CH340
or reset the clock. If the service is absent, the tools fall back to direct USB
and restore cached time, usage, reset, active-account, and BTC data in that same
session. Forms include a local anti-CSRF token, and uploads are limited to 16 MB
before conversion.

The same screen selection is available from Terminal:

```sh
uv run --with pyserial tools/clockctl.py screen clock
uv run --with pyserial tools/clockctl.py screen face
uv run --with pyserial tools/clockctl.py screen usage
uv run --with pyserial tools/clockctl.py screen btc
uv run --with pyserial tools/clockctl.py screen thai
```

## LED Face screen

The Face screen draws only two cyan LED-style eyes on black. It uses small dirty
regions for blinking, glancing, alert pulsing, and celebration movement; the
animation does not clear or redraw the full panel on every frame. The physical
Face screen and animation are visually confirmed. Select a mood from the Home
tab or from Terminal:

```sh
uv run --with pyserial tools/clockctl.py face auto
uv run --with pyserial tools/clockctl.py face happy
uv run --with pyserial tools/clockctl.py face focus
uv run --with pyserial tools/clockctl.py face curious
uv run --with pyserial tools/clockctl.py face sleepy
uv run --with pyserial tools/clockctl.py face alert
uv run --with pyserial tools/clockctl.py face celebrate
uv run --with pyserial tools/clockctl.py face neutral
```

Manual moods remain selected until changed. With V2-A active, `auto` uses Mac
idle time: Focus while active, Curious after five minutes, Sleepy after 30
minutes, and Happy for 30 seconds after activity resumes. The service sends the
current emotion as a RAM-only `EMOTION` override, so it does not wear EEPROM or
change a saved manual mood. Without the service, Auto falls back to the restored
local time: Sleepy from 23:00–06:59, Happy from 07:00–08:59, Focus from
09:00–17:59, and Neutral from 18:00–22:59; before time sync it uses Curious.
Face screen selection and the Auto/manual choice persist across resets.
The physical V2-A activity transitions and eye animation are visually confirmed.

## BTC/USD screen

The BTC screen shows the current whole-dollar BTC/USD price at the top, its
24-hour percentage change, and 24 one-hour OHLC candlesticks below. Green bodies
closed above their open; red bodies closed below it. Select it from **BTC / USD**
on the GUI Home tab or run:

```sh
uv run --with pyserial tools/clockctl.py screen btc
```

The Mac collector fetches the public Coinbase Exchange `BTC-USD` candle endpoint
at 1-hour granularity, normalizes the latest 24 candles, and sends a bounded
validated USB payload. No market-data API key is stored or sent to the clock.
BTC data is RAM-only; the last complete snapshot is cached privately on the Mac
and restored after USB resets. Selecting BTC persists the screen choice through
collector resets. Face has the same persistence behavior and also remembers its
mood. Choosing Usage, Clock, Thai, or Gallery clears the persisted screen choice.

## Daily reminders

The custom firmware stores up to eight daily reminders in EEPROM. A reminder
interrupts the current Face, Usage, BTC, Clock, Thai, or Gallery screen for about 60
seconds, then returns to the same screen. Reminder labels are currently limited
to 1–20 printable ASCII characters; the existing Thai test is a pre-rendered
bitmap and does not provide a general Thai text renderer.

```sh
uv run --with pyserial tools/clockctl.py remind set 0 14:00 "TAKE MEDICINE"
uv run --with pyserial tools/clockctl.py remind list
uv run --with pyserial tools/clockctl.py remind del 0
```

Reminders repeat daily until deleted and survive USB-triggered resets. Wall time
remains RAM-only, so reminders cannot fire after a reset until the Mac restores
time. After time is restored, the clock catches up reminders from the previous
two minutes. Choosing a screen or showing a gallery image during an alert
dismisses it immediately. The local control panel also provides Set/Delete
controls for all eight slots. Reminder list/set/delete sessions restore the
last complete cached usage snapshot and current time after the USB-open reset;
they do not poll Claude or Codex. The collector must have completed at least one
run with the current code before that display cache is available.
If multiple reminders are due in the same catch-up window, the firmware shows
them sequentially instead of silently marking later alerts as already shown.

## Persistent Mac controller and collector

`tools/clock_service.py` owns one persistent CH340 session, provides the local
command/Gallery proxy, polls Mac idle time for Auto emotions, and runs the usage
collector every five minutes. `tools/usage_collector.py` still performs the
provider reads: it refreshes all three Codex values, but just one Claude account
per run, retaining the other two cached values. This respects Claude's rate
limit while each Claude bar still updates at least every 15 minutes. Neither
component logs provider output or sends credentials to the clock.

Create your private configuration and replace all six placeholder commands with
read-only local commands that print a percentage or `{"percent": 42}`:

```sh
cp tools/usage-collector.toml.example tools/usage-collector.toml
uv run --with pyserial tools/usage_collector.py --once
uv run --with pyserial tools/clock_service.py
```

The optional `[emotion]` settings control polling and thresholds. Defaults are
five-second polling, Curious after 300 idle seconds, Sleepy after 1800 seconds,
and Happy for 30 seconds after return. macOS idle time comes from the local
`IOHIDSystem`; no keyboard events, window titles, or typed content are read.

The collector's state file is private and Git-ignored. If no prior Claude
values are cached, it fills C1, C2, and C3 across the first three rotations.
You can seed the first display immediately by adding the last observed values
to the private config:

```toml
claude_initial_percentages = [14, 14, 14]
```

The private configuration is ignored by Git. Once `--once` works and compatible
firmware is flashed, optionally install the persistent service at login. Copy
`tools/com.example.smart-weather-clock-usage.plist.example` to
`~/Library/LaunchAgents/com.example.smart-weather-clock-usage.plist`, replace
every `REPLACE_WITH_...` value with an absolute path on your Mac, then validate
and load it:

```sh
plutil -lint ~/Library/LaunchAgents/com.example.smart-weather-clock-usage.plist
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.example.smart-weather-clock-usage.plist
```

To stop it later, run:

```sh
launchctl bootout gui/$(id -u) \
  ~/Library/LaunchAgents/com.example.smart-weather-clock-usage.plist
```

### Claude provider

`tools/claude_usage.py --keychain --window session` prints the live current
session usage for the Claude Code OAuth identity in the macOS Keychain. Its
token remains in process memory and is never logged. Claude's usage endpoint is
undocumented, so the adapter fails closed if its response changes or fails.

The macOS Keychain currently supplies one shared Claude Code identity. It can
therefore provide a live source for one Claude account only; fully live polling
of all three needs separate credential sources for the second and third account.

Build the stable Keychain helper once. Its fixed executable identity lets macOS
grant the LaunchAgent persistent access:

```sh
mkdir -p tools/.bin
swiftc tools/claude_profile_vault.swift -o tools/.bin/claude_profile_vault
```

After logging into a Claude profile, capture its currently active credential
without writing it to a file or command line. This stores it in a private,
profile-specific macOS Keychain entry for the collector:

```sh
tools/.bin/claude_profile_vault capture c2
```

Choose **Always Allow** if Keychain prompts. The collector calls this same
stable helper with `percent c2`, so its Keychain access remains restricted to
the helper that created the private entry.

### Codex provider

`tools/codex_usage.py --profile your-profile` prints a Codex profile's primary
weekly window percentage. It reads only that profile's existing local OAuth
token in memory and sends a read-only usage request. By default it looks below
`~/.codex-profiles`; pass `--profile-root` or set `CODEX_PROFILE_ROOT` when your
profiles live elsewhere. Keep those paths in the private collector config, not
in a committed template.

The time is intentionally not persisted because an ESP8266 has no battery-backed
RTC. The persistent service restores it once at startup and retains the USB
session. `clockctl.py` automatically uses the service socket when present; its
shell also proxies commands without taking ownership of the serial device:

```sh
uv run --with pyserial tools/clockctl.py shell
```

Recovery controls remain available even if the screen is unreadable:

```sh
uv run --with pyserial tools/clockctl.py test
uv run --with pyserial tools/clockctl.py set rotation 1
uv run --with pyserial tools/clockctl.py set blinvert off
uv run --with pyserial tools/clockctl.py show
uv run --with pyserial tools/clockctl.py help
```

## Flash

Stop the persistent service before flashing so esptool can own the serial device.
The tested clock has a noisy serial link at higher rates, so use 115200. Resolve
the current device name before passing it to esptool (auto-discovery applies to
the clock tools, not esptool):

```sh
launchctl bootout gui/$(id -u) \
  ~/Library/LaunchAgents/com.example.smart-weather-clock-usage.plist
uvx --from esptool esptool \
  --port "$(uv run --with pyserial tools/clockctl.py find-port)" \
  --baud 115200 \
  write-flash 0x0 .pio/build/clock_usb/firmware.bin
```

## Make a private stock backup before first flash

Before flashing a new device, read and verify its entire 4 MB flash into a
private location outside this repository. Treat the backup as sensitive: stock
firmware can contain saved Wi-Fi credentials. Do not commit, upload, or attach
the backup to an issue. Keep its SHA-256 with the backup so you can verify it
before a recovery flash.
