#!/usr/bin/env python3
"""Local browser control panel for the USB Smart Weather Clock.

Run with ``uv run --with pyserial tools/clock_gui.py`` and open the address
printed in Terminal. The server binds only to this Mac's loopback interface.
"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from email.policy import default as email_policy
import html
import io
import re
import secrets
import struct
import subprocess
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import clockctl
import usage_collector


ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = ROOT / "tools" / "usage_collector.py"
CONFIG = ROOT / "tools" / "usage-collector.toml"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
CSRF_TOKEN = secrets.token_urlsafe(32)
FACE_MOODS = (
    "auto",
    "neutral",
    "happy",
    "focus",
    "curious",
    "sleepy",
    "alert",
    "celebrate",
)


def cached_commands() -> list[str]:
    try:
        config = usage_collector.load_config(CONFIG)
        state = usage_collector.state_path(config, CONFIG)
    except usage_collector.CollectorError:
        state = clockctl.DEFAULT_STATE
    return clockctl.cached_display_commands(state)


def clock_commands(commands: list[str], restore_display: bool = False) -> str:
    if restore_display:
        commands = cached_commands() + commands
    response = clockctl.transact(clockctl.DEFAULT_PORT, 115200, commands)
    if not response:
        raise RuntimeError("The clock did not respond. Check its USB cable.")
    if any(line.startswith("ERR") for line in response.splitlines()):
        raise RuntimeError(response)
    return response


def set_clock_screen() -> str:
    clock_commands(["SCREEN CLOCK"], restore_display=True)
    return "Clock view is now showing."


def set_thai_screen() -> str:
    clock_commands(["SCREEN THAI"], restore_display=True)
    return "Thai test screen is now showing."


def set_face_screen(form: dict[str, list[str]]) -> str:
    mood = form.get("mood", [""])[0].lower()
    if mood not in FACE_MOODS:
        raise RuntimeError("Choose a valid Face mood.")
    clock_commands([f"FACE {mood.upper()}"], restore_display=True)
    if mood == "auto":
        return "Face is now showing in automatic time-based mode."
    return f"Face is now showing the {mood} expression."


def set_btc_screen() -> str:
    refresh_usage()
    clock_commands(["SCREEN BTC"], restore_display=True)
    return "BTC/USD price and 1-hour candle chart refreshed."


def refresh_usage() -> str:
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), "--config", str(CONFIG), "--publish-cache"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=45,
    )
    message = (result.stdout or result.stderr).strip()
    if result.returncode:
        raise RuntimeError(message or "Usage refresh failed")
    return "Usage dashboard refreshed successfully."


def set_usage_screen() -> str:
    refresh_usage()
    clock_commands(["SCREEN USAGE"], restore_display=True)
    return "Usage dashboard refreshed and is now showing."


def set_brightness(form: dict[str, list[str]]) -> str:
    value = form.get("brightness", [""])[0]
    try:
        brightness = int(value)
    except ValueError as exc:
        raise RuntimeError("Brightness must be a number from 0 to 100.") from exc
    if not 0 <= brightness <= 100:
        raise RuntimeError("Brightness must be a number from 0 to 100.")
    clock_commands([f"SET BRIGHTNESS {brightness}", "SAVE"], restore_display=True)
    return f"Brightness saved at {brightness}%."


def upload_gallery(image_data: bytes, slot: int) -> str:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Restart the GUI with: uv run --with pyserial --with pillow tools/clock_gui.py") from exc
    try:
        with Image.open(io.BytesIO(image_data)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise RuntimeError("That image is too large to process safely.")
            image = source.convert("RGB")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("That file is not a readable image.") from exc
    image = ImageOps.fit(image, (240, 240), method=Image.Resampling.LANCZOS)
    raw = bytearray()
    for red, green, blue in image.getdata():
        raw.extend(struct.pack("<H", ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)))
    try:
        clockctl.gallery_upload(
            clockctl.DEFAULT_PORT, 115200, cached_commands(), slot, bytes(raw)
        )
    except clockctl.ClockControlError as exc:
        raise RuntimeError(str(exc)) from exc
    return f"Gallery slot {slot + 1} uploaded and shown."


def gallery_slideshow(form: dict[str, list[str]]) -> str:
    action = form.get("gallery_action", [""])[0]
    if action == "stop":
        clock_commands(["GALLERY STOP"], restore_display=True)
        return "Slideshow stopped."
    try:
        seconds = int(form.get("seconds", [""])[0])
    except ValueError as exc:
        raise RuntimeError("Slideshow timing must be 3 to 300 seconds.") from exc
    if not 3 <= seconds <= 300:
        raise RuntimeError("Slideshow timing must be 3 to 300 seconds.")
    clock_commands([f"GALLERY PLAY {seconds}"], restore_display=True)
    return f"Slideshow started at {seconds} seconds per photo."


def parse_reminders(response: str) -> list[tuple[str, str]]:
    reminders = [("", "") for _ in range(8)]
    for line in response.splitlines():
        match = re.fullmatch(
            r"REMIND ([0-7]) (off|(?:[01][0-9]|2[0-3]):[0-5][0-9](?: .*)?)",
            line,
        )
        if not match:
            continue
        slot = int(match.group(1))
        value = match.group(2)
        if value != "off":
            reminder_time, _, label = value.partition(" ")
            reminders[slot] = (reminder_time, label)
    return reminders


def reminder_rows(reminders: list[tuple[str, str]]) -> str:
    rows = []
    for slot, (reminder_time, label) in enumerate(reminders):
        enabled = bool(reminder_time)
        state_class = "enabled" if enabled else "empty"
        state_text = "Daily" if enabled else "Not set"
        delete_disabled = "" if enabled else " disabled"
        rows.append(
            f'<form method="post" class="reminder-card {state_class}">'
            '<input type="hidden" name="action" value="reminder">'
            f'<input type="hidden" name="slot" value="{slot}">'
            '<div class="reminder-heading">'
            f'<div><span class="slot-number">{slot + 1}</span><strong>Reminder {slot + 1}</strong></div>'
            f'<span class="reminder-state"><i></i>{state_text}</span>'
            '</div>'
            '<div class="field-grid">'
            f'<label>Time<input aria-label="Reminder {slot + 1} time" name="time" type="time" value="{html.escape(reminder_time)}"></label>'
            f'<label>Message<input aria-label="Reminder {slot + 1} message" name="label" maxlength="20" placeholder="TAKE MEDICINE" value="{html.escape(label)}"></label>'
            '</div>'
            '<div class="reminder-actions">'
            '<button class="primary compact" name="reminder_action" value="set">Save reminder</button>'
            f'<button class="danger compact" name="reminder_action" value="del"{delete_disabled}>Remove</button>'
            '</div>'
            '</form>'
        )
    return "".join(rows)


def load_reminders() -> tuple[list[tuple[str, str]], str]:
    response = clock_commands(["REMIND LIST"], restore_display=True)
    return parse_reminders(response), response


def update_reminder(form: dict[str, list[str]]) -> tuple[list[tuple[str, str]], str]:
    try:
        slot = int(form.get("slot", [""])[0])
    except ValueError as exc:
        raise RuntimeError("Reminder slot must be from 1 to 8.") from exc
    if not 0 <= slot < 8:
        raise RuntimeError("Reminder slot must be from 1 to 8.")
    action = form.get("reminder_action", [""])[0]
    if action == "del":
        command = f"REMIND DEL {slot}"
        result_message = f"Reminder {slot + 1} removed."
    elif action == "set":
        reminder_time = form.get("time", [""])[0]
        label = form.get("label", [""])[0]
        try:
            label.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RuntimeError("Reminder labels must use ASCII for now.") from exc
        if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", reminder_time):
            raise RuntimeError("Choose a valid reminder time.")
        if int(reminder_time[:2]) > 23:
            raise RuntimeError("Choose a valid reminder time.")
        if not 1 <= len(label) <= 20 or not label.isprintable():
            raise RuntimeError("Reminder label must be 1-20 printable ASCII characters.")
        command = f"REMIND SET {slot} {reminder_time} {label}"
        result_message = f"Reminder {slot + 1} saved for {reminder_time} every day."
    else:
        raise RuntimeError("Choose Set or Delete for the reminder.")
    response = clock_commands([command, "REMIND LIST"], restore_display=True)
    return parse_reminders(response), result_message


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>PHUD Clock Control</title>
<style>
:root{--bg:#090d12;--surface:#121923;--surface-2:#182230;--line:#263446;--text:#f4f7fb;--muted:#92a0b4;--accent:#36d6c4;--accent-dark:#092c2a;--danger:#ff7080;--danger-dark:#35151d;--purple:#b9bdff;--shadow:0 18px 50px rgba(0,0,0,.25)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 50% -20%,#17323b 0,transparent 38%),var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
button,input,select{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.4}.shell{width:min(980px,100%);margin:0 auto;padding:28px 20px 64px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:22px}.brand p,.section-head p,.helper{margin:4px 0 0;color:var(--muted)}h1{font-size:26px;line-height:1.1;margin:0;letter-spacing:.02em}h2{font-size:24px;margin:0}h3{font-size:17px;margin:0}.device-pill{display:flex;align-items:center;gap:8px;padding:9px 13px;border:1px solid #28534f;border-radius:999px;background:#102623;color:#b9fff4;font-size:13px;font-weight:700;white-space:nowrap}.device-pill i,.status-message i,.reminder-state i{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px rgba(54,214,196,.12)}
.status-message{display:flex;align-items:flex-start;gap:12px;padding:14px 16px;margin-bottom:18px;border:1px solid #28534f;border-radius:14px;background:#10201f;color:#d5fff9;box-shadow:var(--shadow)}.status-message.error{border-color:#71303c;background:#2a141a;color:#ffd9de}.status-message.error i{background:var(--danger);box-shadow:0 0 0 4px rgba(255,112,128,.12)}
.tabs{position:sticky;top:10px;z-index:20;display:grid;grid-template-columns:repeat(4,1fr);gap:6px;padding:6px;margin-bottom:24px;border:1px solid var(--line);border-radius:16px;background:rgba(18,25,35,.92);backdrop-filter:blur(16px);box-shadow:var(--shadow)}.tab{border:0;border-radius:11px;padding:12px 8px;background:transparent;color:var(--muted);font-weight:750}.tab:hover{color:var(--text);background:var(--surface-2)}.tab[aria-selected=true]{background:var(--accent);color:#061311}
.panel{display:none}.panel.active{display:block}.section-head{margin:4px 2px 18px}.card{border:1px solid var(--line);border-radius:18px;background:linear-gradient(145deg,var(--surface-2),var(--surface));padding:20px;box-shadow:var(--shadow)}.card-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.action-card{display:flex;flex-direction:column;min-height:196px}.action-card .eyebrow{display:inline-flex;align-self:flex-start;padding:5px 9px;margin-bottom:18px;border-radius:999px;background:#203040;color:#bac7d8;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.07em}.action-card h3{font-size:20px}.action-card p{color:var(--muted);margin:8px 0 20px;flex:1}.face-card{grid-column:1/-1;display:grid;grid-template-columns:180px 1fr;gap:24px;align-items:center}.face-preview{height:150px;border:1px solid #233246;border-radius:22px;background:#020405;display:flex;align-items:center;justify-content:center;gap:24px;box-shadow:inset 0 0 30px #000}.face-eye{display:block;width:48px;height:34px;border-radius:12px;background:var(--accent);box-shadow:0 0 8px rgba(54,214,196,.55),0 0 22px rgba(54,214,196,.18)}.face-copy p{color:var(--muted);margin:6px 0 16px}.mood-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.mood-grid button{padding:10px 8px;font-size:13px}.mood-grid button[value=auto]{background:var(--accent);color:#061311;border-color:transparent}
.primary,.secondary,.danger{width:100%;border:1px solid transparent;border-radius:12px;padding:14px 16px;font-weight:800;transition:transform .12s ease,filter .12s ease}.primary{background:var(--accent);color:#061311}.primary:hover{filter:brightness(1.08);transform:translateY(-1px)}.secondary{background:#242f42;color:#f3f6ff;border-color:#35435b}.secondary:hover{background:#2d3a50}.danger{background:var(--danger-dark);color:#ffc3cb;border-color:#66303a}.compact{padding:11px 14px;font-size:14px}
.reminder-list{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.reminder-card{border:1px solid var(--line);border-radius:18px;background:var(--surface);padding:18px}.reminder-card.enabled{border-color:#28534f;background:linear-gradient(145deg,#142521,var(--surface))}.reminder-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.reminder-heading>div{display:flex;align-items:center;gap:10px}.slot-number{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:#263346;color:#dce6f4;font-weight:850}.reminder-state{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;font-weight:800}.reminder-state i{background:#657286;box-shadow:none}.reminder-card.enabled .reminder-state{color:#aef7ec}.reminder-card.enabled .reminder-state i{background:var(--accent);box-shadow:0 0 0 4px rgba(54,214,196,.1)}
.field-grid{display:grid;grid-template-columns:130px 1fr;gap:10px}.field-grid label,.stack label{display:block;color:#aeb9c9;font-size:12px;font-weight:800;letter-spacing:.03em}.field-grid input,.stack input,.stack select{display:block;width:100%;height:46px;margin-top:6px;padding:10px 12px;border:1px solid #34445a;border-radius:11px;background:#0b1119;color:var(--text);outline:none}.field-grid input:focus,.stack input:focus,.stack select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(54,214,196,.12)}.reminder-actions{display:grid;grid-template-columns:1fr auto;gap:9px;margin-top:13px}.reminder-actions .danger{width:auto}
.two-column{display:grid;grid-template-columns:1fr 1fr;gap:16px}.stack{display:grid;gap:14px}.stack .primary,.stack .secondary{margin-top:2px}.upload-zone{display:block;padding:20px;border:1px dashed #4b617c;border-radius:14px;background:#0d141d;color:#cbd5e3;text-align:center}.upload-zone input{margin:12px auto 0;height:auto;border:0;background:transparent;padding:0}.inline-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px}.range-row{display:grid;grid-template-columns:1fr 58px;gap:14px;align-items:center;margin:18px 0}.range-row input{width:100%;accent-color:var(--accent)}.range-value{display:grid;place-items:center;height:44px;border-radius:10px;background:#0b1119;color:#bffaf3;font-weight:850}.warning{margin:16px 0 0;padding:12px 14px;border-radius:12px;background:#251c13;color:#f4c58e;font-size:13px}.footer-note{text-align:center;color:#68768a;font-size:12px;margin-top:30px}
@media(max-width:760px){.card-grid{grid-template-columns:1fr}.face-card{grid-template-columns:1fr}.face-preview{height:125px}.action-card{min-height:0}.reminder-list,.two-column{grid-template-columns:1fr}.action-card p{min-height:0}.tabs{top:6px}.tab{font-size:13px}.shell{padding:20px 14px 50px}}
@media(max-width:480px){.topbar{align-items:flex-start;flex-direction:column}.device-pill{align-self:flex-start}.tabs{overflow-x:auto;grid-template-columns:repeat(4,minmax(90px,1fr))}.mood-grid{grid-template-columns:repeat(2,1fr)}.field-grid{grid-template-columns:1fr}.reminder-actions{grid-template-columns:1fr 1fr}.reminder-actions .danger{width:100%}.inline-actions{grid-template-columns:1fr}.status-message{font-size:14px}}
</style>
</head>
<body>
<main class="shell">
  <header class="topbar">
    <div class="brand"><h1>WWW.PHUD.ME</h1><p>Smart Clock Control</p></div>
    <div class="device-pill"><i></i>USB clock connected</div>
  </header>

  <div class="status-message __STATUS_CLASS__" role="status" aria-live="polite"><i></i><div>__STATUS__</div></div>

  <nav class="tabs" aria-label="Clock settings">
    <button class="tab" type="button" data-tab="home" aria-selected="true">Home</button>
    <button class="tab" type="button" data-tab="reminders" aria-selected="false">Reminders</button>
    <button class="tab" type="button" data-tab="gallery" aria-selected="false">Gallery</button>
    <button class="tab" type="button" data-tab="settings" aria-selected="false">Settings</button>
  </nav>

  <section class="panel active" id="home">
    <div class="section-head"><h2>What should the clock show?</h2><p>Choose a screen. The clock changes as soon as the command completes.</p></div>
    <div class="card-grid">
      <form method="post" class="card face-card">
        <input type="hidden" name="action" value="face">
        <div class="face-preview" aria-hidden="true"><i class="face-eye"></i><i class="face-eye"></i></div>
        <div class="face-copy"><span class="eyebrow">Animated companion</span><h3>LED Face</h3><p>Show cyan eyes on black. Auto follows Mac activity through the persistent controller; manual moods stay selected through normal USB resets.</p>
          <div class="mood-grid">
            <button class="secondary" name="mood" value="auto">Auto</button>
            <button class="secondary" name="mood" value="neutral">Neutral</button>
            <button class="secondary" name="mood" value="happy">Happy</button>
            <button class="secondary" name="mood" value="focus">Focus</button>
            <button class="secondary" name="mood" value="curious">Curious</button>
            <button class="secondary" name="mood" value="sleepy">Sleepy</button>
            <button class="secondary" name="mood" value="alert">Alert</button>
            <button class="secondary" name="mood" value="celebrate">Celebrate</button>
          </div>
        </div>
      </form>
      <form method="post" class="card action-card"><input type="hidden" name="action" value="usage"><span class="eyebrow">Daily view</span><h3>Usage dashboard</h3><p>Refresh and show all Claude and Codex account usage.</p><button class="primary">Refresh &amp; show</button></form>
      <form method="post" class="card action-card"><input type="hidden" name="action" value="btc"><span class="eyebrow">Market</span><h3>BTC / USD</h3><p>Show the current price, 24-hour change, and 24 one-hour candles.</p><button class="primary">Refresh &amp; show BTC</button></form>
      <form method="post" class="card action-card"><input type="hidden" name="action" value="clock"><span class="eyebrow">Simple</span><h3>Large clock</h3><p>Show the current local time in a clean, easy-to-read layout.</p><button class="secondary">Show clock</button></form>
      <form method="post" class="card action-card"><input type="hidden" name="action" value="thai"><span class="eyebrow">Font test</span><h3>Thai greeting</h3><p>Show the verified Noto Sans Thai greeting “สวัสดี”.</p><button class="secondary">Show Thai screen</button></form>
    </div>
  </section>

  <section class="panel" id="reminders">
    <div class="section-head"><h2>Daily reminders</h2><p>Pick a time, write a short message, and save. Each reminder repeats every day until removed.</p></div>
    <div class="reminder-list">__REMINDERS__</div>
    <p class="footer-note">Messages currently support English letters, numbers, and punctuation up to 20 characters.</p>
  </section>

  <section class="panel" id="gallery">
    <div class="section-head"><h2>Photos &amp; slideshow</h2><p>Upload photos to the clock, then choose how quickly they rotate.</p></div>
    <div class="two-column">
      <form method="post" enctype="multipart/form-data" class="card stack">
        <input type="hidden" name="action" value="gallery">
        <h3>Upload a photo</h3>
        <label class="upload-zone">Choose an image<input name="image" type="file" accept="image/*" required></label>
        <label>Save to slot<select name="slot"><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option><option>7</option></select></label>
        <button class="primary">Upload &amp; show photo</button>
        <p class="helper">The photo is centre-cropped to 240 × 240. Upload takes around 15 seconds.</p>
      </form>
      <div class="card stack">
        <h3>Slideshow</h3>
        <form method="post" class="stack"><input type="hidden" name="action" value="slideshow"><input type="hidden" name="gallery_action" value="play"><label>Seconds per photo<input name="seconds" type="number" min="3" max="300" value="10"></label><button class="primary">Start slideshow</button></form>
        <form method="post"><input type="hidden" name="action" value="slideshow"><input type="hidden" name="gallery_action" value="stop"><button class="secondary">Stop slideshow</button></form>
        <p class="helper">Your slideshow keeps running through normal USB collector resets.</p>
      </div>
    </div>
  </section>

  <section class="panel" id="settings">
    <div class="section-head"><h2>Display settings</h2><p>Adjust the screen or open the hardware test pattern.</p></div>
    <div class="two-column">
      <form method="post" class="card"><input type="hidden" name="action" value="brightness"><h3>Brightness</h3><div class="range-row"><input id="brightness" name="brightness" type="range" min="0" max="100" value="70"><output class="range-value" id="brightness-value">70%</output></div><button class="primary">Save brightness</button></form>
      <form method="post" class="card"><input type="hidden" name="action" value="test"><h3>LCD test pattern</h3><p class="helper">Show red, green, blue, and white blocks to check the panel.</p><button class="danger">Show test pattern</button><p class="warning">The pattern stays visible until you choose another screen from Home.</p></form>
    </div>
  </section>

  <p class="footer-note">Local control only • 127.0.0.1 • No Wi-Fi on the clock</p>
</main>
<script>
const tabs=[...document.querySelectorAll('.tab')];
const panels=[...document.querySelectorAll('.panel')];
function showTab(id){
  if(!document.getElementById(id))id='home';
  tabs.forEach(tab=>tab.setAttribute('aria-selected',String(tab.dataset.tab===id)));
  panels.forEach(panel=>panel.classList.toggle('active',panel.id===id));
  localStorage.setItem('phud-clock-tab',id);
}
tabs.forEach(tab=>tab.addEventListener('click',()=>showTab(tab.dataset.tab)));
document.querySelectorAll('form').forEach(form=>form.addEventListener('submit',()=>{
  const panel=form.closest('.panel');if(panel)localStorage.setItem('phud-clock-tab',panel.id);
  form.querySelectorAll('button:not([name])').forEach(button=>button.disabled=true);
}));
showTab(localStorage.getItem('phud-clock-tab')||'home');
const brightness=document.getElementById('brightness');
const brightnessValue=document.getElementById('brightness-value');
brightness.addEventListener('input',()=>brightnessValue.textContent=brightness.value+'%');
</script>
</body>
</html>"""


