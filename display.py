"""display.py - UI del menu OLED (UI v2, soporte N tiras).

Reemplaza el bloque de UI que antes vivia en demo_full.py. El main loop solo
necesita: init(device, config, wled, router, midi, analyzer, save_config),
on_encoder(n, delta), on_key(n, pressed), render(draw) (dentro de un
`with canvas(device) as draw:`) y tick() (cada frame, para los long-press).

Pantallas (`screen`):
  "pages"       -> 7 paginas raiz (K1 corto cicla): TIRAS/FUENTES/WLED-RED/
                   PRESETS/BRILLO/GLOBAL/ESCENAS. K3 corto = toggle salida
                   WLED, salvo en TIRAS (K3 corto entra al detalle de la
                   tira resaltada con E1) y en ESCENAS (K3 corto aplica la
                   escena resaltada con E1, K3 mantenido la guarda). K2
                   corto = modo VU limpio.
  "tira_detail" -> campos de una tira: E2 mueve el cursor de campo, E1/E3
                   ajustan el valor del campo activo. K3 vuelve a TIRAS.
  "vu_clean"    -> 4 barras verticales, sin texto (modo actuacion en vivo).
                   K2 corto vuelve a "pages".
  "sistema"     -> solo estado de red (informativo) + apagado (K2
                   mantenido), alcanzable con K1 mantenido. Sin escaneo ni
                   conexion manual a otras redes - el Pi se conecta solo a
                   [red WiFi de casa] (prioridad en NetworkManager), quitado a
                   proposito para que no se pueda desconectar sin querer
                   pidiendo una contrasena por error (2026-08-03).
"""
import threading
import time

from PIL import ImageFont

import config as cfg
import scenes as scn
from system_control import get_network_status, shutdown

# ---------------------------------------------------------------------- #
# Fuente compacta (antes: bitmap font por defecto de PIL)
# ---------------------------------------------------------------------- #
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_SIZE = 8
LINE_H = 10
try:
    FONT = ImageFont.truetype(_FONT_PATH, FONT_SIZE)
except OSError:
    FONT = ImageFont.load_default()

# ---------------------------------------------------------------------- #
# Constantes de dominio
# ---------------------------------------------------------------------- #
SOURCES = ["audio", "midi", "both"]
SOURCE_LABELS = {"audio": "audio", "midi": "midi", "both": "audio+midi"}
SOURCE_LETTER = {"audio": "A", "midi": "M", "both": "X"}

BANDS = ["bass", "mid", "treble", "all"]
BAND_LABELS = {"bass": "graves", "mid": "medios", "treble": "agudos", "all": "todo"}

CHANNELS = [1, 2, 3, "all"]
CHANNEL_LABELS = {1: "1", 2: "2", 3: "3", "all": "todos"}

COLOR_PALETTE = [
    ("naranja", (255, 80, 0)),
    ("rojo", (255, 0, 0)),
    ("verde", (0, 255, 0)),
    ("azul", (0, 80, 255)),
    ("cian", (0, 255, 255)),
    ("magenta", (255, 0, 255)),
    ("amarillo", (255, 200, 0)),
    ("blanco", (255, 255, 255)),
    ("purpura", (140, 0, 255)),
]

FIELD_LABELS = {
    "active": "Activa",
    "source": "Fuente",
    "audio_band": "Banda",
    "midi_channel": "Canal MIDI",
    "midi_device": "Disp. MIDI",
    "color": "Color",
    "fx": "Efecto",
    "velocity": "Velocidad",
    "tail": "Cola",
    "sparkle": "Sparkle",
    "reverse": "Reversa",
    "bri_floor": "Brillo amb.",
}

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
SPARKLE_STEP = 5
SPARKLE_MIN, SPARKLE_MAX = 0, 255
FLOOR_STEP = 5
FLOOR_MIN, FLOOR_MAX = 0, 200
FX_SPEED_STEP = 5
FX_SPEED_MIN, FX_SPEED_MAX = 0, 255
CONTRAST_STEP = 5
CONTRAST_MIN, CONTRAST_MAX = 0, 255
BRIGHTNESS_STEP = 5
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 0, 255

