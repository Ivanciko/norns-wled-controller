"""Captura de audio en tiempo real + analisis FFT por bandas.

Abre el codec CS4270 del norns shield directamente via ALSA (pyalsaaudio),
sin depender de PortAudio. El dispositivo se busca por nombre de card para
funcionar independientemente del indice de card asignado en cada arranque.

Modo full-duplex manual: hilo de captura lee bloques, hace FFT y llama al
callback, y a la vez escribe la entrada escalada por `volume` a la salida
(passthrough para monitorizar audio in por J-OUT).
"""
import threading

import alsaaudio
import numpy as np


def _find_alsa_device(card_name="nornsshieldcs42", plugin="plughw"):
    """Devuelve 'plughw:N,0' para el card cuyo nombre contiene card_name."""
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0].isdigit() and card_name.lower() in line.lower():
                    return f"{plugin}:{parts[0]},0"
    except OSError:
        pass
    return f"{plugin}:{card_name},0"


class AudioAnalyzer:
    def __init__(self, on_levels, n_bands=8, samplerate=48000,
                 blocksize=1024, device=None,
                 fmin=40.0, fmax=16000.0, smoothing=0.6,
                 range_db=40.0, calib_blocks=20, volume=0.0):
        self._on_levels = on_levels
        self._n_bands = n_bands
        self._smoothing = smoothing
        self._range_db = range_db
        self.volume = volume
        self._levels = np.zeros(n_bands)
        self._window = np.hanning(blocksize)
        self._blocksize = blocksize
        self._running = True

        self._calib_total = calib_blocks
        self._calib_left = calib_blocks
        self._floor = np.zeros(n_bands)

        freqs = np.fft.rfftfreq(blocksize, d=1.0 / samplerate)
        edges = np.geomspace(fmin, fmax, n_bands + 1)
        self._bin_idx = []
        for i in range(n_bands):
            lo, hi = edges[i], edges[i + 1]
            idx = np.where((freqs >= lo) & (freqs < hi))[0]
            if len(idx) == 0:
                idx = np.array([np.argmin(np.abs(freqs - lo))])
            self._bin_idx.append(idx)

        alsa_dev = device or _find_alsa_device()
        print(f"Audio: abriendo {alsa_dev}")

        fmt = alsaaudio.PCM_FORMAT_FLOAT_LE
        self._pcm_in = alsaaudio.PCM(
            alsaaudio.PCM_CAPTURE, alsaaudio.PCM_NORMAL,
            channels=2, rate=samplerate, format=fmt,
            periodsize=blocksize, device=alsa_dev,
        )
        self._pcm_out = alsaaudio.PCM(
            alsaaudio.PCM_PLAYBACK, alsaaudio.PCM_NORMAL,
            channels=2, rate=samplerate, format=fmt,
            periodsize=blocksize, device=alsa_dev,
        )

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            length, data = self._pcm_in.read()
            if length <= 0:
                continue

            indata = np.frombuffer(data, dtype="float32").reshape(-1, 2)

            # Passthrough: entrada escalada por volume -> salida
            out = np.clip(indata * self.volume, -1.0, 1.0)
            self._pcm_out.write(out.astype("float32").tobytes())

            # FFT por bandas
            mono = indata.mean(axis=1)
            mono = mono - mono.mean()
            spec = np.abs(np.fft.rfft(mono * self._window))
            raw = np.array([spec[idx].mean() for idx in self._bin_idx])

            if self._calib_left > 0:
                self._floor += raw
                self._calib_left -= 1
                if self._calib_left == 0:
                    self._floor /= self._calib_total
                continue

            rel_db = 20 * np.log10((raw + 1e-6) / (self._floor + 1e-6))
            norm = np.clip(rel_db / self._range_db, 0.0, 1.0)

            a = self._smoothing
            self._levels = a * self._levels + (1 - a) * norm
            self._on_levels(self._levels)

    def close(self):
        self._running = False
        self._thread.join(timeout=1.0)
        self._pcm_in.close()
        self._pcm_out.close()
