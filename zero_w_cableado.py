#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zero_w.py — Pi Zero W
  - Lee DHT22 cada SENSOR_INTERVAL_S segundos
  - Controla ventiladores: ciclo ACH base + override AND por temp+hum
  - Envía cada lectura al servidor (Pi 3B+) por HTTP
  - El override queda "anclado" al sensor que lo disparó hasta que
    AMBAS condiciones bajen de sus umbrales (con histéresis)

Dependencias:
  sudo apt-get install -y python3-pip pigpio
  sudo systemctl enable pigpiod && sudo systemctl start pigpiod
  pip3 install pigpio RPi.GPIO requests

  La librería pigpio es más confiable que adafruit-circuitpython-dht
  en el Pi Zero W (evita el error 'board has no attribute D4').
"""

import time
import threading
import signal
import requests
import pigpio

import RPi.GPIO as GPIO

# ── Lectura DHT22 via pigpio ───────────────────────────────────────────────
class DHT22Pigpio:
    """
    Lee el DHT22 usando pigpio, que accede al pin por número BCM directo
    sin depender del módulo 'board' (que falla en Pi Zero W).
    Requiere que el demonio pigpiod esté corriendo:
      sudo systemctl start pigpiod
    """
    def __init__(self, pi: pigpio.pi, gpio_pin: int):
        self._pi   = pi
        self._pin  = gpio_pin
        self._high = 0
        self._tick = 0
        self._bits = []
        self._temp = None
        self._hum  = None
        self._lock = threading.Lock()

        self._cb = pi.callback(gpio_pin, pigpio.EITHER_EDGE, self._pulso)
        self._disparar()

    def _disparar(self):
        """Envía el pulso de inicio al sensor."""
        self._bits = []
        self._pi.set_mode(self._pin, pigpio.OUTPUT)
        self._pi.write(self._pin, pigpio.LOW)
        time.sleep(0.002)          # 2 ms LOW
        self._pi.set_mode(self._pin, pigpio.INPUT)

    def _pulso(self, gpio, level, tick):
        if level == pigpio.HIGH:
            self._high = tick
        else:
            if self._high:
                duracion = pigpio.tickDiff(self._high, tick)
                self._bits.append(1 if duracion > 50 else 0)
                if len(self._bits) == 40:
                    self._decodificar()

    def _decodificar(self):
        bits = self._bits
        def to_int(b): return int(''.join(str(x) for x in b), 2)
        hum_int  = to_int(bits[0:8])
        hum_dec  = to_int(bits[8:16])
        temp_int = to_int(bits[16:24])
        temp_dec = to_int(bits[24:32])
        checksum = to_int(bits[32:40])
        total = (hum_int + hum_dec + temp_int + temp_dec) & 0xFF
        if total == checksum:
            hum  = hum_int  + hum_dec  / 10.0
            temp = temp_int + temp_dec / 10.0
            if bits[16]:   # bit de signo temperatura
                temp = -temp
            with self._lock:
                self._temp = round(temp, 1)
                self._hum  = round(hum,  1)

    def leer(self) -> tuple:
        """Dispara una lectura y espera hasta 0.5 s. Devuelve (temp, hum) o (None, None)."""
        with self._lock:
            self._temp = None
            self._hum  = None
        self._disparar()
        time.sleep(0.5)
        with self._lock:
            return self._temp, self._hum

    def cancelar(self):
        self._cb.cancel()

# ── Config ─────────────────────────────────────────────────────────────────
from config import (
    SERVER_IP, SERVER_PORT,
    RELAY_PINS, ACTIVE_LOW,
    DHT_PINS, DHT_NOMBRES,
    SENSOR_INTERVAL_S,
    VOLUMEN_M3, ACH_OBJETIVO, CAUDAL_M3H_POR_FAN, VENTANA_MIN,
    TEMP_UMBRAL_C, HUM_UMBRAL_PCT, HISTERESIS_TEMP, HISTERESIS_HUM,
)

SERVER_URL = f"http://{SERVER_IP}:{SERVER_PORT}/lectura"

# ── Estado compartido entre hilos ──────────────────────────────────────────
_lock = threading.Lock()
_estado = {
    # Lecturas por sensor: { "arriba_izquierda": {"temp": x, "hum": y}, ... }
    "sensores":        {n: {"temp": None, "hum": None} for n in DHT_NOMBRES},
    "override_on":     False,
    "override_sensor": None,   # nombre del sensor que disparó el override
    "fans_on":         False,
    "modo":            "ACH",  # "ACH" | "OVERRIDE"
}

# ── GPIO ───────────────────────────────────────────────────────────────────
def gpio_setup():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in RELAY_PINS:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH if ACTIVE_LOW else GPIO.LOW)

def _set_fans(encender: bool):
    """Enciende o apaga todos los ventiladores respetando la lógica activo-bajo."""
    nivel = (GPIO.LOW if encender else GPIO.HIGH) if ACTIVE_LOW else (GPIO.HIGH if encender else GPIO.LOW)
    for pin in RELAY_PINS:
        GPIO.output(pin, nivel)
    with _lock:
        _estado["fans_on"] = encender

def fans_on():
    _set_fans(True)

def fans_off():
    _set_fans(False)

# ── Cálculo duty-cycle ACH ─────────────────────────────────────────────────
def calcular_on_s_ventana() -> tuple[float, float]:
    """
    Devuelve (on_s_ventana, off_s_ventana) en segundos para el ciclo ACH.
    """
    caudal_total = CAUDAL_M3H_POR_FAN * len(RELAY_PINS)
    ach_cont = caudal_total / VOLUMEN_M3
    frac = min(1.0, ACH_OBJETIVO / ach_cont)
    ventana_s = VENTANA_MIN * 60
    on_s  = frac * ventana_s
    off_s = ventana_s - on_s
    return on_s, off_s

# ── Lógica de override ─────────────────────────────────────────────────────
def evaluar_override(nombre: str, temp: float, hum: float) -> None:
    """
    Override AND: se activa si temp > umbral Y hum > umbral.
    El sensor que lo disparó queda como "dueño" — el override solo
    se desactiva cuando ESE sensor baja ambas condiciones con histéresis,
    sin importar las lecturas de los otros sensores.
    """
    with _lock:
        override_activo  = _estado["override_on"]
        override_dueño   = _estado["override_sensor"]

    cond_alta = temp > TEMP_UMBRAL_C and hum > HUM_UMBRAL_PCT

    if not override_activo and cond_alta:
        with _lock:
            _estado["override_on"]     = True
            _estado["override_sensor"] = nombre
            _estado["modo"]            = "OVERRIDE"
        print(f"[OVERRIDE] Activado por '{nombre}' — T={temp}°C  H={hum}%")
        return

    if override_activo and override_dueño == nombre:
        cond_baja = (temp <= TEMP_UMBRAL_C - HISTERESIS_TEMP and
                     hum  <= HUM_UMBRAL_PCT - HISTERESIS_HUM)
        if cond_baja:
            with _lock:
                _estado["override_on"]     = False
                _estado["override_sensor"] = None
                _estado["modo"]            = "ACH"
            print(f"[OVERRIDE] Desactivado — '{nombre}' volvió a rango normal")

# ── Hilo: sensor DHT22 ─────────────────────────────────────────────────────
def hilo_un_sensor(pi: pigpio.pi, pin: int, nombre: str):
    """Corre en su propio hilo — lee un DHT22 y actualiza estado + envía al server."""
    sensor = DHT22Pigpio(pi, pin)
    print(f"[DHT] '{nombre}' inicializado en GPIO{pin}")

    # Escalonar arranque para no saturar el bus
    time.sleep(DHT_PINS.index(pin) * 2)

    try:
        while True:
            temp, hum = sensor.leer()

            if temp is not None and hum is not None:
                with _lock:
                    _estado["sensores"][nombre] = {"temp": temp, "hum": hum}

                evaluar_override(nombre, temp, hum)
                enviar_lectura(nombre, temp, hum)
                print(f"[DHT] {nombre}: T={temp}°C  H={hum}%  modo={_estado['modo']}")
            else:
                print(f"[DHT] {nombre} (GPIO{pin}): lectura inválida, reintentando…")

            time.sleep(SENSOR_INTERVAL_S)
    finally:
        sensor.cancelar()

# ── Hilo: ventilación ACH + override ──────────────────────────────────────
def hilo_ventilacion():
    on_s, off_s = calcular_on_s_ventana()
    print(f"[VENT] ACH={ACH_OBJETIVO}  on={on_s:.1f}s  off={off_s:.1f}s  (ventana {VENTANA_MIN} min)")

    while True:
        with _lock:
            override = _estado["override_on"]

        if override:
            # Override activo → ventiladores ON continuamente
            fans_on()
            # Revisar cada 5 s si el override se desactivó
            time.sleep(5)
        else:
            # Ciclo ACH normal
            if on_s > 0:
                fans_on()
                time.sleep(on_s)
            fans_off()
            time.sleep(off_s if off_s > 0 else 0.5)

# ── Envío al servidor ──────────────────────────────────────────────────────
def enviar_lectura(nombre: str, temp: float, hum: float) -> None:
    with _lock:
        modo    = _estado["modo"]
        fans    = _estado["fans_on"]
        ov_sens = _estado["override_sensor"]

    payload = {
        "sensor":          nombre,
        "temperature":     temp,
        "humidity":        hum,
        "fans_on":         fans,
        "modo":            modo,
        "override_sensor": ov_sens,
    }
    try:
        r = requests.post(SERVER_URL, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"[HTTP] Respuesta inesperada: {r.status_code}")
    except requests.exceptions.ConnectionError:
        print("[HTTP] Sin conexión al servidor — reintentando en próximo ciclo")
    except Exception as e:
        print(f"[HTTP] Error: {e}")

# ── Señales de salida limpia ───────────────────────────────────────────────
def salir(*_):
    print("\n[SALIR] Apagando ventiladores y limpiando GPIO...")
    try:
        fans_off()
    except Exception:
        pass
    GPIO.cleanup()
    raise SystemExit

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    gpio_setup()
    signal.signal(signal.SIGINT,  salir)
    signal.signal(signal.SIGTERM, salir)

    # Inicializar pigpiod
    pi = pigpio.pi()
    if not pi.connected:
        print("[ERROR] pigpiod no está corriendo.")
        print("        Ejecutá: sudo systemctl start pigpiod")
        raise SystemExit(1)

    # Lanzar un hilo por cada sensor DHT22
    for pin, nombre in zip(DHT_PINS, DHT_NOMBRES):
        threading.Thread(
            target=hilo_un_sensor,
            args=(pi, pin, nombre),
            daemon=True,
            name=f"sensor_{nombre}"
        ).start()

    # Hilo de ventilación (ACH + override)
    threading.Thread(target=hilo_ventilacion, daemon=True, name="ventilacion").start()

    print("[MAIN] Sistema iniciado con 4 sensores. Ctrl+C para salir.")
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
