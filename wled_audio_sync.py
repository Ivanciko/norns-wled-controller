"""Emisor del protocolo UDP "audio sync" nativo de WLED (usermod Audio
Reactive). Distinto de wled_animator.py: aquel manda pixeles ya calculados
(DRGB), esto manda *datos de audio* para que los efectos nativos de WLED
(Freqmatrix, GEQ, Gravimeter, etc. - incluidos los que vengan dentro de un
preset guardado) tengan a que reaccionar. El ESP32 del controlador WLED no
tiene microfono propio conectado, asi que sin esto "Audio Reactive" activado
en WLED no recibe ninguna senal.

Protocolo: struct "audioSyncPacket" v2 (44 bytes, packed) del usermod
audioreactive, header "00002". Se manda por UDP **unicast** directo a
`host`:11988 (no a 239.0.0.1 multicast, que probo ser poco fiable/roto en
este setup: en modo WLED-AP el ESP32 hace de AP, y el multicast enviado
desde una estacion hacia el propio AP no llegaba de forma fiable pese a que
el socket local aceptaba el sendto() sin error - confirmado el 2026-08-01
mandando el mismo paquete por unicast a 4.3.2.1:11988 y viendo a WLED
validarlo al instante. El unicast ademas evita el bug de IP_MULTICAST_IF
quedandose "pegado" a una interfaz invalida cuando la Pi tiene mas de una
red activa a la vez (eth0 + wlan0) - aqui no hace falta fijar interfaz de
salida, el kernel ya sabe por donde llegar a un host unicast conocido.

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

    def __init__(self, host, port=_DEFAULT_PORT):
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.enabled = True
        self._armed = True
        self._lock = threading.Lock()
        self._midi_level = [0.0] * _NUM_BANDS
        self._midi_peak_frames = 0

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
