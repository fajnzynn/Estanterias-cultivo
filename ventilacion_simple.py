#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Control simple de ventiladores + lecturas DHT22 + promedios diarios.
- Ventilación por 'renovaciones de aire por hora' (ACH) con duty-cycle.
- Lecturas periódicas (cada N minutos), guardadas en CSV.
- Mini API Flask opcional para ver estado/estadísticas desde el celu.
"""

import time, csv, threading, signal, os
from datetime import datetime, date
from collections import deque

import RPi.GPIO as GPIO

# ==== OPCIONAL: descomenta si querés la mini API web ====
# pip install flask
# from flask import Flask, jsonify

# ==== SENSORES (DHT22) ====
# pip install Adafruit_DHT
try:
    import Adafruit_DHT
    HAS_DHT = True
except Exception:
    HAS_DHT = False

# =========================
# CONFIGURACIÓN RÁPIDA
# =========================
# Pines de los relés (BCM)
RELAY_PINS = [17, 27]
ACTIVE_LOW = True   # True si el relé se activa con nivel LOW (común en módulos de 4 canales)

# DHT22
USE_DHT22 = True
DHT_PIN = 4
DHT_SENSOR = Adafruit_DHT.DHT22 if (HAS_DHT and USE_DHT22) else None
MINUTOS_ENTRE_LECTURAS = 5  # Cambiá a gusto (ej. 2, 10, 15…)

# Estantería (m)
ALTO = 1.65
ANCHO = 0.92
PROFUNDO = 0.30
VOLUMEN_M3 = ALTO * ANCHO * PROFUNDO  # ≈ 0.04554 m3 con tus medidas

# Ventilación deseada
ACH_OBJETIVO = 12            # Renovaciones/hora (entre 10 y 15 como pediste)
CAUDAL_M3H_POR_FAN = 6.0     # <-- Ajustá según tu modelo (m3/h por ventilador 50mm 5V)
NUM_FANS = len(RELAY_PINS)
CSV_PATH = "/home/pi/dht_log.csv"

# API web (opcional)
HABILITAR_API = False          # poné True para usar la mini API
API_HOST = "0.0.0.0"
API_PORT = 8080

# =========================
# GPIO / Ventiladores
# =========================
def gpio_setup(pines, active_low=True):
    GPIO.setmode(GPIO.BCM)
    for p in pines:
        GPIO.setup(p, GPIO.OUT, initial=GPIO.HIGH if active_low else GPIO.LOW)

def fan_on(pin, active_low=True):
    """Enciende un ventilador en 'pin'."""
    GPIO.output(pin, GPIO.LOW if active_low else GPIO.HIGH)

def fan_off(pin, active_low=True):
    """Apaga un ventilador en 'pin'."""
    GPIO.output(pin, GPIO.HIGH if active_low else GPIO.LOW)

def all_fans_on(pines, active_low=True):
    for p in pines: fan_on(p, active_low)

def all_fans_off(pines, active_low=True):
    for p in pines: fan_off(p, active_low)

# =========================
# Cálculo duty-cycle por ACH
# =========================
def segundos_encendido_por_minuto(ach_obj, volumen_m3, caudal_total_m3h):
    """
    Devuelve cuántos segundos por minuto deben estar ENCENDIDOS los ventiladores
    para lograr 'ach_obj' (renovaciones/hora) para un volumen dado.

    Fórmula: ACH_continua = Q_total / V  (con Q en m3/h, V en m3)
             fracción_on = ach_obj / ACH_continua
             on_per_min = fracción_on * 60 s
    """
    if caudal_total_m3h <= 0 or volumen_m3 <= 0:
        return 0.0
    ach_cont = caudal_total_m3h / volumen_m3
    frac = ach_obj / ach_cont
    on_s = max(0.0, min(60.0, frac * 60.0))
    return on_s

# =========================
# DHT22
# =========================
def leer_dht22(pin):
    """Devuelve (temp_C, hum_%) o (None, None) si falla."""
    if not (HAS_DHT and DHT_SENSOR):
        return None, None
    hum, temp = Adafruit_DHT.read_retry(DHT_SENSOR, pin)
    if hum is None or temp is None:
        return None, None
    return float(temp), float(hum)

def asegurar_csv(path):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "date", "time", "temp_C", "hum_%"])

def guardar_medicion_csv(path, temp, hum):
    asegurar_csv(path)
    ts = datetime.now()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([ts.isoformat(timespec="seconds"),
                    ts.date().isoformat(),
                    ts.strftime("%H:%M:%S"),
                    f"{temp:.2f}" if temp is not None else "",
                    f"{hum:.2f}" if hum is not None else ""])

def promedios_diarios(path, dia_iso=None):
    """
    Lee el CSV y devuelve promedios {'temp': x, 'hum': y} del día indicado (YYYY-MM-DD).
    Si dia_iso es None, usa el día de hoy.
    """
    dia = dia_iso or date.today().isoformat()
    if not os.path.exists(path):
        return {"dia": dia, "temp_prom": None, "hum_prom": None, "n": 0}
    temps, hums = [], []
    with open(path, "r") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["date"] == dia:
                try:
                    if row["temp_C"]:
                        temps.append(float(row["temp_C"]))
                    if row["hum_%"]:
                        hums.append(float(row["hum_%"]))
                except ValueError:
                    pass
    t_prom = round(sum(temps)/len(temps), 2) if temps else None
    h_prom = round(sum(hums)/len(hums), 2) if hums else None
    return {"dia": dia, "temp_prom": t_prom, "hum_prom": h_prom, "n": len(temps)}

# =========================
# Hilos de trabajo
# =========================
_estado = {
    "on_s_por_min": 0.0,
    "lectura_actual": deque(maxlen=1),   # guarda último (ts, temp, hum)
}

def hilo_ventilacion():
    caudal_total = CAUDAL_M3H_POR_FAN * NUM_FANS
    on_s = segundos_encendido_por_minuto(ACH_OBJETIVO, VOLUMEN_M3, caudal_total)
    _estado["on_s_por_min"] = on_s

    print(f"[VENT] Volumen={VOLUMEN_M3:.5f} m3 | Q_total={caudal_total:.2f} m3/h | "
          f"ACH_obj={ACH_OBJETIVO} -> ON {on_s:.2f}s/min")

    while True:
        if on_s <= 0:
            all_fans_off(RELAY_PINS, ACTIVE_LOW)
            time.sleep(60)
            continue

        # Encender por on_s
        all_fans_on(RELAY_PINS, ACTIVE_LOW)
        time.sleep(on_s)

        # Apagar por el resto del minuto
        off_s = max(0.0, 60.0 - on_s)
        all_fans_off(RELAY_PINS, ACTIVE_LOW)
        time.sleep(off_s)

def hilo_dht():
    while True:
        temp, hum = leer_dht22(DHT_PIN)
        ts = datetime.now().isoformat(timespec="seconds")
        _estado["lectura_actual"].append((ts, temp, hum))
        guardar_medicion_csv(CSV_PATH, temp, hum)
        print(f"[DHT] {ts} -> T={temp}°C  H={hum}%")
        time.sleep(MINUTOS_ENTRE_LECTURAS * 60)

# =========================
# Mini API (opcional)
# =========================
# app = Flask(__name__)
# @app.get("/status")
# def status():
#     ts, t, h = _estado["lectura_actual"][0] if _estado["lectura_actual"] else (None, None, None)
#     hoy = promedios_diarios(CSV_PATH)
#     return jsonify({
#         "on_s_por_min": _estado["on_s_por_min"],
#         "ultima_lectura": {"ts": ts, "temp_C": t, "hum_%": h},
#         "promedios_hoy": hoy
#     })

# =========================
# Main
# =========================
def main():
    gpio_setup(RELAY_PINS, ACTIVE_LOW)

    # Apagar limpio al salir
    def salir_sig(*_):
        all_fans_off(RELAY_PINS, ACTIVE_LOW)
        GPIO.cleanup()
        print("\n[SALIR] GPIO limpio.")
        raise SystemExit

    signal.signal(signal.SIGINT, salir_sig)
    signal.signal(signal.SIGTERM, salir_sig)

    # Hilos
    th_fans = threading.Thread(target=hilo_ventilacion, daemon=True)
    th_fans.start()

    if USE_DHT22 and HAS_DHT:
        th_dht = threading.Thread(target=hilo_dht, daemon=True)
        th_dht.start()
    else:
        print("[DHT] Deshabilitado o sin librería. Solo ventilación por tiempo.")

    # # API opcional
    # if HABILITAR_API:
    #     app.run(host=API_HOST, port=API_PORT)

    # Loop “idle”
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
