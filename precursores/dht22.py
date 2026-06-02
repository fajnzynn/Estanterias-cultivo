import Adafruit_DHT
import requests
import schedule
import time

sensor = Adafruit_DHT.DHT22
gpio = 4
server_url = "http://192.168.0.XXX:5000/sensor"  # IP de la RPi 3B+

def read_and_send():
    humidity, temperature = Adafruit_DHT.read_retry(sensor, gpio)
    if humidity is not None and temperature is not None:
        payload = {
            "temperature": round(temperature, 1),
            "humidity": round(humidity, 1)
        }
        try:
            response = requests.post(server_url, json=payload)
            print("Enviado:", payload, "Respuesta:", response.status_code)
        except Exception as e:
            print("Error al enviar:", e)
    else:
        print("Error al leer el sensor")

# Ejecutar cada 10 segundos
schedule.every(10).seconds.do(read_and_send)

while True:
    schedule.run_pending()
    time.sleep(1)
