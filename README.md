# norns-wled-controller

Audio and MIDI reactive LED controller built on a **Raspberry Pi 4** with a **monome norns shield**, controlling up to **4 WS2812B LED strips** via **WLED** (tested with a GLEDOPTO 4-channel Ethernet controller).

The system runs fully autonomous — no computer needed. Boot the Pi and everything starts automatically: audio analysis, MIDI detection, WLED connection, and OLED menu.

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

By default the Pi connects to the WiFi network created by the WLED ESP32 controller itself (**WLED-AP**, password `wled1234`). This is the standard WLED access point that every ESP32 running WLED creates out of the box — no router needed. Both devices talk directly to each other over this network, with the WLED ESP32 at `4.3.2.1`.

The Pi can also connect to any other WiFi network (home router, etc.) from the SISTEMA menu. Saved networks reconnect automatically on boot without entering the password again.

---

## What it does

- **Audio reactive**: analyses the audio input in real time (16-band FFT) and fires light pulses on every beat/onset, per strip
- **MIDI reactive**: a `note_on` message from any connected USB-MIDI device fires a pulse on the strips listening on that channel
- **Per-strip effect**: each strip independently runs either the reactive DRGB pulse, or a continuous native WLED effect (see Effects below)
- **Autonomous boot**: runs as a systemd user service, starts on power-on with no interaction needed
- **MIDI hot-plug**: if the MIDI device is not connected at boot, the system polls every 3 seconds and connects automatically when it appears
- **WiFi management**: connect to saved networks or new networks from the OLED menu, no keyboard needed

---

## OLED Menu

**K1 short** cycles through the 5 root pages. **K1 hold (~1.2s)** enters the SISTEMA screen (from anywhere, back with K1 hold again). **K2 short** (root pages only) toggles the clean VU-only performance view. **K3 short** toggles WLED output ON/OFF, except on the TIRAS page where K3 enters the selected strip's detail screen.

### Root pages

| Page | Content |
|---|---|
| **TIRAS** | All strips at once: name, source letter, level bar; footer shows size/LED count/pin of the strip highlighted with E1. K3 opens that strip's detail |
| **FUENTES** | E1 audio input gain, E2 output volume, E3 detection threshold |
| **WLED/RED** | WLED host, pulse direction (reverse) for strips 1 and 2, presets/effects loaded count |
| **PRESETS** | E1 cycles WLED's own saved presets, applied instantly (whole device) |
| **BRILLO** | E1 adjusts OLED screen contrast |

### Strip detail (from TIRAS, K3 on the highlighted strip)

E2 moves the field cursor, E1/E3 adjust the active field's value. Fields (shown conditionally): Active → Source (audio/midi/both) → Audio band → MIDI channel → MIDI device → Color (curated palette) → Effect (reactive pulse, or any native WLED effect) → Speed → Tail (reactive only) → **Reverse** (reactive only, strips 3 and 4 only — strips 1/2 use the WLED/RED page's global reverse) → Ambient brightness. K3 returns to TIRAS.

### Clean VU mode (K2 on any root page)

Four large vertical level bars, no text — meant for live performance. K2 again returns to the menu.

### SISTEMA screen (K1 hold)

Shows current WiFi network and IP address.

| Control | Action |
|---|---|
| K2 short | Edit WLED IP address (octet editor) |
| K2 hold (~1.5s) | Safe shutdown |
| K3 short | Scan and connect to WiFi networks |
| K3 hold | Toggle AP mode (Pi creates "LightReactive" hotspot) |

---

## Per-strip effects

Each strip's **Effect** field (strip detail screen) picks between:

- **Reactive pulse (default)**: the Pi calculates pixel data for each pulse — position, tail, fade curve — and streams it to WLED at 30fps via UDP (DRGB protocol). Speed and Tail control the pulse shape; multiple simultaneous pulses coexist using max blending (not additive).
- **Any native WLED effect**: the strip runs that WLED effect continuously on its own WLED Segment (`fx`/`sx`/`col` via the JSON API) — the Pi does not drive it beat-by-beat. Requires WLED to have a Segment defined matching that strip's LED range (see Installation).

Ambient brightness (0–200, per strip) sets the idle glow shown between reactive pulses; it has no effect while a strip is in native-effect mode.

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
git clone https://github.com/Ivanciko/norns-wled-controller ~/wled-controller
cd ~/wled-controller
chmod +x setup.sh
./setup.sh
```

After setup:
1. Copy `config.json.example` to `config.json`, edit `strips` to match your physical wiring (length/LED count/pin are informational, shown in the TIRAS screen) and set `wled_host`
2. In the WLED web UI, set up the LED Preferences (Outputs) matching your strips' start/length/pin, and set the PSU current limit safely below your power supply's real max
3. If any strip will use a native WLED effect (not the default reactive pulse), also define a matching WLED **Segment** for it (same LED range as the strip) — required for per-strip `fx` control
4. Connect the Pi to your WLED controller's network from the SISTEMA menu, or pre-set `wled_host` in `config.json`
5. `sudo reboot`