ROOT_PAGES = ["TIRAS", "FUENTES", "WLED/RED", "PRESETS", "BRILLO", "GLOBAL", "ESCENAS"]
DETAIL_VISIBLE_ROWS = 4

# Campos que la pagina GLOBAL ajusta de forma relativa (+delta*step) en las
# 4 tiras a la vez. "bri_floor" aplica siempre; velocity/tail/sparkle solo
# a tiras en modo "reactive" (en modo native effect no tienen sentido - ver
# _adjust_field/_strip_fields).
GLOBAL_FIELDS = ["velocity", "tail", "sparkle", "bri_floor"]

LONG_PRESS_K1 = 1.2
LONG_PRESS_K2 = 1.5
LONG_PRESS_K3 = 1.5


def cycle(options, current, delta):
    i = options.index(current)
    return options[(i + delta) % len(options)]


def _clamp_step(value, delta, step, lo, hi, round_to=2):
    return round(min(hi, max(lo, value + delta * step)), round_to)


# ---------------------------------------------------------------------- #
# Estado (poblado por init())
# ---------------------------------------------------------------------- #
_device = None
_config = None
_wled = None
_router = None
_midi = None
_analyzer = None
_save_config = None

seg_state = []
page = 0
prev_page = 0
screen = "pages"

tira_cursor = 0        # fila resaltada en la pagina TIRAS
detail_strip_id = 0    # tira en edicion en tira_detail
detail_field_idx = 0   # cursor de campo en tira_detail
preset_index = 0        # cursor en la pagina PRESETS
global_field_idx = 0    # cursor de campo en la pagina GLOBAL

scenes = []             # lista de N_SCENES snapshots (o None), ver init()
scene_index = 0         # escena resaltada en la pagina ESCENAS
scene_msg = ""          # feedback transitorio ("guardada"/"vacia"...)
scene_msg_ts = 0.0

network_status = {"iface": None, "ip": "", "ssid": None}

key_press_ts = {1: None, 2: None, 3: None}
key_long_fired = {1: False, 2: False, 3: False}

# Guardado con debounce: girar un encoder dispara muchos cambios seguidos:
# agrupamos en una sola escritura a disco tras SAVE_DEBOUNCE_S sin cambios
# nuevos, en vez de escribir en cada tick. Reduce desgaste de la SD.
SAVE_DEBOUNCE_S = 0.7
_save_timer = None
_save_lock = threading.Lock()


def init(device, config, wled, router, midi, analyzer, save_config):
    global _device, _config, _wled, _router, _midi, _analyzer, _save_config
    global seg_state, preset_index, scenes
    _device, _config, _wled, _router = device, config, wled, router
    _midi, _analyzer, _save_config = midi, analyzer, save_config

    seg_state = [{"id": s["id"], "bri": 0} for s in config["strips"]]

    preset_list = sorted(wled.presets.items())
    saved = config.get("wled_preset", 1)
    preset_index = next((i for i, (pid, _) in enumerate(preset_list) if pid == saved), 0)

    scenes = scn.load_scenes()

    _sync_all_strips_to_wled()


def _sync_all_strips_to_wled():
    """Empuja a WLED el ambiente/efecto nativo de cada tira activa segun el
    `_config` actual. Llamado al arrancar y tras aplicar una escena (que
    cambia varios campos de golpe sin pasar por _adjust_field)."""
    for strip in _config["strips"]:
        if not strip.get("active", True):
            continue
        idx = cfg.animator_index(_config, strip["id"])
        _wled.set_segment_ambient(idx, strip["bri_floor"], strip["pulse_color"])
        if strip["fx"] != "reactive":
            _wled.set_segment_effect(idx, strip["fx"], strip.get("fx_speed"), strip["pulse_color"])


def _apply_scene_and_sync(index):
    """Aplica la escena `index` sobre _config (in place) y dispara los mismos
    efectos secundarios que _adjust_field hace campo a campo (ambiente/efecto
    nativo por tira), ademas de guardar el nuevo estado a disco."""
    global scene_msg, scene_msg_ts
    ok = scn.apply_scene(scenes, index, _config)
    scene_msg_ts = time.monotonic()
    if ok:
        _sync_all_strips_to_wled()
        _save_config(_config)
        scene_msg = "aplicada"
    else:
        scene_msg = "(vacia)"


