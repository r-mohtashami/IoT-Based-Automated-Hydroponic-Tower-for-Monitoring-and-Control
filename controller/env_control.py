import time
import json
import requests
import paho.mqtt.client as mqtt
from catalog.utils import get_limits

# Catalog service address
CATALOG_URL = "http://localhost:8080"

class UniversalEnvController:
    def __init__(self):
        self.client = None
        self.COOLDOWN = 5
        
        # Separate memory for each tower
        # Structure: { "tower_1": {"is_cooling": False, "last_cmd": 0, "last_alert": 0}, ... }
        self.tower_states = {}

    def start(self):
        print("🌡️ Universal Env Controller Started (Smart Mode).")
        
        # 1. Receive settings from Catalog
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
                print("⏳ Env Ctrl: Waiting for Catalog...")
                time.sleep(2)

        # 2. Connect to MQTT
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_env_ctrl")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(broker, port, 60)
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ Env Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("✅ Env Controller Connected.")
            # Listening to sensors from all towers
            client.subscribe("garden/+/sensors/data")

    def get_tower_memory(self, tower_id):
        if tower_id not in self.tower_states:
            self.tower_states[tower_id] = {
                "is_cooling": False,
                "last_cmd": 0,
                "last_alert": 0
            }
        return self.tower_states[tower_id]

    def send_command(self, tower_id, action):
        memory = self.get_tower_memory(tower_id)
        
        # Prevent command repetition (only for turning ON)
        if action == "ON" and (time.time() - memory["last_cmd"]) < self.COOLDOWN:
            return

        target = "cooling_fan"
        topic = f"garden/{tower_id}/cmd/{target}"
        payload = {"target": target, "action": action, "timestamp": time.time()}
        
        self.client.publish(topic, json.dumps(payload))
        memory["last_cmd"] = time.time()
        # print(f"⚡ {tower_id}: Fan -> {action}")

    def send_alert(self, tower_id, message):
        memory = self.get_tower_memory(tower_id)
        
        # Prevent alert spamming (once every 30 seconds)
        if (time.time() - memory["last_alert"]) < 30:
            return

        topic = f"garden/{tower_id}/alerts"
        payload = {
            "sender": "env_controller",
            "msg": message,
            "level": "CRITICAL",
            "timestamp": time.time()
        }
        self.client.publish(topic, json.dumps(payload))
        memory["last_alert"] = time.time()
        print(f"⚠️ {tower_id} ALERT: {message}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic = msg.topic
            
            # --- 1. Identify tower ---
            parts = topic.split("/")
            if len(parts) < 3: return
            tower_id = parts[1]
            
            # --- 2. Receive temperature and plant name ---
            temp = payload.get("air_temperature") or payload.get("air_temp")
            plant_name = payload.get("plant")

            if temp is None: return
            current_temp = float(temp)
            
            # --- 3. Receive smart limits ---
            # Here we provide the plant name to get the specific max temp for that plant
            limits = get_limits("air_temperature", plant_name)
            max_temp = limits.get("max", 28.0)
            
            # --- 4. Control logic ---
            memory = self.get_tower_memory(tower_id)

            # A) Temp is high -> Fan ON
            if current_temp > max_temp:
                if not memory["is_cooling"]:
                    print(f"🔥 {tower_id} ({plant_name}): High Temp ({current_temp} > {max_temp}) -> FAN ON")
                    self.send_command(tower_id, "ON")
                    memory["is_cooling"] = True
                
                # Check critical status (3 degrees above limit)
                if current_temp > (max_temp + 3):
                    self.send_alert(tower_id, f"Critical Temp: {current_temp}°C (Max: {max_temp})")

            # B) Temp cooled down (Hysteresis: 2 degrees lower) -> Fan OFF
            elif current_temp < (max_temp - 2):
                if memory["is_cooling"]:
                    print(f"❄️ {tower_id} ({plant_name}): Temp Normal ({current_temp}) -> FAN OFF")
                    self.send_command(tower_id, "OFF")
                    memory["is_cooling"] = False

        except Exception as e:
            print(f"⚠️ Env Logic Error: {e}")

if __name__ == "__main__":
    UniversalEnvController().start()