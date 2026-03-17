import time
import json
import datetime
import requests
import paho.mqtt.client as mqtt
from catalog.utils import get_limits

# Catalog service address
CATALOG_URL = "http://localhost:8080"

class UniversalLightingController:
    def __init__(self):
        self.client = None
        
        # Status memory for each tower
        # Structure: { "tower_1": "OFF", "tower_2": "ON" }
        self.tower_states = {}

    def start(self):
        print("💡 Universal Lighting Controller Started (Smart Hysteresis Mode).")
        
        # 1. Get network settings
        broker = "localhost"
        port = 1883
        
        while True:
            try:
                res = requests.get(f"{CATALOG_URL}/config", timeout=2)
                if res.status_code == 200:
                    cfg = res.json()
                    broker = cfg["mqtt"]["host"]
                    port = cfg["mqtt"]["port"]
                    break
            except:
                print("⏳ Light Ctrl: Waiting for Catalog...")
                time.sleep(2)

        # 2. Connecting to MQTT
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_light_ctrl")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(broker, port, 60)
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ Lighting Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("✅ Lighting Connected.")
            client.subscribe("garden/+/sensors/data")

    def send_command(self, tower_id, action):
        """Send command only if status changes"""
        current_state = self.tower_states.get(tower_id, "UNKNOWN")
        
        if current_state == action:
            return 

        topic = f"garden/{tower_id}/cmd/grow_light"
        payload = {"target": "grow_light", "action": action, "timestamp": time.time()}
        
        self.client.publish(topic, json.dumps(payload))
        
        # Memory update
        self.tower_states[tower_id] = action
        print(f"💡 {tower_id}: Light -> {action}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic = msg.topic
            
            parts = topic.split("/")
            if len(parts) < 3: return
            tower_id = parts[1]
            
            lux = payload.get("light_intensity") or payload.get("light")
            plant_name = payload.get("plant")
            
            if lux is None: return
            current_lux = float(lux)
            
            # Receive the minimum required light
            limits = get_limits("light_intensity", plant_name)
            min_required = limits.get("min", 500) 

            # --- Logic Hysteresis (Anti-flicker) ---
            # We estimate that the grow light adds about 700-800 lux to the light.
            # So to turn it off, the light has to be much higher than the minimum.
            LAMP_CONTRIBUTION = 800 
            OFF_THRESHOLD = min_required + LAMP_CONTRIBUTION + 100 

            # Check the current status of the lamp.
            current_state = self.tower_states.get(tower_id, "OFF")

            # Logic Time (8 am to 8 pm)
            now = datetime.datetime.now().time()
            start_time = datetime.time(8, 0)
            end_time = datetime.time(20, 0)
            is_daytime = start_time <= now <= end_time
            
            desired_action = "OFF"
            
            if is_daytime:
                if current_state == "OFF":
                    # If it is off, check according to the lower limit.
                    if current_lux < min_required:
                        desired_action = "ON"
                    else:
                        desired_action = "OFF"
                
                elif current_state == "ON":
                    # If it's on, only turn it off when the sunlight is too strong.
                    # (i.e. the sensor will show a number much higher than the lamp light)
                    if current_lux > OFF_THRESHOLD:
                        desired_action = "OFF"
                    else:
                        desired_action = "ON" # Stay on so it doesn't fluctuate.
            else:
                desired_action = "OFF"

            self.send_command(tower_id, desired_action)

        except Exception as e:
            print(f"⚠️ Light Logic Error: {e}")

if __name__ == "__main__":
    UniversalLightingController().start()