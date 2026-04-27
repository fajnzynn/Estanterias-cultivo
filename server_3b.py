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
    RELAY_PINS,
)

# ── Estado en memoria ──────────────────────────────────────────────────────
_lock  = threading.Lock()
_datos = deque(maxlen=500)   # últimas 500 lecturas en RAM

_ultima = {
    "ts":             None,
    "temperature":    None,
    "humidity":       None,
    "fans_on":        None,
    "modo":           None,
    "override_sensor": None,
}

# Alertas
_ultima_alerta = {
    "temp": 0.0,
    "hum":  0.0,
}

# ── CSV ────────────────────────────────────────────────────────────────────
def asegurar_csv():
    if not os.path.exists(CSV_PATH):
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True) if os.path.dirname(CSV_PATH) else None
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(
                ["timestamp", "date", "time", "temp_C", "hum_%",
                 "fans_on", "modo", "override_temp", "override_hum"]
            )

def guardar_csv(datos: dict):
    asegurar_csv()
    ts  = datetime.now()
    ov  = datos.get("override_sensor") or {}
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow([
            ts.isoformat(timespec="seconds"),
            ts.date().isoformat(),
            ts.strftime("%H:%M:%S"),
            datos.get("temperature", ""),
            datos.get("humidity", ""),
            int(bool(datos.get("fans_on"))),
            datos.get("modo", ""),
            ov.get("temp", ""),
            ov.get("hum", ""),
        ])

def promedios_hoy() -> dict:
    dia = date.today().isoformat()
    if not os.path.exists(CSV_PATH):
        return {"dia": dia, "temp_prom": None, "hum_prom": None, "n": 0}
    temps, hums = [], []
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            if row["date"] == dia:
                if row["temp_C"]:
                    try: temps.append(float(row["temp_C"]))
                    except: pass
                if row["hum_%"]:
                    try: hums.append(float(row["hum_%"]))
                    except: pass
    return {
        "dia":       dia,
        "temp_prom": round(sum(temps)/len(temps), 2) if temps else None,
        "hum_prom":  round(sum(hums)/len(hums),  2) if hums  else None,
        "n":         len(temps),
    }

# ── Telegram ───────────────────────────────────────────────────────────────
def enviar_telegram(mensaje: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        req_lib.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}, timeout=5)
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")

def check_alertas(temp, hum) -> None:
    ahora = time.time()
    cooldown = ALERTA_COOLDOWN_MIN * 60

    if temp is not None and temp > TEMP_UMBRAL_C:
        if ahora - _ultima_alerta["temp"] > cooldown:
            _ultima_alerta["temp"] = ahora
            enviar_telegram(
                f"🌡️ *ALERTA TEMPERATURA*\n"
                f"Valor actual: {temp:.1f}°C (umbral: {TEMP_UMBRAL_C}°C)\n"
                f"Ventiladores: {'ON' if _ultima.get('fans_on') else 'OFF'}"
            )

    if hum is not None and hum > HUM_UMBRAL_PCT:
        if ahora - _ultima_alerta["hum"] > cooldown:
            _ultima_alerta["hum"] = ahora
            enviar_telegram(
                f"💧 *ALERTA HUMEDAD*\n"
                f"Valor actual: {hum:.1f}% (umbral: {HUM_UMBRAL_PCT}%)\n"
                f"Ventiladores: {'ON' if _ultima.get('fans_on') else 'OFF'}"
            )

# ── Flask ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.post("/lectura")
def recibir_lectura():
    datos = request.get_json(force=True, silent=True)
    if not datos:
        return jsonify({"error": "payload vacío"}), 400

    ts = datetime.now().isoformat(timespec="seconds")
    with _lock:
        _ultima.update({**datos, "ts": ts})
        _datos.append({**datos, "ts": ts})

    guardar_csv(datos)

    temp = datos.get("temperature")
    hum  = datos.get("humidity")
    check_alertas(temp, hum)

    print(f"[RECIBIDO] {ts}  T={temp}°C  H={hum}%  modo={datos.get('modo')}")
    return jsonify({"status": "ok"}), 200


