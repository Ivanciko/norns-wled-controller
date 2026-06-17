#!/usr/bin/env python3
"""
controls.py - Lectura de los 3 encoders y 3 botones del norns shield (Fase 4)

Pinout confirmado en hardware real (ver memoria del proyecto):
  Encoder 1: A1=GPIO4,  B1=GPIO27
  Encoder 2: A2=GPIO24, B2=GPIO23
  Encoder 3: A3=GPIO12, B3=GPIO25
  Boton K1=GPIO22, K2=GPIO26, K3=GPIO13

Todos los pines usan pull-up interno (reposo=1, activo=0).

Las alertas/callbacks de lgpio no funcionan en este sistema (probado y
confirmado), asi que esta clase usa un hilo de sondeo (polling) en segundo
plano, leyendo los 9 pines a intervalos muy cortos.
"""

import threading
import time

import lgpio

ENCODERS = {
    1: {"a": 4, "b": 27},
    2: {"a": 24, "b": 23},
    3: {"a": 12, "b": 25},
}

BUTTONS = {
    1: 22,
    2: 26,
    3: 13,
}

# Tabla de cuadratura: combina (estado_anterior << 2) | estado_nuevo -> delta
# estado = (A << 1) | B. Solo transiciones de codigo Gray (1 bit cambia) son validas.
_QUAD_TABLE = {
    0b0001: +1, 0b0111: +1, 0b1110: +1, 0b1000: +1,
    0b0010: -1, 0b1011: -1, 0b1101: -1, 0b0100: -1,
}


class Controls:
    def __init__(self, on_encoder=None, on_key=None, debounce_s=0.005, poll_interval=0.0005):
        """
        on_encoder(n, delta): se llama cuando el encoder n (1..3) gira.
            delta es +1 o -1 por cada paso de cuadratura (4 pasos = 1 "click" tipico).
        on_key(n, pressed): se llama cuando el boton n (1..3) cambia de estado.
            pressed=True al pulsar, False al soltar.
        """
        self.on_encoder = on_encoder
        self.on_key = on_key
        self.debounce_s = debounce_s
        self.poll_interval = poll_interval
        self.h = lgpio.gpiochip_open(0)

        self._enc_state = {}
        for n, pins in ENCODERS.items():
            for pin in (pins["a"], pins["b"]):
                lgpio.gpio_claim_input(self.h, pin, lgpio.SET_PULL_UP)
            a = lgpio.gpio_read(self.h, pins["a"])
            b = lgpio.gpio_read(self.h, pins["b"])
            self._enc_state[n] = (a << 1) | b

        self._key_state = {}
        self._key_last_change = {}
        for n, pin in BUTTONS.items():
            lgpio.gpio_claim_input(self.h, pin, lgpio.SET_PULL_UP)
            self._key_state[n] = lgpio.gpio_read(self.h, pin)
            self._key_last_change[n] = 0.0

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        while self._running:
            for n, pins in ENCODERS.items():
                a = lgpio.gpio_read(self.h, pins["a"])
                b = lgpio.gpio_read(self.h, pins["b"])
                new_state = (a << 1) | b
                old_state = self._enc_state[n]
                if new_state != old_state:
                    transition = (old_state << 2) | new_state
                    delta = _QUAD_TABLE.get(transition)
                    self._enc_state[n] = new_state
                    if delta and self.on_encoder:
                        self.on_encoder(n, delta)

            now = time.monotonic()
            for n, pin in BUTTONS.items():
                level = lgpio.gpio_read(self.h, pin)
                if level != self._key_state[n] and (now - self._key_last_change[n]) >= self.debounce_s:
                    self._key_state[n] = level
                    self._key_last_change[n] = now
                    if self.on_key:
                        self.on_key(n, level == 0)  # 0 = pulsado (pull-up)

            time.sleep(self.poll_interval)

    def close(self):
        self._running = False
        self._thread.join(timeout=1)
        lgpio.gpiochip_close(self.h)
