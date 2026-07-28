"""Entrada MIDI en tiempo real con reconexion automatica, multi-dispositivo.

Escucha TODOS los puertos MIDI disponibles a la vez (excepto "Midi Through")
y entrega cada mensaje junto con el nombre corto de su dispositivo de
origen - util para tener varios controladores conectados (p.ej. un Elektron
Digitakt y un Teensy), cada uno enrutado a su propia tira via el filtro de
dispositivo y/o canal MIDI por tira.

Si algun dispositivo no esta conectado al arrancar (o se enchufa mas tarde),
se sondea en background cada pocos segundos y se abre en cuanto aparece,
sin tocar los que ya estaban abiertos.
"""
import threading
import time

import mido


class MidiInput:
    def __init__(self, on_message, exclude="Midi Through", poll_interval=3.0):
        """`on_message(msg, device_name)` se llama por cada mensaje, con el
        nombre corto (antes de los ':') del dispositivo que lo envio."""
        self._on_message = on_message
        self._exclude = exclude
        self._poll_interval = poll_interval
        self._ports = {}  # nombre completo mido -> puerto abierto
        self._lock = threading.Lock()
        self._closed = False

        self._open_new_ports()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    @staticmethod
    def _short_name(full_name):
        return full_name.split(":")[0]

    @property
    def name(self):
        """Nombres cortos de todos los dispositivos conectados ahora mismo,
        unidos por ' + ', o 'ninguno' si no hay ninguno."""
        names = self.device_names
        return " + ".join(names) if names else "ninguno"

    @property
    def device_names(self):
        """Nombres cortos (sin duplicar) de los dispositivos conectados
        ahora mismo, ordenados - para poblar el selector de "Dispositivo
        MIDI" por tira en la UI."""
        with self._lock:
            full_names = list(self._ports)
        return sorted({self._short_name(n) for n in full_names})

    def _open_new_ports(self):
        try:
            available = mido.get_input_names()
        except Exception as e:
            print(f"MIDI: error listando puertos: {e}")
            return

        for name in available:
            if self._exclude in name:
                continue
            with self._lock:
                if name in self._ports:
                    continue
            short = self._short_name(name)
            try:
                port = mido.open_input(name, callback=lambda msg, s=short: self._callback(msg, s))
            except (OSError, IOError) as e:
                print(f"MIDI: no se pudo abrir '{name}': {e}")
                continue
            with self._lock:
                self._ports[name] = port
            print(f"MIDI: conectado a '{name}'")

    def _poll_loop(self):
        while not self._closed:
            time.sleep(self._poll_interval)
            if not self._closed:
                self._open_new_ports()

    def _callback(self, message, device_name):
        self._on_message(message, device_name)

    def close(self):
        self._closed = True
        with self._lock:
            ports, self._ports = self._ports, {}
        for port in ports.values():
            port.close()
