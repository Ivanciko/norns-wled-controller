#!/usr/bin/env python3
"""Demo integrada: control de LED1/LED2 por menu en la OLED, mas pantalla de
SISTEMA (apagado seguro y configuracion WiFi), con cliente WLED HTTP real.

Paginas (K1 corto cicla LED1 -> LED2 -> GLOBAL -> WLED -> BRILLO -> LED1...):

  LED1/LED2:
    E1: fuente (audio / midi / audio+midi)
    E2: banda de audio (graves / medios / agudos / todo)
    E3: canal MIDI (1 / 2 / 3 / todos)

  GLOBAL:
    E1: ganancia de audio in
    E2: volumen de salida
    E3: umbral de audio in

  WLED:
    E1: cicla presets (aplica instantaneamente)
    E2: velocidad del pulso (LEDs/s)
    E3: estela del pulso (LEDs)

  BRILLO:
    E1: brillo ambiente (0=apagado, >0=luz de fondo)

Pantalla SISTEMA (mantener K1 ~1.2s): estado de red, apagado (K2 hold),
WiFi scan/connect (K3).
"""
import time

from luma.core.render import canvas

from audio_analysis import AudioAnalyzer
from controls import Controls
from midi_input import MidiInput
from router import Router, load_config, save_config
from ssd1322_norns import ssd1322_norns
from system_control import connect_wifi, get_network_status, scan_wifi, shutdown
from router import band_level
from wled_animator import WLEDAnimator

N_BANDS = 16

SOURCES = ["audio", "midi", "both"]
SOURCE_LABELS = {"audio": "audio", "midi": "midi", "both": "audio+midi"}

BANDS = ["bass", "mid", "treble", "all"]
BAND_LABELS = {"bass": "graves", "mid": "medios", "treble": "agudos", "all": "todo"}

CHANNELS = [1, 2, 3, "all"]
CHANNEL_LABELS = {1: "1", 2: "2", 3: "3", "all": "todos"}

GAIN_STEP = 0.05
GAIN_MIN, GAIN_MAX = 0.1, 12.0

VOLUME_STEP = 0.05
VOLUME_MIN, VOLUME_MAX = 0.0, 24.0

THRESHOLD_STEP = 0.02
THRESHOLD_MIN, THRESHOLD_MAX = 0.0, 0.95

VEL_STEP = 10
VEL_MIN, VEL_MAX = 20, 600

TAIL_STEP = 5
TAIL_MIN, TAIL_MAX = 5, 150

FLOOR_STEP = 5
FLOOR_MIN, FLOOR_MAX = 0, 200

PRESET_BRI_STEP = 5
PRESET_BRI_MIN, PRESET_BRI_MAX = 0, 255

PAGE_NAMES = ["LED1", "LED2", "GLOBAL", "WLED", "BRILLO"]

BAR_TOP = 38
BAR_W = 44
BAR_GAP = 8

KEYBOARD_CHARS = (
    list("abcdefghijklmnopqrstuvwxyz")
    + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("0123456789")
    + list(" -_.@!?#*/")
)
KBD_GROUPS = [0, 26, 52, 62]  # a-z, A-Z, 0-9, symbols

LONG_PRESS_K1 = 1.2
LONG_PRESS_K2 = 1.5
LONG_PRESS_K3 = 1.5

config = load_config()
router = Router(config, rate_hz=10)
segments = config["segments"]

# WLED
WLED_HOST = config.get("wled_host", "192.168.50.151")
_seg_sizes = config.get("wled_seg_sizes", [150, 150])
_saved_preset = config.get("wled_preset", 1)
wled_preset_index = 0
wled_preset_mode_index = 0  # índice del preset disparado en modo preset


def _on_wled_presets_loaded(presets):
    global wled_preset_index, wled_preset_mode_index
    preset_list = sorted(presets.items())
    idx = next(
        (i for i, (pid, _) in enumerate(preset_list) if pid == _saved_preset), 0
    )
    wled_preset_index = idx
    pid, pname = preset_list[idx]
    wled.apply_preset(pid)
    print(f"WLED: preset aplicado -> {pname}")

    mode_id = config.get("wled_preset_mode_id", 1)
    wled_preset_mode_index = next(
        (i for i, (pid, _) in enumerate(preset_list) if pid == mode_id), 0
    )


