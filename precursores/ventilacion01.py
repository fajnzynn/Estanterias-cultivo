#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Control simple de ventiladores + lecturas DHT22 + promedios diarios + API.
- Ventilación por 'renovaciones por hora' (ACH) usando duty-cycle agrupado.
- Lecturas DHT22 periódicas (configurable) a CSV.
- API Flask para ver estado y descargar CSV.

Requisitos:
  sudo apt-get update
  sudo apt-get install -y python3-pip
  pip3 install Adafruit_DHT flask
Cableado:
  - Relés activos LOW (común): IN a pines BCM, Vcc, GND; contacto NO/COM al fan.
  - DHT22 al pin BCM DHT_PIN con resistencia 10k entre Vcc y Data.
"""

import os, csv, time, threading, signal
from datetime import datetime, date
from collections import deque

import RPi.GPIO as GPIO

# ====== Sensores (DHT22) ======
try:
    import Adafruit_DHT
    HAS_DHT = True
except Exception:
    HAS_DHT = False

from flask import Flask, jsonify, send_file, abort

# =========================
# CONFIGURACIÓN RÁPIDA
# =========================
# Relés (BCM)
RELAY_PINS = [17, 27]
ACTIVE_LOW = False  # True: se activa con nivel LOW

# DHT22
USE_DHT22 = True
DHT_PIN = 4
MINUTOS_ENTRE_LECTURAS = 3  # pedido: cada 3 minutos

# Estantería (m) – volumen correcto ≈ 0.4554 m³
ALTO, ANCHO, PROFUNDO = 1.65, 0.92, 0.30
VOLUMEN_M3 = ALTO * ANCHO * PROFUNDO

# Ventilación deseada (renovaciones/h)
ACH_OBJETIVO = 12

# VD5010-5V: ~10 CFM -> 10 * 1.699 = 16.99 m³/h por fan
CAUDAL_M3H_POR_FAN = 10.0 * 1.699
NUM_FANS = len(RELAY_PINS)

# CSV
CSV_PATH = "/home/pi/dht_log.csv"

# API
HABILITAR_API = True
API_HOST, API_PORT = "0.0.0.0", 8080

# Ventana de duty (min) para no “moler” el relé con pulsos cortos
VENTANA_MIN = 5

# =========================
# Estado en memoria
# =========================
_estado = {
    "on_s_por_min": 0.0,
    "on_s_ventana": 0.0,
    "ventana_min": VENTANA_MIN,
    "lectura_actual": deque(maxlen=1),  # (ts, temp, hum)
}

# =========================
# GPIO / Ventiladores
# =========================
def gpio_setup(pines, active_low=True):
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for p in pines:
        GPIO.setup(p, GPIO.OUT, initial=GPIO.HIGH if active_low else GPIO.LOW)

def fan_on(pin, active_low=True):
    GPIO.output(pin, GPIO.LOW if active_low else GPIO.HIGH)

def fan_off(pin, active_low=True):
    GPIO.output(pin, GPIO.HIGH if active_low else GPIO.LOW)

def all_fans_on(pines, active_low=True):
    for p in pines:
        fan_on(p, active_low)

def all_fans_off(pines, active_low=True):
    for p in pines:
        fan_off(p, active_low)

# =========================
# Cálculo duty-cycle por ACH
# =========================
def segundos_encendido_por_minuto(ach_obj, volumen_m3, caudal_total_m3h):
    """
    ACH_continua = Q_total / V   (Q en m³/h, V en m³)
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
# DHT22 y CSV
# =========================
def leer_dht22(pin):
    """Devuelve (temp_C, hum_%) o (None, None) si falla."""
    if not (HAS_DHT and USE_DHT22):
        return None, None
    hum, temp = Adafruit_DHT.read_retry(Adafruit_DHT.DHT22, pin)
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
    """Promedios del día (YYYY-MM-DD)."""
    dia = dia_iso or date.today().isoformat()
    if not os.path.exists(path):
        return {"dia": dia, "temp_prom": None, "hum_prom": None, "n": 0}
    temps, hums = [], []
    with open(path, "r") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["date"] == dia:
                if row["temp_C"]:
                    try: temps.append(float(row["temp_C"]))
                    except: pass
                if row["hum_%"]:
                    try: hums.append(float(row["hum_%"]))
                    except: pass
    t_prom = round(sum(temps)/len(temps), 2) if temps else None
    h_prom = round(sum(hums)/len(hums), 2) if hums else None
    return {"dia": dia, "temp_prom": t_prom, "hum_prom": h_prom, "n": len(temps)}

