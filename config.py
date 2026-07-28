"""Config: carga/guarda config.json y define el esquema de tiras (N tiras,
hoy 4). Antes vivia dentro de router.py junto con la logica de Router.

Cada tira en `strips` combina datos de hardware (tamano, pin fisico en el
controlador WLED - solo informativos, para la pantalla "Config Tiras") con
sus ajustes de comportamiento (fuente, banda/canal, color/efecto/velocidad/
cola de pulso, brillo ambiente).
"""
import json

HARDWARE_DEFAULTS = [
    {"id": 0, "name": "Tira 1", "length_m": 3, "num_leds": 150, "pin": "IO16"},
    {"id": 1, "name": "Tira 2", "length_m": 3, "num_leds": 150, "pin": "IO12"},
    {"id": 2, "name": "Tira 3", "length_m": 5, "num_leds": 250, "pin": "IO4"},
    {"id": 3, "name": "Tira 4", "length_m": 5, "num_leds": 250, "pin": "IO2"},
]

BEHAVIOR_DEFAULTS = {
    "source": "audio",
    "audio_band": "all",
    "midi_channel": "all",
    "midi_device": "all",
    "pulse_color": [255, 80, 0],
    "pulse_velocity": 150,
    "pulse_tail": 30,
    "fx": "reactive",
    "fx_speed": 128,
    "bri_floor": 0,
    "active": True,
}


def _default_strip(i):
    strip = dict(HARDWARE_DEFAULTS[i])
    strip.update(BEHAVIOR_DEFAULTS)
    strip["pulse_color"] = list(BEHAVIOR_DEFAULTS["pulse_color"])
    return strip


DEFAULT_CONFIG = {
    "audio_gain": 1.0,
    "audio_volume": 1.0,
    "audio_threshold": 0.0,
    "strips": [_default_strip(i) for i in range(len(HARDWARE_DEFAULTS))],
    "wled_host": "192.168.1.100",
    "wled_preset": 1,
    "wled_pulse_reverse": True,
    "wled_output_enabled": True,
    "wled_brightness": 255,
    "oled_contrast": 127,
    "ap_ssid": "LightReactive",
    "ap_password": "lightreact",
    "wifi_saved": {},
}


def _migrate_legacy_segments(config):
    """Convierte el esquema viejo (2 tiras, `segments` + ajustes globales de
    pulso) al esquema nuevo de `strips` (N tiras, ajustes por tira). No hace
    nada si el config ya tiene `strips`."""
    if "strips" in config:
        return

    old_segments = config.pop("segments", [])
    global_color = config.pop("wled_pulse_color", list(BEHAVIOR_DEFAULTS["pulse_color"]))
    global_velocity = config.pop("wled_velocity", BEHAVIOR_DEFAULTS["pulse_velocity"])
    global_tail = config.pop("wled_tail", BEHAVIOR_DEFAULTS["pulse_tail"])
    global_bri_floor = config.pop("wled_bri_floor", BEHAVIOR_DEFAULTS["bri_floor"])
    config.pop("wled_seg_sizes", None)
    config.pop("output_mode", None)
    config.pop("wled_preset_mode_id", None)
    config.pop("wled_preset_bri_idle", None)

    old_by_id = {seg["id"]: seg for seg in old_segments}

    strips = []
    for i, hw in enumerate(HARDWARE_DEFAULTS):
        strip = dict(hw)
        old = old_by_id.get(i)
        if old is not None:
            strip["name"] = old.get("name", strip["name"])
            strip["source"] = old.get("source", BEHAVIOR_DEFAULTS["source"])
            strip["audio_band"] = old.get("audio_band", BEHAVIOR_DEFAULTS["audio_band"])
            strip["midi_channel"] = old.get("midi_channel", BEHAVIOR_DEFAULTS["midi_channel"])
            strip["midi_device"] = BEHAVIOR_DEFAULTS["midi_device"]
            strip["pulse_color"] = list(global_color)
            strip["pulse_velocity"] = global_velocity
            strip["pulse_tail"] = global_tail
            strip["bri_floor"] = global_bri_floor
            strip["fx"] = "reactive"
            strip["fx_speed"] = BEHAVIOR_DEFAULTS["fx_speed"]
            strip["active"] = True
        else:
            strip.update(BEHAVIOR_DEFAULTS)
            strip["pulse_color"] = list(BEHAVIOR_DEFAULTS["pulse_color"])
        strips.append(strip)

    config["strips"] = strips


def _backfill_strip_defaults(config):
    """Rellena en cada tira cualquier campo de BEHAVIOR_DEFAULTS que aun no
    exista (p.ej. tras anadir un campo nuevo a un config.json ya migrado a
    `strips`, como `midi_device`). Devuelve True si rellano algo."""
    changed = False
    for strip in config.get("strips", []):
        for key, default in BEHAVIOR_DEFAULTS.items():
            if key not in strip:
                strip[key] = list(default) if isinstance(default, list) else default
                changed = True
    return changed


def load_config(path="config.json"):
    try:
        with open(path) as f:
            config = json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_CONFIG))  # copia profunda

    migrated = "strips" not in config
    _migrate_legacy_segments(config)
    backfilled = _backfill_strip_defaults(config)
    if migrated or backfilled:
        save_config(config, path)  # persiste el esquema actualizado de inmediato
    return config


def save_config(config, path="config.json"):
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def active_strips(config):
    """Tiras activas (`active`=True), ordenadas por id. Las inactivas no
    cuentan para el tamano del buffer de WLEDAnimator ni reciben triggers -
    utiles para desactivar tiras que aun no estan conectadas al controlador
    WLED fisico (p.ej. mientras se espera hardware nuevo)."""
    return sorted((s for s in config["strips"] if s.get("active", True)), key=lambda s: s["id"])


def animator_index(config, strip_id):
    """Posicion de `strip_id` dentro de WLEDAnimator, que solo conoce las
    tiras activas (en orden). None si esa tira esta inactiva - en ese caso
    no hay que llamar a ningun metodo de WLEDAnimator para ella."""
    for i, s in enumerate(active_strips(config)):
        if s["id"] == strip_id:
            return i
    return None


def strip_led_counts(config):
    """Tamanos (num_leds) de las tiras activas en orden de id, para construir
    WLEDAnimator (reemplaza el antiguo wled_seg_sizes, ahora derivado)."""
    return [s["num_leds"] for s in active_strips(config)]
