# Handoff: Reminder / time-alert feature

Status: implemented, flashed, and visually verified on 2026-07-17. The user
confirmed a 60-second alert returned to Usage without flickering or blanking.
The later audit build added sequential handling for reminders due together,
was flashed with protocol verification, and the user visually reconfirmed that
the physical display remained normal.
Read `AGENTS.md` and `USB_CLOCK.md` first — they are the source of truth for
hardware safety, the build/flash workflow, and recovery commands. This file
only adds the reminder design agreed with the user.

## Feature request (user-approved behavior)

The user wants scheduled reminders, e.g. "take medicine at 14:00":

- A reminder fires on the device itself at its HH:MM, using the RAM clock.
- When it fires, a full-screen alert interrupts whatever screen is showing
  (Usage, BTC, Clock, Thai, or Gallery).
- After ~60 seconds the display automatically returns to the exact previous
  screen. There are no buttons on the device, so auto-return is the only
  dismissal.
- Reminders repeat daily until deleted.
- Labels are ASCII for now (e.g. `TAKE MEDICINE`). Thai labels are a possible
  follow-up using the same pre-rendered bitmap technique as the existing Thai
  screen (`thai_greeting_bitmap.h`), and are OUT OF SCOPE for this pass.

## Critical constraints that shape the design

1. **The board resets every time a serial session opens** (CH340 auto-reset).
   The usage collector LaunchAgent opens the port every 5 minutes, so any
   RAM-only reminder store would be wiped several times an hour.
   → Reminders MUST persist in EEPROM, like title/city.
2. **Wall time is RAM-only** and restored by the collector's `SET TIME` within
   ≤5 minutes of any reset. After a reset and before the first time sync the
   device cannot fire reminders; this is accepted.
3. **A reset can eat an alert** (e.g. the collector opens the port at 14:00
   sharp). Mitigation: catch-up logic — after boot + first time sync, fire any
   reminder whose time is within the last 2 minutes and was not marked fired.
4. **Partial redraw rule** (see `AGENTS.md`): never redraw the full panel every
   second. The alert screen is drawn once; only entry/exit are full redraws.
5. Keep bounded fixed-size buffers; validate all serial input; never echo
   secrets.

## Current firmware state (verify before editing)

`src/usb_clock.cpp` already has, all working and flashed as of 2026-07-17:

- Five screens: `ScreenMode { USAGE, BTC, CLOCK, THAI, GALLERY }` (THAI is a
  visually-verified Noto Sans Thai bitmap view — do not break it).
- Usage bars with threshold colors (amber ≥70, red ≥90), trend arrows,
  active-account dot.
- `RESETS` command + quota-reset countdown on the usage sync line
  (`resetDurationMillis[6]`, `resetBaseMillis`, `resetKnownMask`).
- EEPROM map: 512 bytes total; `PersistedConfig` at offset 0 (~70 bytes),
  `GalleryPlayback` at offset 128 (12 bytes + checksum pattern).
  **Free region: offset 160 through 511.**
- Main `loop()` dispatches on `screenDirty` / `usageDirty` / per-minute ticks.

Mac side: `tools/usage_collector.py` can run through a private LaunchAgent
every 5 min and publishes
`SET TIME` + `USAGE` + `RESETS` (+ `ACTIVE` and public `BTC`) in one session.
The `USAGE` payload may include the preceding six values so trend arrows survive
the USB reset.
`tools/clock_gui.py` is the local browser GUI on 127.0.0.1:8765.
`tools/clockctl.py` is the CLI; `clockctl.transact()` is the shared USB helper.

## Design

### Firmware storage (EEPROM offset 160)

```cpp
constexpr uint32_t REMINDER_MAGIC = 0x524D4E44;  // "RMND"
constexpr uint8_t REMINDER_SLOTS = 8;

struct ReminderStore {
  uint32_t magic;
  struct {
    uint16_t minuteOfDay;  // 0-1439, 0xFFFF = empty slot
    char label[21];        // ASCII, NUL-terminated
    uint8_t enabled;
  } slots[REMINDER_SLOTS];
  uint32_t checksum;       // same FNV pattern as PersistedConfig
};
```

≈ 4 + 8×24 + 4 = 200 bytes → fits 160..360. Reuse the existing
checksum/`EEPROM.put`/`EEPROM.get` pattern (`checksumConfig` as template).
Write EEPROM only on `REMIND SET/DEL` — not on fire, not periodically.

RAM-only runtime state (not persisted):

```cpp
uint8_t reminderFiredMask = 0;      // bit per slot, cleared at midnight
uint16_t lastReminderCheckMinute;   // to detect minute change + midnight wrap
ScreenMode alertReturnMode;         // screen to restore
bool alertActive = false;
uint32_t alertStartMillis = 0;
```

### Serial protocol

```
REMIND SET <slot 0-7> <HH:MM> <text up to 20 chars>
REMIND DEL <slot>
REMIND LIST                     -> one line per slot: REMIND <n> <HH:MM|off> <text>
```

- `REMIND SET/DEL` persist immediately (no separate SAVE needed — mirror how
  gallery playback persists itself), reply `OK`.