wled = WLEDAnimator(WLED_HOST, seg_sizes=_seg_sizes,
                    on_presets_loaded=_on_wled_presets_loaded)

if wled.presets:
    _on_wled_presets_loaded(wled.presets)
else:
    print("WLED: sin presets en el arranque, reintentando en background...")

wled.bri_floor = config.get("wled_bri_floor", 0)
wled.ambient_color = tuple(config.get("wled_pulse_color", [255, 80, 0]))
wled.pulse_velocity = config.get("wled_velocity", 150)
wled.pulse_tail = config.get("wled_tail", 30)
wled.output_enabled = config.get("wled_output_enabled", True)
wled.preset_mode_id = config.get("wled_preset_mode_id", 1)  # antes que output_mode
wled.preset_bri_idle = config.get("wled_preset_bri_idle", 80)
wled.output_mode = config.get("output_mode", "drgb")

_beat_armed = {seg["id"]: True for seg in segments}

seg_state = [{"id": seg["id"], "bri": 0} for seg in segments]
page = 0
prev_page = 0

screen = "pages"
network_status = {"iface": None, "ip": "", "ssid": None}

wifi_networks = []
wifi_index = 0
wifi_password = ""
kbd_char_index = 0
wifi_connecting = False
wifi_result_msg = ""

key_press_ts = {1: None, 2: None, 3: None}
key_long_fired = {1: False, 2: False, 3: False}


def cycle(options, current, delta):
    i = options.index(current)
    return options[(i + delta) % len(options)]


def held_ratio(n, threshold):
    ts = key_press_ts[n]
    if ts is None:
        return 0.0
    return min(1.0, (time.monotonic() - ts) / threshold)


def on_levels(levels):
    s = router.update(levels)
    if s is not None:
        seg_state[:] = s["seg"]

    gain = config.get("audio_gain", 1.0)
    threshold = max(config.get("audio_threshold", 0.0), 0.05)
    reverse = config.get("wled_pulse_reverse", True)

    for seg in config["segments"]:
        if seg["source"] not in ("audio", "both"):
            continue
        seg_id = seg["id"]
        level = float(min(1.0, max(0.0, band_level(levels, seg["audio_band"]) * gain)))

        if level >= threshold and _beat_armed.get(seg_id, True):
            _beat_armed[seg_id] = False
            color = tuple(config.get("wled_pulse_color", [255, 255, 255]))
            wled.trigger(seg_ids=[seg_id], velocity=level, color=color, reverse=reverse)
        elif level < threshold * 0.55:
            _beat_armed[seg_id] = True


def start_wifi_connect(ssid, password):
    global screen, wifi_connecting, wifi_result_msg
    wifi_connecting = True
    wifi_result_msg = ""
    screen = "wifi_result"

    def on_done(ok, msg):
        global wifi_connecting, wifi_result_msg
        wifi_connecting = False
        if ok:
            wifi_result_msg = "conectado"
            config.setdefault("wifi_saved", {})[ssid] = password
            save_config(config)
        else:
            wifi_result_msg = f"error: {msg[:30]}"

    connect_wifi(ssid, password, on_done)


OUTPUT_MODES = ["drgb", "preset"]


