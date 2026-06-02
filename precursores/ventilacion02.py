#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ventilación Raspberry Pi (VD5010-5V + DHT22) con UI web.
- Control por ACH con duty agrupado (ventanas de 5 min).
- Lecturas DHT22 periódicas (CSV).
- API/UI Flask: / (UI), /status, /hoy, /ultimas?n=80, /csv

Dependencias:
  sudo apt-get update
  sudo apt-get install -y python3-pip
  pip3 install Adafruit_DHT flask
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

# ====== API (Flask) ======
try:
    from flask import Flask, jsonify, send_file, abort, request, Response
    HAS_FLASK = True
except Exception as e:
    HAS_FLASK = False
    FLASK_ERR = repr(e)

# =========================
# CONFIGURACIÓN RÁPIDA
# =========================
RELAY_PINS = [17, 27]       # GPIO (BCM) de tus relés
ACTIVE_LOW = False          # Tus relés son activos HIGH -> False

# DHT22
USE_DHT22 = True
DHT_PIN = 4
MINUTOS_ENTRE_LECTURAS = 3  # pedido: cada 3 minutos

# Estantería (m): 1.65 x 0.92 x 0.30
ALTO, ANCHO, PROFUNDO = 1.65, 0.92, 0.30
VOLUMEN_M3 = ALTO * ANCHO * PROFUNDO  # ≈ 0.4554 m³

# Ventilación (renovaciones/h)
ACH_OBJETIVO = 12

# VD5010-5V: ~10 CFM -> 10 * 1.699 = 16.99 m³/h por fan
CAUDAL_M3H_POR_FAN = 10.0 * 1.699
NUM_FANS = len(RELAY_PINS)

# CSV
CSV_PATH = "/home/user/dht_log.csv"

# API
HABILITAR_API = True
API_HOST, API_PORT = "0.0.0.0", 8080

# Ventana de duty (min) para evitar pulsos muy cortos
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
    # Inicial OFF: LOW si active_high, HIGH si active_low
    for p in pines:
        GPIO.setup(p, GPIO.OUPUT if hasattr(GPIO, "OUPUT") else GPIO.OUT,
                   initial=(GPIO.HIGH if active_low else GPIO.LOW))

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
    ACH_cont = Q_total / V  (Q: m³/h, V: m³)
    fracción_on = ach_obj / ACH_cont
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

