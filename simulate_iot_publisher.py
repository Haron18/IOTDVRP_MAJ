"""
Script à exécuter SÉPARÉMENT (dans un second terminal, pas dans Streamlit) pour
simuler des boîtiers GPS/IoT embarqués qui publient de la télémétrie en direct
sur le broker MQTT public.

Usage :
    python simulate_iot_publisher.py
"""

import json
import time

import numpy as np
import paho.mqtt.client as mqtt

BROKER = "broker.hivemq.com"
PORT = 1883
VEHICLE_IDS = ["V1", "V2", "V3", "V4"]
EVENT_TYPES = ["GPS_MISE_A_JOUR", "ALERTE_TEMPERATURE", "EMBOUTEILLAGE", "NOUVELLE_COMMANDE", "PANNE_VEHICULE"]
EVENT_WEIGHTS = [0.55, 0.15, 0.15, 0.1, 0.05]

DEPOT = (36.7025, 3.1612)  # Oued Smar, Alger


def main() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()
    print("🚀 Simulation des boîtiers GPS/IoT démarrée (Ctrl+C pour arrêter)...")

    rng = np.random.default_rng()
    try:
        while True:
            v_id = str(rng.choice(VEHICLE_IDS[:2]))  # 2 véhicules actifs par défaut
            event_type = str(rng.choice(EVENT_TYPES, p=EVENT_WEIGHTS))

            payload = {
                "vehicle_id": v_id,
                "timestamp": time.time(),
                "gps": {
                    "lat": DEPOT[0] + float(rng.uniform(-0.05, 0.05)),
                    "lon": DEPOT[1] + float(rng.uniform(-0.05, 0.05)),
                    "speed": round(float(rng.uniform(20, 80)), 1),
                },
                "sensors": {
                    "temp": round(float(rng.uniform(2, 9)), 1),
                    "fuel_level": round(float(rng.uniform(20, 100)), 1),
                },
                "event_type": event_type,
            }

            topic = f"fleet/telemetry/{v_id}"
            client.publish(topic, json.dumps(payload))
            print(f"[{topic}] {event_type}  (temp={payload['sensors']['temp']}°C)")
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nArrêt de la simulation.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
