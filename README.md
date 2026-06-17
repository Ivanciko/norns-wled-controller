# norns-wled-controller

Audio and MIDI reactive LED controller built on a **Raspberry Pi 4** with a **monome norns shield**, controlling **WS2812B 2 LED strips** via **WLED** on an ESP32.

The system runs fully autonomous — no computer needed. Boot the Pi and everything starts automatically: audio analysis, MIDI detection, WLED connection, and OLED menu.

---

## Hardware

| Component | Details |
|---|---|
| **Raspberry Pi 4** | Runs the controller software |
| **monome norns shield** | OLED display, 3 encoders, 3 buttons, I2S audio codec |
| **ESP32 + WLED** | Controls the LED strips (tested with Gledopto, WLED v16.0.0) |
| **WS2812B LED strips** | 2 × 150 LEDs, physically mounted from a central point outward |
| **Audio source** | Any line-level audio in to the norns shield jack |
| **MIDI controller** | Any USB-MIDI device (tested with Elektron Digitakt) |

---

## Norns Shield Compatibility

> **This project requires the norns shield with the CS4270 audio codec.**

monome norns shields shipped in two hardware revisions with different codecs:

- **Rev < 211028** (serial numbers before October 2021) → **CS4270** codec ✅ Compatible
- **Rev ≥ 211028** (serial numbers from October 2021 onwards) → CS4271 codec ❌ Not compatible (different driver)

To check your revision, look at the PCB silkscreen on the back of the norns shield. If the date code is before `211028`, this project will work.

---

## What it does

- **Audio reactive**: analyses the audio input in real time (16-band FFT) and fires light pulses down the LED strips on every beat/onset
- **MIDI reactive**: a `note_on` message from any connected USB-MIDI device fires a pulse
- **UDP/DRGB**: sends pixel data directly to WLED at 30fps — WLED stays in live mode, pulses are rendered on the Pi and streamed to the ESP32
- **Autonomous boot**: runs as a systemd user service, starts on power-on with no interaction needed
- **MIDI hot-plug**: if the MIDI device is not connected at boot, the system polls every 3 seconds and connects automatically when it appears

---

## OLED Menu

K1 (short press) cycles through pages. K1 (hold ~1.2s) enters the SISTEMA screen.

| Page | E1 | E2 | E3 |
|---|---|---|---|
| **LED1 / LED2** | Source (audio / midi / both) | Audio band (bass / mid / treble / all) | MIDI channel |
| **GLOBAL** | Audio gain | Output volume | Detection threshold |
| **WLED** | Cycle presets | Pulse speed (LEDs/s) | Pulse tail (LEDs) |
| **BRILLO** | Ambient brightness (0 = off) | — | — |
| **SISTEMA** | — | Hold K2 = shutdown | K3 = WiFi setup |

---

## Software stack

- **Python 3** (venv)
- `pyalsaaudio` — audio capture/playback (bypasses PortAudio for reliable card detection)
- `numpy` — FFT analysis
- `mido` + `python-rtmidi` — MIDI input
- `luma.oled` — SSD1322 OLED driver
- `requests` — WLED HTTP preset control
- `lgpio` — encoder and button polling

---

## Key files

| File | Purpose |
|---|---|
| `demo_full.py` | Main entry point — menu, audio/MIDI callbacks, OLED render loop |
| `audio_analysis.py` | AudioAnalyzer — 16-band FFT, noise floor calibration, passthrough |
| `wled_animator.py` | WLED client — UDP/DRGB pulse animation at 30fps |
| `midi_input.py` | MIDI input with hot-plug detection |
| `controls.py` | Encoder and button driver (polling-based) |
| `ssd1322_norns.py` | SSD1322 OLED driver |
| `router.py` | Audio level routing + config persistence |
| `system_control.py` | WiFi (nmcli) and shutdown helpers |
| `wled-controller.service` | systemd user service file |
| `config.json` | Persistent config — all settings auto-saved on change |

---

## How pulses work

Each audio onset or MIDI note fires an independent light pulse per LED strip. Multiple pulses can coexist and travel simultaneously. Rendering uses **max blending** (not additive) so overlapping pulses at high BPM don't saturate the strip.

Pulse parameters controllable from the OLED:
- **Speed** (20–600 LEDs/s)
- **Tail length** (5–150 LEDs)
- **Ambient brightness** between pulses (0 = off)
- **WLED preset** (applied instantly, loaded dynamically from the ESP32)