@app.get("/status")
def status():
    with _lock:
        return jsonify(_ultima)


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
    html = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cultivo Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:      #080f0a;
    --panel:   #0d1a10;
    --border:  #1a3320;
    --green:   #22c55e;
    --green2:  #16a34a;
    --amber:   #f59e0b;
    --red:     #ef4444;
    --muted:   #4b7260;
    --text:    #d1fae5;
    --mono:    'Space Mono', monospace;
    --sans:    'DM Sans', sans-serif;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    padding: 1.5rem;
  }

  /* ── header ── */
  header {
    display: flex;
    align-items: baseline;
    gap: 1rem;
    margin-bottom: 2rem;
    border-bottom: 1px solid var(--border);
    padding-bottom: 1rem;
  }
  header h1 {
    font-family: var(--mono);
    font-size: 1.1rem;
    letter-spacing: .1em;
    color: var(--green);
  }
  #last-update {
    font-size: .75rem;
    color: var(--muted);
    margin-left: auto;
    font-family: var(--mono);
  }
  .dot {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    margin-right: .4rem;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%,100% { opacity:1; }
    50%      { opacity:.3; }
  }

  /* ── grid ── */
  .grid-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.25rem 1rem;
    position: relative;
    overflow: hidden;
  }
  .card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--green);
    opacity: .4;
  }
  .card.alert-card::before { background: var(--red); opacity: .9; }
  .card-label {
    font-size: .65rem;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: var(--muted);
    font-family: var(--mono);
    margin-bottom: .5rem;
  }
  .card-value {
    font-family: var(--mono);
    font-size: 2rem;
    font-weight: 700;
    line-height: 1;
    color: var(--green);
  }
  .card-value.warn  { color: var(--amber); }
  .card-value.alert { color: var(--red); }
  .card-sub {
    font-size: .7rem;
    color: var(--muted);
    margin-top: .4rem;
    font-family: var(--mono);
  }

  /* ── badge modo ── */
  .badge {
    display: inline-block;
    font-family: var(--mono);
    font-size: .7rem;
    padding: .25rem .6rem;
    border-radius: 3px;
    background: var(--border);
    color: var(--green);
    letter-spacing: .08em;
  }
  .badge.override { background: #7c2d12; color: #fca5a5; }

  /* ── chart ── */
  .chart-wrap {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.25rem;
    margin-bottom: 1.5rem;
  }
  .chart-title {
    font-family: var(--mono);
    font-size: .7rem;
    letter-spacing: .1em;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 1rem;
  }

  /* ── promedios ── */
  .avg-row {
    display: flex;
    gap: .75rem;
    flex-wrap: wrap;
    margin-bottom: 1.5rem;
  }
  .avg-pill {
    font-family: var(--mono);
    font-size: .72rem;
    padding: .3rem .8rem;
    border: 1px solid var(--border);
    border-radius: 20px;
    color: var(--text);
  }

  /* ── acciones ── */
  .actions { display: flex; gap: .75rem; flex-wrap: wrap; }
  .btn {
    font-family: var(--mono);
    font-size: .72rem;
    letter-spacing: .08em;
    padding: .45rem 1rem;
    border-radius: 4px;
    border: 1px solid var(--green);
    color: var(--green);
    background: transparent;
    text-decoration: none;
    cursor: pointer;
    transition: background .15s, color .15s;
  }
  .btn:hover { background: var(--green); color: var(--bg); }

  /* ── override info ── */
  #override-box {
    display: none;
    background: #1c0a0a;
    border: 1px solid #7c2d12;
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 1.5rem;
    font-family: var(--mono);
    font-size: .75rem;
    color: #fca5a5;
  }
</style>
</head>
<body>

<header>
  <h1><span class="dot"></span>CULTIVO MONITOR</h1>
  <span id="last-update">esperando datos…</span>
</header>

<div class="grid-cards">
  <div class="card" id="card-temp">
    <div class="card-label">Temperatura</div>
    <div class="card-value" id="val-temp">--</div>
    <div class="card-sub" id="sub-temp">°C</div>
  </div>
  <div class="card" id="card-hum">
    <div class="card-label">Humedad</div>
    <div class="card-value" id="val-hum">--</div>
    <div class="card-sub" id="sub-hum">%</div>
  </div>
  <div class="card">
    <div class="card-label">Ventiladores</div>
    <div class="card-value" id="val-fans">--</div>
    <div class="card-sub" id="val-modo">--</div>
  </div>
  <div class="card">
    <div class="card-label">Modo</div>
    <div id="val-badge" class="badge">--</div>
    <div class="card-sub" id="val-ts" style="margin-top:.6rem">--</div>
  </div>
</div>

<div id="override-box">
  ⚠ OVERRIDE activo · disparado por T=<span id="ov-temp">--</span>°C  H=<span id="ov-hum">--</span>%
</div>

<div class="avg-row" id="avg-row">
  <span class="avg-pill">Prom. hoy — cargando…</span>
</div>

<div class="chart-wrap">
  <div class="chart-title">Historial reciente — temperatura & humedad</div>
  <canvas id="chart" height="120"></canvas>
</div>

<div class="actions">
  <a class="btn" href="/csv">⬇ Descargar CSV</a>
  <a class="btn" href="/hoy" target="_blank">Promedios JSON</a>
  <a class="btn" href="/health" target="_blank">Health check</a>
</div>

<script>
let chart;

const TEMP_UMBRAL = """ + str(TEMP_UMBRAL_C) + r""";
const HUM_UMBRAL  = """ + str(HUM_UMBRAL_PCT) + r""";

function fmt(v, dec=1) { return v !== null && v !== undefined ? (+v).toFixed(dec) : '--'; }

