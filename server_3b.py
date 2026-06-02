#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_3b.py — Pi 3B+  (v2)

Mejoras respecto a v1:
  1. SQLite reemplaza el deque en RAM → historial persiste entre reinicios.
     El CSV se sigue generando igual (compatibilidad hacia atrás).
  2. Endpoint único /dashboard-data devuelve status + histórico + promedios
     en un solo request, reduciendo carga en la Pi.
  3. SSE (/stream) — el dashboard recibe push en lugar de polling cada 15 s.
     Cada vez que llega una lectura el servidor notifica a todos los clientes
     suscritos; no hay polling del browser al servidor.
  4. Waitress reemplaza app.run() de Flask para uso en producción.

Dependencias:
    pip3 install flask requests waitress
    (sqlite3 viene con Python estándar — no necesita instalación)

Arranque como servicio: igual que antes, cambiá ExecStart a:
    /usr/bin/python3 /home/pi/cultivo/server_3b.py
"""

import os
import csv
import time
import queue
import sqlite3
import threading
import requests as req_lib
from contextlib import contextmanager
from datetime import datetime, date
from flask import Flask, jsonify, request, send_file, abort, Response

from config import (
    SERVER_PORT,
    CSV_PATH, DB_PATH, DB_MAX_ROWS,
    TEMP_UMBRAL_C, HUM_UMBRAL_PCT,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ALERTA_COOLDOWN_MIN,
    ACH_OBJETIVO, VENTANA_MIN, CAUDAL_M3H_POR_FAN, VOLUMEN_M3,
    RELAY_PINS,
)

# ── SQLite ─────────────────────────────────────────────────────────────────
# check_same_thread=False es seguro porque usamos un lock propio (_db_lock)
# para serializar todos los accesos.
_db_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None

def db_init():
    """Crea la tabla si no existe y abre la conexión global."""
    global _db_conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db_conn.row_factory = sqlite3.Row
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS lecturas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            date         TEXT    NOT NULL,
            temperature  REAL,
            humidity     REAL,
            fans_on      INTEGER,
            modo         TEXT,
            override_temp REAL,
            override_hum  REAL
        )
    """)
    _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON lecturas(date)")
    _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_ts   ON lecturas(ts)")
    _db_conn.commit()
    print(f"[DB] SQLite abierta en {DB_PATH}")

@contextmanager
def db():
    """Context manager que serializa el acceso a la conexión global."""
    with _db_lock:
        yield _db_conn

