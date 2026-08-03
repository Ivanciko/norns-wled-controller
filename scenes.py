"""scenes.py - guardar/aplicar snapshots completos de las 4 tiras ("escenas").

Una escena es un snapshot de comportamiento (fuente, color, efecto,
velocidad, cola, sparkle, brillo ambiente...) de las 4 tiras a la vez, ver
config.strips_snapshot()/apply_strips_snapshot(). Guardadas en scenes.json,
separado de config.json (config.json es el estado "en vivo" actual; scenes
son fotos guardadas aparte). Mismo patron de load/save que config.py.

Nombres fijos "Escena 1".."Escena N" (sin editor de texto - decidido con el
usuario: mas simple y mas rapido de usar en directo que un teclado en pantalla).
"""
import json

import config as cfg

N_SCENES = 8
SCENE_NAMES = [f"Escena {i + 1}" for i in range(N_SCENES)]


def load_scenes(path="scenes.json"):
    """Lista de N_SCENES elementos (snapshot o None si ese slot esta vacio)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    return [data.get(str(i)) for i in range(N_SCENES)]


def save_scene(scenes, index, config, path="scenes.json"):
    """Guarda la config actual de las 4 tiras en el slot `index`, sobrescribiendo
    lo que hubiera. Actualiza `scenes` in place y persiste a disco."""
    scenes[index] = cfg.strips_snapshot(config)
    _write(scenes, path)


def apply_scene(scenes, index, config):
    """Aplica el snapshot del slot `index` sobre config["strips"], in place.
    No hace nada (y devuelve False) si ese slot esta vacio."""
    snapshot = scenes[index]
    if snapshot is None:
        return False
    cfg.apply_strips_snapshot(config, snapshot)
    return True


def _write(scenes, path):
    data = {str(i): s for i, s in enumerate(scenes) if s is not None}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
