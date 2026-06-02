#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ventilación Raspberry Pi (VD5010-5V) + 4x DHT22 con UI web (pulido).
- Control por ACH con duty agrupado (ventanas de N min).
- Lecturas DHT22 con periodicidad exacta (SAMPLE_EVERY_SEC).
- Lecturas escalonadas por sensor para suavizar carga.
- CSV robusto (cabecera asegurada).
- Override de humedad (>=98% ON continuo, libera a <=93%).
- UI: tarjetas por sensor + gráfico multi-serie + badges + botones de rango.

Dependencias:
  sudo apt-get update
  sudo apt-get install -y python3-pip
  pip3 install Adafruit_DHT flask
"""

import os, csv, time, threading, signal
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path

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

# ---- DHT22 (4 sensores) ----
# Mapeo según indicaste:
# - Arriba izquierda  -> GPIO 17
# - Abajo  izquierda  -> GPIO 4
# - Arriba derecha    -> GPIO 27
# - Abajo  derecha    -> GPIO 22
SENSORES = [
    {"id": "arriba_izquierda", "pin": 17},
    {"id": "abajo_izquierda",  "pin": 4},
    {"id": "arriba_derecha",   "pin": 27},
    {"id": "abajo_derecha",    "pin": 22},
]
USE_DHT22 = True

# Periodicidad exacta de muestreo (segundos)
# 30 s recomendado (evita saturar, buen detalle). Acepta 10..600 s.
SAMPLE_EVERY_SEC = 30

# ---- Umbrales de humedad (histéresis) ----
HUM_ON  = 98.0  # si cualquier sensor >= 98% -> override ON
HUM_OFF = 93.0  # libera override cuando TODOS <= 93%

# ---- Relés / Ventiladores ----
# Tus relés están en GPIO 20 y 21 (no comparten pines con DHT)
RELAY_PINS = [20, 21]
ACTIVE_LOW = False      # relés activos en HIGH

# Estantería (m) y volumen (1.65 x 0.92 x 0.30)
ALTO, ANCHO, PROFUNDO = 1.65, 0.92, 0.30
VOLUMEN_M3 = ALTO * ANCHO * PROFUNDO  # ≈ 0.4554 m³

# Ventilación (renovaciones/h)
ACH_OBJETIVO = 12

# VD5010-5V: ~10 CFM -> 10 * 1.699 = 16.99 m³/h por fan
CAUDAL_M3H_POR_FAN = 10.0 * 1.699
NUM_FANS = len(RELAY_PINS)

# CSV (en tu home)
CSV_PATH = str(Path.home() / "dht_log.csv")

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
    # Última lectura por sensor_id: dict[sensor_id] = (ts, temp, hum)
    "ultimas_por_sensor": {},
    "humid_override": False,   # True si override por humedad está activo
    "max_hum": None            # última humedad máxima observada entre sensores
}

# Evento para override de humedad (thread-safe)
_hum_override_event = threading.Event()

# =========================
# GPIO / Ventiladores
# =========================
def gpio_setup(pines, active_low=True):
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    # Inicial OFF: LOW si active_high, HIGH si active_low
    for p in pines:
        GPIO.setup(p, GPIO.OUT, initial=(GPIO.HIGH if active_low else GPIO.LOW))

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
# ACH -> duty
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
# CSV helpers
# =========================
def asegurar_csv(path):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "date", "time", "sensor_id", "temp_C", "hum_%"])

def guardar_medicion_csv(path, sensor_id, temp, hum, ts=None):
    asegurar_csv(path)
    ts = ts or datetime.now()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            ts.isoformat(timespec="seconds"),
            ts.date().isoformat(),
            ts.strftime("%H:%M:%S"),
            sensor_id,
            f"{temp:.2f}" if temp is not None else "",
            f"{hum:.2f}" if hum is not None else ""
        ])

def promedios_diarios(path, dia_iso=None, sensor_id=None):
    """
    Promedios por día. Si sensor_id es None -> promedia todas las lecturas del día.
    Tolerante a cabeceras con BOM.
    """
    dia = dia_iso or date.today().isoformat()
    if not os.path.exists(path):
        return {"dia": dia, "sensor_id": sensor_id, "temp_prom": None, "hum_prom": None, "n": 0}

    temps, hums = [], []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            if any(k.startswith("\ufeff") for k in row.keys()):
                row = { (k.lstrip("\ufeff")): v for k, v in row.items() }

            d = (row.get("date") or "").strip()
            if not d:
                ts = (row.get("timestamp") or "").strip()
                if len(ts) >= 10:
                    d = ts[:10]
            if d != dia:
                continue

            sid = (row.get("sensor_id") or "").strip()
            if sensor_id and sid != sensor_id:
                continue

            t_str = (row.get("temp_C") or "").strip()
            h_str = (row.get("hum_%") or "").strip()
            try:
                if t_str != "":
                    temps.append(float(t_str))
            except ValueError:
                pass
            try:
                if h_str != "":
                    hums.append(float(h_str))
            except ValueError:
                pass

    t_prom = round(sum(temps)/len(temps), 2) if temps else None
    h_prom = round(sum(hums)/len(hums), 2) if hums else None
    return {"dia": dia, "sensor_id": sensor_id, "temp_prom": t_prom, "hum_prom": h_prom, "n": len(temps)}

def ultimas_mediciones(path, n=120, sensor_id=None):
    """
    Devuelve dict {sensor_id: {"timestamps": [], "temp_C": [], "hum_%": []}}
    Si sensor_id es None, incluye todos.
    """
    res = defaultdict(lambda: {"timestamps": [], "temp_C": [], "hum_%": []})
    if not os.path.exists(path):
        return res
    with open(path, "r", newline="") as f:
        rows = list(csv.DictReader(f))
        if sensor_id:
            rows = [r for r in rows if (r.get("sensor_id") or "").strip() == sensor_id]
        rows = rows[-n:]
        for row in rows:
            if not row:
                continue
            if any(k.startswith("\ufeff") for k in row.keys()):
                row = { (k.lstrip("\ufeff")): v for k, v in row.items() }
            sid = (row.get("sensor_id") or "").strip() or "unknown"
            tstamp = (row.get("timestamp") or "").strip()
            t = (row.get("temp_C") or "").strip()
            h = (row.get("hum_%") or "").strip()

            res[sid]["timestamps"].append(tstamp if tstamp else "")
            try:
                res[sid]["temp_C"].append(float(t) if t != "" else None)
            except ValueError:
                res[sid]["temp_C"].append(None)
            try:
                res[sid]["hum_%"].append(float(h) if h != "" else None)
            except ValueError:
                res[sid]["hum_%"].append(None)
    return res

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
        if _hum_override_event.is_set():
            if not _estado["humid_override"]:
                print("[VENT] OVERRIDE HUMEDAD -> ON continuo")
            _estado["humid_override"] = True
            all_fans_on(RELAY_PINS, ACTIVE_LOW)
            time.sleep(2.0)
            continue
        else:
            if _estado["humid_override"]:
                print("[VENT] OVERRIDE HUMEDAD liberado -> vuelve duty ACH")
            _estado["humid_override"] = False

        if on_s_ventana > 0:
            print(f"[VENT] ON {on_s_ventana:.1f}s")
            all_fans_on(RELAY_PINS, ACTIVE_LOW)
            time.sleep(on_s_ventana)
        print(f"[VENT] OFF {off_s_ventana:.1f}s")
        all_fans_off(RELAY_PINS, ACTIVE_LOW)
        time.sleep(off_s_ventana if off_s_ventana > 0 else 0.1)

def hilo_dht():
    """
    Periodicidad exacta:
      - Lee cada SAMPLE_EVERY_SEC (alineado al reloj).
      - Escalona cada sensor con un pequeño desfase para suavizar carga.
    """
    # Clamp de intervalo (10..600 s)
    interval = max(10, min(int(SAMPLE_EVERY_SEC), 600))
    # Desfase entre sensores (reparte el intervalo)
    # p.ej., con 30 s y 4 sensores → 0s, 7.5s, 15s, 22.5s
    per_sensor_offset = interval / max(1, len(SENSORES))

    # Lectura inicial (una pasada rápida escalonada)
    base = time.monotonic()
    for idx, s in enumerate(SENSORES):
        due = base + idx * per_sensor_offset
        delay = max(0, due - time.monotonic())
        if delay > 0:
            time.sleep(delay)
        hum, temp = Adafruit_DHT.read_retry(Adafruit_DHT.DHT22, s["pin"]) if (HAS_DHT and USE_DHT22) else (None, None)
        ts = datetime.now().isoformat(timespec="seconds")
        if temp is not None and hum is not None:
            t_val, h_val = float(temp), float(hum)
            _estado["ultimas_por_sensor"][s["id"]] = (ts, t_val, h_val)
            guardar_medicion_csv(CSV_PATH, s["id"], t_val, h_val, ts=datetime.fromisoformat(ts))
            print(f"[DHT] INIT {s['id']} -> T={t_val:.1f}°C H={h_val:.1f}%")
        else:
            print(f"[DHT] INIT {s['id']} -> lectura inválida")

    # Bucle periódico alineado al reloj
    next_tick = time.monotonic() + interval
    while True:
        base = next_tick
        hums_validas = []

        for idx, s in enumerate(SENSORES):
            # escalonar cada sensor dentro del ciclo
            due = base + idx * per_sensor_offset
            delay = max(0, due - time.monotonic())
            if delay > 0:
                time.sleep(delay)

            hum, temp = Adafruit_DHT.read_retry(Adafruit_DHT.DHT22, s["pin"]) if (HAS_DHT and USE_DHT22) else (None, None)
            ts = datetime.now()
            ts_str = ts.isoformat(timespec="seconds")

            if temp is not None and hum is not None:
                t_val, h_val = float(temp), float(hum)
                _estado["ultimas_por_sensor"][s["id"]] = (ts_str, t_val, h_val)
                guardar_medicion_csv(CSV_PATH, s["id"], t_val, h_val, ts=ts)
                hums_validas.append(h_val)
                print(f"[DHT] {s['id']} -> T={t_val:.1f}°C H={h_val:.1f}%")
            else:
                print(f"[DHT] {s['id']} -> lectura inválida")

        # Override humedad (histéresis)
        max_h = max(hums_validas) if hums_validas else None
        _estado["max_hum"] = max_h
        if max_h is not None:
            if (not _hum_override_event.is_set()) and (max_h >= HUM_ON):
                _hum_override_event.set()
                print(f"[HUM] max_h={max_h:.1f}% >= {HUM_ON}% -> OVERRIDE ON")
            elif _hum_override_event.is_set() and (max_h <= HUM_OFF):
                _hum_override_event.clear()
                print(f"[HUM] max_h={max_h:.1f}% <= {HUM_OFF}% -> OVERRIDE OFF")

        # calcular siguiente tick exacto (sin deriva)
        next_tick += interval
        sleep_time = max(0, next_tick - time.monotonic())
        time.sleep(sleep_time)

# =========================
# API Flask (JSON + UI)
# =========================
if HAS_FLASK:
    app = Flask(__name__)

    @app.get("/status")
    def status():
        por_sensor = {}
        for s in SENSORES:
            sid = s["id"]
            ts, t, h = _estado["ultimas_por_sensor"].get(sid, (None, None, None))
            por_sensor[sid] = {"ts": ts, "temp_C": t, "hum_%": h}
        return jsonify({
            "on_s_por_min": round(_estado["on_s_por_min"], 2),
            "on_s_cada_ventana": round(_estado["on_s_ventana"], 1),
            "ventana_min": _estado["ventana_min"],
            "humid_override": _hum_override_event.is_set(),
            "hum_on": HUM_ON,
            "hum_off": HUM_OFF,
            "max_hum": _estado["max_hum"],
            "sample_every_sec": int(max(10, min(int(SAMPLE_EVERY_SEC), 600))),
            "sensors": por_sensor
        })

    @app.get("/hoy")
    def hoy():
        sid = request.args.get("sensor")  # opcional ?sensor=arriba_izquierda
        return jsonify(promedios_diarios(CSV_PATH, sensor_id=sid))

    @app.get("/ultimas")
    def ultimas():
        n = request.args.get("n", default=120, type=int)
        sid = request.args.get("sensor")  # opcional
        data = ultimas_mediciones(CSV_PATH, n=n, sensor_id=sid)
        return jsonify({"sensors": data})

    @app.get("/csv")
    def csv_download():
        if not os.path.exists(CSV_PATH):
            abort(404, description="CSV no encontrado")
        return send_file(CSV_PATH, as_attachment=True)

    @app.get("/health")
    def health():
        return jsonify({
            "HAS_FLASK": True,
            "HAS_DHT": bool(HAS_DHT),
            "USE_DHT22": bool(USE_DHT22),
            "CSV_PATH": CSV_PATH,
            "RELAY_PINS": RELAY_PINS,
            "ACTIVE_LOW": ACTIVE_LOW,
            "VENTANA_MIN": VENTANA_MIN,
            "ACH_OBJETIVO": ACH_OBJETIVO,
            "SENSORES": SENSORES,
            "on_s_por_min": round(_estado["on_s_por_min"], 2),
            "on_s_cada_ventana": round(_estado["on_s_ventana"], 1),
            "humid_override": _hum_override_event.is_set(),
            "hum_on": HUM_ON,
            "hum_off": HUM_OFF,
            "max_hum": _estado["max_hum"],
            "sample_every_sec": int(max(10, min(int(SAMPLE_EVERY_SEC), 600)))
        })

    @app.get("/")
    def ui_root():
        # Botonera simple para cambiar el rango (n) desde la UI
        sensor_labels = [s["id"] for s in SENSORES]
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
  .value {{ font-size:1.6rem; font-weight:700; }}
  .sensor-title {{ font-weight:600; }}
  .badge-ok {{ background:#16a34a; }}
  .badge-warn {{ background:#ef4444; }}
  a.btn {{ white-space: nowrap; }}
</style>
</head>
<body>
<div class="container py-4">
  <div class="d-flex align-items-center justify-content-between">
    <h1 class="mb-1">Ventilación Raspberry Pi</h1>
    <span id="badge-override" class="badge">...</span>
  </div>
  <p class="muted mb-3">4× DHT22 · actualiza cada 30s</p>

  <div class="row g-3">
    <div class="col-lg-3 col-md-6" >
      <div class="card p-3 h-100">
        <div class="muted">Duty (s/min)</div>
        <div id="val-dutymin" class="value">--</div>
        <div class="muted small">Ventana: <span id="val-ventana">--</span> min · ON/ventana <span id="val-dutywin">--</span> s</div>
        <div class="muted small">Húmedad máx: <span id="val-maxhum">--</span>% (ON {HUM_ON}%, OFF {HUM_OFF}%)</div>
      </div>
    </div>
    {"".join([f'''
    <div class="col-lg-3 col-md-6">
      <div class="card p-3 h-100">
        <div class="sensor-title">{sid}</div>
        <div class="muted">Temp / Hum</div>
        <div class="value"><span id="t-{sid}">--</span> °C · <span id="h-{sid}">--</span> %</div>
        <div class="muted small" id="ts-{sid}">--</div>
      </div>
    </div>''' for sid in sensor_labels])}
  </div>

  <div class="card p-3 mt-3">
    <div class="d-flex gap-2 flex-wrap mb-2">
      <a class="btn btn-outline-light btn-sm" href="#" onclick="setN(30)">Últ. 30</a>
      <a class="btn btn-outline-light btn-sm" href="#" onclick="setN(120)">Últ. 120</a>
      <a class="btn btn-outline-light btn-sm" href="#" onclick="setN(240)">Últ. 240</a>
    </div>
    <canvas id="chart" height="120"></canvas>
  </div>

  <div class="mt-3 d-flex gap-2 flex-wrap">
    <a class="btn btn-outline-light btn-sm" href="/hoy">Promedios de hoy (todos)</a>
    {"".join([f'<a class="btn btn-outline-light btn-sm" href="/hoy?sensor={sid}">Hoy {sid}</a>' for sid in sensor_labels])}
    <a class="btn btn-outline-light btn-sm" href="/csv">Descargar CSV</a>
    <a class="btn btn-outline-light btn-sm" href="/health">Health</a>
  </div>
</div>

<script>
let chart;
let N = 120; // muestras a mostrar (editable con botones)
const SENSOR_IDS = {sensor_labels};

function fmt(x, suf="") {{
  if (x===null || x===undefined) return "--";
  return (typeof x === 'number') ? x.toFixed(1)+suf : x+suf;
}}

function setN(n) {{
  N = n;
  render();
}}

async function fetchStatus() {{
  const r = await fetch('/status');
  return r.json();
}}

async function fetchUltimas(n) {{
  const r = await fetch(`/ultimas?n=${{n}}`);
  return r.json();
}}

function randomAlpha(i) {{
  const base = (i*97)%360;
  return `hsla(${{base}},70%,60%,1)`;
}}

async function render() {{
  try {{
    const st = await fetchStatus();
    const ul = await fetchUltimas(N);

    // Duty y humedad
    document.getElementById('val-dutymin').textContent = fmt(st.on_s_por_min, " s");
    document.getElementById('val-dutywin').textContent = fmt(st.on_s_cada_ventana, " s");
    document.getElementById('val-ventana').textContent = st.ventana_min ?? "--";
    document.getElementById('val-maxhum').textContent = st.max_hum !== null ? st.max_hum.toFixed(1) : "--";

    const badge = document.getElementById('badge-override');
    if (st.humid_override) {{
      badge.className = "badge badge-warn";
      badge.textContent = "OVERRIDE HUMEDAD: ON";
    }} else {{
      badge.className = "badge badge-ok";
      badge.textContent = "OVERRIDE HUMEDAD: OFF";
    }}

    // Tarjetas por sensor
    for (const sid of SENSOR_IDS) {{
      const s = st.sensors[sid] || {{}};
      document.getElementById(`t-${{sid}}`).textContent  = fmt(s.temp_C);
      document.getElementById(`h-${{sid}}`).textContent  = fmt(s.hum_);
      document.getElementById(`ts-${{sid}}`).textContent = s.ts || "--";
    }}

    // Gráfico multi-serie
    const datasets = [];
    let labels = [];
    let idx = 0;
    for (const sid of SENSOR_IDS) {{
      const d = ul.sensors[sid];
      if (!d) continue;
      if (d.timestamps && d.timestamps.length > labels.length) {{
        labels = d.timestamps.map(ts => ts ? ts.slice(11,16) : "");
      }}
      datasets.push({{
        label: `Temp · ${{sid}} (°C)`,
        data: d.temp_C,
        borderWidth: 2,
        tension: 0.25,
        spanGaps: true,
        yAxisID: 'y',
        borderColor: randomAlpha(idx),
      }});
      datasets.push({{
        label: `Hum · ${{sid}} (%)`,
        data: d.hum_%,
        borderWidth: 2,
        tension: 0.25,
        spanGaps: true,
        yAxisID: 'y2',
        borderColor: randomAlpha(idx+1),
      }});
      idx += 2;
    }}

    if (!chart) {{
      const ctx = document.getElementById('chart').getContext('2d');
      chart = new Chart(ctx, {{
        type: 'line',
        data: {{ labels, datasets }},
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
      chart.data.datasets = datasets;
      chart.update();
    }}
  }} catch (e) {{
    console.error(e);
  }}
}}

// Refrescar cada 30 s (no muy frecuente para no saturar)
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
    # Asegurar CSV antes de iniciar hilos
    asegurar_csv(CSV_PATH)

    # Preparar GPIO para relés
    gpio_setup(RELAY_PINS, ACTIVE_LOW)

    def salir_sig(*_):
        all_fans_off(RELAY_PINS, ACTIVE_LOW)
        GPIO.cleanup()
        print("\n[SALIR] GPIO limpio.")
        raise SystemExit

    signal.signal(signal.SIGINT, salir_sig)
    signal.signal(signal.SIGTERM, salir_sig)

    threading.Thread(target=hilo_ventilacion, daemon=True).start()

    if HAS_DHT and USE_DHT22:
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
