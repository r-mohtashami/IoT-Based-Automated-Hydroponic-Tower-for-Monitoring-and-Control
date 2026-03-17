import time
import json
import requests
import paho.mqtt.client as mqtt
from catalog.utils import get_limits

# Catalog service address
CATALOG_URL = "http://localhost:8080"

class UniversalPHController:
    def __init__(self):
        self.client = None
        self.COOLDOWN = 5
        
        # Separate memory for each tower status
        # Structure: { "tower_1": { "state": "STABLE", "last_cmd": 0}, ... }
        self.tower_states = {}

    def start(self):
        print("🧪 Universal pH Controller Started (Smart Mode).")
        
        # 1. Get network settings from the catalog
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
                print("⏳ pH Ctrl: Waiting for Catalog...")
                time.sleep(2)

        # 2. Connecting to MQTT
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_ph_ctrl")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(broker, port, 60)
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ pH Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("✅ pH Controller Connected.")
            # Subscribe to all sensors
            client.subscribe("garden/+/sensors/data")

    def get_tower_state(self, tower_id):
        """Memory retrieval for a tower"""
        if tower_id not in self.tower_states:
            self.tower_states[tower_id] = {
                "state": "STABLE",
                "last_cmd": 0
            }
        return self.tower_states[tower_id]

    def send_command(self, tower_id, target_pump, action):
        """Send command to specific tower"""
        topic = f"garden/{tower_id}/cmd/{target_pump}"
        payload = {"target": target_pump, "action": action, "timestamp": time.time()}
        
        self.client.publish(topic, json.dumps(payload))
        
        # Update time of last command
        self.tower_states[tower_id]["last_cmd"] = time.time()
        # print(f"⚡ {tower_id}: {target_pump} -> {action}")

    def on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            # --- 1. Tower identification ---
            parts = topic.split("/")
            if len(parts) < 3: return
            tower_id = parts[1] # tower_1
            
            # --- 2. Get pH data and plant name ---
            ph = payload.get("ph")
            plant_name = payload.get("plant") # Plant name

            if ph is None: return
            current_ph = float(ph)
            
            # --- 3. Get smart limits ---
            # If it is the name of a plant, it takes its specific limits.
            limits = get_limits("ph", plant_name)
            
            min_ph = limits.get("min", 5.5)
            max_ph = limits.get("max", 6.5)

            # --- 4. Memory access ---
            memory = self.get_tower_state(tower_id)
            current_state = memory["state"]
            last_time = memory["last_cmd"]

            # --- 5. Logic Control ---
            
            # A)Normal status (Stable)
            if min_ph <= current_ph <= max_ph:
                if current_state != "STABLE":
                    print(f"✅ {tower_id} ({plant_name}): pH Stable ({current_ph}) -> STOP")
                    self.send_command(tower_id, "pump_ph_up", "OFF")
                    self.send_command(tower_id, "pump_ph_down", "OFF")
                    memory["state"] = "STABLE"

            # B)pH is too low -> needs alkali (Base/Up)
            elif current_ph < min_ph:
                if (time.time() - last_time) > self.COOLDOWN:
                    print(f"📉 {tower_id} ({plant_name}): Low pH ({current_ph} < {min_ph}) -> Dosing UP")
                    self.send_command(tower_id, "pump_ph_up", "DOSE")
                    self.send_command(tower_id, "pump_ph_down", "OFF")
                    memory["state"] = "DOSING_UP"

            # C)pH is too high -> needs acid (Acid/Down)
            elif current_ph > max_ph:
                if (time.time() - last_time) > self.COOLDOWN:
                    print(f"📈 {tower_id} ({plant_name}): High pH ({current_ph} > {max_ph}) -> Dosing DOWN")
                    self.send_command(tower_id, "pump_ph_down", "DOSE")
                    self.send_command(tower_id, "pump_ph_up", "OFF")
                    memory["state"] = "DOSING_DOWN"

        except Exception as e:
            print(f"⚠️ pH Logic Error: {e}")

if __name__ == "__main__":
    UniversalPHController().start()