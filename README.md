# norns-wled-controller

Audio and MIDI reactive LED controller built on a **Raspberry Pi 4** with a **monome norns shield**, controlling **WS2812B 2 LED strips of 3m** via **WLED** on an ESP32.

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

## Networking

By default the Pi connects to the WiFi network created by the WLED ESP32 controller itself (**WLED-AP**, password `wled1234`). This is the standard WLED access point that every ESP32 running WLED creates out of the box — no router needed. Both devices talk directly to each other over this network, with the WLED ESP32 at `4.3.2.1`.

The Pi can also connect to any other WiFi network (home router, etc.) from the SISTEMA menu. Saved networks reconnect automatically on boot without entering the password again.

---

## What it does

- **Audio reactive**: analyses the audio input in real time (16-band FFT) and fires light pulses or brightness kicks on every beat/onset
- **MIDI reactive**: a `note_on` message from any connected USB-MIDI device fires a pulse
- **Two output modes**: DRGB (pixel-perfect pulse animation streamed at 30fps) or Preset (brightness kick on any native WLED effect)
- **Autonomous boot**: runs as a systemd user service, starts on power-on with no interaction needed
- **MIDI hot-plug**: if the MIDI device is not connected at boot, the system polls every 3 seconds and connects automatically when it appears
- **WiFi management**: connect to saved networks or new networks from the OLED menu, no keyboard needed

---

## OLED Menu

**K1 short** cycles through pages. **K1 hold (~1.2s)** enters the SISTEMA screen. **K3 (any page)** toggles WLED output ON/OFF — header shows `W:OFF` when disabled, `W:PRE` when in Preset mode.

### Pages

| Page | E1 | E2 | E3 |
|---|---|---|---|
| **LED1 / LED2** | Source: audio / midi / both | Audio band: bass / mid / treble / all | MIDI channel: 1 / 2 / 3 / all |
| **GLOBAL** | Audio input gain | Output volume | Detection threshold |
| **WLED** | Output mode: DRGB / PRESET *(see below)* | *depends on mode* | *depends on mode* |
| **BRILLO** | WLED ambient brightness between pulses (0–200) | OLED screen brightness (0–255) | — |

### WLED page — DRGB mode

| Control | Action |
|---|---|
| E1 | Switch to PRESET mode |
| E2 | Pulse speed (20–600 LEDs/s) |
| E3 | Pulse tail length (5–150 LEDs) |

### WLED page — PRESET mode

| Control | Action |
|---|---|
| E1 | Switch to DRGB mode |
| E2 | Select which WLED preset to trigger on each beat |
| E3 | Idle brightness between beats (0 = black, 255 = always full) |

### SISTEMA screen (K1 hold)

Shows current WiFi network and IP address.

| Control | Action |
|---|---|
| K2 short | Edit WLED IP address (octet editor) |
| K2 hold (~1.5s) | Safe shutdown |
| K3 short | Scan and connect to WiFi networks |
| K3 hold | Toggle AP mode (Pi creates "LightReactive" hotspot) |

---

## Output Modes

### DRGB mode (default)

The Pi calculates pixel data for each pulse — position, tail, fade curve — and streams it to the ESP32 at 30fps via UDP (WLED DRGB protocol). WLED stays in live mode and renders exactly what the Pi sends. This gives pixel-perfect control of speed, tail length, and color.

Multiple simultaneous pulses coexist using max blending (not additive), so overlapping pulses at high BPM don't saturate the strip.

### PRESET mode

The Pi keeps detecting beats and onsets as usual, but instead of streaming pixels it sends a **brightness kick** to WLED on each beat: an instant flash to full brightness (`bri=255`) followed by a smooth fade back to the configured idle brightness over ~1.5 seconds. WLED plays its own native effect continuously — the Pi just modulates the brightness.

This works with any WLED effect (Meteor, Comet, Fireworks, Wavesins, etc.). The effect runs freely between beats; each beat lights it up. The **idle brightness** (E3) controls how visible the effect is between beats: `0` = black between flashes for a strobe feel, `80` = soft ambient glow, `255` = always at full brightness.

Switch modes instantly from the WLED page with E1 — no restart needed.

---

## How pulses work (DRGB mode)

Each audio onset or MIDI note fires an independent light pulse per LED strip. Pulse parameters:

| Parameter | Range | Control |
|---|---|---|
| Speed | 20–600 LEDs/s | WLED page E2 |
| Tail length | 5–150 LEDs | WLED page E3 |
| Color | RGB | `wled_pulse_color` in config |
| Direction | normal / reverse | `wled_pulse_reverse` in config |
| Ambient glow | 0–200 | BRILLO page E1 |

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
| `demo_full.py` | Main entry point — menu, audio/MIDI callbacks, OLED render loop |
| `audio_analysis.py` | AudioAnalyzer — 16-band FFT, noise floor calibration, passthrough |
| `wled_animator.py` | WLED client — DRGB pulse animation (30fps) and Preset brightness kick |
| `midi_input.py` | MIDI input with hot-plug detection |
| `controls.py` | Encoder and button driver (polling-based, 1 event per physical click) |
| `ssd1322_norns.py` | SSD1322 OLED driver |
| `router.py` | Audio level routing + config persistence |
| `system_control.py` | WiFi (nmcli) and shutdown helpers |
| `wled-controller.service` | systemd user service file |
| `config.json.example` | Config template — copy to `config.json` and edit `wled_host` |

`config.json` is excluded from the repo (contains WiFi credentials). On a fresh install, copy `config.json.example` to `config.json` and set your WLED ESP32 IP in `wled_host`.

---

## Installation

```bash
git clone https://github.com/Ivanciko/norns-wled-controller ~/wled-controller
cd ~/wled-controller
chmod +x setup.sh
./setup.sh
```

After setup:
1. Copy `config.json.example` to `config.json`
2. Power on the ESP32 with WLED — it will create the **WLED-AP** network (`wled1234`)
3. Connect the Pi to WLED-AP from the SISTEMA menu, or pre-set `wled_host` to `4.3.2.1` in `config.json`
4. `sudo reboot`