def ultimas_mediciones(path, n=50):
    """Devuelve (timestamps, temps, hums) con las últimas n filas."""
    if not os.path.exists(path):
        return [], [], []
    ts, temps, hums = [], [], []
    with open(path, "r") as f:
        rows = list(csv.DictReader(f))
        for row in rows[-n:]:
            tstamp = row.get("timestamp")
            t = row.get("temp_C")
            h = row.get("hum_%")
            if tstamp:
                ts.append(tstamp)
                temps.append(float(t) if t else None)
                hums.append(float(h) if h else None)
    return ts, temps, hums

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
          f"ACH={ACH_OBJETIVO} -> {on_s_min:.2f}s/min (~{on_s_ventana:.1f}s cada {VENTANA_MIN} min)")

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
# API Flask (JSON + UI)
# =========================
if HAS_FLASK:
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


    @app.get("/health")
    def health():
        ts, t, h = _estado["lectura_actual"][0] if _estado["lectura_actual"] else (None, None, None)
        return jsonify({
            "HAS_FLASK": True,
            "HAS_DHT": bool(HAS_DHT),
            "USE_DHT22": bool(USE_DHT22),
            "CSV_PATH": CSV_PATH,
            "ultima_lectura": {"ts": ts, "temp_C": t, "hum_%": h},
            "on_s_por_min": round(_estado["on_s_por_min"], 2),
            "on_s_cada_ventana": round(_estado["on_s_ventana"], 1),
            "ventana_min": _estado["ventana_min"],
            "config": {
                "ACH_OBJETIVO": ACH_OBJETIVO,
                "MINUTOS_ENTRE_LECTURAS": MINUTOS_ENTRE_LECTURAS,
                "RELAY_PINS": RELAY_PINS,
                "ACTIVE_LOW": ACTIVE_LOW
            }
        })


    @app.get("/hoy")
    def hoy():
        return jsonify(promedios_diarios(CSV_PATH))

    @app.get("/ultimas")
    def ultimas():
        n = request.args.get("n", default=60, type=int)
        ts, temps, hums = ultimas_mediciones(CSV_PATH, n=n)
        return jsonify({"timestamps": ts, "temp_C": temps, "hum_%": hums})

    @app.get("/csv")
    def csv_download():
        if not os.path.exists(CSV_PATH):
            abort(404, description="CSV no encontrado")
        return send_file(CSV_PATH, as_attachment=True)

    @app.get("/")
    def ui_root():
        html = f"""
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ventilación Pi · Monitor</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1"></script>
<style>
  body {{ background:#0f172a; color:#e2e8f0; }}
  .card {{ background:#111827; border:1px solid #1f2937; }}
  .muted {{ color:#94a3b8; }}
  .value {{ font-size:2.2rem; font-weight:700; }}
</style>
</head>
<body>
<div class="container py-4">
  <h1 class="mb-3">Ventilación Raspberry Pi</h1>
  <p class="muted mb-4">Estado en vivo · actualiza cada 30s</p>

  <div class="row g-3">
    <div class="col-md-3">
      <div class="card p-3 h-100">
        <div class="muted">Temperatura</div>
        <div id="val-temp" class="value">--</div>
        <div id="ts-temp" class="muted small">--</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="card p-3 h-100">
        <div class="muted">Humedad</div>
        <div id="val-hum" class="value">--</div>
        <div id="ts-hum" class="muted small">--</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="card p-3 h-100">
        <div class="muted">Duty (s/min)</div>
        <div id="val-dutymin" class="value">--</div>
        <div class="muted small">Ventana: <span id="val-ventana">--</span> min</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="card p-3 h-100">
        <div class="muted">Duty por ventana</div>
        <div id="val-dutywin" class="value">--</div>
        <div class="muted small">ON/ventana (seg)</div>
      </div>
    </div>
  </div>

  <div class="card p-3 mt-3">
    <canvas id="chart" height="110"></canvas>
  </div>

  <div class="mt-3 d-flex gap-2">
    <a class="btn btn-outline-light btn-sm" href="/hoy">Promedios de hoy (JSON)</a>
    <a class="btn btn-outline-light btn-sm" href="/csv">Descargar CSV</a>
  </div>
</div>

<script>
let chart;

async function fetchStatus() {{
  const r = await fetch('/status'); 
  return r.json();
}}
async function fetchUltimas(n=60) {{
  const r = await fetch(`/ultimas?n=${{n}}`);
  return r.json();
}}

function fmt(x, suf="") {{
  if (x===null || x===undefined) return "--";
  return `${{x.toFixed ? x.toFixed(1) : x}}${{suf}}`;
}}

async function render() {{
  try {{
    const st = await fetchStatus();
    const ul = await fetchUltimas(80);

    // Tarjetas
    const lastTs = st.ultima_lectura?.ts || "--";
    const t = st.ultima_lectura?.temp_C;
    const h = st.ultima_lectura?.hum_%;

    document.getElementById('val-temp').textContent = fmt(t, " °C");
    document.getElementById('val-hum').textContent  = fmt(h, " %");
    document.getElementById('ts-temp').textContent  = lastTs;
    document.getElementById('ts-hum').textContent   = lastTs;

    document.getElementById('val-dutymin').textContent = fmt(st.on_s_por_min, " s");
    document.getElementById('val-dutywin').textContent = fmt(st.on_s_cada_ventana, " s");
    document.getElementById('val-ventana').textContent = st.ventana_min ?? "--";

    // Gráfico
    const labels = ul.timestamps.map(ts => ts.slice(11, 16)); // HH:MM
    const dataT = ul.temp_C;
    const dataH = ul.hum_;

    if (!chart) {{
      const ctx = document.getElementById('chart').getContext('2d');
      chart = new Chart(ctx, {{
        type: 'line',
        data: {{
          labels,
          datasets: [
            {{
              label: 'Temp (°C)',
              data: dataT,
              borderWidth: 2,
              tension: 0.25,
              spanGaps: true,
            }},
            {{
              label: 'Humedad (%)',
              data: dataH,
              borderWidth: 2,
              tension: 0.25,
              spanGaps: true,
              yAxisID: 'y2'
            }},
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          plugins: {{
            legend: {{ labels: {{ color: '#e2e8f0' }} }}
          }},
          scales: {{
            x: {{
              ticks: {{ color: '#94a3b8' }},
              grid: {{ color: 'rgba(148,163,184,0.15)' }}
            }},
            y: {{
              position: 'left',
              ticks: {{ color: '#94a3b8' }},
              grid: {{ color: 'rgba(148,163,184,0.15)' }}
            }},
            y2: {{
              position: 'right',
              ticks: {{ color: '#94a3b8' }},
              grid: {{ display:false }}
            }}
          }}
        }}
      }});
    }} else {{
      chart.data.labels = labels;
      chart.data.datasets[0].data = dataT;
      chart.data.datasets[1].data = dataH;
      chart.update();
    }}
  }} catch (e) {{
    console.error(e);
  }}
}}

render();
setInterval(render, 30000);
</script>
</body>
</html>
"""
        return Response(html, mimetype="text/html")

    def hilo_api():
        print(f"[API] Iniciando Flask en http://{API_HOST}:{API_PORT} ...")
        app.run(host=API_HOST, port=API_PORT, threaded=True)
else:
    def hilo_api():
        print(f"[API] Flask no disponible: {FLASK_ERR if 'FLASK_ERR' in globals() else 'no instalada'}")

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

    if HABILITAR_API:
        threading.Thread(target=hilo_api, daemon=True).start()
    else:
        print("[API] Deshabilitada por flag.")

    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