def on_encoder(n, delta):
    global page, wifi_index, kbd_char_index, wled_preset_index, wled_preset_mode_index
    if not delta:
        return
    if screen == "pages":
        if page == 0 or page == 1:
            seg = segments[page]
            if n == 1:
                seg["source"] = cycle(SOURCES, seg["source"], delta)
            elif n == 2 and seg["source"] in ("audio", "both"):
                seg["audio_band"] = cycle(BANDS, seg["audio_band"], delta)
            elif n == 3 and seg["source"] in ("midi", "both"):
                seg["midi_channel"] = cycle(CHANNELS, seg["midi_channel"], delta)
            else:
                return
        elif page == 2:  # GLOBAL
            if n == 1:
                gain = config.get("audio_gain", 1.0) + delta * GAIN_STEP
                config["audio_gain"] = round(min(GAIN_MAX, max(GAIN_MIN, gain)), 2)
            elif n == 2:
                volume = config.get("audio_volume", 1.0) + delta * VOLUME_STEP
                config["audio_volume"] = round(min(VOLUME_MAX, max(VOLUME_MIN, volume)), 2)
                analyzer.volume = config["audio_volume"]
            elif n == 3:
                threshold = config.get("audio_threshold", 0.0) + delta * THRESHOLD_STEP
                config["audio_threshold"] = round(min(THRESHOLD_MAX, max(THRESHOLD_MIN, threshold)), 2)
            else:
                return
        elif page == 3:  # WLED
            preset_list = sorted(wled.presets.items())
            cur_mode = config.get("output_mode", "drgb")
            if n == 1:
                new_mode = cycle(OUTPUT_MODES, cur_mode, delta)
                config["output_mode"] = new_mode
                wled.output_mode = new_mode
                print(f"WLED: modo -> {new_mode}")
            elif n == 2:
                if cur_mode == "drgb":
                    vel = config.get("wled_velocity", 150) + delta * VEL_STEP
                    config["wled_velocity"] = int(min(VEL_MAX, max(VEL_MIN, vel)))
                    wled.pulse_velocity = config["wled_velocity"]
                elif preset_list:
                    wled_preset_mode_index = (wled_preset_mode_index + delta) % len(preset_list)
                    pid, pname = preset_list[wled_preset_mode_index]
                    config["wled_preset_mode_id"] = pid
                    wled.preset_mode_id = pid
                    print(f"WLED preset mode -> {pname}")
            elif n == 3:
                if cur_mode == "drgb":
                    tail = config.get("wled_tail", 30) + delta * TAIL_STEP
                    config["wled_tail"] = int(min(TAIL_MAX, max(TAIL_MIN, tail)))
                    wled.pulse_tail = config["wled_tail"]
                else:
                    bri = config.get("wled_preset_bri_idle", 80) + delta * PRESET_BRI_STEP
                    config["wled_preset_bri_idle"] = int(min(PRESET_BRI_MAX, max(PRESET_BRI_MIN, bri)))
                    wled.preset_bri_idle = config["wled_preset_bri_idle"]
            else:
                return
        elif page == 4:  # BRILLO
            if n == 1:
                floor = config.get("wled_bri_floor", 0) + delta * FLOOR_STEP
                config["wled_bri_floor"] = int(min(FLOOR_MAX, max(FLOOR_MIN, floor)))
                wled.bri_floor = config["wled_bri_floor"]
            else:
                return
        else:
            return
        save_config(config)
    elif screen == "wifi_list":
        if n == 1 and wifi_networks:
            wifi_index = (wifi_index + delta) % len(wifi_networks)
    elif screen == "wifi_kbd":
        if n == 1:
            kbd_char_index = (kbd_char_index + delta) % len(KEYBOARD_CHARS)
        elif n == 2:
            cur_group = max(i for i, s in enumerate(KBD_GROUPS) if s <= kbd_char_index)
            next_group = (cur_group + delta) % len(KBD_GROUPS)
            kbd_char_index = KBD_GROUPS[next_group]


def on_key(n, pressed):
    global page, screen, wifi_networks, wifi_index, wifi_password, kbd_char_index, network_status

    if pressed:
        key_press_ts[n] = time.monotonic()
        key_long_fired[n] = False
        return

    held_long = key_long_fired[n]
    key_press_ts[n] = None
    if held_long:
        return

    if screen == "pages":
        if n == 1:
            page = (page + 1) % len(PAGE_NAMES)
        elif n == 3:
            wled.output_enabled = not wled.output_enabled
            config["wled_output_enabled"] = wled.output_enabled
            save_config(config)
    elif screen == "sistema":
        if n == 3:
            wifi_networks = scan_wifi()
            wifi_index = 0
            screen = "wifi_list"
    elif screen == "wifi_list":
        if n == 1 and wifi_networks:
            net = wifi_networks[wifi_index]
            saved = config.get("wifi_saved", {})
            if net["ssid"] in saved:
                start_wifi_connect(net["ssid"], saved[net["ssid"]])
            elif net["security"] in ("", "--"):
                start_wifi_connect(net["ssid"], "")
            else:
                wifi_password = ""
                kbd_char_index = 0
                screen = "wifi_kbd"
        elif n == 2:
            wifi_networks = scan_wifi()
            wifi_index = 0
        elif n == 3:
            screen = "sistema"
    elif screen == "wifi_kbd":
        if n == 1:
            wifi_password += KEYBOARD_CHARS[kbd_char_index]
        elif n == 2:
            wifi_password = wifi_password[:-1]
        elif n == 3:
            net = wifi_networks[wifi_index]
            start_wifi_connect(net["ssid"], wifi_password)
    elif screen == "wifi_result":
        screen = "sistema"
        network_status = get_network_status()