- Add the three lines to `printHelp()` and a `reminders=<n>` count to
  `printStatus()`.
- Parse with the same `strtok`/bounds-check style as `parseUsage`.

### Alert behavior (in `loop()`)

Once per minute (reuse the existing minute-tick pattern) and only when
`timeWasSet`:

1. `minute = currentMinuteOfDay()`. On wrap past midnight
   (`minute < lastReminderCheckMinute`) clear `reminderFiredMask`.
2. For each enabled slot: fire if `minuteOfDay == minute` and its fired bit is
   clear. Catch-up: also fire if `0 < minute - minuteOfDay <= 2` and unfired
   (covers boot/reset eating the exact minute).
3. Do not scan or mark another slot while an alert is active. On fire: set the
   fired bit, save `alertReturnMode = screenMode`, set `alertActive` and
   `alertStartMillis`, then switch to the alert view (full redraw once).
4. While `alertActive`: after 60 s restore `screenMode = alertReturnMode`,
   `screenDirty = true`, `alertActive = false`, then re-check the same catch-up
   window. This displays reminders due together sequentially.
5. Any user `SCREEN`/`GALLERY SHOW` command during an alert cancels the alert
   and honors the command (user intent wins).
6. Alert must not fight the gallery slideshow timer: skip gallery advance while
   `alertActive`.

Alert view: full black screen, accent-colored top band (reuse style), big
centered `!` header or bell glyph, reminder label at text size 2 centered,
HH:MM below, and a thin countdown hint (`BACK IN 60S` static text is fine —
do NOT tick it every second). Optional attention flash: toggle backlight
2–3× via `applyBrightness()` values on entry only.

Reset interaction: an alert interrupted by a serial-open reset simply
disappears; the catch-up rule (step 2) re-fires it after time sync. The fired
mask is RAM so it clears on reset — the 2-minute catch-up window bounds
duplicate alerts to that window; acceptable.

### clockctl.py

Add a `remind` subcommand mapping to raw commands:

```
clockctl.py remind set 0 14:00 "TAKE MEDICINE"
clockctl.py remind del 0
clockctl.py remind list
```

Reminder commands prepend current time and the collector's last complete cached
usage snapshot. This was added after hardware testing showed that the serial-open
reset otherwise changed the Usage screen to `MAC OFFLINE`. Cache restoration is
local-only and does not poll any provider.

### clock_gui.py

Add a "Reminders" card: 8 rows, each with time input + text input + Set/Delete,
populated via `REMIND LIST` on page load. Reuse the existing transact plumbing.
Keep it 127.0.0.1-only, as-is.

### Docs

Update `USB_CLOCK.md` (commands + behavior), `AGENTS.md` "Current verified
state", and `CLAUDE.md` mode list after the user visually confirms.

## Build / flash / verify (mandatory sequence)

```sh
uvx --from platformio platformio run -e clock_usb
git diff --check
uvx --from esptool esptool image-info .pio/build/clock_usb/firmware.bin
shasum -a 256 /private/path/to/your-stock-backup.bin
launchctl bootout gui/$(id -u)/com.example.smart-weather-clock-usage  # if installed
uvx --from esptool esptool --port "$(uv run --with pyserial tools/clockctl.py find-port)" --baud 115200 \
  write-flash 0x0 .pio/build/clock_usb/firmware.bin           # expect "Hash of data verified"
# Re-start your private LaunchAgent if you installed one.
```

Test plan (single `clockctl.transact` sessions; remember every port open
resets the board and wipes RAM time):

1. `SET TIME <now>` + `REMIND SET 0 <now+2min> TEST PILL` + `REMIND LIST` →
   verify echo.
2. Wait without opening the port; alert must appear at the set minute and
   auto-return to Usage after ~60 s (collector repopulates usage within 5 min).
3. Reset-survival: open a new session (`clockctl.py status`) → `REMIND LIST`
   must still show slot 0 (EEPROM).
4. Catch-up: set a reminder 1 minute in the past + `SET TIME` → must fire
   within the check tick.
5. Midnight wrap: set time 23:59, reminder 00:00 → fires after wrap.
6. `REMIND DEL 0` → LIST shows slot off; no alert next day.
7. Ask the user to visually confirm the alert layout — a serial response is
   NOT sufficient (AGENTS.md rule).

## Out of scope / future

- Thai reminder labels via Mac-side pre-rendered bitmaps (same technique as
  the Thai screen). Would need a small per-slot bitmap store in LittleFS.
- Repeat patterns (weekdays only), multiple fires per reminder, snooze.
- AI screen (`codex exec`) — separate brainstorm, see conversation notes:
  preferred approach is Mac-rendered 240×240 image streamed to the display
  without a flash write; hourly refresh; not started.

## Session hygiene reminders

- Do not commit or push; the dirty worktree is user-owned.
- Never print the private TOML, state file, tokens, or provider stderr.
- Rebuilding `tools/.bin/claude_profile_vault` changes its Keychain identity
  and re-triggers password prompts for the user — avoid rebuilding it unless
  required, and warn the user first.