def db_insertar(datos: dict):
    ts  = datetime.now().isoformat(timespec="seconds")
    dia = ts[:10]
    ov  = datos.get("override_sensor") or {}
    with db() as con:
        con.execute(
            """INSERT INTO lecturas
               (ts, date, temperature, humidity, fans_on, modo, override_temp, override_hum)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                ts, dia,
                datos.get("temperature"),
                datos.get("humidity"),
                int(bool(datos.get("fans_on"))),
                datos.get("modo", ""),
                ov.get("temp"),
                ov.get("hum"),
            )
        )
        con.commit()
    _purgar_si_necesario()

def _purgar_si_necesario():
    """Elimina las filas más viejas si se supera DB_MAX_ROWS (0 = sin límite)."""
    if DB_MAX_ROWS <= 0:
        return
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM lecturas").fetchone()[0]
        if total > DB_MAX_ROWS:
            exceso = total - DB_MAX_ROWS
            con.execute(
                "DELETE FROM lecturas WHERE id IN "
                "(SELECT id FROM lecturas ORDER BY id ASC LIMIT ?)", (exceso,)
            )
            con.commit()

def db_historico(n: int = 120) -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM lecturas ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]

def db_promedios_hoy() -> dict:
    dia = date.today().isoformat()
    with db() as con:
        row = con.execute(
            """SELECT
                   AVG(temperature) AS temp_prom,
                   AVG(humidity)    AS hum_prom,
                   COUNT(*)         AS n
               FROM lecturas WHERE date = ?""",
            (dia,)
        ).fetchone()
    return {
        "dia":       dia,
        "temp_prom": round(row["temp_prom"], 2) if row["temp_prom"] is not None else None,
        "hum_prom":  round(row["hum_prom"],  2) if row["hum_prom"]  is not None else None,
        "n":         row["n"],
    }

# ── Estado en memoria (última lectura) ────────────────────────────────────
_state_lock = threading.Lock()
_ultima = {
    "ts":              None,
    "temperature":     None,
    "humidity":        None,
    "fans_on":         None,
    "modo":            None,
    "override_sensor": None,
}

# ── SSE — cola de eventos ──────────────────────────────────────────────────
# Cada cliente SSE recibe su propia queue. Cuando llega una lectura nueva
# se pone un evento en todas las queues activas.
_sse_lock    = threading.Lock()
_sse_clients: list[queue.Queue] = []

def _sse_broadcast(datos: dict):
    """Notifica a todos los clientes SSE suscritos."""
    import json
    msg = f"data: {json.dumps(datos)}\n\n"
    with _sse_lock:
        muertos = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                muertos.append(q)
        for q in muertos:
            _sse_clients.remove(q)

# ── CSV (se mantiene para compatibilidad) ─────────────────────────────────
def _asegurar_csv():
    if not os.path.exists(CSV_PATH):
        if os.path.dirname(CSV_PATH):
            os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "date", "time", "temp_C", "hum_%",
                "fans_on", "modo", "override_temp", "override_hum"
            ])

def _guardar_csv(datos: dict):
    _asegurar_csv()
    ts = datetime.now()
    ov = datos.get("override_sensor") or {}
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

# ── Telegram ───────────────────────────────────────────────────────────────
_ultima_alerta = {"temp": 0.0, "hum": 0.0}

def _check_alertas(temp, hum) -> None:
    ahora   = time.time()
    cooldown = ALERTA_COOLDOWN_MIN * 60
    fans_str = "ON" if _ultima.get("fans_on") else "OFF"

    if temp is not None and temp > TEMP_UMBRAL_C:
        if ahora - _ultima_alerta["temp"] > cooldown:
            _ultima_alerta["temp"] = ahora
            _enviar_telegram(
                f"🌡️ *ALERTA TEMPERATURA*\n"
                f"Valor: {temp:.1f}°C (umbral: {TEMP_UMBRAL_C}°C)\n"
                f"Ventiladores: {fans_str}"
            )
    if hum is not None and hum > HUM_UMBRAL_PCT:
        if ahora - _ultima_alerta["hum"] > cooldown:
            _ultima_alerta["hum"] = ahora
            _enviar_telegram(
                f"💧 *ALERTA HUMEDAD*\n"
                f"Valor: {hum:.1f}% (umbral: {HUM_UMBRAL_PCT}%)\n"
                f"Ventiladores: {fans_str}"
            )

def _enviar_telegram(mensaje: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        req_lib.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje,
                                "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")

# ── Flask ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.post("/lectura")
def recibir_lectura():
    datos = request.get_json(force=True, silent=True)
    if not datos:
        return jsonify({"error": "payload vacío"}), 400

    ts = datetime.now().isoformat(timespec="seconds")

    with _state_lock:
        _ultima.update({**datos, "ts": ts})

    db_insertar(datos)
    _guardar_csv(datos)
    _check_alertas(datos.get("temperature"), datos.get("humidity"))
    _sse_broadcast({**datos, "ts": ts})

    print(f"[RECIBIDO] {ts} T={datos.get('temperature')}°C "
          f"H={datos.get('humidity')}% modo={datos.get('modo')}")
    return jsonify({"status": "ok"}), 200

@app.get("/status")
def status():
    with _state_lock:
        return jsonify(_ultima)

@app.get("/historico")
def historico():
    n = request.args.get("n", default=120, type=int)
    return jsonify(db_historico(n))

@app.get("/hoy")
def hoy():
    return jsonify(db_promedios_hoy())

@app.get("/dashboard-data")
def dashboard_data():
    """
    Endpoint único que devuelve todo lo que el dashboard necesita en un request.
    Reemplaza los 3 fetches paralelos de la v1 (/status + /historico + /hoy).
    """
    n = request.args.get("n", default=120, type=int)
    with _state_lock:
        ultima = dict(_ultima)
    return jsonify({
        "status":    ultima,
        "historico": db_historico(n),
        "hoy":       db_promedios_hoy(),
    })

@app.get("/stream")
def stream():
    """
    Server-Sent Events: el cliente se suscribe aquí y recibe un evento
    cada vez que llega una lectura nueva. No hay polling del browser.
    """
    def event_stream(q: queue.Queue):
        # Enviar estado actual al conectarse para que el dashboard
        # no quede en blanco mientras espera la primera lectura
        import json
        with _state_lock:
            inicial = dict(_ultima)
        yield f"data: {json.dumps(inicial)}\n\n"

        while True:
            try:
                msg = q.get(timeout=30)
                yield msg
            except queue.Empty:
                # Heartbeat para mantener la conexión viva
                yield ": heartbeat\n\n"

    q: queue.Queue = queue.Queue(maxsize=10)
    with _sse_lock:
        _sse_clients.append(q)

    return Response(
        event_stream(q),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",   # necesario si hay nginx adelante
        }
    )

@app.get("/csv")
def descargar_csv():
    if not os.path.exists(CSV_PATH):
        abort(404, description="CSV no encontrado aún")
    return send_file(CSV_PATH, as_attachment=True)

@app.get("/health")
def health():
    with db() as con:
        total_lecturas = con.execute("SELECT COUNT(*) FROM lecturas").fetchone()[0]
    return jsonify({
        "ok":              True,
        "csv_existe":      os.path.exists(CSV_PATH),
        "lecturas_en_db":  total_lecturas,
        "clientes_sse":    len(_sse_clients),
        "config": {
            "ACH_OBJETIVO":   ACH_OBJETIVO,
            "TEMP_UMBRAL_C":  TEMP_UMBRAL_C,
            "HUM_UMBRAL_PCT": HUM_UMBRAL_PCT,
            "VENTANA_MIN":    VENTANA_MIN,
        }
    })

# ── Dashboard ──────────────────────────────────────────────────────────────
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
  --bg:     #080f0a;
  --panel:  #0d1a10;
  --border: #1a3320;
  --green:  #22c55e;
  --amber:  #f59e0b;
  --red:    #ef4444;
  --muted:  #4b7260;
  --text:   #d1fae5;
  --mono:   'Space Mono', monospace;
  --sans:   'DM Sans', sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding:1.5rem}

/* header */
header{display:flex;align-items:baseline;gap:1rem;margin-bottom:2rem;border-bottom:1px solid var(--border);padding-bottom:1rem}
header h1{font-family:var(--mono);font-size:1.1rem;letter-spacing:.1em;color:var(--green)}
#last-update{font-size:.75rem;color:var(--muted);margin-left:auto;font-family:var(--mono)}
#conn-status{font-size:.7rem;font-family:var(--mono);padding:.2rem .5rem;border-radius:3px}
#conn-status.live{background:#14532d;color:#86efac}
#conn-status.off{background:#450a0a;color:#fca5a5}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:.4rem;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* cards */
.grid-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:1.5rem}
.card{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:1.25rem 1rem;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--green);opacity:.4}
.card-label{font-size:.65rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-family:var(--mono);margin-bottom:.5rem}
.card-value{font-family:var(--mono);font-size:2rem;font-weight:700;line-height:1;color:var(--green)}
.card-value.warn{color:var(--amber)}
.card-value.alert{color:var(--red)}
.card-sub{font-size:.7rem;color:var(--muted);margin-top:.4rem;font-family:var(--mono)}
.badge{display:inline-block;font-family:var(--mono);font-size:.7rem;padding:.25rem .6rem;border-radius:3px;background:var(--border);color:var(--green);letter-spacing:.08em}
.badge.override{background:#7c2d12;color:#fca5a5}

/* chart */
.chart-wrap{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:1.25rem;margin-bottom:1.5rem}
.chart-title{font-family:var(--mono);font-size:.7rem;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:1rem}

/* promedios */
.avg-row{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1.5rem}
.avg-pill{font-family:var(--mono);font-size:.72rem;padding:.3rem .8rem;border:1px solid var(--border);border-radius:20px;color:var(--text)}

/* acciones */
.actions{display:flex;gap:.75rem;flex-wrap:wrap}
.btn{font-family:var(--mono);font-size:.72rem;letter-spacing:.08em;padding:.45rem 1rem;border-radius:4px;border:1px solid var(--green);color:var(--green);background:transparent;text-decoration:none;cursor:pointer;transition:background .15s,color .15s}
.btn:hover{background:var(--green);color:var(--bg)}

/* override */
#override-box{display:none;background:#1c0a0a;border:1px solid #7c2d12;border-radius:6px;padding:1rem;margin-bottom:1.5rem;font-family:var(--mono);font-size:.75rem;color:#fca5a5}
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>CULTIVO MONITOR</h1>
  <span id="conn-status" class="off">sin señal</span>
  <span id="last-update">esperando datos…</span>
</header>

<div class="grid-cards">
  <div class="card">
    <div class="card-label">Temperatura</div>
    <div class="card-value" id="val-temp">--</div>
    <div class="card-sub">°C</div>
  </div>
  <div class="card">
    <div class="card-label">Humedad</div>
    <div class="card-value" id="val-hum">--</div>
    <div class="card-sub">%</div>
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
  ⚠ OVERRIDE activo · disparado por T=<span id="ov-temp">--</span>°C H=<span id="ov-hum">--</span>%
</div>

<div class="avg-row" id="avg-row">
  <span class="avg-pill">Cargando promedios…</span>
</div>

<div class="chart-wrap">
  <div class="chart-title">Historial reciente — temperatura &amp; humedad</div>
  <canvas id="chart" height="120"></canvas>
</div>

<div class="actions">
  <a class="btn" href="/csv">⬇ Descargar CSV</a>
  <a class="btn" href="/hoy" target="_blank">Promedios JSON</a>
  <a class="btn" href="/health" target="_blank">Health check</a>
</div>

<script>
const TEMP_UMBRAL = """ + str(TEMP_UMBRAL_C) + r""";
const HUM_UMBRAL  = """ + str(HUM_UMBRAL_PCT) + r""";
let chart = null;
let histData = [];

function fmt(v, dec=1){ return (v !== null && v !== undefined) ? (+v).toFixed(dec) : '--'; }
function colorTemp(v){ return v>TEMP_UMBRAL+2?'var(--red)':v>TEMP_UMBRAL?'var(--amber)':'var(--green)'; }
function colorHum(v){  return v>HUM_UMBRAL+5 ?'var(--red)':v>HUM_UMBRAL ?'var(--amber)':'var(--green)'; }

function actualizarCards(st){
  const t = st.temperature, h = st.humidity;
  const vt = document.getElementById('val-temp');
  vt.textContent = fmt(t) + '°';
  vt.style.color = colorTemp(t);
  const vh = document.getElementById('val-hum');
  vh.textContent = fmt(h) + '%';
  vh.style.color = colorHum(h);
  const vf = document.getElementById('val-fans');
  vf.textContent = st.fans_on ? 'ON' : 'OFF';
  vf.style.color = st.fans_on ? 'var(--green)' : 'var(--muted)';
  document.getElementById('val-modo').textContent = st.modo || '--';
  const badge = document.getElementById('val-badge');
  badge.textContent = (st.modo||'--').toUpperCase();
  badge.className = 'badge' + (st.modo==='OVERRIDE'?' override':'');
  document.getElementById('val-ts').textContent = st.ts ? st.ts.slice(11,19) : '--';
  document.getElementById('last-update').textContent =
    'actualizado ' + new Date().toLocaleTimeString('es-AR');

  const ovBox = document.getElementById('override-box');
  if(st.modo==='OVERRIDE' && st.override_sensor){
    ovBox.style.display='block';
    document.getElementById('ov-temp').textContent = fmt(st.override_sensor.temp);
    document.getElementById('ov-hum').textContent  = fmt(st.override_sensor.hum);
  } else { ovBox.style.display='none'; }
}

function actualizarPromedios(hoy){
  const row = document.getElementById('avg-row');
  if(hoy.n > 0){
    row.innerHTML = `
      <span class="avg-pill">Prom. T hoy: ${fmt(hoy.temp_prom)}°C</span>
      <span class="avg-pill">Prom. H hoy: ${fmt(hoy.hum_prom)}%</span>
      <span class="avg-pill">Muestras: ${hoy.n}</span>`;
  }
}

function renderChart(hist){
  const labels = hist.map(d => d.ts ? d.ts.slice(11,16) : '');
  const dataT  = hist.map(d => d.temperature);
  const dataH  = hist.map(d => d.humidity);
  if(!chart){
    const ctx = document.getElementById('chart').getContext('2d');
    chart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: [
        { label:'Temp (°C)', data:dataT, borderColor:'#22c55e',
          backgroundColor:'rgba(34,197,94,.08)', fill:true, borderWidth:2,
          tension:.3, pointRadius:0, spanGaps:true, yAxisID:'yT' },
        { label:'Humedad (%)', data:dataH, borderColor:'#38bdf8',
          backgroundColor:'rgba(56,189,248,.06)', fill:true, borderWidth:2,
          tension:.3, pointRadius:0, spanGaps:true, yAxisID:'yH' },
      ]},
      options:{
        responsive:true, maintainAspectRatio:false,
        interaction:{mode:'index',intersect:false},
        plugins:{
          legend:{labels:{color:'#4b7260',font:{family:'Space Mono',size:11}}},
          tooltip:{backgroundColor:'#0d1a10',borderColor:'#1a3320',borderWidth:1,
                   titleColor:'#d1fae5',bodyColor:'#4b7260'}
        },
        scales:{
          x:{ ticks:{color:'#4b7260',font:{family:'Space Mono',size:10},maxTicksLimit:12},
              grid:{color:'rgba(26,51,32,.6)'} },
          yT:{ position:'left',  ticks:{color:'#22c55e',font:{family:'Space Mono',size:10}},
               grid:{color:'rgba(26,51,32,.6)'} },
          yH:{ position:'right', ticks:{color:'#38bdf8',font:{family:'Space Mono',size:10}},
               grid:{display:false} },
        }
      }
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = dataT;
    chart.data.datasets[1].data = dataH;
    chart.update('none');
  }
}

// ── Carga inicial: status + historial + promedios en un solo request ──────
async function cargaInicial(){
  try {
    const r    = await fetch('/dashboard-data?n=120');
    const data = await r.json();
    actualizarCards(data.status);
    actualizarPromedios(data.hoy);
    histData = data.historico;
    renderChart(histData);
  } catch(e){ console.error('cargaInicial:', e); }
}

// ── SSE: actualizaciones en tiempo real sin polling ───────────────────────
function conectarSSE(){
  const es = new EventSource('/stream');

  es.onopen = () => {
    const cs = document.getElementById('conn-status');
    cs.textContent = 'en vivo'; cs.className = 'conn-status live';
  };

  es.onmessage = (e) => {
    const st = JSON.parse(e.data);
    if(!st.ts) return;                    // heartbeat / estado vacío inicial
    actualizarCards(st);

    // Agregar al historial y refrescar gráfico
    histData.push(st);
    if(histData.length > 120) histData.shift();
    renderChart(histData);

    // Refrescar promedios cada 10 lecturas para no sobrecargar la DB
    if(histData.length % 10 === 0){
      fetch('/hoy').then(r=>r.json()).then(actualizarPromedios).catch(()=>{});
    }
  };

  es.onerror = () => {
    const cs = document.getElementById('conn-status');
    cs.textContent = 'reconectando…'; cs.className = 'conn-status off';
    // EventSource reconecta automáticamente; no hace falta lógica extra
  };
}

cargaInicial();
conectarSSE();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    db_init()
    _asegurar_csv()

    print(f"[SERVER] Iniciando con Waitress en http://0.0.0.0:{SERVER_PORT}")
    print(f"[SERVER] DB  → {DB_PATH}")
    print(f"[SERVER] CSV → {CSV_PATH}")

    # Waitress es un servidor WSGI de producción; reemplaza app.run() de Flask.
    # Soporta múltiples hilos, manejo correcto de conexiones y no muestra
    # el warning de "development server" de Flask.
    from waitress import serve
    serve(app, host="0.0.0.0", port=SERVER_PORT, threads=8)
