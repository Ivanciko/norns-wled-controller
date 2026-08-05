# audio-midi-reactive-wled-controller

Built by **1V4N** — *Cables i Tramuntana*

Audio and MIDI reactive LED controller built on a **Raspberry Pi 4** with a **monome norns shield**, controlling up to **4 WS2812B LED strips** via **WLED** (tested with a GLEDOPTO 4-channel Ethernet controller).

The system runs fully autonomous — no computer needed. Boot the Pi and everything starts automatically: audio analysis, MIDI detection, WLED connection, and OLED menu.

[![Demo](https://img.youtube.com/vi/l5sBzIcei7o/hqdefault.jpg)](https://youtube.com/shorts/l5sBzIcei7o)

---

## Hardware

| Component | Details |
|---|---|
| **Raspberry Pi 4** | Runs the controller software |
| **monome norns shield** | OLED display, 3 encoders, 3 buttons, I2S audio codec |
| **WLED controller** | GLEDOPTO Elite 4D-EXMU (4 channels, Ethernet), WLED v16.0.0 |
| **WS2812B LED strips** | Up to 4, independently configurable length/LED count/pin — see `config.json` `strips` |
| **Audio source** | Any line-level audio in to the norns shield jack |
| **MIDI controller** | Any USB-MIDI device (tested with Elektron Digitakt) |

The number of strips is not hardcoded — `router.py`, `wled_animator.py` and the OLED menu all size themselves from the `strips` list in `config.json` (see `config.py`).

---

## Norns Shield Compatibility

> **This project requires the norns shield with the CS4270 audio codec.**

monome norns shields shipped in two hardware revisions with different codecs:

- **Rev < 211028** (serial numbers before October 2021) → **CS4270** codec ✅ Compatible
- **Rev ≥ 211028** (serial numbers from October 2021 onwards) → CS4271 codec ❌ Not compatible (different driver)

To check your revision, look at the PCB silkscreen on the back of the norns shield. If the date code is before `211028`, this project will work.

---

## Networking

Two supported setups:

- **WLED's own access point (default, no router needed)**: the Pi connects to the WiFi network created by the WLED ESP32/Ethernet controller itself (**WLED-AP**, password `wled1234`), with the controller at `4.3.2.1`. This AP is set to always broadcast (`ap.behav = 2` in WLED's WiFi settings) so it stays reachable even when the controller also has an active Ethernet link — useful for reaching the WLED web UI from a phone without disconnecting anything.
- **Direct Ethernet link (lower jitter)**: on setups with an Ethernet-capable WLED controller (e.g. GLEDOPTO 4D-EXMU), the Pi's `eth0` can be wired point-to-point to the controller on its own subnet (`192.168.10.0/24`, Pi at `.1`, WLED at `.2`, static — no DHCP server needed), while `wlan0` stays on the normal home WiFi for SSH/internet. This removes WiFi jitter from the realtime pixel stream. Note: some WLED versions won't bring up a static-IP Ethernet link on a router-less point-to-point segment unless the **Static gateway** field is set to the Pi's own IP (not `0.0.0.0`) — a known WLED quirk, not a config mistake.

The Pi does **not** offer a WiFi scan/connect menu on its own OLED (removed — see below); it always auto-connects via NetworkManager profiles configured ahead of time, with a priority order (e.g. home WiFi first, `WLED-AP` as fallback).

---

## What it does

- **Audio reactive**: analyses the audio input in real time (16-band FFT) and fires light pulses on every beat/onset, per strip
- **MIDI reactive**: a `note_on` message from any connected USB-MIDI device fires a pulse on the strips listening on that channel
- **Per-strip effect**: each strip independently runs the reactive DRGB pulse, the triggered Meteor look, or a continuous native WLED effect (see Effects below)
- **Autonomous boot**: runs as a systemd user service, starts on power-on with no interaction needed
- **MIDI hot-plug**: if the MIDI device is not connected at boot, the system polls every 3 seconds and connects automatically when it appears
- **WiFi**: connects automatically on boot via pre-configured NetworkManager profiles (priority order, e.g. home WiFi then `WLED-AP` fallback) — no scan/connect menu on the device itself (see Networking)

---

## OLED Menu

**K1 short** cycles through the 7 root pages. **K1 hold (~1.2s)** enters the SISTEMA screen (from anywhere, back with K1 hold again). **K2 short** (root pages only) toggles the clean VU-only performance view. **K3 short** toggles WLED output ON/OFF, except on the TIRAS page (K3 enters the highlighted strip's detail) and the ESCENAS page (K3 short applies the highlighted scene, K3 held saves it).

### Root pages

| Page | Content |
|---|---|
| **TIRAS** | All strips at once: name, source letter, level bar; footer shows size/LED count/pin of the strip highlighted with E1. K3 opens that strip's detail |
| **FUENTES** | E1 audio input gain, E2 output volume, E3 detection threshold |
| **WLED/RED** | WLED host, pulse direction (reverse) for strips 1 and 2, presets/effects loaded count |
| **PRESETS** | E1 cycles WLED's own saved presets, applied instantly (whole device) |
| **BRILLO** | E1 strip master brightness (WLED), E2 OLED screen contrast |
| **GLOBAL** | Adjusts Speed/Tail/Sparkle/Ambient brightness on all 4 strips at once, relative to their current values (keeps the balance between strips) — E2 picks the field, E1/E3 apply the delta |
| **ESCENAS** | 8 fixed-name snapshot slots capturing every strip's full behavior (source, color, effect, speed, etc.) — E1 picks a slot, K3 applies it, K3 held overwrites it |

### Strip detail (from TIRAS, K3 on the highlighted strip)

E2 moves the field cursor, E1/E3 adjust the active field's value. Fields (shown conditionally): Active → Source (audio/midi/both) → Audio band → MIDI channel → MIDI device → Color (curated palette) → **Effect** → then fields specific to the chosen Effect (see below) → Ambient brightness. K3 returns to TIRAS.

### Clean VU mode (K2 on any root page)

Four large vertical level bars, no text — meant for live performance. K2 again returns to the menu.

### SISTEMA screen (K1 hold)

Read-only network status (current WiFi/Ethernet interface, IP) plus safe shutdown. No WiFi scan/connect menu here — see Networking for why.

| Control | Action |
|---|---|
| K2 hold (~1.5s) | Safe shutdown |
| K1 hold | Back to the root pages |

---

## Per-strip effects

Each strip's **Effect** field (strip detail screen) picks between:

- **Reactive pulse (default)**: the Pi calculates pixel data for each pulse — position, tail, fade curve, optional random "sparkle" flicker — and streams it to WLED at 30fps via UDP (DRGB protocol). Speed and Tail control the pulse shape; multiple simultaneous pulses coexist using max blending (not additive).
- **Meteor disparado (Meteor triggered)**: same DRGB pulse pipeline, but rendered with a faithful port of WLED's own `mode_meteor()` effect — a per-pixel trail with randomized decay and a sweeping head, colored either as a rainbow-by-position gradient or the strip's own color. A new meteor is born on every trigger (note/onset), travels, and fades out — unlike WLED's native Meteor effect, which loops forever and can't be restarted on demand (WLED only resets an effect's internal state when its `fx` value actually changes, and the non-"Smooth" Meteor variant doesn't use that internal state for its position anyway — driving it via the native WLED effect API was tried and doesn't work, see commit history). Speed and Tail (here: trail decay length) are adjustable the same way as the reactive pulse.
- **Any native WLED effect**: the strip runs that WLED effect continuously on its own WLED Segment (`fx`/`sx`/`col` via the JSON API) — the Pi does not drive it beat-by-beat. Requires WLED to have a Segment defined matching that strip's LED range (see Installation). Native effects share the device with any strip using a reactive/Meteor pulse; WLED's own effect rendering is suppressed while any DRGB realtime stream is active.

Ambient brightness (0–200, per strip) sets the idle glow shown between pulses; it has no effect while a strip is in native-effect mode.

---

## Software stack

- **Python 3** (venv)
- `pyalsaaudio` — audio capture/playback (bypasses PortAudio for reliable card detection)
- `numpy` — FFT analysis
- `mido` + `python-rtmidi` — MIDI input
- `luma.oled` — SSD1322 OLED driver
- `requests` — WLED HTTP control
- `lgpio` — encoder and button polling

---

## Key files

| File | Purpose |
|---|---|
| `demo_full.py` | Main entry point — wires audio/MIDI/WLED/display, render loop |
| `display.py` | OLED menu: all screens, encoder/button handling |
| `config.py` | `strips` schema, config load/save, legacy-schema migration |
| `audio_analysis.py` | AudioAnalyzer — 16-band FFT, noise floor calibration, passthrough |
| `wled_animator.py` | WLED client — DRGB pulse animation (30fps), presets, per-segment native effects |
| `midi_input.py` | MIDI input with hot-plug detection |
| `controls.py` | Encoder and button driver (polling-based, 1 event per physical click) |
| `ssd1322_norns.py` | SSD1322 OLED driver |
| `router.py` | Audio level → per-strip brightness routing (drives the OLED bars) |
| `system_control.py` | WiFi (nmcli) and shutdown helpers |
| `wled-controller.service` | systemd user service file |
| `config.json.example` | Config template — copy to `config.json` and edit `wled_host`/`strips` |

`config.json` is excluded from the repo (contains WiFi credentials). On a fresh install, copy `config.json.example` to `config.json` and set your WLED IP in `wled_host`. Configs saved by an older 2-strip version of this project are migrated automatically on first load (see `config.py`).

---

## Installation

```bash
git clone https://github.com/Ivanciko/audio-midi-reactive-wled-controller ~/wled-controller
cd ~/wled-controller
chmod +x setup.sh
./setup.sh
```

After setup:
1. Copy `config.json.example` to `config.json`, edit `strips` to match your physical wiring (length/LED count/pin are informational, shown in the TIRAS screen) and set `wled_host`
2. In the WLED web UI, set up the LED Preferences (Outputs) matching your strips' start/length/pin, and set the PSU current limit safely below your power supply's real max
3. If any strip will use a native WLED effect (not the default reactive pulse), also define a matching WLED **Segment** for it (same LED range as the strip) — required for per-strip `fx` control
4. Set up WiFi/Ethernet connectivity between the Pi and your WLED controller ahead of time via NetworkManager (see Networking) and set `wled_host` in `config.json` — there is no on-device WiFi setup menu
5. `sudo reboot`