def _debounced_save():
    """Programa una escritura de config.json tras SAVE_DEBOUNCE_S de
    inactividad, cancelando cualquier escritura pendiente anterior."""
    global _save_timer
    with _save_lock:
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(SAVE_DEBOUNCE_S, flush_pending_save)
        _save_timer.daemon = True
        _save_timer.start()


def flush_pending_save():
    """Escribe ya cualquier cambio pendiente. Llamar antes de apagar/salir
    para no perder el ultimo ajuste hecho justo antes de un guardado con
    debounce todavia no disparado."""
    global _save_timer
    with _save_lock:
        _save_timer = None
    _save_config(_config)


def held_ratio(n, threshold):
    ts = key_press_ts[n]
    if ts is None:
        return 0.0
    return min(1.0, (time.monotonic() - ts) / threshold)


def _strips_by_id():
    return {s["id"]: s for s in _config["strips"]}


def _sorted_strips():
    return sorted(_config["strips"], key=lambda s: s["id"])


# ------------------------------------------------------------------ #
# Campos editables de una tira (detalle)
# ------------------------------------------------------------------ #

def _strip_fields(strip):
    fields = ["active"]
    if not strip.get("active", True):
        return fields
    fields.append("source")
    if strip["source"] in ("audio", "both"):
        fields.append("audio_band")
    if strip["source"] in ("midi", "both"):
        fields.append("midi_channel")
        fields.append("midi_device")
    fields += ["color", "fx", "velocity"]
    if strip["fx"] == "reactive":
        fields.append("tail")
        fields.append("sparkle")
        if "pulse_reverse" in strip:  # solo Tira 3 y Tira 4 (ver cfg.REVERSIBLE_STRIP_IDS)
            fields.append("reverse")
    fields.append("bri_floor")
    return fields


def _fx_options():
    return ["reactive"] + list(range(len(_wled.effects)))


def _fx_label(fx):
    if fx == "reactive":
        return "Pulso reactivo"
    if _wled.effects and 0 <= fx < len(_wled.effects):
        return _wled.effects[fx][:16]
    return f"FX {fx}"


def _palette_index(color):
    color = tuple(color)
    for i, (_, rgb) in enumerate(COLOR_PALETTE):
        if rgb == color:
            return i
    return 0


def _field_value_str(strip, key):
    if key == "active":
        return "si" if strip.get("active", True) else "no"
    if key == "source":
        return SOURCE_LABELS[strip["source"]]
    if key == "audio_band":
        return BAND_LABELS[strip["audio_band"]]
    if key == "midi_channel":
        return CHANNEL_LABELS[strip["midi_channel"]]
    if key == "midi_device":
        dev = strip.get("midi_device", "all")
        return "todos" if dev == "all" else dev
    if key == "color":
        return COLOR_PALETTE[_palette_index(strip["pulse_color"])][0]
    if key == "fx":
        return _fx_label(strip["fx"])
    if key == "velocity":
        return str(strip["pulse_velocity"]) if strip["fx"] == "reactive" else str(strip["fx_speed"])
    if key == "tail":
        return str(strip["pulse_tail"])
    if key == "sparkle":
        return str(strip.get("sparkle", 0))
    if key == "reverse":
        return "si" if strip.get("pulse_reverse", True) else "no"
    if key == "bri_floor":
        return str(strip["bri_floor"])
    return ""


