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
  pip3 install adafruit-circuitpython-dht RPi.GPIO requests
  sudo apt-get install libgpiod2
"""

import time
import threading
import signal
import requests
import board
import adafruit_dht

import RPi.GPIO as GPIO

# ── Config ─────────────────────────────────────────────────────────────────
from config import (
    SERVER_IP, SERVER_PORT,
    RELAY_PINS, ACTIVE_LOW, DHT_PIN,
    SENSOR_INTERVAL_S,
    VOLUMEN_M3, ACH_OBJETIVO, CAUDAL_M3H_POR_FAN, VENTANA_MIN,
    TEMP_UMBRAL_C, HUM_UMBRAL_PCT, HISTERESIS_TEMP, HISTERESIS_HUM,
)

SERVER_URL = f"http://{SERVER_IP}:{SERVER_PORT}/lectura"

# ── Estado compartido entre hilos ──────────────────────────────────────────
_lock = threading.Lock()
_estado = {
    "temp":            None,   # último valor leído
    "hum":             None,
    "override_on":     False,  # ventiladores forzados ON por condición
    "override_sensor": None,   # qué lectura disparó el override (dict)
    "fans_on":         False,  # estado actual de los ventiladores
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
def evaluar_override(temp: float | None, hum: float | None) -> None:
    """
    Actualiza el estado de override basándose en la última lectura.
    Reglas:
      - Activar:   temp > UMBRAL AND hum > UMBRAL
      - Desactivar: temp <= (UMBRAL - HIST_TEMP) AND hum <= (UMBRAL - HIST_HUM)
      - Mientras override está activo y el sensor que lo disparó sigue
        superando algún umbral, no se desactiva aunque otra lectura sea normal.
    """
    if temp is None or hum is None:
        return

    with _lock:
        override_activo = _estado["override_on"]

    cond_alta = temp > TEMP_UMBRAL_C and hum > HUM_UMBRAL_PCT

    if not override_activo and cond_alta:
        # Activar override
        lectura_disparo = {"temp": temp, "hum": hum}
        with _lock:
            _estado["override_on"]     = True
            _estado["override_sensor"] = lectura_disparo
            _estado["modo"]            = "OVERRIDE"
        print(f"[OVERRIDE] Activado — T={temp:.1f}°C  H={hum:.1f}%")
        return

    if override_activo:
        # Chequear si la condición bajó lo suficiente (con histéresis)
        cond_baja = (temp <= TEMP_UMBRAL_C - HISTERESIS_TEMP and
                     hum  <= HUM_UMBRAL_PCT - HISTERESIS_HUM)
        if cond_baja:
            with _lock:
                _estado["override_on"]     = False
                _estado["override_sensor"] = None
                _estado["modo"]            = "ACH"
            print(f"[OVERRIDE] Desactivado — T={temp:.1f}°C  H={hum:.1f}%")

# ── Hilo: sensor DHT22 ─────────────────────────────────────────────────────
# Mapeo de pin GPIO BCM → objeto board
_BCM_A_BOARD = {4: board.D4, 17: board.D17, 27: board.D27}

def hilo_sensor():
    pin_board = _BCM_A_BOARD.get(DHT_PIN, board.D4)
    sensor = adafruit_dht.DHT22(pin_board)

    while True:
        try:
            temp = sensor.temperature
            hum  = sensor.humidity
        except RuntimeError:
            # El DHT22 falla esporádicamente — es normal, reintentar
            time.sleep(2)
            continue
        except Exception as e:
            print(f"[DHT] Error inesperado: {e}")
            time.sleep(5)
            continue

        if temp is not None and hum is not None:
            temp = round(temp, 1)
            hum  = round(hum, 1)
            with _lock:
                _estado["temp"] = temp
                _estado["hum"]  = hum

            evaluar_override(temp, hum)
            enviar_lectura(temp, hum)
            print(f"[DHT] T={temp}°C  H={hum}%  modo={_estado['modo']}")

        time.sleep(SENSOR_INTERVAL_S)

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
def enviar_lectura(temp: float, hum: float) -> None:
    with _lock:
        modo    = _estado["modo"]
        fans    = _estado["fans_on"]
        ov_sens = _estado["override_sensor"]

    payload = {
        "temperature":      temp,
        "humidity":         hum,
        "fans_on":          fans,
        "modo":             modo,
        "override_sensor":  ov_sens,
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
    fans_off()
    GPIO.cleanup()
    raise SystemExit

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    gpio_setup()
    signal.signal(signal.SIGINT,  salir)
    signal.signal(signal.SIGTERM, salir)

    threading.Thread(target=hilo_sensor,      daemon=True, name="sensor").start()
    threading.Thread(target=hilo_ventilacion, daemon=True, name="ventilacion").start()

    print("[MAIN] Sistema iniciado. Ctrl+C para salir.")
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
