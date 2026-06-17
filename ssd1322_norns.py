"""Driver SSD1322 (128x64) para el OLED del norns shield, vía SPI + GPIO crudos.

Replica la secuencia de init/refresh del firmware oficial de norns
(matron/src/hardware/screen/ssd1322.cc), que es la que realmente
funciona con este panel (NHD-2.7-12864WDW3). El driver generico
luma.oled.device.ssd1322 NO funciona con este panel (defaults distintos).

Expone una interfaz compatible con luma.core.render.canvas:
  - .mode, .size, .bounding_box
  - .display(image)
  - .contrast(value)
"""
import time

import lgpio
import spidev


class ssd1322_norns:
    width = 128
    height = 64

    def __init__(self, spi_port=0, spi_device=0, gpio_chip=0,
                 dc_line=5, reset_line=6, is_shield=True):
        self.size = (self.width, self.height)
        self.mode = "L"
        self.bounding_box = (0, 0, self.width - 1, self.height - 1)
        self._should_turn_on = True

        self._spi = spidev.SpiDev()
        self._spi.open(spi_port, spi_device)
        self._spi.mode = 0
        self._spi.bits_per_word = 8
        self._spi.max_speed_hz = 18750000  # 1200MHz / 64, igual que norns
        self._spi.lsbfirst = False

        self._dc = dc_line
        self._reset = reset_line
        self._h = lgpio.gpiochip_open(gpio_chip)
        lgpio.gpio_claim_output(self._h, self._dc, 0)
        lgpio.gpio_claim_output(self._h, self._reset, 1)

        self._init(is_shield)

    def _cmd(self, command, data=None):
        lgpio.gpio_write(self._h, self._dc, 0)
        self._spi.writebytes([command])
        if data:
            lgpio.gpio_write(self._h, self._dc, 1)
            self._spi.writebytes(list(data))

    def _init(self, is_shield):
        lgpio.gpio_write(self._h, self._reset, 0)
        time.sleep(0.01)
        lgpio.gpio_write(self._h, self._reset, 1)
        time.sleep(0.01)

        self._cmd(0xAE)               # display off
        self._cmd(0xB9)                # default linear gray scale
        self._cmd(0xB3, [0x91])         # oscillator frequency
        self._cmd(0xCA, [0x3F])         # multiplex ratio
        self._cmd(0xA2, [0x00])         # display offset
        self._cmd(0xA1, [0x00])         # display start line
        self._cmd(0xAB, [0x01])         # VDD regulator
        self._cmd(0xB4, [0xA0, 0xFD])   # display enhancement A
        self._cmd(0xC1, [0x7F])         # contrast current
        self._cmd(0xC7, [0x0F])         # master current control
        self._cmd(0xB1, [0xF2])         # phase length
        self._cmd(0xBB, [0x1F])         # precharge voltage
        self._cmd(0xBE, [0x04])         # VCOMH voltage
        self._cmd(0xA6)                 # normal display mode

        # set remap / dual-COM line mode: 0x04,0x11 para norns shield
        # (la unidad "norns" oficial usaria 0x16,0x11)
        remap = [0x04, 0x11] if is_shield else [0x16, 0x11]
        self._cmd(0xA0, remap)

    def contrast(self, value):
        self._cmd(0xC1, [value & 0xFF])

    def display(self, image):
        if image.mode != "L":
            image = image.convert("L")
        if image.size != self.size:
            image = image.resize(self.size)

        data = bytearray(self.width * self.height)
        for i, v in enumerate(image.getdata()):
            # SSD1322 espera 4 bits de gris por pixel, duplicados en
            # ambos nibbles del byte (igual que hace norns con NEON).
            data[i] = (v & 0xF0) | (v >> 4)

        self._cmd(0x15, [28, 91])  # ventana de columnas (offset 28!)
        self._cmd(0x75, [0, 63])   # ventana de filas
        self._cmd(0x5C)            # write RAM

        lgpio.gpio_write(self._h, self._dc, 1)
        chunk = 4096
        for i in range(0, len(data), chunk):
            self._spi.writebytes(list(data[i:i + chunk]))

        if self._should_turn_on:
            self._cmd(0xAF)  # display on (solo tras el primer frame)
            self._should_turn_on = False

    def cleanup(self):
        self._cmd(0xAE)  # display off (sin forzar reset de hardware)
        self._spi.close()
        lgpio.gpiochip_close(self._h)