def _adjust_field(strip, key, delta):
    # `idx` es la posicion de esta tira dentro de WLEDAnimator (que solo
    # conoce las tiras activas) - None si esta inactiva o si activar/
    # desactivar requiere reiniciar el servicio para que WLEDAnimator
    # reconstruya su buffer con el nuevo conjunto de tiras.
    idx = cfg.animator_index(_config, strip["id"])
    if key == "active":
        strip["active"] = not strip.get("active", True)
    elif key == "source":
        strip["source"] = cycle(SOURCES, strip["source"], delta)
    elif key == "audio_band":
        strip["audio_band"] = cycle(BANDS, strip["audio_band"], delta)
    elif key == "midi_channel":
        strip["midi_channel"] = cycle(CHANNELS, strip["midi_channel"], delta)
    elif key == "midi_device":
        options = ["all"] + _midi.device_names
        cur = strip.get("midi_device", "all")
        cur_idx = options.index(cur) if cur in options else 0
        strip["midi_device"] = options[(cur_idx + delta) % len(options)]
    elif key == "color":
        pidx = (_palette_index(strip["pulse_color"]) + delta) % len(COLOR_PALETTE)
        strip["pulse_color"] = list(COLOR_PALETTE[pidx][1])
        if idx is not None:
            _wled.set_segment_ambient(idx, strip["bri_floor"], strip["pulse_color"])
            if strip["fx"] != "reactive":
                _wled.set_segment_effect(idx, strip["fx"], strip["fx_speed"], strip["pulse_color"])
    elif key == "fx":
        options = _fx_options()
        cur_idx = options.index(strip["fx"]) if strip["fx"] in options else 0
        strip["fx"] = options[(cur_idx + delta) % len(options)]
        if strip["fx"] != "reactive" and idx is not None:
            _wled.set_segment_effect(idx, strip["fx"], strip["fx_speed"], strip["pulse_color"])
    elif key == "velocity":
        if strip["fx"] == "reactive":
            strip["pulse_velocity"] = int(_clamp_step(strip["pulse_velocity"], delta, VEL_STEP, VEL_MIN, VEL_MAX, 0))
        else:
            strip["fx_speed"] = int(_clamp_step(strip["fx_speed"], delta, FX_SPEED_STEP, FX_SPEED_MIN, FX_SPEED_MAX, 0))
            if idx is not None:
                _wled.set_segment_effect(idx, strip["fx"], strip["fx_speed"], strip["pulse_color"])
    elif key == "tail":
        strip["pulse_tail"] = int(_clamp_step(strip["pulse_tail"], delta, TAIL_STEP, TAIL_MIN, TAIL_MAX, 0))
    elif key == "sparkle":
        strip["sparkle"] = int(_clamp_step(strip.get("sparkle", 0), delta, SPARKLE_STEP, SPARKLE_MIN, SPARKLE_MAX, 0))
    elif key == "reverse":
        strip["pulse_reverse"] = not strip.get("pulse_reverse", True)
    elif key == "bri_floor":
        strip["bri_floor"] = int(_clamp_step(strip["bri_floor"], delta, FLOOR_STEP, FLOOR_MIN, FLOOR_MAX, 0))
        if idx is not None:
            _wled.set_segment_ambient(idx, strip["bri_floor"], strip["pulse_color"])
    _debounced_save()


# ------------------------------------------------------------------ #
# Encoders
# ------------------------------------------------------------------ #

