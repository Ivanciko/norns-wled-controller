"""Entrada MIDI en tiempo real con reconexion automatica.

Escucha un puerto MIDI y entrega cada mensaje via callback. Si no hay ningun
dispositivo al arrancar (o se desconecta), sondea en background cada pocos
segundos hasta que aparezca uno.
"""
import threading
import time

import mido


class MidiInput:
    def __init__(self, on_message, port_name=None, poll_interval=3.0):
        self._on_message = on_message
        self._poll_interval = poll_interval
        self._port = None
        self.name = "ninguno"
        self._closed = False

        if port_name is not None:
            self._open(port_name)
        else:
            if not self._try_open_first():
                t = threading.Thread(target=self._poll_loop, daemon=True)
                t.start()

    def _try_open_first(self):
        try:
            self._open(self._find_port())
            return True
        except RuntimeError:
            return False

    def _open(self, port_name):
        self._port = mido.open_input(port_name, callback=self._callback)
        self.name = self._port.name
        print(f"MIDI: conectado a '{self.name}'")

    def _poll_loop(self):
        while not self._closed and self._port is None:
            time.sleep(self._poll_interval)
            self._try_open_first()

    @staticmethod
    def _find_port(exclude="Midi Through"):
        for name in mido.get_input_names():
            if exclude not in name:
                return name
        raise RuntimeError("No se encontro ningun puerto MIDI de entrada")

    def _callback(self, message):
        self._on_message(message)

    def close(self):
        self._closed = True
        if self._port is not None:
            self._port.close()
