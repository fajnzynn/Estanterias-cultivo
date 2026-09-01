# Roadmap — Migración a arquitectura Zigbee + MQTT + Docker

Plan de migración del sistema actual (scripts `.py` con control directo por GPIO en el Pi) a la nueva arquitectura (ESP32 + Zigbee + MQTT + InfluxDB + Grafana, dockerizada), sin interrumpir el control del cultivo en el proceso.

## 1. Infraestructura en paralelo (no toca el sistema actual)
- [ ] Instalar Docker y Docker Compose en la Raspberry Pi 3B+
- [ ] Levantar contenedor Mosquitto (broker MQTT)
- [ ] Levantar contenedor Zigbee2MQTT y emparejar el coordinador Zigbee
- [ ] Levantar contenedor InfluxDB
- [ ] Levantar contenedor Grafana, conectarlo a InfluxDB como fuente de datos
- [ ] Definir el esquema de tópicos MQTT (`camaras/{id}/sensores/...`, `.../actuadores/.../estado`, `.../comando`, `.../sistema/estado`)
- [ ] Definir política de retención/downsampling en InfluxDB

## 2. Validar el pipeline con datos simulados
- [ ] Script de prueba que publique valores falsos de temperatura/humedad/CO2 por MQTT
- [ ] Confirmar que los datos llegan a InfluxDB
- [ ] Armar el primer dashboard en Grafana con esos datos de prueba
- [ ] Configurar una alerta de prueba en Grafana (Telegram)

## 3. Firmware del ESP32 — solo sensado, sin control todavía
- [ ] Emparejar los sensores Zigbee de temperatura/humedad (×2) al coordinador
- [ ] Programar el ESP32 para leer el sensor de CO2 (MH-Z19B o SCD30/40) y publicar por MQTT
- [ ] Definir la máquina de estados (`NORMAL`, `SIN_RED`, `FALLA_SENSOR`, `CALIBRANDO`)
- [ ] Implementar buffer local para cuando se cae la red (guardar y reenviar al reconectar)
- [ ] Guardar configuración activa en flash (NVS) para sobrevivir reinicios

## 4. Migrar el control, un actuador a la vez
- [ ] Migrar ventilación (el de menor riesgo si falla) al ESP32, correr en paralelo al script viejo unos días
- [ ] Migrar humidificador
- [ ] Migrar manta térmica (control PID)
- [ ] Validar cada migración con un ciclo de cultivo real antes de pasar al siguiente actuador

## 5. Apagar el sistema viejo
- [ ] Confirmar que el ESP32 sostiene el control completo de forma estable
- [ ] Archivar/documentar los scripts `.py` viejos (no borrar, dejarlos de referencia)
- [ ] Actualizar el README del repo con la arquitectura nueva

## Roadmap (no bloqueante, para después de tener 1 cámara andando)
- [ ] Seguridad básica en Mosquitto (usuario/contraseña o TLS)
- [ ] Backups automáticos de los volúmenes Docker (InfluxDB, configs)
- [ ] Acceso remoto vía VPN (Tailscale) para que el profesor vea el dashboard desde afuera
- [ ] OTA (actualización de firmware por wifi) para cuando haya varias cámaras desplegadas
- [ ] Evaluar upgrade a Pi 4/5 si el 3B+ empieza a quedarse corto