def on_encoder(n, delta):
    global tira_cursor, detail_field_idx, preset_index
    global global_field_idx, scene_index
    if not delta:
        return

    if screen == "pages":
        if page == 0:  # TIRAS
            if n == 1:
                tira_cursor = (tira_cursor + delta) % len(_config["strips"])
        elif page == 1:  # FUENTES
            if n == 1:
                _config["audio_gain"] = _clamp_step(_config.get("audio_gain", 1.0), delta, GAIN_STEP, GAIN_MIN, GAIN_MAX)
            elif n == 2:
                _config["audio_volume"] = _clamp_step(_config.get("audio_volume", 1.0), delta, VOLUME_STEP, VOLUME_MIN, VOLUME_MAX)
                _analyzer.volume = _config["audio_volume"]
            elif n == 3:
                _config["audio_threshold"] = _clamp_step(_config.get("audio_threshold", 0.0), delta, THRESHOLD_STEP, THRESHOLD_MIN, THRESHOLD_MAX)
            else:
                return
            _debounced_save()
        elif page == 2:  # WLED/RED
            if n == 1:
                _config["wled_pulse_reverse"] = not _config.get("wled_pulse_reverse", True)
                _debounced_save()
        elif page == 3:  # PRESETS
            preset_list = sorted(_wled.presets.items())
            if n == 1 and preset_list:
                preset_index = (preset_index + delta) % len(preset_list)
                pid, _ = preset_list[preset_index]
                _config["wled_preset"] = pid
                _debounced_save()
                _wled.apply_preset(pid)
        elif page == 4:  # BRILLO
            if n == 1:
                b = int(_clamp_step(_config.get("wled_brightness", 255), delta, BRIGHTNESS_STEP, BRIGHTNESS_MIN, BRIGHTNESS_MAX, 0))
                _config["wled_brightness"] = b
                _wled.brightness = b
                _debounced_save()
            elif n == 2:
                c = int(_clamp_step(_config.get("oled_contrast", 127), delta, CONTRAST_STEP, CONTRAST_MIN, CONTRAST_MAX, 0))
                _config["oled_contrast"] = c
                _device.contrast(c)
                _debounced_save()
        elif page == 5:  # GLOBAL
            if n == 2:
                global_field_idx = (global_field_idx + delta) % len(GLOBAL_FIELDS)
            elif n in (1, 3):
                key = GLOBAL_FIELDS[global_field_idx]
                for strip in _config["strips"]:
                    if key != "bri_floor" and strip["fx"] != "reactive":
                        continue  # velocity/tail/sparkle no aplican en modo native effect
                    _adjust_field(strip, key, delta)
        elif page == 6:  # ESCENAS
            if n == 1:
                scene_index = (scene_index + delta) % scn.N_SCENES
    elif screen == "tira_detail":
        strip = _strips_by_id()[detail_strip_id]
        fields = _strip_fields(strip)
        detail_field_idx = min(detail_field_idx, len(fields) - 1)
        if n == 2:
            detail_field_idx = (detail_field_idx + delta) % len(fields)
        elif n in (1, 3):
            _adjust_field(strip, fields[detail_field_idx], delta)


# ------------------------------------------------------------------ #
# Botones
# ------------------------------------------------------------------ #

def on_key(n, pressed):
    global page, screen, tira_cursor, detail_strip_id, detail_field_idx, network_status

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
            page = (page + 1) % len(ROOT_PAGES)
        elif n == 2:
            screen = "vu_clean"
        elif n == 3:
            if page == 0:
                detail_strip_id = _sorted_strips()[tira_cursor]["id"]
                detail_field_idx = 0
                screen = "tira_detail"
            elif page == 6:
                _apply_scene_and_sync(scene_index)
            else:
                _wled.output_enabled = not _wled.output_enabled
                _config["wled_output_enabled"] = _wled.output_enabled
                _save_config(_config)
    elif screen == "tira_detail":
        if n == 3:
            screen = "pages"
    elif screen == "vu_clean":
        if n == 2:
            screen = "pages"


def tick():
    """Chequeo de long-press; llamar una vez por frame desde el main loop."""
    global screen, page, prev_page, network_status, scene_msg, scene_msg_ts
    now = time.monotonic()

    if (screen == "pages" and page == 6
            and key_press_ts[3] is not None and not key_long_fired[3]):
        if now - key_press_ts[3] >= LONG_PRESS_K3:
            key_long_fired[3] = True
            scn.save_scene(scenes, scene_index, _config)
            scene_msg = "guardada"
            scene_msg_ts = now

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
            flush_pending_save()
            shutdown()


# ------------------------------------------------------------------ #
# Dibujo
# ------------------------------------------------------------------ #

def _text(draw, x, y, s, fill="white"):
    draw.text((x, y), s, font=FONT, fill=fill)


def _row_bg(draw, y, w=128):
    draw.rectangle((0, y, w - 1, y + LINE_H - 1), fill="white")


def _status_tag():
    return "W:OFF" if not _wled.output_enabled else ""


def _header(draw, title, tag=None):
    if tag is None:
        tag = _status_tag()
    _text(draw, 2, 0, title)
    if tag:
        w = draw.textlength(tag, font=FONT)
        draw.text((126 - w, 0), tag, font=FONT, fill="white")


def render(draw):
    if screen == "pages":
        _render_pages(draw)
    elif screen == "tira_detail":
        _render_tira_detail(draw)
    elif screen == "vu_clean":
        _render_vu_clean(draw)
    elif screen == "sistema":
        _render_sistema(draw)


