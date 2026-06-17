"""Router (Fase 5+): convierte niveles de audio por banda + eventos MIDI en el
estado de los segmentos WLED (solo brillo/on-off), segun la configuracion por
tira (LED1/LED2) en config.json.

El resultado tiene la forma del JSON API de WLED:
    {"seg": [{"id": 0, "on": true, "bri": 180}, ...]}
listo para enviarse tal cual a `POST /json/state` (Fase 6, aun no implementada).

El color de cada segmento NO se gestiona aqui: se configura directamente en
WLED.
"""
import json
import time

import numpy as np

DEFAULT_CONFIG = {
    "audio_gain": 1.0,
    "audio_volume": 1.0,
    "audio_threshold": 0.0,
    "segments": [
        {"id": 0, "name": "LED1", "source": "audio", "audio_band": "all", "midi_channel": "all"},
        {"id": 1, "name": "LED2", "source": "audio", "audio_band": "all", "midi_channel": "all"},
    ],
}


def load_config(path="config.json"):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_CONFIG))  # copia profunda


def save_config(config, path="config.json"):
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def band_level(levels, band):
    """Media de `levels` (bandas log de graves a agudos) para el grupo
    `band`: "bass"/"mid"/"treble" (cada uno un tercio de las bandas) o
    "all" (todas)."""
    if band == "all":
        values = levels
    else:
        n = len(levels)
        thirds = [round(n * k / 3) for k in range(4)]
        i = ("bass", "mid", "treble").index(band)
        values = levels[thirds[i]:thirds[i + 1]]
    return float(np.mean(values)) if len(values) else 0.0


class Router:
    def __init__(self, config, rate_hz=10, flash_half_life=0.12):
        self.config = config
        self._min_interval = 1.0 / rate_hz
        self._last_update = 0.0
        self._flash_half_life = flash_half_life
        self._flash = {seg["id"]: 0.0 for seg in config["segments"]}

    def flash(self, velocity=1.0, segments=None):
        """Dispara un destello (p.ej. desde un note_on MIDI). `velocity` en
        0..1 fija la intensidad inicial; decae exponencialmente con
        `flash_half_life` en cada `update()` posterior. `segments`, si se
        da, limita el destello a esos ids de segmento."""
        ids = segments if segments is not None else self._flash.keys()
        for seg_id in ids:
            self._flash[seg_id] = max(self._flash.get(seg_id, 0.0), velocity)

    def update(self, levels):
        """Calcula el estado WLED para los niveles de audio actuales,
        respetando `rate_hz`. Devuelve None si toca esperar (rate limiting)."""
        now = time.monotonic()
        dt = now - self._last_update
        if dt < self._min_interval:
            return None
        self._last_update = now
        decay = 0.5 ** (dt / self._flash_half_life)
        gain = self.config.get("audio_gain", 1.0)
        threshold = self.config.get("audio_threshold", 0.0)

        segments = []
        for seg in self.config["segments"]:
            audio_bri = 0
            if seg["source"] in ("audio", "both"):
                level = float(np.clip(band_level(levels, seg["audio_band"]) * gain, 0.0, 1.0))
                if level <= threshold:
                    level = 0.0
                elif threshold > 0.0:
                    level = (level - threshold) / (1.0 - threshold)
                audio_bri = int(round(level * 255))

            flash_level = self._flash.get(seg["id"], 0.0)
            flash_bri = int(round(flash_level * 255))
            self._flash[seg["id"]] = flash_level * decay

            bri = max(audio_bri, flash_bri)
            segments.append({"id": seg["id"], "on": bri > 0, "bri": bri})
        return {"seg": segments}
