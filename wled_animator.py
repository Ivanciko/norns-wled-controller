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

Un tercer modo, "meteor" (ver set_segment_meteor/trigger(style="meteor")),
NO usa el efecto nativo Meteor de WLED (fx:76) - se probo disparandolo via
HTTP (reiniciandolo en cada trigger) y no funciona por dos motivos
confirmados contra hardware real y el codigo fuente de WLED:
  1. Mientras haya CUALQUIER tira en modo "reactive" (o el propio keepalive
     de este loop), WLED se queda en modo "live" GLOBAL (confirmado via
     /json/info -> "live":true, "liveseg":-1 = todo el dispositivo, no solo
     un segmento) - eso suprime el render de CUALQUIER efecto nativo en
     CUALQUIER segmento, no solo el disparado.
  2. Incluso sin eso: los presets Meteor guardados usan la variante
     "clasica" (o3/check3=false) de mode_meteor() (WLED FX.cpp) - en esa
     variante la posicion del meteoro se calcula de `strip.now` (reloj
     interno de WLED), NO de un contador que markForReset() resetee. O sea
     que "reiniciar" el efecto no mueve el meteoro al principio, solo
     limpia el rastro de brillo un frame.
En su lugar, este modo re-implementa el algoritmo real de mode_meteor()
(rastro por pixel con decaimiento aleatorio, cabeza que barre el segmento,
color por posicion para paletas tipo arcoiris) directamente sobre nuestro
propio pulso DRGB - mismo aspecto visual, pero con el ciclo de vida que
queremos (nace en el trigger, viaja, se apaga), no el barrido infinito del
efecto nativo real.
"""
import colorsys
import random
import socket
import threading
import time

import requests

_DRGB_TYPE = 2
_DNRGB_TYPE = 4
_DRGB_TIMEOUT = 2   # segundos que WLED espera antes de retomar su modo
_MAX_CHUNK_LEDS = 480   # tope por paquete UDP para no superar la MTU (~1500B)
                        # y evitar fragmentacion IP - con >490 LEDs en un solo
                        # DRGB el paquete se fragmenta y el AP del ESP32 lo
                        # descarta casi siempre (confirmado 2026-08-01: con
                        # 550 LEDs activos el pulso dejaba de llegar salvo por
                        # azar; con DNRGB en trozos de 480 llega siempre)
_FPS_ACTIVE = 30    # fps mientras hay pulsos en movimiento
_FPS_IDLE = 2       # fps cuando no hay pulsos (solo keepalive)
_DEFAULT_VELOCITY = 150.0   # LEDs/segundo
_DEFAULT_TAIL = 30          # LEDs de cola
_DEFAULT_SPARKLE = 0        # 0-255, ver trigger()/_paint() - 0 = sin chispas (igual que antes)
_RETRY_DELAY = 10.0         # segundos entre reintentos de carga (presets/efectos)

_SPARKLE_MAX_PROB = 0.12   # prob. por pixel y por frame con sparkle=255 - calibrar en hardware

# Parametros del render "meteor" (puerto de mode_meteor() en WLED FX.cpp,
# variante clasica/no-smooth, la que usan los presets guardados por el
# usuario: sx=128/32/110, ix=128, pal=11 o 2, o3=false). Ver docstring.
_METEOR_DECAY_PROB = 0.5           # ~ (255-intensity)/255 con intensity=128
# Rango de scale8(v, 128+rand(127)) a "Cola" (pulse_tail) de referencia -
# ver _meteor_decay_range(), reutiliza el campo "Cola" ya existente en vez
# de fijar un largo de cola a ciegas por codigo (pedido tras verlo en
# hardware real, 2026-08-05: "la cola deberia ser mas corta").
_METEOR_DECAY_TAIL_REF = 30.0
_METEOR_DECAY_MIN, _METEOR_DECAY_MAX = 0.35, 0.99


class WLEDAnimator:
    """Gestiona presets/efectos (via HTTP) y animacion de pulsos (via UDP/DRGB)."""

    def __init__(self, host, seg_sizes=(150, 150, 250, 250), udp_port=21324,
                 on_presets_loaded=None):
        self._base = f"http://{host}"
        # Sesion HTTP persistente (keep-alive): sin esto, cada requests.post()
        # suelto abre y cierra su propia conexion TCP - con "Meteor triggered"
        # mandando 2 POSTs por nota, eso es un handshake TCP completo de mas
        # por cada uno. Reutilizar la conexion evita ese coste en triggers
        # seguidos (notas rapidas).
        self._session = requests.Session()
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
        self._pulse_sparkle = _DEFAULT_SPARKLE
        self._output_enabled = True
        self._brightness = 255       # brillo maestro WLED (0-255), ver propiedad brightness
        self._preset_data = {}       # datos completos de presets.json

        self.presets = {}
        self.effects = []
        self._pulses = []
        self._lock = threading.Lock()
        self._wake = threading.Event()  # despierta el loop al instante en cada trigger

        # Rastro persistente por pixel para el render "meteor" (0-255, ver
        # _decay_and_blend_meteor) - separado de `_bg`/`_pixels` porque
        # decae solo (aleatoriamente) frame a frame, independiente de si
        # hay algun pulso viajando ahora mismo.
        self._trail = bytearray(self._n_leds)
        self._meteor_segments = {}   # seg_id -> dict(start,n,multicolor,color,hue_lut)

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
    def pulse_sparkle(self):
        return self._pulse_sparkle

    @pulse_sparkle.setter
    def pulse_sparkle(self, value):
        self._pulse_sparkle = int(value)

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
            r = self._session.get(f"{self._base}/presets.json", timeout=2.0)
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
            r = self._session.get(f"{self._base}/json/eff", timeout=2.0)
            self.effects = list(r.json())
        except Exception as e:
            print(f"WLED: no se pudieron cargar efectos: {e}")

    def set_segment_effect(self, seg_id, fx, sx=None, ix=None, pal=None, color=None):
        """Aplica un efecto nativo WLED de forma continua al segmento
        `seg_id` (requiere que ese Segment exista en WLED con el mismo
        rango que la tira). `sx`=velocidad, `ix`=intensidad, `pal`=paleta
        (todos 0-255 salvo `pal`, indice de paleta WLED)."""
        seg = {"id": seg_id, "fx": int(fx)}
        if sx is not None:
            seg["sx"] = int(sx)
        if ix is not None:
            seg["ix"] = int(ix)
        if pal is not None:
            seg["pal"] = int(pal)
        if color is not None:
            seg["col"] = [list(color)]
        self._post_state({"seg": [seg]})

    def _segment_offset(self, seg_id):
        return sum(self._seg_sizes[:seg_id])

    def _rainbow_lut(self, n):
        """Tabla de color por posicion fisica (0..n-1), precalculada una vez
        por segmento - evita llamar a hsv_to_rgb() por pixel y por frame."""
        lut = []
        for i in range(n):
            r, g, b = colorsys.hsv_to_rgb(i / max(1, n), 1.0, 1.0)
            lut.append((int(r * 255), int(g * 255), int(b * 255)))
        return lut

    def _meteor_decay_range(self, tail):
        """Traduce el campo "Cola" (pulse_tail, 5-150) a un rango de
        decaimiento por frame - reutiliza el mismo control que ya existe en
        el menu en vez de fijar un largo de cola por codigo. `tail` mas
        bajo que la referencia (30) decae mas rapido (cola visualmente mas
        corta), mas alto decae mas lento (cola mas larga)."""
        scale = max(0.15, min(2.0, tail / _METEOR_DECAY_TAIL_REF))
        lo = max(0.05, min(0.9, _METEOR_DECAY_MIN * scale))
        hi = max(lo + 0.02, min(0.995, _METEOR_DECAY_MAX * scale))
        return lo, hi

    def set_segment_meteor(self, seg_id, enabled, multicolor=True, color=(255, 255, 255), tail=_DEFAULT_TAIL):
        """Activa/desactiva el render "meteor" (ver docstring del modulo)
        para el segmento `seg_id`. `multicolor`=True usa un color por
        posicion tipo arcoiris (como pal:11 en los presets guardados),
        False usa el color propio de la tira (como pal:2). `tail` controla
        el largo de la cola (ver _meteor_decay_range)."""
        if not enabled:
            self._meteor_segments.pop(seg_id, None)
            return
        n = self._seg_sizes[seg_id]
        decay_lo, decay_hi = self._meteor_decay_range(tail)
        self._meteor_segments[seg_id] = {
            "start": self._segment_offset(seg_id),
            "n": n,
            "multicolor": multicolor,
            "color": tuple(color),
            "decay_lo": decay_lo,
            "decay_hi": decay_hi,
            "hue_lut": self._rainbow_lut(n) if multicolor else None,
        }

    # ------------------------------------------------------------------ #
    # Pulse API                                                            #
    # ------------------------------------------------------------------ #

    def trigger(self, seg_ids=None, velocity=1.0, color=(255, 255, 255),
                reverse=True, pulse_velocity=None, pulse_tail=None, pulse_sparkle=None,
                style="classic"):
        """Dispara un pulso DRGB reactivo. `pulse_velocity`/`pulse_tail`/
        `pulse_sparkle` sobrescriben, solo para este disparo, los valores por
        defecto de la instancia (permite que cada tira tenga los suyos).
        `style="meteor"` usa el render de set_segment_meteor en vez del fade
        clasico - `pulse_tail`/`pulse_sparkle` no aplican en ese caso (el
        rastro lo gestiona el decaimiento aleatorio, no un largo fijo)."""
        if not self._output_enabled:
            return
        if seg_ids is None:
            seg_ids = list(range(len(self._seg_sizes)))

        vel = self._pulse_velocity if pulse_velocity is None else float(pulse_velocity)
        tail = self._pulse_tail if pulse_tail is None else int(pulse_tail)
        sparkle = self._pulse_sparkle if pulse_sparkle is None else int(pulse_sparkle)

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
                        "sparkle": sparkle,
                        "reverse": reverse,
                        "style": style,
                    })
                offset += size
        self._wake.set()  # despierta el loop inmediatamente

    # ------------------------------------------------------------------ #
    # Loop interno                                                         #
    # ------------------------------------------------------------------ #

    def _pulse_alive(self, p):
        if p["style"] == "meteor":
            # Sin "tail" propio - el rastro vive en self._trail y decae solo
            # (ver _decay_and_blend_meteor). El pulso en si solo representa
            # la cabeza; muere al salir del segmento.
            return p["pos"] < p["n"] + (1 + p["n"] // 20)
        return p["pos"] - p["tail"] < p["n"]

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
                self._pulses = [p for p in self._pulses if self._pulse_alive(p)]
                active = list(self._pulses)

            self._pixels[:] = self._bg
            for p in active:
                if p["style"] == "meteor":
                    self._mark_meteor_head(p)
                else:
                    self._paint(p)
            meteor_active = self._decay_and_blend_meteor()
            self._send()

            elapsed = time.monotonic() - now
            if active or meteor_active:
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
        # Prob. de chispa por pixel, recalculada al azar cada frame - no se
        # guarda en `p`, asi que una posicion que destella en este frame no
        # deja estela propia: en el siguiente vuelve a valer solo el fade
        # normal (o vuelve a tener suerte). Con sparkle=0 esto es 0 y el
        # bucle de abajo es identico byte a byte al comportamiento de antes
        # (ni siquiera se llama a random).
        sparkle_prob = (p["sparkle"] / 255.0) * _SPARKLE_MAX_PROB

        i_from = max(0, int(head - tail) - 1)
        i_to = min(n - 1, int(head) + 1)

        for i in range(i_from, i_to + 1):
            dist = head - i
            if dist < 0 or dist > tail:
                continue
            if sparkle_prob and random.random() < sparkle_prob:
                # Chispa: color propio de la tira a brillo pleno (como la
                # cabeza del pulso), no atenuado por el fade de esta
                # posicion - destello que corta la cola.
                vr = int(r0 * bri)
                vg = int(g0 * bri)
                vb = int(b0 * bri)
            else:
                fade = (1.0 - dist / tail) ** 1.5
                intensity = bri * fade
                vr = int(r0 * intensity)
                vg = int(g0 * intensity)
                vb = int(b0 * intensity)
            phys = (n - 1 - i) if rev else i
            idx = (start + phys) * 3
            if vr > self._pixels[idx]:     self._pixels[idx]     = vr
            if vg > self._pixels[idx + 1]: self._pixels[idx + 1] = vg
            if vb > self._pixels[idx + 2]: self._pixels[idx + 2] = vb

    def _mark_meteor_head(self, p):
        """Enciende a brillo pleno los pixeles de la "cabeza" del meteoro en
        self._trail (mismo tamano que mode_meteor(): ~5% del segmento,
        `1 + n/20`) - el decaimiento del rastro lo hace por separado
        _decay_and_blend_meteor(), una vez por frame, no por pulso.

        Pinta todo el tramo recorrido desde el ultimo frame (no solo la
        posicion actual): `pos` ya avanzo (vel*dt) antes de llegar aqui, asi
        que a velocidades altas o justo al arrancar (dt puede llegar a ser
        un frame entero de golpe) la cabeza puede saltarse varios LEDs de
        un frame a otro - si solo pintaramos la banda en `pos`, esos LEDs
        saltados (incluidos los primeros del inicio, si el primer frame ya
        salto hacia adelante) nunca se encenderian."""
        n = p["n"]
        size = 1 + n // 20
        start = p["start"]
        head = int(p["pos"])
        rev = p["reverse"]
        lo = max(0, p.get("_head_mark", 0))
        hi = min(n - 1, head + size - 1)
        for i in range(lo, hi + 1):
            phys = (n - 1 - i) if rev else i
            self._trail[start + phys] = 255
        p["_head_mark"] = head + 1

    def _decay_and_blend_meteor(self):
        """Decae aleatoriamente el rastro de cada tira en modo "meteor"
        (puerto de `trail[i] = scale8(trail[i], 128+random8(127))` con
        ~50% de probabilidad por pixel y por frame, ver mode_meteor() en
        WLED FX.cpp) y lo mezcla (max-blend, igual que los pulsos clasicos)
        en self._pixels. Devuelve True si queda algun pixel con brillo
        (para que el loop sepa que aun tiene que ir a 30fps aunque no haya
        ningun pulso "cabeza" viajando - el rastro sigue apagandose solo)."""
        any_active = False
        for seg in self._meteor_segments.values():
            start, n = seg["start"], seg["n"]
            lut, color = seg["hue_lut"], seg["color"]
            decay_lo, decay_hi = seg["decay_lo"], seg["decay_hi"]
            for i in range(start, start + n):
                v = self._trail[i]
                if not v:
                    continue
                if random.random() < _METEOR_DECAY_PROB:
                    v = int(v * random.uniform(decay_lo, decay_hi))
                    self._trail[i] = v
                if not v:
                    continue
                any_active = True
                r0, g0, b0 = lut[i - start] if lut else color
                idx = i * 3
                vr = int(r0 * v / 255)
                vg = int(g0 * v / 255)
                vb = int(b0 * v / 255)
                if vr > self._pixels[idx]:     self._pixels[idx]     = vr
                if vg > self._pixels[idx + 1]: self._pixels[idx + 1] = vg
                if vb > self._pixels[idx + 2]: self._pixels[idx + 2] = vb
        return any_active

    def _send(self):
        if not self._output_enabled:
            return
        try:
            if self._n_leds <= _MAX_CHUNK_LEDS:
                self._udp.sendto(self._header + bytes(self._pixels), self._udp_addr)
            else:
                # DNRGB en trozos: un solo DRGB con >490 LEDs se fragmenta a
                # nivel IP y el ESP32 en modo AP lo pierde casi siempre.
                for start in range(0, self._n_leds, _MAX_CHUNK_LEDS):
                    end = min(start + _MAX_CHUNK_LEDS, self._n_leds)
                    chunk_header = bytes([_DNRGB_TYPE, _DRGB_TIMEOUT]) + start.to_bytes(2, "big")
                    self._udp.sendto(chunk_header + bytes(self._pixels[start * 3:end * 3]), self._udp_addr)
        except OSError:
            # Red no disponible aun (arrancando) — el loop sigue vivo y
            # reintentara en el siguiente frame cuando la red este lista.
            pass

    def _post_state(self, payload):
        try:
            self._session.post(f"{self._base}/json/state", json=payload, timeout=0.5)
        except Exception as e:
            print(f"WLED: {e}")

    def stop(self):
        self._udp.close()