# =========================
# Hilos
# =========================
def hilo_ventilacion():
    caudal_total = CAUDAL_M3H_POR_FAN * NUM_FANS
    on_s_min = segundos_encendido_por_minuto(ACH_OBJETIVO, VOLUMEN_M3, caudal_total)
    on_s_ventana = on_s_min * VENTANA_MIN
    off_s_ventana = max(0.0, VENTANA_MIN*60 - on_s_ventana)
    _estado["on_s_por_min"] = on_s_min
    _estado["on_s_ventana"] = on_s_ventana

    print(f"[VENT] V={VOLUMEN_M3:.4f} m³ | Q_total={caudal_total:.2f} m³/h | "
          f"ACH={ACH_OBJETIVO} -> {on_s_min:.2f}s/min  (~{on_s_ventana:.1f}s cada {VENTANA_MIN} min)")

    while True:
        if on_s_ventana > 0:
            all_fans_on(RELAY_PINS, ACTIVE_LOW)
            time.sleep(on_s_ventana)
        all_fans_off(RELAY_PINS, ACTIVE_LOW)
        time.sleep(off_s_ventana if off_s_ventana > 0 else 0.1)

def hilo_dht():
    while True:
        temp, hum = leer_dht22(DHT_PIN)
        ts = datetime.now().isoformat(timespec="seconds")
        _estado["lectura_actual"].clear()
        _estado["lectura_actual"].append((ts, temp, hum))
        guardar_medicion_csv(CSV_PATH, temp, hum)
        print(f"[DHT] {ts} -> T={temp}°C  H={hum}%")
        time.sleep(MINUTOS_ENTRE_LECTURAS * 60)

# =========================
# API Flask
# =========================
app = Flask(__name__)


@app.get("/status")
def status():
    ts, t, h = _estado["lectura_actual"][0] if _estado["lectura_actual"] else (None, None, None)
    return jsonify({
        "on_s_por_min": round(_estado["on_s_por_min"], 2),
        "on_s_cada_ventana": round(_estado["on_s_ventana"], 1),
        "ventana_min": _estado["ventana_min"],
        "ultima_lectura": {"ts": ts, "temp_C": t, "hum_%": h}
    })

@app.get("/hoy")
def hoy():
    return jsonify(promedios_diarios(CSV_PATH))

@app.get("/csv")
def csv_download():
    if not os.path.exists(CSV_PATH):
        abort(404, description="CSV no encontrado")
    return send_file(CSV_PATH, as_attachment=True)

def hilo_api():
    app.run(host=API_HOST, port=API_PORT, threaded=True)

# =========================
# Main
# =========================
def main():
    gpio_setup(RELAY_PINS, ACTIVE_LOW)

    def salir_sig(*_):
        all_fans_off(RELAY_PINS, ACTIVE_LOW)
        GPIO.cleanup()
        print("\n[SALIR] GPIO limpio.")
        raise SystemExit

    signal.signal(signal.SIGINT, salir_sig)
    signal.signal(signal.SIGTERM, salir_sig)

    threading.Thread(target=hilo_ventilacion, daemon=True).start()

    if USE_DHT22 and HAS_DHT:
        threading.Thread(target=hilo_dht, daemon=True).start()
    else:
        print("[DHT] Deshabilitado o sin librería.")

    # API en hilo propio
    threading.Thread(target=hilo_api, daemon=True).start()

    # Loop idle
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
