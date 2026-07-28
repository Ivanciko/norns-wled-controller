"""Animacion de pulsos sobre WLED via UDP (protocolo DRGB), mas control de
presets y efectos nativos WLED via HTTP JSON.

Cada llamada a trigger() lanza un pulso nuevo e independiente: sale del
extremo del segmento y fluye hasta el otro extremo a 30 fps. Si llegan
varios triggers seguidos, varios pulsos viajan a la vez, cada uno en
su propia posicion. Velocidad y cola del pulso son por defecto las de la
instancia, pero cada llamada puede pasar las suyas (para que cada tira
tenga las suyas propias).

WLED recibe los datos de pixel en crudo via UDP (DRGB, puerto 21324) y
entra en modo live. Cuando no hay pulsos, se envia un frame de mantenimiento
a 2fps (solo para que WLED no salga de live mode) con el color ambiente.

Ademas de DRGB, una tira puede configurarse en modo "efecto nativo WLED"
(ver set_segment_effect): en ese caso el pulso reactivo no se usa para esa
tira y en su lugar corre un efecto WLED continuo sobre su segmento nativo
(requiere que WLED tenga definidos Segments que coincidan con los rangos de
cada tira, ademas de los Outputs fisicos).
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
_RETRY_DELAY = 10.0         # segundos entre reintentos de carga (presets/efectos)


class WLEDAnimator:
    """Gestiona presets/efectos (via HTTP) y animacion de pulsos (via UDP/DRGB)."""

    def __init__(self, host, seg_sizes=(150, 150, 250, 250), udp_port=21324,
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

        self._seg_ambient = {}  # seg_id -> (bri_floor:int, color:tuple), ver set_segment_ambient
        self._pulse_velocity = _DEFAULT_VELOCITY
        self._pulse_tail = _DEFAULT_TAIL
        self._output_enabled = True
        self._brightness = 255       # brillo maestro WLED (0-255), ver propiedad brightness
        self._preset_data = {}       # datos completos de presets.json

        self.presets = {}
        self.effects = []
        self._pulses = []
        self._lock = threading.Lock()
        self._wake = threading.Event()  # despierta el loop al instante en cada trigger

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        # Intentos iniciales; si fallan (controlador aun arrancando), reintenta en background.
        # Si los presets ya estan disponibles de inmediato, el llamador (fuera de esta clase)
        # es responsable de consultar `self.presets` tras construir el objeto - no invocamos
        # on_presets_loaded aqui de forma sincrona porque en ese momento la variable que
        # referencia a esta misma instancia (en el codigo del llamador) puede no existir aun.
        self.load_presets()
        if not self.presets:
            threading.Thread(
                target=self._retry_until_loaded,
                args=(self.load_presets, lambda: bool(self.presets), "presets"),
                daemon=True,
            ).start()

        self.load_effects()
        if not self.effects:
            threading.Thread(
                target=self._retry_until_loaded,
                args=(self.load_effects, lambda: bool(self.effects), "efectos"),
                daemon=True,
            ).start()

    # ------------------------------------------------------------------ #
    # Propiedades configurables desde fuera                                #
    # ------------------------------------------------------------------ #

    def set_segment_ambient(self, seg_id, bri_floor, color):
        """Brillo/color ambiente (entre pulsos) de una tira concreta.
        0=apagado entre pulsos, >0=glow tenue con `color`."""
        self._seg_ambient[seg_id] = (int(bri_floor), tuple(color))
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
    def brightness(self):
        """Brillo maestro de WLED (0-255) - escala por encima de cada pixel
        que mandamos por DRGB, incluidos pulsos y ambiente. Distinto del
        `bri_floor` por tira (ese es solo el nivel del glow ambiente que
        nosotros calculamos por pixel)."""
        return self._brightness

    @brightness.setter
    def brightness(self, value):
        self._brightness = int(value)
        self._post_state({"bri": self._brightness})

    def _update_bg(self):
        """Recalcula el buffer de fondo cuando cambia el ambiente de alguna
        tira. Cada segmento usa su propio floor/color (0,(0,0,0) si nunca
        se configuro con set_segment_ambient)."""
        offset = 0
        for seg_id, size in enumerate(self._seg_sizes):
            floor, color = self._seg_ambient.get(seg_id, (0, (0, 0, 0)))
            r0, g0, b0 = color
            ri = int(r0 * floor / 255)
            gi = int(g0 * floor / 255)
            bi = int(b0 * floor / 255)
            for j in range(size):
                idx = (offset + j) * 3
                self._bg[idx] = ri
                self._bg[idx + 1] = gi
                self._bg[idx + 2] = bi
            offset += size

    # ------------------------------------------------------------------ #
    # Carga de presets / efectos (whole-device)                            #
    # ------------------------------------------------------------------ #

    def _retry_until_loaded(self, loader, has_result, label, delay=_RETRY_DELAY):
        """Reintenta `loader` indefinidamente hasta que `has_result()` sea
        verdadero. Usado tanto para presets como para efectos."""
        attempt = 0
        while True:
            time.sleep(delay)
            attempt += 1
            loader()
            if has_result():
                print(f"WLED: {label} cargados tras {attempt} reintento(s)")
                if label == "presets" and self._on_presets_loaded:
                    self._on_presets_loaded(self.presets)
                return
            print(f"WLED: {label} - intento {attempt} fallido, reintentando en {delay}s...")

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

    def load_effects(self):
        try:
            r = requests.get(f"{self._base}/json/eff", timeout=2.0)
            self.effects = list(r.json())
        except Exception as e:
            print(f"WLED: no se pudieron cargar efectos: {e}")

    def set_segment_effect(self, seg_id, fx, sx=None, color=None):
        """Aplica un efecto nativo WLED de forma continua al segmento
        `seg_id` (requiere que ese Segment exista en WLED con el mismo
        rango que la tira). `sx` es la velocidad del efecto (0-255)."""
        seg = {"id": seg_id, "fx": int(fx)}
        if sx is not None:
            seg["sx"] = int(sx)
        if color is not None:
            seg["col"] = [list(color)]
        self._post_state({"seg": [seg]})

    # ------------------------------------------------------------------ #
    # Pulse API                                                            #
    # ------------------------------------------------------------------ #

    def trigger(self, seg_ids=None, velocity=1.0, color=(255, 255, 255),
                reverse=True, pulse_velocity=None, pulse_tail=None):
        """Dispara un pulso DRGB reactivo. `pulse_velocity`/`pulse_tail`
        sobrescriben, solo para este disparo, los valores por defecto de la
        instancia (permite que cada tira tenga su propia velocidad/cola)."""
        if not self._output_enabled:
            return
        if seg_ids is None:
            seg_ids = list(range(len(self._seg_sizes)))

        vel = self._pulse_velocity if pulse_velocity is None else float(pulse_velocity)
        tail = self._pulse_tail if pulse_tail is None else int(pulse_tail)

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
                        "vel": vel,
                        "bri": float(min(1.0, max(0.2, velocity))),
                        "color": color,
                        "tail": tail,
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
