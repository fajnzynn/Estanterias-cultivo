# Sistema de Monitoreo de Cultivo
## Raspberry Pi Zero W + Pi 3B+

---

## Arquitectura

```
[Pi Zero W]                          [Pi 3B+]
  DHT22 sensor                         Flask server
  Relé → ventiladores   ──HTTP──►      Dashboard web
  ACH + override logic                 CSV export
                                        Alertas Telegram
```

---

## Instalación

### En ambas Pis — copiar config.py
```bash
# Editá la IP del 3B+ en config.py antes de cualquier cosa
nano config.py
```

### Pi Zero W

```bash
sudo apt-get update
sudo apt-get install -y python3-pip libgpiod2
pip3 install adafruit-circuitpython-dht RPi.GPIO requests
```

Ejecutar:
```bash
python3 zero_w.py
```

Ejecutar como servicio (arranque automático):
```bash
sudo nano /etc/systemd/system/cultivo-zero.service
```
```ini
[Unit]
Description=Cultivo Zero W
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/cultivo/zero_w.py
WorkingDirectory=/home/pi/cultivo
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable cultivo-zero
sudo systemctl start cultivo-zero
```

---

### Pi 3B+

```bash
pip3 install flask requests
```

Ejecutar:
```bash
python3 server_3b.py
```

Dashboard disponible en: `http://<IP_3B+>:5000`

Ejecutar como servicio:
```bash
sudo nano /etc/systemd/system/cultivo-server.service
```
```ini
[Unit]
Description=Cultivo Server 3B+
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/cultivo/server_3b.py
WorkingDirectory=/home/pi/cultivo
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable cultivo-server
sudo systemctl start cultivo-server
```

---

## Endpoints del servidor (Pi 3B+)

| URL | Descripción |
|-----|-------------|
| `/` | Dashboard web en tiempo real |
| `/status` | Última lectura (JSON) |
| `/historico?n=120` | Últimas N lecturas en RAM (JSON) |
| `/hoy` | Promedios del día (JSON) |
| `/csv` | Descargar CSV completo |
| `/health` | Estado del sistema |

---

## Configuración de alertas Telegram

1. Creá un bot con [@BotFather](https://t.me/botfather) → copiá el token
2. Escribile al bot para activarlo, luego abrí:  
   `https://api.telegram.org/bot<TOKEN>/getUpdates`  
   para obtener tu `chat_id`
3. Pegá ambos valores en `config.py`:
   ```python
   TELEGRAM_TOKEN   = "123456:ABCdef..."
   TELEGRAM_CHAT_ID = "987654321"
   ```

---

## Verificar HIGH/LOW del relé

Si los ventiladores están siempre encendidos (o nunca encienden), invertí esta línea en `config.py`:

```python
ACTIVE_LOW = True   # probá False si está al revés
```

Los módulos relé con optoacoplador (IN1/IN2 + JD-VCC) típicamente son **activo bajo** (`ACTIVE_LOW = True`).

---

## Agregar la humidificadora (futuro)

En `config.py` agregás:
```python
HUMIDIFICADOR_PIN = 22
HUM_MIN_PCT = 55.0   # encender si baja de este valor
```

En `zero_w.py` ya está previsto el hilo de ventilación separado del sensor — solo hay que agregar un `hilo_humidificador()` similar.
