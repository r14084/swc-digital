# Claude session hand-off — Smart Weather Clock

Read `AGENTS.md` and `USB_CLOCK.md` before editing, building, or flashing.
They are the source of truth for hardware safety, recovery, and user commands.

## Current product

This is a USB-only ESP8266 clock auto-detected through its CH340 USB bridge; it must never be
given Wi-Fi credentials or cloud dependencies. It has six modes; the new Face
mode is flashed, USB-verified, and visually confirmed on the physical display:

- **Face**: two cyan LED-style eyes on black with partial-region blink/look
  animation and Auto, Neutral, Happy, Focus, Curious, Sleepy, Alert, and
  Celebrate moods. Face selection and mood persist across USB resets.
- **Usage**: six one-column bars for C1–C3 Claude session usage and X1–X3
  Codex weekly usage, including active-account, recommendation, trend, and
  sync indicators.
- **BTC**: current BTC/USD price, 24-hour change, and 24 normalized one-hour
  candles. The Mac fetches public Coinbase Exchange data; the clock uses no key.
- **Clock**: large local time supplied by the Mac over USB.
- **Thai**: visually verified Noto Sans Thai test view showing `สวัสดี`.
- **Gallery**: up to seven local 240 × 240 images with a 3–300 second slideshow.
  Photos and slideshow state live only in the clock's LittleFS partition.
- **Reminders**: up to eight EEPROM-backed daily alerts. A verified alert stays
  steady for about 60 seconds and then returns to the previous screen.

V2-A is flashed and installed. `tools/clock_service.py` owns one persistent
serial connection, proxies normal CLI/GUI/Gallery operations through a user-only
Unix socket, and supplies RAM-only Auto emotions from Mac idle time. Stable
serial ownership, socket CLI routing, cached state, and `auto_emotion=focus` are
verified. The user visually confirmed the V2-A transitions and animation.

Run the local GUI with:

```sh
uv run --with pyserial --with pillow tools/clock_gui.py
```

It listens only on `127.0.0.1:8765`. The GUI is the normal path for gallery
uploads and slideshow control; do not replace it with a network service.
Its forms use a per-process anti-CSRF token, image requests are bounded, and all
actions restore cached RAM-only display data after the USB-open reset. Gallery
uploads use a checksum-verified temporary file before replacing a slot.

## Usage automation

The private `tools/usage-collector.toml` and its state file are Git-ignored.
Never print their contents, OAuth tokens, Keychain values, provider stderr, or
account identifiers. The collector rotates one Claude account per five-minute
run because Claude's undocumented endpoint rate-limits requests; it caches the
other Claude values. Codex profiles refresh every run. The Usage GUI action
publishes cached Claude values and refreshes Codex only.

The stable ignored helper is `tools/.bin/claude_profile_vault`. Keychain prompts
require the user's macOS login password; only the user should enter it.

## Safe device workflow

1. Preserve the dirty worktree and each device's private stock backup.
2. Build only `clock_usb` and run `git diff --check` plus `esptool image-info`.
3. Before a flash, verify the SHA-256 of that device's private stock backup.
4. Flash only at 115200, then verify the written-data hash and a USB response.
5. Ask the user to visually confirm every visual change. A passing build or
   serial response alone is not visual confirmation.

Opening the serial port resets the clock. Restore time in the same connection
before sending dynamic data; `clockctl.py` and the collector already do this.
The collector caches the preceding complete usage values and sends them as
optional `USAGE` history so trend arrows survive those resets. A fresh collector
state may contain `null` Claude slots while the first three rotations complete;
that partial baseline is valid and must remain loadable.
Gallery slideshow playback persists through those resets, while Clock and Usage
are intentionally RAM-only. BTC market data is RAM-only, but BTC and Face screen
selection persist so collector resets return to the selected screen. Face also
persists its selected mood.

Do not commit, push, publish, erase flash, restore stock firmware, or alter
user credentials unless the user explicitly asks.
