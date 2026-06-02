#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — Configuración centralizada del sistema de cultivo.
Editá este archivo para adaptar el sistema a tu hardware.
"""

# =============================================================================
# RED
# =============================================================================
# IP fija del Pi 3B+ (servidor). Asignala en tu router por MAC para que no cambie.
SERVER_IP   = "192.168.0.185"
SERVER_PORT = 5000

# =============================================================================
# HARDWARE — Pi Zero W
# =============================================================================
# Pines GPIO (numeración BCM) de los relés que controlan los ventiladores
RELAY_PINS = [17, 27]

# ¿El relé se activa con nivel LOW?
# - True  → activo bajo (típico en módulos con optoacoplador, IN1/IN2)
# - False → activo alto (menos común)
ACTIVE_LOW = True

# Pin de datos del DHT22
DHT_PIN = 4

# Intervalo entre lecturas del sensor (segundos)
SENSOR_INTERVAL_S = 30

# =============================================================================
# ESTANTERÍA
# =============================================================================
ALTO_M     = 1.65
ANCHO_M    = 0.92
PROFUNDO_M = 0.30
VOLUMEN_M3 = ALTO_M * ANCHO_M * PROFUNDO_M   # ≈ 0.4554 m³

# =============================================================================
# VENTILACIÓN — ACH base
# =============================================================================
ACH_OBJETIVO         = 12
CAUDAL_M3H_POR_FAN   = 16.99   # VD5010 5V ≈ 10 CFM → 16.99 m³/h
VENTANA_MIN          = 5

# =============================================================================
# OVERRIDE POR CONDICIONES — AND lógico
# =============================================================================
TEMP_UMBRAL_C    = 28.0
HUM_UMBRAL_PCT   = 70.0
HISTERESIS_TEMP  = 1.5
HISTERESIS_HUM   = 5.0

# =============================================================================
# ALMACENAMIENTO — Pi 3B+
# =============================================================================
CSV_PATH    = "/home/pi/cultivo_log.csv"
# Base de datos SQLite (historial persistente entre reinicios)
DB_PATH     = "/home/pi/cultivo.db"
# Cuántas lecturas conservar en la DB (0 = sin límite)
DB_MAX_ROWS = 50_000

# =============================================================================
# ALERTAS — Telegram (opcional)
# =============================================================================
TELEGRAM_TOKEN   = ""   # ej: "123456789:AABBCCxxx..."
TELEGRAM_CHAT_ID = ""   # ej: "987654321"
ALERTA_COOLDOWN_MIN = 15
