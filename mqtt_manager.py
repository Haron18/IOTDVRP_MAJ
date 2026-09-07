"""
Pont MQTT entre le broker (télémétrie GPS/IoT en temps réel) et l'application Streamlit.

Correctif critique par rapport à la version d'origine :
Le callback `on_message` de paho-mqtt s'exécute dans un THREAD SÉPARÉ (le thread réseau
de la librairie). La version d'origine écrivait directement dans
`st.session_state.mqtt_events.append(...)` depuis ce thread. Streamlit n'est PAS conçu
pour être modifié depuis un autre thread que le thread principal du script : au mieux
rien ne s'affiche, au pire l'application plante ou perd des messages de façon
imprévisible.

La solution standard consiste à faire écrire le thread MQTT dans une `queue.Queue`
(qui, elle, est thread-safe), puis à "vider" cette file d'attente uniquement depuis le
thread principal Streamlit, à chaque rafraîchissement de la page (`drain_events`).
"""

import json
import queue

import paho.mqtt.client as mqtt

BROKER = "broker.hivemq.com"   # broker public gratuit — usage DÉMO uniquement, non sécurisé
PORT = 1883
TOPIC_TELEMETRY = "fleet/telemetry/+"
TOPIC_COMMANDS = "fleet/commands/"


class MQTTBridge:
    def __init__(self):
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self.connected = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self.connected = (rc == 0)
        if self.connected:
            client.subscribe(TOPIC_TELEMETRY)

    def _on_message(self, client, userdata, msg):
        # Exécuté dans le thread réseau paho-mqtt : on se contente de pousser
        # dans la queue thread-safe, rien d'autre.
        try:
            payload = json.loads(msg.payload.decode())
            self._queue.put_nowait(payload)
        except Exception:
            pass

    def start(self) -> bool:
        """Tente la connexion au broker. Ne lève jamais d'exception (réseau coupé, etc.)."""
        try:
            self.client.connect(BROKER, PORT, keepalive=60)
            self.client.loop_start()
            return True
        except Exception as e:
            print(f"Erreur de connexion MQTT: {e}")
            return False

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def send_command(self, vehicle_id: str, command_data: dict):
        topic = f"{TOPIC_COMMANDS}{vehicle_id}"
        self.client.publish(topic, json.dumps(command_data))

    def drain_events(self, max_items: int = 50) -> list[dict]:
        """À appeler UNIQUEMENT depuis le thread principal Streamlit.
        Vide la file d'attente thread-safe et retourne les nouveaux événements reçus.
        """
        events = []
        while not self._queue.empty() and len(events) < max_items:
            events.append(self._queue.get_nowait())
        return events
