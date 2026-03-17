import time
import json
import requests
import paho.mqtt.client as mqtt
from catalog.utils import get_limits

# Catalog service address
CATALOG_URL = "http://localhost:8080"

class UniversalECController:
    def __init__(self):
        self.client = None
        self.COOLDOWN = 5 
        
        # Separate memory for each tower
        # Structure: { "tower_1": {"is_dosing": False, "last_cmd": 0}, ... }
        self.tower_states = {}

    def start(self):
        print("🧪 Universal EC Controller Started (Smart Mode).")
        
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
                print("⏳ EC Ctrl: Waiting for Catalog...")
                time.sleep(2)

        # 2. Connect to MQTT
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_ec_ctrl")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(broker, port, 60)
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ EC Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("✅ EC Controller Connected.")
            # Listening to sensors from all towers
            client.subscribe("garden/+/sensors/data")

    def get_tower_memory(self, tower_id):
        """Create or retrieve memory for a tower"""
        if tower_id not in self.tower_states:
            self.tower_states[tower_id] = {
                "is_dosing": False,
                "last_cmd": 0
            }
        return self.tower_states[tower_id]

    def send_command(self, tower_id, action):
        memory = self.get_tower_memory(tower_id)
        
        # Check Cooldown (specifically for turning ON)
        if action == "ON" and (time.time() - memory["last_cmd"]) < self.COOLDOWN:
            return

        target = "nutrient_pump"
        topic = f"garden/{tower_id}/cmd/{target}"
        payload = {"target": target, "action": action, "timestamp": time.time()}
        
        self.client.publish(topic, json.dumps(payload))
        
        # Update last command time
        memory["last_cmd"] = time.time()
        # print(f"⚡ {tower_id}: EC -> {action}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic = msg.topic
            
            # --- 1. Identify tower ---
            parts = topic.split("/")
            if len(parts) < 3: return
            tower_id = parts[1]
            
            # --- 2. Receive EC data and plant name ---
            ec = payload.get("ec")
            plant_name = payload.get("plant") # e.g., lettuce

            if ec is None: return
            current_ec = float(ec)
            
            # --- 3. Receive smart limits ---
            # If plant name exists, fetch specific limits
            limits = get_limits("ec", plant_name)
            
            min_ec = limits.get("min", 1.0)
            max_ec = limits.get("max", 2.5)
            
            # Target stop point (range average)
            target_stop = (min_ec + max_ec) / 2

            # --- 4. Control logic ---
            memory = self.get_tower_memory(tower_id)

            # A) Turn OFF (Brake)
            # If reached target or exceeded maximum
            if current_ec >= target_stop:
                if memory["is_dosing"] or current_ec > max_ec:
                    print(f"✅ {tower_id} ({plant_name}): EC Good ({current_ec}) -> STOP")
                    self.send_command(tower_id, "OFF")
                    memory["is_dosing"] = False
            
            # B) Turn ON (Need nutrients)
            elif current_ec < min_ec:
                if not memory["is_dosing"]:
                    print(f"📉 {tower_id} ({plant_name}): Low EC ({current_ec} < {min_ec}) -> DOSE")
                    self.send_command(tower_id, "ON")
                    memory["is_dosing"] = True

        except Exception as e:
            print(f"⚠️ EC Logic Error: {e}")

if __name__ == "__main__":
    UniversalECController().start()