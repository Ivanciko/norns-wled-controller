"""Animacion de pulsos sobre WLED via UDP (protocolo DRGB).

Cada llamada a trigger() lanza un pulso nuevo e independiente: sale del
extremo del segmento y fluye hasta el otro extremo a 30 fps. Si llegan
varios triggers seguidos, varios pulsos viajan a la vez, cada uno en
su propia posicion.

WLED recibe los datos de pixel en crudo via UDP (DRGB, puerto 21324) y
entra en modo live. Cuando no hay pulsos, se envia un frame de mantenimiento
a 2fps (solo para que WLED no salga de live mode) con el color ambiente.
"""
import socket
import threading
import time

import requests

_DRGB_TYPE = 2
_DRGB_TIMEOUT = 2   # segundos que WLED espera antes de retomar su modo
_FPS_ACTIVE = 30    # fps mientras hay pulsos en movimiento
_FPS_IDLE = 2       # fps cuando no hay pulsos (solo keepalive)
_DEFAULT_VELOCITY = 150.0   # LEDs/segundo
_DEFAULT_TAIL = 30          # LEDs de cola


class WLEDAnimator:
    """Gestiona presets (via HTTP) y animacion de pulsos (via UDP/DRGB)."""

    def __init__(self, host, seg_sizes=(150, 150), udp_port=21324,
                 on_presets_loaded=None):
        self._base = f"http://{host}"
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_addr = (host, udp_port)
        self._n_leds = sum(seg_sizes)
        self._seg_sizes = list(seg_sizes)
        self._current_preset = None
        self._on_presets_loaded = on_presets_loaded

        # Buffers pre-alojados (sin allocacion por frame)
        self._pixels = bytearray(self._n_leds * 3)
        self._bg = bytearray(self._n_leds * 3)  # fondo precalculado
        self._header = bytes([_DRGB_TYPE, _DRGB_TIMEOUT])

        self._bri_floor = 0
        self._ambient_color = (255, 80, 0)
        self._pulse_velocity = _DEFAULT_VELOCITY
        self._pulse_tail = _DEFAULT_TAIL
        self._output_enabled = True
        self._output_mode = "drgb"   # "drgb" o "preset"
        self._preset_mode_id = 1     # preset activo en modo preset
        self._preset_data = {}       # datos completos de presets.json
        self._preset_bri_idle = 80   # brillo entre beats en modo preset (0-255)
        self._preset_restore_at = None  # monotonic timestamp: cuando lanzar el fade de vuelta

        self.presets = {}
        self._pulses = []
        self._lock = threading.Lock()
        self._wake = threading.Event()  # despierta el loop al instante en cada trigger

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # Intento inicial; si falla (ESP32 aun arrancando), reintenta en background
        self.load_presets()
        if not self.presets:
            threading.Thread(target=self._retry_load_presets, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Propiedades configurables desde fuera                                #
    # ------------------------------------------------------------------ #

    @property
    def bri_floor(self):
        return self._bri_floor

    @bri_floor.setter
    def bri_floor(self, value):
        self._bri_floor = int(value)
        self._update_bg()

    @property
    def ambient_color(self):
        return self._ambient_color

    @ambient_color.setter
    def ambient_color(self, value):
        self._ambient_color = tuple(value)
        self._update_bg()

    @property
    def pulse_velocity(self):
        return self._pulse_velocity

    @pulse_velocity.setter
    def pulse_velocity(self, value):
        self._pulse_velocity = float(value)

    @property
    def output_enabled(self):
        return self._output_enabled

    @output_enabled.setter
    def output_enabled(self, value):
        self._output_enabled = bool(value)
        if not self._output_enabled:
            with self._lock:
                self._pulses.clear()

    @property
    def pulse_tail(self):
        return self._pulse_tail

    @pulse_tail.setter
    def pulse_tail(self, value):
        self._pulse_tail = int(value)

    @property
    def output_mode(self):
        return self._output_mode

    @output_mode.setter
    def output_mode(self, value):
        self._output_mode = value
        if value == "preset":
            with self._lock:
                self._pulses.clear()
            self._preset_restore_at = None
            self.apply_preset(self._preset_mode_id)
            self._wake.set()

    @property
    def preset_mode_id(self):
        return self._preset_mode_id

    @preset_mode_id.setter
    def preset_mode_id(self, value):
        self._preset_mode_id = int(value)
        if self._output_mode == "preset":
            self.apply_preset(self._preset_mode_id)

    @property
    def preset_bri_idle(self):
        return self._preset_bri_idle

    @preset_bri_idle.setter
    def preset_bri_idle(self, value):
        self._preset_bri_idle = int(value)

    def _update_bg(self):
        """Recalcula el buffer de fondo cuando cambia floor o color."""
        floor = self._bri_floor
        r0, g0, b0 = self._ambient_color
        ri = int(r0 * floor / 255)
        gi = int(g0 * floor / 255)
        bi = int(b0 * floor / 255)
        for i in range(self._n_leds):
            self._bg[i * 3]     = ri
            self._bg[i * 3 + 1] = gi
            self._bg[i * 3 + 2] = bi

    # ------------------------------------------------------------------ #
    # Preset API                                                           #
    # ------------------------------------------------------------------ #

    def _retry_load_presets(self, delay=10.0):
        """Reintenta cargar presets indefinidamente hasta que WLED responda."""
        attempt = 0
        while True:
            time.sleep(delay)
            attempt += 1
            self.load_presets()
            if self.presets:
                print(f"WLED: presets cargados tras {attempt} reintento(s) ({len(self.presets)} presets)")
                if self._on_presets_loaded:
                    self._on_presets_loaded(self.presets)
                return
            print(f"WLED: intento {attempt} fallido, reintentando en {delay}s...")

    def load_presets(self):
        try:
            r = requests.get(f"{self._base}/presets.json", timeout=2.0)
            data = r.json()
            self._preset_data = {
                int(k): v for k, v in data.items()
                if k != "0" and v.get("n")
            }
            self.presets = {pid: v["n"] for pid, v in self._preset_data.items()}
        except Exception as e:
            print(f"WLED: no se pudieron cargar presets: {e}")

    def apply_preset(self, preset_id):
        self._current_preset = preset_id
        self._post_state({"ps": preset_id})

    # ------------------------------------------------------------------ #
    # Pulse API                                                            #
    # ------------------------------------------------------------------ #

    def trigger(self, seg_ids=None, velocity=1.0, color=(255, 255, 255),
                reverse=True):
        """Dispara un pulso (DRGB) o activa un preset WLED (modo preset)."""
        if not self._output_enabled:
            return
        if self._output_mode == "preset":
            # Flash instantáneo al máximo; el loop programa el fade de vuelta
            self._preset_restore_at = time.monotonic() + 0.15
            self._post_state({"bri": 255, "transition": 0})
            self._wake.set()
            return
        if seg_ids is None:
            seg_ids = list(range(len(self._seg_sizes)))

        offset = 0
        with self._lock:
            for i, size in enumerate(self._seg_sizes):
                if i in seg_ids:
                    active = sum(1 for p in self._pulses if p["start"] == offset)
                    if active >= 16:
                        offset += size
                        continue
                    self._pulses.append({
                        "start": offset,
                        "n": size,
                        "pos": 0.0,
                        "vel": self._pulse_velocity,
                        "bri": float(min(1.0, max(0.2, velocity))),
                        "color": color,
                        "tail": self._pulse_tail,
                        "reverse": reverse,
                    })
                offset += size
        self._wake.set()  # despierta el loop inmediatamente

    # ------------------------------------------------------------------ #
    # Loop interno                                                         #
    # ------------------------------------------------------------------ #

    def _loop(self):
        _max_dt = 1.0 / _FPS_ACTIVE  # techo para frame_dt: evita saltos al despertar
        last = time.monotonic()

        while True:
            # En modo preset WLED reproduce su efecto nativo: solo gestionamos el fade de bri
            if self._output_mode == "preset":
                restore_at = self._preset_restore_at
                if restore_at is not None:
                    now = time.monotonic()
                    if now >= restore_at:
                        self._preset_restore_at = None
                        self._post_state({"bri": self._preset_bri_idle, "transition": 15})
                        timeout = 1.0
                    else:
                        timeout = restore_at - now
                else:
                    timeout = 1.0
                self._wake.wait(timeout=timeout)
                self._wake.clear()
                last = time.monotonic()
                continue

            now = time.monotonic()
            dt = min(now - last, _max_dt)  # nunca mas de un frame activo de salto
            last = now

            with self._lock:
                for p in self._pulses:
                    p["pos"] += p["vel"] * dt
                self._pulses = [
                    p for p in self._pulses
                    if p["pos"] - p["tail"] < p["n"]
                ]
                active = list(self._pulses)

            self._pixels[:] = self._bg
            for p in active:
                self._paint(p)
            self._send()

            elapsed = time.monotonic() - now
            if active:
                time.sleep(max(0.0, _max_dt - elapsed))
            else:
                # Sin pulsos: espera hasta 0.5s o hasta que llegue un trigger
                self._wake.wait(timeout=1.0 / _FPS_IDLE)
                self._wake.clear()
                last = time.monotonic()  # resetea dt para no acumular el tiempo de espera

    def _paint(self, p):
        head = p["pos"]
        tail = p["tail"]
        bri = p["bri"]
        r0, g0, b0 = p["color"]
        start = p["start"]
        n = p["n"]
        rev = p["reverse"]

        i_from = max(0, int(head - tail) - 1)
        i_to = min(n - 1, int(head) + 1)

        for i in range(i_from, i_to + 1):
            dist = head - i
            if dist < 0 or dist > tail:
                continue
            fade = (1.0 - dist / tail) ** 1.5
            intensity = bri * fade
            phys = (n - 1 - i) if rev else i
            idx = (start + phys) * 3
            vr = int(r0 * intensity)
            vg = int(g0 * intensity)
            vb = int(b0 * intensity)
            if vr > self._pixels[idx]:     self._pixels[idx]     = vr
            if vg > self._pixels[idx + 1]: self._pixels[idx + 1] = vg
            if vb > self._pixels[idx + 2]: self._pixels[idx + 2] = vb

    def _send(self):
        if not self._output_enabled:
            return
        try:
            self._udp.sendto(self._header + bytes(self._pixels), self._udp_addr)
        except OSError:
            # Red no disponible aun (arrancando) — el loop sigue vivo y
            # reintentara en el siguiente frame cuando la red este lista.
            pass

    def _post_state(self, payload):
        try:
            requests.post(f"{self._base}/json/state", json=payload, timeout=0.5)
        except Exception as e:
            print(f"WLED: {e}")

    def stop(self):
        self._udp.close()
