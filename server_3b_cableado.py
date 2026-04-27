#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_3b.py — Pi 3B+
  - Recibe lecturas del Pi Zero W vía POST /lectura
  - Guarda historial en CSV
  - Sirve dashboard web en tiempo real (polling cada 15 s)
  - Envía alertas por Telegram cuando temp o hum superan umbrales
  - Endpoints: /, /status, /historico?n=200, /csv, /health

Dependencias:
  pip3 install flask requests
"""

import os
import csv
import time
import threading
import requests as req_lib
from datetime import datetime, date
from collections import deque

from flask import Flask, jsonify, request, send_file, abort, Response

from config import (
    SERVER_PORT,
    CSV_PATH,
    TEMP_UMBRAL_C, HUM_UMBRAL_PCT,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ALERTA_COOLDOWN_MIN,
    ACH_OBJETIVO, VENTANA_MIN, CAUDAL_M3H_POR_FAN, VOLUMEN_M3,
    RELAY_PINS, DHT_NOMBRES,
)

SENSORES = DHT_NOMBRES  # ["arriba_izquierda", "abajo_izquierda", "arriba_derecha", "abajo_derecha"]

# ── Estado en memoria ──────────────────────────────────────────────────────
_lock  = threading.Lock()
_datos = deque(maxlen=1000)  # últimas 1000 lecturas (todos los sensores)

# Última lectura por sensor
_ultimas = {nombre: {"ts": None, "temperature": None, "humidity": None} for nombre in SENSORES}

_estado_fans = {
    "fans_on":         None,
    "modo":            None,
    "override_sensor": None,
    "ts":              None,
}

# Alertas — cooldown por sensor
_ultima_alerta = {nombre: {"temp": 0.0, "hum": 0.0} for nombre in SENSORES}

# ── CSV ────────────────────────────────────────────────────────────────────
CABECERA_CSV = [
    "timestamp", "date", "time", "sensor",
    "temp_C", "hum_%", "fans_on", "modo", "override_sensor"
]

def asegurar_csv():
    if not os.path.exists(CSV_PATH):
        if os.path.dirname(CSV_PATH):
            os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(CABECERA_CSV)

def guardar_csv(datos: dict):
    asegurar_csv()
    ts = datetime.now()
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow([
            ts.isoformat(timespec="seconds"),
            ts.date().isoformat(),
            ts.strftime("%H:%M:%S"),
            datos.get("sensor", ""),
            datos.get("temperature", ""),
            datos.get("humidity", ""),
            int(bool(datos.get("fans_on"))),
            datos.get("modo", ""),
            datos.get("override_sensor") or "",
        ])

def promedios_hoy() -> dict:
    dia = date.today().isoformat()
    if not os.path.exists(CSV_PATH):
        return {"dia": dia, "sensores": {}}
    result = {n: {"temps": [], "hums": []} for n in SENSORES}
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            if row["date"] != dia:
                continue
            nombre = row.get("sensor", "")
            if nombre not in result:
                continue
            if row["temp_C"]:
                try: result[nombre]["temps"].append(float(row["temp_C"]))
                except: pass
            if row["hum_%"]:
                try: result[nombre]["hums"].append(float(row["hum_%"]))
                except: pass
    out = {}
    for nombre, vals in result.items():
        t = vals["temps"]; h = vals["hums"]
        out[nombre] = {
            "temp_prom": round(sum(t)/len(t), 2) if t else None,
            "hum_prom":  round(sum(h)/len(h), 2) if h else None,
            "n": len(t),
        }
    return {"dia": dia, "sensores": out}

# ── Telegram ───────────────────────────────────────────────────────────────
def enviar_telegram(mensaje: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        req_lib.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}, timeout=5)
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")

def check_alertas(nombre: str, temp, hum) -> None:
    ahora = time.time()
    cooldown = ALERTA_COOLDOWN_MIN * 60

    if temp is not None and temp > TEMP_UMBRAL_C:
        if ahora - _ultima_alerta[nombre]["temp"] > cooldown:
            _ultima_alerta[nombre]["temp"] = ahora
            enviar_telegram(
                f"🌡️ *ALERTA TEMPERATURA*\n"
                f"Sensor: {nombre}\n"
                f"Valor: {temp:.1f}°C (umbral: {TEMP_UMBRAL_C}°C)\n"
                f"Ventiladores: {'ON' if _estado_fans.get('fans_on') else 'OFF'}"
            )

    if hum is not None and hum > HUM_UMBRAL_PCT:
        if ahora - _ultima_alerta[nombre]["hum"] > cooldown:
            _ultima_alerta[nombre]["hum"] = ahora
            enviar_telegram(
                f"💧 *ALERTA HUMEDAD*\n"
                f"Sensor: {nombre}\n"
                f"Valor: {hum:.1f}% (umbral: {HUM_UMBRAL_PCT}%)\n"
                f"Ventiladores: {'ON' if _estado_fans.get('fans_on') else 'OFF'}"
            )

# ── Flask ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.post("/lectura")
def recibir_lectura():
    datos = request.get_json(force=True, silent=True)
    if not datos:
        return jsonify({"error": "payload vacío"}), 400

    ts     = datetime.now().isoformat(timespec="seconds")
    nombre = datos.get("sensor", "desconocido")
    temp   = datos.get("temperature")
    hum    = datos.get("humidity")

    with _lock:
        if nombre in _ultimas:
            _ultimas[nombre] = {"ts": ts, "temperature": temp, "humidity": hum}
        _estado_fans.update({
            "fans_on":         datos.get("fans_on"),
            "modo":            datos.get("modo"),
            "override_sensor": datos.get("override_sensor"),
            "ts":              ts,
        })
        _datos.append({**datos, "ts": ts})

    guardar_csv(datos)
    check_alertas(nombre if nombre in _ultima_alerta else list(_ultima_alerta.keys())[0], temp, hum)

    print(f"[RECIBIDO] {ts}  {nombre}: T={temp}°C  H={hum}%  modo={datos.get('modo')}")
    return jsonify({"status": "ok"}), 200


@app.get("/status")
def status():
    with _lock:
        return jsonify({
            "sensores":        _ultimas,
            "fans":            _estado_fans,
        })


@app.get("/historico")
def historico():
    n = request.args.get("n", default=120, type=int)
    with _lock:
        filas = list(_datos)[-n:]
    return jsonify(filas)


@app.get("/hoy")
def hoy():
    return jsonify(promedios_hoy())


@app.get("/csv")
def descargar_csv():
    if not os.path.exists(CSV_PATH):
        abort(404, description="CSV no encontrado aún")
    return send_file(CSV_PATH, as_attachment=True)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "csv_existe": os.path.exists(CSV_PATH),
        "lecturas_en_ram": len(_datos),
        "config": {
            "ACH_OBJETIVO": ACH_OBJETIVO,
            "TEMP_UMBRAL_C": TEMP_UMBRAL_C,
            "HUM_UMBRAL_PCT": HUM_UMBRAL_PCT,
            "VENTANA_MIN": VENTANA_MIN,
        }
    })


@app.get("/")
def dashboard():
    sensores_js = str(SENSORES)   # lista Python → JS array
    html = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cultivo Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:    #080f0a; --panel: #0d1a10; --border: #1a3320;
    --green: #22c55e; --amber: #f59e0b; --red: #ef4444;
    --blue:  #38bdf8; --purple: #a78bfa; --orange: #fb923c; --pink: #f472b6;
    --muted: #4b7260; --text: #d1fae5;
    --mono: 'Space Mono', monospace; --sans: 'DM Sans', sans-serif;
  }}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding:1.5rem}}
  header{{display:flex;align-items:baseline;gap:1rem;margin-bottom:1.5rem;border-bottom:1px solid var(--border);padding-bottom:1rem}}
  header h1{{font-family:var(--mono);font-size:1rem;letter-spacing:.1em;color:var(--green)}}
  #last-update{{font-size:.72rem;color:var(--muted);margin-left:auto;font-family:var(--mono)}}
  .dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:.4rem;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}

  /* sensor grid */
  .sensor-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;margin-bottom:1.5rem}}
  .sensor-card{{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:1rem;position:relative;overflow:hidden}}
  .sensor-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent,var(--green));opacity:.7}}
  .sensor-name{{font-family:var(--mono);font-size:.62rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:.75rem}}
  .sensor-row{{display:flex;gap:1.5rem;align-items:baseline}}
  .sensor-val{{font-family:var(--mono);font-size:1.7rem;font-weight:700;line-height:1}}
  .sensor-unit{{font-family:var(--mono);font-size:.75rem;color:var(--muted)}}
  .sensor-ts{{font-family:var(--mono);font-size:.6rem;color:var(--muted);margin-top:.5rem}}

  /* status bar */
  .status-bar{{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1.5rem;align-items:center}}
  .badge{{font-family:var(--mono);font-size:.7rem;padding:.3rem .7rem;border-radius:3px;background:var(--border);color:var(--green);letter-spacing:.08em}}
  .badge.override{{background:#7c2d12;color:#fca5a5}}
  .pill{{font-family:var(--mono);font-size:.7rem;padding:.25rem .7rem;border:1px solid var(--border);border-radius:20px;color:var(--text)}}

  /* override box */
  #override-box{{display:none;background:#1c0a0a;border:1px solid #7c2d12;border-radius:6px;padding:.9rem 1rem;margin-bottom:1.5rem;font-family:var(--mono);font-size:.75rem;color:#fca5a5}}

  /* chart */
  .chart-wrap{{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:1.25rem;margin-bottom:1rem}}
  .chart-tabs{{display:flex;gap:.5rem;margin-bottom:1rem}}
  .tab{{font-family:var(--mono);font-size:.65rem;padding:.25rem .7rem;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s}}
  .tab.active{{background:var(--border);color:var(--text)}}
  .chart-title{{font-family:var(--mono);font-size:.65rem;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:.75rem}}

  /* promedios */
  .avg-section{{margin-bottom:1.5rem}}
  .avg-title{{font-family:var(--mono);font-size:.65rem;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:.6rem}}
  .avg-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.6rem}}
  .avg-card{{background:var(--panel);border:1px solid var(--border);border-radius:5px;padding:.7rem .9rem;font-family:var(--mono);font-size:.7rem}}
  .avg-card-name{{color:var(--muted);margin-bottom:.3rem;font-size:.62rem;text-transform:uppercase;letter-spacing:.08em}}

  /* actions */
  .actions{{display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1rem}}
  .btn{{font-family:var(--mono);font-size:.72rem;letter-spacing:.08em;padding:.45rem 1rem;border-radius:4px;border:1px solid var(--green);color:var(--green);background:transparent;text-decoration:none;cursor:pointer;transition:background .15s,color .15s}}
  .btn:hover{{background:var(--green);color:var(--bg)}}
</style>
</head>
<body>

<header>
  <h1><span class="dot"></span>CULTIVO MONITOR</h1>
  <span id="last-update">esperando datos…</span>
</header>

<!-- Tarjetas de sensores -->
<div class="sensor-grid" id="sensor-grid"></div>

<!-- Barra de estado -->
<div class="status-bar">
  <span id="fans-badge" class="badge">FANS --</span>
  <span id="modo-badge" class="badge">--</span>
  <span id="ts-pill" class="pill">--</span>
</div>

<div id="override-box">
  ⚠ OVERRIDE activo · disparado por sensor: <strong id="ov-sensor">--</strong>
</div>

<!-- Gráfico -->
<div class="chart-wrap">
  <div class="chart-tabs" id="chart-tabs"></div>
  <div class="chart-title" id="chart-title">temperatura — todos los sensores</div>
  <canvas id="chart" height="130"></canvas>
</div>

<!-- Promedios de hoy -->
<div class="avg-section">
  <div class="avg-title">Promedios de hoy</div>
  <div class="avg-grid" id="avg-grid"></div>
</div>

<div class="actions">
  <a class="btn" href="/csv">⬇ Descargar CSV</a>
  <a class="btn" href="/hoy" target="_blank">Promedios JSON</a>
  <a class="btn" href="/health" target="_blank">Health</a>
</div>

<script>
const SENSORES    = {sensores_js};
const TEMP_UMBRAL = {TEMP_UMBRAL_C};
const HUM_UMBRAL  = {HUM_UMBRAL_PCT};
const COLORES     = ['#22c55e','#38bdf8','#a78bfa','#fb923c'];
const ACENTOS     = ['--green','--blue','--purple','--orange'];

let chart;
let modoGrafico = 'temp';  // 'temp' | 'hum'

function fmt(v, dec=1) {{ return (v !== null && v !== undefined) ? (+v).toFixed(dec) : '--'; }}

function colorVal(v, umbral) {{
  if (v === null || v === undefined) return 'var(--muted)';
  if (v > umbral + 2) return 'var(--red)';
  if (v > umbral)     return 'var(--amber)';
  return 'var(--green)';
}}

// ── Inicializar tarjetas de sensores ──
function initSensorCards() {{
  const grid = document.getElementById('sensor-grid');
  SENSORES.forEach((nombre, i) => {{
    const label = nombre.replace(/_/g,' ');
    grid.innerHTML += `
      <div class="sensor-card" id="card-${{nombre}}" style="--accent:${{COLORES[i]}}">
        <div class="sensor-name">${{label}}</div>
        <div class="sensor-row">
          <div>
            <span class="sensor-val" id="t-${{nombre}}">--</span>
            <span class="sensor-unit">°C</span>
          </div>
          <div>
            <span class="sensor-val" id="h-${{nombre}}">--</span>
            <span class="sensor-unit">%</span>
          </div>
        </div>
        <div class="sensor-ts" id="ts-${{nombre}}">sin datos</div>
      </div>`;
  }});
}}

// ── Inicializar tabs del gráfico ──
function initTabs() {{
  const tabs = document.getElementById('chart-tabs');
  [['temp','Temperatura'],['hum','Humedad']].forEach(([key,label]) => {{
    const btn = document.createElement('button');
    btn.className = 'tab' + (key === modoGrafico ? ' active' : '');
    btn.textContent = label;
    btn.onclick = () => {{
      modoGrafico = key;
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
    }};
    tabs.appendChild(btn);
  }});
}}

async function fetchJSON(url) {{
  const r = await fetch(url); return r.json();
}}

async function render() {{
  try {{
    const [st, hist, hoy] = await Promise.all([
      fetchJSON('/status'),
      fetchJSON('/historico?n=200'),
      fetchJSON('/hoy'),
    ]);

    // ── Tarjetas ──
    const sens = st.sensores || {{}};
    SENSORES.forEach(nombre => {{
      const d = sens[nombre] || {{}};
      const t = d.temperature, h = d.humidity;
      const tel = document.getElementById('t-'+nombre);
      const hel = document.getElementById('h-'+nombre);
      const tsel = document.getElementById('ts-'+nombre);
      if (tel) {{ tel.textContent = fmt(t)+'°'; tel.style.color = colorVal(t, TEMP_UMBRAL); }}
      if (hel) {{ hel.textContent = fmt(h)+'%';  hel.style.color = colorVal(h, HUM_UMBRAL); }}
      if (tsel) tsel.textContent = d.ts ? d.ts.slice(11,19) : 'sin datos';
    }});

    // ── Status bar ──
    const fans = st.fans || {{}};
    const fansBadge = document.getElementById('fans-badge');
    fansBadge.textContent = 'FANS ' + (fans.fans_on ? 'ON' : 'OFF');
    fansBadge.style.color = fans.fans_on ? 'var(--green)' : 'var(--muted)';
    const modoBadge = document.getElementById('modo-badge');
    modoBadge.textContent = (fans.modo || '--').toUpperCase();
    modoBadge.className = 'badge' + (fans.modo === 'OVERRIDE' ? ' override' : '');
    document.getElementById('ts-pill').textContent = fans.ts ? fans.ts.slice(11,19) : '--';

    // ── Override ──
    const ovBox = document.getElementById('override-box');
    if (fans.modo === 'OVERRIDE' && fans.override_sensor) {{
      ovBox.style.display = 'block';
      document.getElementById('ov-sensor').textContent = fans.override_sensor.replace(/_/g,' ');
    }} else {{
      ovBox.style.display = 'none';
    }}

    document.getElementById('last-update').textContent =
      'actualizado ' + new Date().toLocaleTimeString('es-AR');

    // ── Gráfico — agrupar historial por sensor ──
    // Construir timeline unificado
    const tsSet = [...new Set(hist.map(d => d.ts ? d.ts.slice(0,19) : null).filter(Boolean))].sort();
    // Por sensor, buscar valor en cada timestamp
    const datasets = SENSORES.map((nombre, i) => {{
      const porTs = {{}};
      hist.filter(d => d.sensor === nombre).forEach(d => {{
        if (d.ts) porTs[d.ts.slice(0,19)] = modoGrafico === 'temp' ? d.temperature : d.humidity;
      }});
      return {{
        label: nombre.replace(/_/g,' '),
        data: tsSet.map(ts => porTs[ts] ?? null),
        borderColor: COLORES[i],
        backgroundColor: COLORES[i] + '15',
        borderWidth: 1.5,
        tension: .3,
        pointRadius: 0,
        spanGaps: true,
      }};
    }});

    const labels = tsSet.map(ts => ts.slice(11,16));
    const yLabel = modoGrafico === 'temp' ? 'Temperatura (°C)' : 'Humedad (%)';
    document.getElementById('chart-title').textContent =
      (modoGrafico === 'temp' ? 'temperatura' : 'humedad') + ' — todos los sensores';

    if (!chart) {{
      const ctx = document.getElementById('chart').getContext('2d');
      chart = new Chart(ctx, {{
        type: 'line',
        data: {{ labels, datasets }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          plugins: {{
            legend: {{ labels: {{ color: '#4b7260', font: {{ family: 'Space Mono', size: 10 }} }} }},
            tooltip: {{ backgroundColor: '#0d1a10', borderColor: '#1a3320', borderWidth: 1,
                        titleColor: '#d1fae5', bodyColor: '#94a3b8' }}
          }},
          scales: {{
            x: {{ ticks: {{ color: '#4b7260', font: {{ family: 'Space Mono', size: 9 }}, maxTicksLimit: 10 }},
                   grid: {{ color: 'rgba(26,51,32,.5)' }} }},
            y: {{ ticks: {{ color: '#4b7260', font: {{ family: 'Space Mono', size: 9 }} }},
                   grid: {{ color: 'rgba(26,51,32,.5)' }},
                   title: {{ display: true, text: yLabel, color: '#4b7260',
                             font: {{ family: 'Space Mono', size: 9 }} }} }}
          }}
        }}
      }});
    }} else {{
      chart.data.labels = labels;
      chart.data.datasets = datasets;
      chart.options.scales.y.title.text = yLabel;
      chart.update('none');
    }}

    // ── Promedios ──
    const avgGrid = document.getElementById('avg-grid');
    const hoyS = hoy.sensores || {{}};
    avgGrid.innerHTML = SENSORES.map((nombre, i) => {{
      const d = hoyS[nombre] || {{}};
      return `<div class="avg-card" style="border-top:2px solid ${{COLORES[i]}}">
        <div class="avg-card-name">${{nombre.replace(/_/g,' ')}}</div>
        T prom: ${{fmt(d.temp_prom)}}°C &nbsp;|&nbsp; H prom: ${{fmt(d.hum_prom)}}% &nbsp;|&nbsp; n=${{d.n ?? 0}}
      </div>`;
    }}).join('');

  }} catch(e) {{
    console.error('Error render:', e);
  }}
}}

initSensorCards();
initTabs();
render();
setInterval(render, 15000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asegurar_csv()
    print(f"[SERVER] Iniciando en http://0.0.0.0:{SERVER_PORT}")
    print(f"[SERVER] CSV → {CSV_PATH}")
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