def _render_pages(draw):
    if page == 0:
        _render_tiras(draw)
    elif page == 1:
        _render_fuentes(draw)
    elif page == 2:
        _render_wled_red(draw)
    elif page == 3:
        _render_presets(draw)
    elif page == 4:
        _render_brillo(draw)
    elif page == 5:
        _render_global(draw)
    elif page == 6:
        _render_escenas(draw)


def _render_tiras(draw):
    _header(draw, "TIRAS")
    strips = _sorted_strips()
    label_w = 34
    bar_x0 = label_w
    bar_w = 128 - bar_x0 - 2
    bar_h = 6
    for i, strip in enumerate(strips):
        y = LINE_H * (i + 1)
        selected = (i == tira_cursor)
        if selected:
            _row_bg(draw, y, w=label_w)
        label = f"T{strip['id'] + 1} {SOURCE_LETTER[strip['source']]}"
        draw.text((2, y), label, font=FONT, fill=("black" if selected else "white"))

        if not strip.get("active", True):
            _text(draw, bar_x0, y + 1, "(inactiva)")
            continue

        bri = next((s["bri"] for s in seg_state if s["id"] == strip["id"]), 0)
        bar_y = y + (LINE_H - bar_h) // 2
        draw.rectangle((bar_x0, bar_y, bar_x0 + bar_w - 1, bar_y + bar_h - 1), outline="white")
        fill_w = int(bar_w * bri / 255.0)
        if fill_w > 0:
            draw.rectangle((bar_x0, bar_y, bar_x0 + fill_w - 1, bar_y + bar_h - 1), fill="white")

    sel = strips[tira_cursor]
    if sel.get("active", True):
        footer = f"{sel['name']}: {sel['length_m']}m {sel['num_leds']}LED {sel['pin']}"
    else:
        footer = f"{sel['name']}: {sel['length_m']}m {sel['num_leds']}LED [OFF]"
    _text(draw, 2, LINE_H * 5, footer[:24])


def _render_fuentes(draw):
    _header(draw, "FUENTES")
    _text(draw, 2, LINE_H, f"ganancia in: {_config.get('audio_gain', 1.0):.2f}")
    _text(draw, 2, LINE_H * 2, f"volumen out: {_config.get('audio_volume', 1.0):.2f}")
    _text(draw, 2, LINE_H * 3, f"umbral in: {_config.get('audio_threshold', 0.0):.2f}")


def _render_wled_red(draw):
    _header(draw, "WLED/RED")
    _text(draw, 2, LINE_H, f"host: {_config.get('wled_host', '?')}")
    rev = "si" if _config.get("wled_pulse_reverse", True) else "no"
    _text(draw, 2, LINE_H * 2, f"reversa pulso: {rev}")
    _text(draw, 2, LINE_H * 3, f"presets:{len(_wled.presets)} fx:{len(_wled.effects)}")
    _text(draw, 2, LINE_H * 4, "K3: on/off salida")


def _render_presets(draw):
    _header(draw, "PRESETS")
    preset_list = sorted(_wled.presets.items())
    if not preset_list:
        _text(draw, 2, LINE_H, "conectando WLED...")
        return
    idx = min(preset_index, len(preset_list) - 1)
    pid, pname = preset_list[idx]
    _text(draw, 2, LINE_H, pname[:20])
    _text(draw, 2, LINE_H * 2, f"[{idx + 1}/{len(preset_list)}]  id={pid}")
    _text(draw, 2, LINE_H * 3, "E1: cambiar (aplica ya)")


