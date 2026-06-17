"""Cliente WLED HTTP real (Fase 6): aplica presets y envia actualizaciones
de brillo por segmento a POST /json/state."""
import requests


class WLEDClient:
    def __init__(self, host, timeout=0.5):
        self._base = f"http://{host}"
        self._timeout = timeout
        self.presets = {}  # {id (int): nombre (str)}
        self._load_presets()

    def _load_presets(self):
        try:
            r = requests.get(f"{self._base}/presets.json", timeout=self._timeout)
            data = r.json()
            self.presets = {
                int(k): v.get("n", f"Preset {k}")
                for k, v in data.items()
                if k != "0" and v.get("n")
            }
        except Exception as e:
            print(f"WLED: no se pudieron cargar presets: {e}")

    def apply_preset(self, preset_id):
        try:
            requests.post(
                f"{self._base}/json/state",
                json={"ps": preset_id},
                timeout=self._timeout,
            )
        except Exception as e:
            print(f"WLED: error aplicando preset {preset_id}: {e}")

    def send_state(self, state):
        """Envia brillo por segmento con transicion instantanea (transition=0)
        para maxima reactividad al audio."""
        try:
            requests.post(
                f"{self._base}/json/state",
                json=dict(state, transition=0),
                timeout=self._timeout,
            )
        except Exception as e:
            print(f"WLED: error enviando estado: {e}")
