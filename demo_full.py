#!/usr/bin/env python3
"""Punto de entrada: orquesta audio/MIDI/WLED/pantalla para N tiras LED
(hoy 4, ver config.py). La UI completa (menus, pantallas de sistema/wifi)
vive en display.py; aqui solo se conectan las piezas y se enruta el audio/
MIDI reactivo hacia WLED.

Paginas raiz (K1 corto cicla): TIRAS / FUENTES / WLED-RED / PRESETS / BRILLO.
Ver display.py para el detalle de cada pantalla y los controles.
"""
import time

from luma.core.render import canvas

from audio_analysis import AudioAnalyzer
from controls import Controls
import config as cfg
import display
from midi_input import MidiInput
from router import Router, band_level
from ssd1322_norns import ssd1322_norns
from wled_animator import WLEDAnimator
from wled_audio_sync import WLEDAudioSync

N_BANDS = 16

config = cfg.load_config()
router = Router(config, rate_hz=10)

WLED_HOST = config.get("wled_host", "192.168.1.100")
seg_sizes = cfg.strip_led_counts(config)


def _on_wled_presets_loaded(presets):
    """Solo actualiza el cursor de la pagina PRESETS con el ultimo preset
    elegido a mano - por defecto el sistema arranca en modo reactivo (pulso
    DRGB por tira), sin aplicar ningun preset WLED. El preset solo se aplica
    si el usuario lo elige explicitamente en la pagina PRESETS."""
    preset_list = sorted(presets.items())
    if not preset_list:
        return
    saved = config.get("wled_preset", preset_list[0][0])
    idx = next((i for i, (pid, _) in enumerate(preset_list) if pid == saved), 0)
    display.preset_index = idx


wled = WLEDAnimator(WLED_HOST, seg_sizes=seg_sizes, on_presets_loaded=_on_wled_presets_loaded)

if wled.presets:
    _on_wled_presets_loaded(wled.presets)
else:
    print("WLED: sin presets en el arranque, reintentando en background...")

wled.brightness = config.get("wled_brightness", 255)

# Datos de audio hacia el usermod "Audio Reactive" de WLED (protocolo UDP
# sync, distinto del streaming DRGB de arriba) - sin esto los presets/efectos
# nativos audio-reactivos de WLED no tienen a que reaccionar.
audio_sync = WLEDAudioSync(WLED_HOST)

_beat_armed = {strip["id"]: True for strip in config["strips"]}


def on_levels(levels):
    s = router.update(levels)
    if s is not None:
        display.seg_state[:] = s["seg"]

    gain = config.get("audio_gain", 1.0)
    threshold = max(config.get("audio_threshold", 0.0), 0.05)
    global_reverse = config.get("wled_pulse_reverse", True)

    audio_sync.send(levels, analyzer.band_centers, gain=gain, threshold=threshold)

    for strip in config["strips"]:
        if (not strip.get("active", True) or strip["fx"] != "reactive"
                or strip["source"] not in ("audio", "both")):
            continue
        seg_id = strip["id"]
        idx = cfg.animator_index(config, seg_id)
        level = float(min(1.0, max(0.0, band_level(levels, strip["audio_band"]) * gain)))

        if level >= threshold and _beat_armed.get(seg_id, True):
            _beat_armed[seg_id] = False
            wled.trigger(
                seg_ids=[idx], velocity=level, color=tuple(strip["pulse_color"]),
                reverse=strip.get("pulse_reverse", global_reverse),
                pulse_velocity=strip["pulse_velocity"], pulse_tail=strip["pulse_tail"],
                pulse_sparkle=strip.get("sparkle", 0),
            )
        elif level < threshold * 0.55:
            _beat_armed[seg_id] = True


def on_midi(msg, device_name):
    if msg.type != "note_on" or msg.velocity <= 0:
        return
    ch = msg.channel + 1
    velocity = msg.velocity / 127.0

    # Nota relevante para el audio-sync hacia WLED (efectos/presets nativos
    # audio-reactivos) siempre que alguna tira la escuche, sin importar si
    # esa tira esta en modo pulso DRGB ("reactive") o en modo preset - el
    # audio-sync es una señal global del dispositivo, no depende de eso.
    midi_relevant = any(
        strip.get("active", True) and strip["source"] in ("midi", "both")
        and (strip["midi_channel"] == "all" or strip["midi_channel"] == ch)
        and (strip.get("midi_device", "all") in ("all", device_name))
        for strip in config["strips"]
    )
    if midi_relevant:
        audio_sync.note_on(velocity)

    # Pulso DRGB reactivo: solo para tiras en modo "reactive" (las de modo
    # preset no reciben pixeles nuestros, para no tapar el efecto nativo).
    matching = [
        strip for strip in config["strips"]
        if strip.get("active", True) and strip["fx"] == "reactive"
        and strip["source"] in ("midi", "both")
        and (strip["midi_channel"] == "all" or strip["midi_channel"] == ch)
        and (strip.get("midi_device", "all") in ("all", device_name))
    ]
    if not matching:
        return
    global_reverse = config.get("wled_pulse_reverse", True)
    router.flash(velocity=velocity, segments=[s["id"] for s in matching])

    for strip in matching:
        wled.trigger(
            seg_ids=[cfg.animator_index(config, strip["id"])], velocity=velocity, color=tuple(strip["pulse_color"]),
            reverse=strip.get("pulse_reverse", global_reverse),
            pulse_velocity=strip["pulse_velocity"], pulse_tail=strip["pulse_tail"],
            pulse_sparkle=strip.get("sparkle", 0),
        )


device = ssd1322_norns(is_shield=True)
device.contrast(config.get("oled_contrast", 127))
analyzer = AudioAnalyzer(on_levels=on_levels, n_bands=N_BANDS, volume=config.get("audio_volume", 1.0))
controls = Controls(on_encoder=display.on_encoder, on_key=display.on_key)
midi = MidiInput(on_message=on_midi)

display.init(device, config, wled, router, midi, analyzer, cfg.save_config)

print("Demo control 4 tiras. K1 cambia de pagina (mantener = sistema), K2 = modo VU limpio. Ctrl+C para salir.")

# Pantalla de texto/barras: ~20fps ya se ve fluida a simple vista, sin gastar
# CPU de mas redibujando mas rapido de lo que el ojo distingue. Los
# encoders/botones no dependen de este bucle (Controls tiene su propio hilo
# de polling de alta frecuencia), asi que bajar el fps de aqui no afecta a
# la capacidad de respuesta de los controles.
OLED_FRAME_S = 1.0 / 20

try:
    while True:
        frame_start = time.monotonic()
        display.tick()
        with canvas(device) as draw:
            display.render(draw)
        elapsed = time.monotonic() - frame_start
        time.sleep(max(0.0, OLED_FRAME_S - elapsed))
except KeyboardInterrupt:
    pass
finally:
    display.flush_pending_save()
    midi.close()
    controls.close()
    analyzer.close()
    wled.stop()
    audio_sync.close()
    device.cleanup()
    print("\nListo, saliendo.")
