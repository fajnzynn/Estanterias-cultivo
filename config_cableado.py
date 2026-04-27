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
# GPIO 27 → ventilador abajo izquierda
# GPIO 22 → ventilador arriba derecha
RELAY_PINS = [27, 22]

# ¿El relé se activa con nivel LOW?
# - True  → activo bajo  (típico en módulos con optoacoplador, IN1/IN2)
# - False → activo alto  (menos común)
# Para verificar: corré test_rele.py y seguí las instrucciones.
ACTIVE_LOW = True

# Pines de datos de los sensores DHT22 (numeración BCM)
# GPIO 5  → sensor arriba izquierda
# GPIO 6  → sensor abajo izquierda
# GPIO 4  → sensor arriba derecha
# GPIO 17 → sensor abajo derecha
DHT_PINS = [5, 6, 4, 17]
DHT_NOMBRES = [
    "arriba_izquierda",
    "abajo_izquierda",
    "arriba_derecha",
    "abajo_derecha",
]

# Pin principal (retrocompatibilidad)
DHT_PIN = DHT_PINS[0]

# Intervalo entre lecturas del sensor (segundos)
SENSOR_INTERVAL_S = 30

# =============================================================================
# ESTANTERÍA
# =============================================================================
ALTO_M      = 1.65
ANCHO_M     = 0.92
PROFUNDO_M  = 0.30
VOLUMEN_M3  = ALTO_M * ANCHO_M * PROFUNDO_M   # ≈ 0.4554 m³

# =============================================================================
# VENTILACIÓN — ACH base
# =============================================================================
# Renovaciones de aire por hora (Air Changes per Hour)
ACH_OBJETIVO = 12

# Caudal real de tus ventiladores (m³/h por unidad).
# VD5010 5V ≈ 10 CFM → 10 × 1.699 = 16.99 m³/h
# Ajustá si sabés el dato exacto de tu modelo.
CAUDAL_M3H_POR_FAN = 16.99

# Duración de la ventana de duty (minutos).
# El ciclo encendido/apagado se agrupa en esta ventana para no picar el relé.
VENTANA_MIN = 5

# =============================================================================
# OVERRIDE POR CONDICIONES — AND lógico (ambas deben superarse)
# =============================================================================
# Si temperatura AND humedad superan sus umbrales → ventiladores ON forzado.
# El sensor que disparó el override queda "dueño" hasta que AMBAS condiciones
# vuelvan a nivel seguro (con histéresis), evitando que lecturas de otro
# sensor lo anulen prematuramente.

TEMP_UMBRAL_C   = 28.0   # °C — encender si se supera este valor
HUM_UMBRAL_PCT  = 70.0   # %  — encender si se supera este valor
HISTERESIS_TEMP = 1.5    # °C — apagar override cuando temp baja X grados
HISTERESIS_HUM  = 5.0    # %  — apagar override cuando hum baja X puntos

# =============================================================================
# ALMACENAMIENTO — Pi 3B+
# =============================================================================
CSV_PATH = "/home/pi/cultivo_log.csv"

# =============================================================================
# ALERTAS — Telegram (opcional)
# =============================================================================
# Dejá en "" para deshabilitar.
TELEGRAM_TOKEN   = ""          # ej: "123456789:AABBCCxxx..."
TELEGRAM_CHAT_ID = ""          # ej: "987654321"

# Cuánto tiempo mínimo entre alertas del mismo tipo (minutos).
# Evita spam si la condición persiste.
ALERTA_COOLDOWN_MIN = 15