function colorTemp(v) {
  if (v === null) return 'var(--green)';
  if (v > TEMP_UMBRAL + 2) return 'var(--red)';
  if (v > TEMP_UMBRAL)     return 'var(--amber)';
  return 'var(--green)';
}
function colorHum(v) {
  if (v === null) return 'var(--green)';
  if (v > HUM_UMBRAL + 5) return 'var(--red)';
  if (v > HUM_UMBRAL)     return 'var(--amber)';
  return 'var(--green)';
}

async function fetchJSON(url) {
  const r = await fetch(url);
  return r.json();
}

async function render() {
  try {
    const [st, hist, hoy] = await Promise.all([
      fetchJSON('/status'),
      fetchJSON('/historico?n=120'),
      fetchJSON('/hoy'),
    ]);

    // ── Cards ──
    const t = st.temperature, h = st.humidity;

    document.getElementById('val-temp').textContent = fmt(t) + '°';
    document.getElementById('val-temp').style.color = colorTemp(t);
    document.getElementById('val-hum').textContent  = fmt(h) + '%';
    document.getElementById('val-hum').style.color  = colorHum(h);

    document.getElementById('val-fans').textContent = st.fans_on ? 'ON' : 'OFF';
    document.getElementById('val-fans').style.color = st.fans_on ? 'var(--green)' : 'var(--muted)';
    document.getElementById('val-modo').textContent = st.modo || '--';

    const badge = document.getElementById('val-badge');
    badge.textContent = (st.modo || '--').toUpperCase();
    badge.className = 'badge' + (st.modo === 'OVERRIDE' ? ' override' : '');

    document.getElementById('val-ts').textContent = st.ts ? st.ts.slice(11,19) : '--';
    document.getElementById('last-update').textContent =
      'actualizado ' + new Date().toLocaleTimeString('es-AR');

    // ── Override box ──
    const ovBox = document.getElementById('override-box');
    if (st.modo === 'OVERRIDE' && st.override_sensor) {
      ovBox.style.display = 'block';
      document.getElementById('ov-temp').textContent = fmt(st.override_sensor.temp);
      document.getElementById('ov-hum').textContent  = fmt(st.override_sensor.hum);
    } else {
      ovBox.style.display = 'none';
    }

    // ── Promedios ──
    const avgRow = document.getElementById('avg-row');
    if (hoy.n > 0) {
      avgRow.innerHTML = `
        <span class="avg-pill">Prom. T hoy: ${fmt(hoy.temp_prom)}°C</span>
        <span class="avg-pill">Prom. H hoy: ${fmt(hoy.hum_prom)}%</span>
        <span class="avg-pill">Muestras: ${hoy.n}</span>
      `;
    }

    // ── Gráfico ──
    const labels = hist.map(d => d.ts ? d.ts.slice(11,16) : '');
    const dataT  = hist.map(d => d.temperature);
    const dataH  = hist.map(d => d.humidity);
    const dataFans = hist.map(d => d.fans_on ? 1 : 0);

    if (!chart) {
      const ctx = document.getElementById('chart').getContext('2d');
      chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: 'Temp (°C)',
              data: dataT,
              borderColor: '#22c55e',
              backgroundColor: 'rgba(34,197,94,.08)',
              fill: true,
              borderWidth: 2,
              tension: .3,
              pointRadius: 0,
              spanGaps: true,
              yAxisID: 'yT',
            },
            {
              label: 'Humedad (%)',
              data: dataH,
              borderColor: '#38bdf8',
              backgroundColor: 'rgba(56,189,248,.06)',
              fill: true,
              borderWidth: 2,
              tension: .3,
              pointRadius: 0,
              spanGaps: true,
              yAxisID: 'yH',
            },
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: '#4b7260', font: { family: 'Space Mono', size: 11 } } },
            tooltip: {
              backgroundColor: '#0d1a10',
              borderColor: '#1a3320',
              borderWidth: 1,
              titleColor: '#d1fae5',
              bodyColor: '#4b7260',
            }
          },
          scales: {
            x: {
              ticks: { color: '#4b7260', font: { family: 'Space Mono', size: 10 }, maxTicksLimit: 12 },
              grid:  { color: 'rgba(26,51,32,.6)' },
            },
            yT: {
              position: 'left',
              ticks: { color: '#22c55e', font: { family: 'Space Mono', size: 10 } },
              grid:  { color: 'rgba(26,51,32,.6)' },
            },
            yH: {
              position: 'right',
              ticks: { color: '#38bdf8', font: { family: 'Space Mono', size: 10 } },
              grid:  { display: false },
            },
          }
        }
      });
    } else {
      chart.data.labels = labels;
      chart.data.datasets[0].data = dataT;
      chart.data.datasets[1].data = dataH;
      chart.update('none');
    }

  } catch(e) {
    console.error('Error actualizando dashboard:', e);
  }
}

render();
setInterval(render, 15000);  // actualizar cada 15 segundos
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
