import time
import json
import requests
import paho.mqtt.client as mqtt
from catalog.utils import get_limits

# Catalog service URL
CATALOG_URL = "http://localhost:8080"

class UniversalAlertManager:
    def __init__(self):
        self.client = None
        self.ALERT_COOLDOWN = 30 # Repeat every 30 seconds
        
        # Memory of the latest alerts, maintained separately for each tower
        # Structure: { "tower_1": { "ph_high": 1234567890, "ec_low": ... }, ... }
        self.alert_history = {}

    def start(self):
        print("🔔 Universal Alert Manager Started.")
        
        # 1. Fetch the network settings from the catalog
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
                print("⏳ Alert Mgr: Waiting for Catalog...")
                time.sleep(2)

        # 2. MQTT connection
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_alert_mgr")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(broker, port, 60)
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ Alert Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("✅ Alert Manager Connected & Watching Garden.")
            # Listen to all sensors in the garden
            client.subscribe("garden/+/sensors/data")

    def check_cooldown(self, tower_id, alert_key):
        # Check whether we can issue an alert or whether we should wait
        if tower_id not in self.alert_history:
            self.alert_history[tower_id] = {}
        
        last_time = self.alert_history[tower_id].get(alert_key, 0)
        if (time.time() - last_time) < self.ALERT_COOLDOWN:
            return False # It is still within the cooldown period.
        
        return True

    def send_alert(self, tower_id, plant_name, message, alert_key):
        # Send an alert to that tower’s dedicated topic
        if not self.check_cooldown(tower_id, alert_key):
            return

        # Topic: garden/tower_1/alerts
        topic = f"garden/{tower_id}/alerts"
        
        full_msg = f"⚠️ {tower_id} ({plant_name}): {message}"
        
        payload = {
            "sender": "alert_manager",
            "msg": full_msg,
            "level": "WARNING",
            "timestamp": time.time()
        }
        
        self.client.publish(topic, json.dumps(payload))
        print(full_msg)
        
        # Record the send time
        self.alert_history[tower_id][alert_key] = time.time()

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic = msg.topic
            
            # --- 1. Identify the tower---
            parts = topic.split("/") # garden/tower_1/sensors/data
            if len(parts) < 3: return
            tower_id = parts[1]
            
            # Plant name (for smart thresholds)
            plant_name = payload.get("plant", "unknown")

            # --- 2. Check each sensor one by one ---
            # List of sensors we want to check
            sensors_map = {
                "ph": "ph",
                "ec": "ec",
                "air_temperature": "air_temperature",
                "water_level": "water_level"
            }
            
            for sensor_key, limit_key in sensors_map.items():
                val = payload.get(sensor_key)
                
                # Handle different naming variants (e.g., air_temp instead of air_temperature)
                if val is None and sensor_key == "air_temperature":
                    val = payload.get("air_temp")
                if val is None and sensor_key == "water_level":
                    val = payload.get("level_r1")

                if val is not None:
                    current_val = float(val)
                    
                    # Retrieve smart thresholds (based on the plant)
                    limits = get_limits(limit_key, plant_name)
                    min_val = limits.get("min")
                    max_val = limits.get("max")
                    
                    # a) Check the lower limit (Low)
                    if min_val is not None and current_val < min_val:
                        self.send_alert(
                            tower_id, plant_name,
                            f"{sensor_key.upper()} Low ({current_val} < {min_val})",
                            f"{sensor_key}_low"
                        )
                    
                    # b) Check the upper limit (High)
                    elif max_val is not None and current_val > max_val:
                        self.send_alert(
                            tower_id, plant_name,
                            f"{sensor_key.upper()} High ({current_val} > {max_val})",
                            f"{sensor_key}_high"
                        )

        except Exception as e:
            # print(f"Alert Logic Error: {e}")
            pass

if __name__ == "__main__":
    UniversalAlertManager().start()