"""Emisor del protocolo UDP "audio sync" nativo de WLED (usermod Audio
Reactive). Distinto de wled_animator.py: aquel manda pixeles ya calculados
(DRGB), esto manda *datos de audio* para que los efectos nativos de WLED
(Freqmatrix, GEQ, Gravimeter, etc. - incluidos los que vengan dentro de un
preset guardado) tengan a que reaccionar. El ESP32 del controlador WLED no
tiene microfono propio conectado, asi que sin esto "Audio Reactive" activado
en WLED no recibe ninguna senal.

Protocolo: struct "audioSyncPacket" v2 (44 bytes, packed) del usermod
audioreactive, header "00002". Se manda por UDP *multicast* a 239.0.0.1,
puerto 11988 por defecto - el contenido no necesita saber la IP de WLED,
pero la interfaz de salida si: si la Pi tiene mas de una red activa (p.ej.
wlan0 en la red del propio WLED-AP + eth0 con internet), el kernel puede
mandar el multicast por la interfaz por defecto (la de internet) en vez de
la que realmente llega a WLED, y el paquete se pierde en silencio. Por eso
el constructor recibe `host` (el wled_host de config.json) solo para
resolver, via el truco connect()+getsockname(), que interfaz local se usa
para llegar a el, y fijar esa como interfaz multicast (IP_MULTICAST_IF).

Requiere, una vez, en la web de WLED > Config Audio Reactive > Sync: modo
"Receive" (envia audioSyncEnabled=2 al JSON de esa pagina). Sin eso, WLED
ignora estos paquetes aunque lleguen bien.

Ademas del microfono, note_on() deja que las notas MIDI inyecten un golpe de
banda ancha (pensado para drum machines/percusion) con caida exponencial en
las llamadas a send() siguientes - asi las notas MIDI tambien mueven los
efectos audio-reactivos de WLED, no solo lo que capta el microfono.
"""
import socket
import struct
import threading

_MCAST_GRP = "239.0.0.1"
_DEFAULT_PORT = 11988
_HEADER = b"00002"
_STRUCT_FMT = "<6s2sffBB16sHff"  # == sizeof(audioSyncPacket) == 44 bytes
_NUM_BANDS = 16
_MIDI_DECAY = 0.80    # atenuacion del golpe MIDI por cada llamada a send() (~46/s -> ~<5% en ~320ms)
_MIDI_PEAK_FRAMES = 1  # llamadas a send() tras un note_on que reportan samplePeak=True


class WLEDAudioSync:
    """Manda el estado actual de audio (bandas FFT + pico) a WLED a razon de
    una llamada a send() por bloque de audio analizado (~46 fps con la
    config actual de audio_analysis.py). note_on() se llama aparte, desde el
    hilo de MIDI, y su golpe se combina (maximo) con el nivel de microfono
    en la siguiente llamada a send()."""

    def __init__(self, host, port=_DEFAULT_PORT, ttl=1):
        self._addr = (_MCAST_GRP, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        self._bind_multicast_if(host)
        self.enabled = True
        self._armed = True
        self._lock = threading.Lock()
        self._midi_level = [0.0] * _NUM_BANDS
        self._midi_peak_frames = 0

    def _bind_multicast_if(self, host):
        """Fija la interfaz de salida del multicast a la que el kernel usa
        para llegar a `host` (truco connect()+getsockname(), no manda nada
        de verdad - solo resuelve la ruta). Sin esto, con mas de una red
        activa en la Pi, el multicast puede salir por la interfaz
        equivocada y WLED nunca recibe nada, en silencio."""
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((host, 1))
            local_ip = probe.getsockname()[0]
            probe.close()
            self._sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip)
            )
        except OSError as e:
            print(f"WLEDAudioSync: no se pudo fijar interfaz multicast para {host}: {e}")

    def note_on(self, velocity):
        """Llamar desde el hilo MIDI en cada nota que deba reflejarse en el
        audio-sync hacia WLED. `velocity` normalizada 0-1."""
        velocity = max(0.0, min(1.0, velocity))
        with self._lock:
            self._midi_level = [max(lv, velocity) for lv in self._midi_level]
            self._midi_peak_frames = _MIDI_PEAK_FRAMES

    def send(self, levels, band_centers, gain=1.0, threshold=0.0):
        """`levels`: array de 16 bandas normalizadas 0-1 (audio_analysis.py).
        `band_centers`: frecuencia central en Hz de cada banda, mismo orden
        (AudioAnalyzer.band_centers). `gain`/`threshold`: los mismos
        audio_gain/audio_threshold ya usados para los pulsos DRGB, para que
        el "beat" que ve WLED coincida con el que ya dispara los pulsos."""
        if not self.enabled:
            return

        scaled = [max(0.0, min(1.0, lv * gain)) for lv in levels[:_NUM_BANDS]]

        with self._lock:
            midi_peak = self._midi_peak_frames > 0
            self._midi_peak_frames = max(0, self._midi_peak_frames - 1)
            combined = [max(a, m) for a, m in zip(scaled, self._midi_level)]
            self._midi_level = [m * _MIDI_DECAY for m in self._midi_level]

        fft_bytes = bytes(int(lv * 254) for lv in combined)

        avg = sum(combined) / len(combined)
        peak_level = max(combined)

        is_peak = midi_peak
        if threshold > 0:
            if peak_level >= threshold and self._armed:
                is_peak = True
                self._armed = False
            elif peak_level < threshold * 0.55:
                self._armed = True

        major_idx = max(range(len(combined)), key=combined.__getitem__)
        major_peak_hz = float(band_centers[major_idx])
        magnitude = min(254.0, peak_level * 255.0)

        packet = struct.pack(
            _STRUCT_FMT,
            _HEADER, b"",
            min(255.0, peak_level * 255.0), min(255.0, avg * 255.0),
            1 if is_peak else 0, 0,
            fft_bytes,
            0,
            magnitude, major_peak_hz,
        )
        try:
            self._sock.sendto(packet, self._addr)
        except OSError:
            pass

    def close(self):
        self._sock.close()
