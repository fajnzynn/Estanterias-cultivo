import RPi.GPIO as GPIO
import time

# --- Configuración de pines ---
VENTILADORES = {"ventilador_1": 17, "ventilador_2": 27}

def setup():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in VENTILADORES.values():
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)  # apagados al inicio

def encender_todos():
    for nombre, pin in VENTILADORES.items():
        GPIO.output(pin, GPIO.HIGH)  # activo en alto
        print(f"{nombre} encendido.")

def apagar_todos():
    for nombre, pin in VENTILADORES.items():
        GPIO.output(pin, GPIO.LOW)   # apagado
        print(f"{nombre} apagado.")

def ciclo():
    setup()
    try:
        while True:
            print("Esperando 10 segundos...")
            time.sleep(10)

            print("Encendiendo ventiladores por 15 segundos...")
            encender_todos()
            time.sleep(15)

            print("Apagando ventiladores.")
            apagar_todos()
    except KeyboardInterrupt:
        print("Interrumpido por el usuario.")
    finally:
        GPIO.cleanup()

if __name__ == "__main__":
    ciclo()