def render_page(
    status: str,
    reminders: list[tuple[str, str]],
    csrf_token: str = CSRF_TOKEN,
) -> bytes:
    status_class = "error" if status.startswith("Could not") else ""
    page = (PAGE.replace("__STATUS__", html.escape(status))
            .replace("__STATUS_CLASS__", status_class)
            .replace("__REMINDERS__", reminder_rows(reminders)))
    csrf_field = (
        '<input type="hidden" name="csrf_token" value="'
        + html.escape(csrf_token, quote=True)
        + '">'
    )
    page = re.sub(r"(<form\b[^>]*>)", lambda match: match.group(1) + csrf_field, page)
    return page.encode()


class ClockGuiHandler(BaseHTTPRequestHandler):
    status = "Ready. Choose a screen."
    reminders = [("", "") for _ in range(8)]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def respond(self) -> None:
        content = render_page(self.status, self.reminders)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self.reminders, _ = load_reminders()
        except Exception as exc:
            self.status = f"Could not load reminders: {exc}"
        self.respond()

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise RuntimeError("Invalid request size.") from exc
            if length < 0:
                raise RuntimeError("Invalid request size.")
            if length > MAX_REQUEST_BYTES:
                self.send_error(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "Image upload is limited to 16 MB",
                )
                return
            content_type = self.headers.get("Content-Type", "")
            multipart = content_type.startswith("multipart/form-data")
            body = self.rfile.read(length)
            if multipart:
                message = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
                )
                form = {
                    part.get_param("name", header="content-disposition"): part
                    for part in message.iter_parts()
                    if part.get_param("name", header="content-disposition")
                }
            else:
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            token = (
                form.get("csrf_token").get_content()
                if multipart and form.get("csrf_token")
                else form.get("csrf_token", [""])[0]
            )
            if not isinstance(token, str) or not secrets.compare_digest(token, CSRF_TOKEN):
                raise RuntimeError("This control page expired. Reload it and try again.")
            action = (form.get("action").get_content() if multipart and form.get("action")
                      else form.get("action", [""])[0])
            if action == "usage":
                self.status = set_usage_screen()
            elif action == "face":
                self.status = set_face_screen(form)
            elif action == "btc":
                self.status = set_btc_screen()
            elif action == "clock":
                self.status = set_clock_screen()
            elif action == "thai":
                self.status = set_thai_screen()
            elif action == "brightness":
                self.status = set_brightness(form)
            elif action == "reminder":
                self.reminders, self.status = update_reminder(form)
            elif action == "test":
                clock_commands(["TEST"], restore_display=True)
                self.status = "LCD test pattern is now showing."
            elif action == "slideshow":
                self.status = gallery_slideshow(form)
            elif action == "gallery" and multipart:
                item = form["image"]
                slot_field = form.get("slot")
                slot = int(slot_field.get_content() if slot_field else "0") - 1
                image_data = item.get_payload(decode=True)
                if not image_data or not 0 <= slot < 7:
                    raise RuntimeError("Choose an image and a gallery slot from 1 to 7.")
                self.status = upload_gallery(image_data, slot)
            else:
                raise RuntimeError("Unknown control")
        except Exception as exc:  # Present a safe local action error to the user.
            self.status = f"Could not complete action: {exc}"
        self.respond()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local USB clock control panel.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), ClockGuiHandler)
    address = f"http://127.0.0.1:{args.port}"
    print(f"PHUD Clock Control: {address}")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPHUD Clock Control stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
