import RPi.GPIO as GPIO
import time

# --- Configuración de pines ---
VENTILADORES = {
    "ventilador_1": 20,
    "ventilador_2": 21,
}

# --- Inicialización ---
def setup():
    GPIO.setmode(GPIO.BCM)
    for pin in VENTILADORES.values():
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.HIGH)  # Apagado al inicio

# --- Encender ventiladores ---
def encender_todos():
    for nombre, pin in VENTILADORES.items():
        GPIO.output(pin, GPIO.HIGH)
        print(f"{nombre} encendido.")

# --- Apagar ventiladores ---
def apagar_todos():
    for nombre, pin in VENTILADORES.items():
        GPIO.output(pin, GPIO.LOW)
        print(f"{nombre} apagado.")

# --- Ciclo de prueba ---
def ciclo_prueba():
    setup()
    try:
        while True:
            print("Esperando 1 minuto antes de encender ventiladores...")
            time.sleep(15)

            print("Encendiendo ventiladores por 3 minutos...")
            encender_todos()
            time.sleep(30)

            print("Apagando ventiladores.")
            apagar_todos()

    except KeyboardInterrupt:
        print("Interrumpido por el usuario.")
    finally:
        GPIO.cleanup()

# --- Ejecutar ---
if __name__ == "__main__":
    ciclo_prueba()