def on_midi(msg):
    if msg.type == "note_on" and msg.velocity > 0:
        ch = msg.channel + 1
        ids = [
            seg["id"] for seg in segments
            if seg["source"] in ("midi", "both")
            and (seg["midi_channel"] == "all" or seg["midi_channel"] == ch)
        ]
        if ids:
            router.flash(velocity=msg.velocity / 127.0, segments=ids)
            wled.trigger(
                seg_ids=ids,
                velocity=msg.velocity / 127.0,
                color=tuple(config.get("wled_pulse_color", [255, 255, 255])),
                reverse=config.get("wled_pulse_reverse", True),
            )


device = ssd1322_norns(is_shield=True)
analyzer = AudioAnalyzer(on_levels=on_levels, n_bands=N_BANDS, volume=config.get("audio_volume", 1.0))
controls = Controls(on_encoder=on_encoder, on_key=on_key)

midi = MidiInput(on_message=on_midi)

print(f"Demo control LED1/LED2. K1 cambia de pagina (mantener = sistema). Ctrl+C para salir.")

try:
    while True:
        now = time.monotonic()

        if key_press_ts[1] is not None and not key_long_fired[1]:
            if now - key_press_ts[1] >= LONG_PRESS_K1:
                key_long_fired[1] = True
                if screen == "pages":
                    prev_page = page
                    screen = "sistema"
                    network_status = get_network_status()
                else:
                    screen = "pages"
                    page = prev_page

        if screen == "sistema" and key_press_ts[2] is not None and not key_long_fired[2]:
            if now - key_press_ts[2] >= LONG_PRESS_K2:
                key_long_fired[2] = True
                shutdown()

        with canvas(device) as draw:
            if screen == "pages":
                title = PAGE_NAMES[page]
                if page < 2 and segments[page]["source"] in ("midi", "both"):
                    title += f" {midi.name.split(chr(58))[0]}"
                draw.text((2, 1), title, fill="white")
                if not wled.output_enabled:
                    draw.text((100, 1), "W:OFF", fill="white")
                elif config.get("output_mode", "drgb") == "preset":
                    draw.text((100, 1), "W:PRE", fill="white")

                if page == 0 or page == 1:
                    seg = segments[page]
                    draw.text((2, 14), f"fuente: {SOURCE_LABELS[seg['source']]}", fill="white")
                    line3 = ""
                    if seg["source"] in ("audio", "both"):
                        line3 += f"banda: {BAND_LABELS[seg['audio_band']]}"
                    if seg["source"] in ("midi", "both"):
                        if line3:
                            line3 += "   "
                        line3 += f"canal: {CHANNEL_LABELS[seg['midi_channel']]}"
                    draw.text((2, 26), line3, fill="white")
                elif page == 2:  # GLOBAL
                    draw.text((2, 11), f"ganancia audio in: {config.get('audio_gain', 1.0):.2f}", fill="white")
                    draw.text((2, 21), f"volumen salida: {config.get('audio_volume', 1.0):.2f}", fill="white")
                    draw.text((2, 31), f"umbral audio in: {config.get('audio_threshold', 0.0):.2f}", fill="white")
                elif page == 3:  # WLED
                    preset_list = sorted(wled.presets.items())
                    cur_mode = config.get("output_mode", "drgb")
                    mode_label = "DRGB" if cur_mode == "drgb" else "PRESET"
                    draw.text((2, 11), f"modo: {mode_label}", fill="white")
                    if cur_mode == "drgb":
                        vel = config.get("wled_velocity", 150)
                        tail = config.get("wled_tail", 30)
                        draw.text((2, 21), f"vel: {vel}", fill="white")
                        draw.text((2, 31), f"est: {tail}", fill="white")
                    else:
                        if preset_list:
                            idx = min(wled_preset_mode_index, len(preset_list) - 1)
                            pid, pname = preset_list[idx]
                            draw.text((2, 21), f"{pname[:18]} [{idx+1}/{len(preset_list)}]", fill="white")
                        else:
                            draw.text((2, 21), "conectando WLED...", fill="white")
                        bri_idle = config.get("wled_preset_bri_idle", 80)
                        draw.text((2, 31), f"fondo: {bri_idle}", fill="white")
                elif page == 4:  # BRILLO
                    floor = config.get("wled_bri_floor", 0)
                    draw.text((2, 11), f"brillo ambiente: {floor}", fill="white")
                    if floor == 0:
                        draw.text((2, 21), "tubo apagado", fill="white")
                    else:
                        pct = int(floor * 100 / FLOOR_MAX)
                        draw.text((2, 21), f"encendido ({pct}%)", fill="white")

                bar_h = device.height - BAR_TOP
                x0_start = (device.width - (BAR_W * 2 + BAR_GAP)) // 2
                for i, seg in enumerate(seg_state):
                    x0 = x0_start + i * (BAR_W + BAR_GAP)
                    x1 = x0 + BAR_W - 1
                    h = int((seg["bri"] / 255.0) * bar_h)
                    if h > 0:
                        draw.rectangle((x0, device.height - h, x1, device.height - 1), fill="white")
                    draw.text((x0 + BAR_W // 2 - 3, device.height - 20), f"L{seg['id'] + 1}", fill="white")

            elif screen == "sistema":
                draw.text((2, 1), "SISTEMA", fill="white")
                if network_status["iface"] is None:
                    draw.text((2, 16), "sin red", fill="white")
                else:
                    label = network_status["ssid"] or network_status["iface"]
                    draw.text((2, 16), label[:21], fill="white")
                    draw.text((2, 28), network_status["ip"] or "(sin ip)", fill="white")
                draw.text((2, 42), "K3: wifi   K1: volver", fill="white")
                ratio = held_ratio(2, LONG_PRESS_K2)
                if ratio > 0:
                    draw.text((2, 52), "apagando (manten K2)", fill="white")
                    draw.rectangle((2, 60, 2 + int(120 * ratio), 63), fill="white")

            elif screen == "wifi_list":
                draw.text((2, 1), "WIFI - redes", fill="white")
                if not wifi_networks:
                    draw.text((2, 20), "sin redes", fill="white")
                    draw.text((2, 46), "K2:buscar K3:volver", fill="white")
                else:
                    net = wifi_networks[wifi_index]
                    saved = config.get("wifi_saved", {})
                    known = net["ssid"] in saved
                    sec = "abierta" if net["security"] in ("", "--") else net["security"]
                    ssid_lbl = (net["ssid"][:19] + " *") if known else net["ssid"][:21]
                    draw.text((2, 16), ssid_lbl, fill="white")
                    draw.text((2, 28), f"senal: {net['signal']}%  {sec}", fill="white")
                    draw.text((2, 40), f"{wifi_index + 1}/{len(wifi_networks)}", fill="white")
                    lbl = "K1:conectar" if known else "K1:elegir"
                    draw.text((2, 52), f"{lbl} K2:buscar K3:volver", fill="white")

            elif screen == "wifi_kbd":
                net = wifi_networks[wifi_index]
                draw.text((2, 1), f"WIFI: {net['ssid'][:18]}", fill="white")
                draw.text((2, 13), f"{wifi_password[-20:]}_", fill="white")
                context = "".join(
                    f"[{KEYBOARD_CHARS[(kbd_char_index + o) % len(KEYBOARD_CHARS)]}]"
                    if o == 0 else
                    f" {KEYBOARD_CHARS[(kbd_char_index + o) % len(KEYBOARD_CHARS)]} "
                    for o in range(-3, 4)
                )
                draw.text((2, 27), context, fill="white")
                grp_names = ["a-z", "A-Z", "0-9", "!@#"]
                cur_grp = max(i for i, s in enumerate(KBD_GROUPS) if s <= kbd_char_index)
                draw.text((2, 42), f"E1:char  E2:{grp_names[cur_grp]}", fill="white")
                draw.text((2, 53), "K1:add K2:del K3:conectar", fill="white")

            elif screen == "wifi_result":
                draw.text((2, 1), "WIFI", fill="white")
                if wifi_connecting:
                    draw.text((2, 20), "conectando...", fill="white")
                else:
                    draw.text((2, 20), wifi_result_msg[:21], fill="white")
                    draw.text((2, 40), "pulsa un boton", fill="white")

        time.sleep(0.02)
except KeyboardInterrupt:
    pass
finally:
    midi.close()
    controls.close()
    analyzer.close()
    wled.stop()
    device.cleanup()
    print("\nListo, saliendo.")