def _render_brillo(draw):
    _header(draw, "BRILLO")
    bar_w = 100

    b = _config.get("wled_brightness", 255)
    _text(draw, 2, LINE_H, f"brillo tiras: {b}")
    bar_y1 = LINE_H * 2
    draw.rectangle((2, bar_y1, 2 + bar_w, bar_y1 + 6), outline="white")
    fill_w = int(bar_w * b / BRIGHTNESS_MAX)
    if fill_w > 0:
        draw.rectangle((2, bar_y1, 2 + fill_w, bar_y1 + 6), fill="white")

    c = _config.get("oled_contrast", 127)
    _text(draw, 2, LINE_H * 3 + 2, f"contraste pantalla: {c}")
    bar_y2 = LINE_H * 4 + 2
    draw.rectangle((2, bar_y2, 2 + bar_w, bar_y2 + 6), outline="white")
    fill_w = int(bar_w * c / CONTRAST_MAX)
    if fill_w > 0:
        draw.rectangle((2, bar_y2, 2 + fill_w, bar_y2 + 6), fill="white")


def _render_global(draw):
    key = GLOBAL_FIELDS[global_field_idx]
    _header(draw, "GLOBAL", FIELD_LABELS[key])
    for i, strip in enumerate(_sorted_strips()):
        y = LINE_H * (i + 1)
        if key != "bri_floor" and strip["fx"] != "reactive":
            val = "(native)"
        else:
            val = _field_value_str(strip, key)
        _text(draw, 2, y, f"T{strip['id'] + 1}: {val}")
    _text(draw, 2, LINE_H * 5, "E2:campo E1/E3:+/- (rel.)")


def _render_escenas(draw):
    _header(draw, "ESCENAS")
    name = scn.SCENE_NAMES[scene_index]
    estado = "vacia" if scenes[scene_index] is None else "guardada"
    _text(draw, 2, LINE_H, name)
    _text(draw, 2, LINE_H * 2, f"[{scene_index + 1}/{scn.N_SCENES}]  {estado}")
    if scene_msg and time.monotonic() - scene_msg_ts < 1.5:
        _text(draw, 2, LINE_H * 3, scene_msg)
    _text(draw, 2, LINE_H * 4, "E1: elegir")
    _text(draw, 2, LINE_H * 5, "K3: aplicar  K3 manten: guardar")


def _render_tira_detail(draw):
    global detail_field_idx
    strip = _strips_by_id()[detail_strip_id]
    fields = _strip_fields(strip)
    detail_field_idx = min(detail_field_idx, len(fields) - 1)

    title = f"T{strip['id'] + 1}: {strip['name'][:10]}"
    _header(draw, title, f"{detail_field_idx + 1}/{len(fields)}")

    start = max(0, min(detail_field_idx - DETAIL_VISIBLE_ROWS // 2, len(fields) - DETAIL_VISIBLE_ROWS))
    for row, idx in enumerate(range(start, min(start + DETAIL_VISIBLE_ROWS, len(fields)))):
        key = fields[idx]
        y = LINE_H * (row + 1)
        selected = (idx == detail_field_idx)
        if selected:
            _row_bg(draw, y)
        line = f"{FIELD_LABELS[key]}: {_field_value_str(strip, key)}"
        draw.text((2, y), line[:24], font=FONT, fill=("black" if selected else "white"))

    _text(draw, 2, LINE_H * 5, "E2:fila E1/E3:val K3<-")


def _render_vu_clean(draw):
    strips = _sorted_strips()
    n = len(strips) or 1
    gap = 4
    col_w = (128 - gap * (n + 1)) // n
    for i, strip in enumerate(strips):
        x0 = gap + i * (col_w + gap)
        if not strip.get("active", True):
            continue
        bri = next((s["bri"] for s in seg_state if s["id"] == strip["id"]), 0)
        h = int((bri / 255.0) * 64)
        if h > 0:
            draw.rectangle((x0, 64 - h, x0 + col_w - 1, 63), fill="white")


def _render_sistema(draw):
    _text(draw, 2, 1, "SISTEMA")
    if network_status["iface"] is None:
        _text(draw, 2, 16, "sin red")
    else:
        label = network_status["ssid"] or network_status["iface"]
        _text(draw, 2, 16, label[:21])
        _text(draw, 2, 28, network_status["ip"] or "(sin ip)")
    _text(draw, 2, 42, "K1: volver")
    ratio = held_ratio(2, LONG_PRESS_K2)
    if ratio > 0:
        _text(draw, 2, 52, "apagando (manten K2)")
        draw.rectangle((2, 60, 2 + int(120 * ratio), 63), fill="white")
