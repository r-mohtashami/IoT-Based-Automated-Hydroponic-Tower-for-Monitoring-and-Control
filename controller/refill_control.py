import time
import json
import paho.mqtt.client as mqtt
from catalog.utils import get_limits

class UniversalRefillController:
    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_refill_ctrl")
        self.COOLDOWN = 10 
        
        # Separate memory for each tower
        # Structure: { "tower_1": { "is_filling": False, "last_cmd": 0}, ... }
        self.tower_states = {}

    def start(self):
        print("💧 Universal Refill Controller Started (Smart Mode).")
        
        # Connecting to the broker (local)
        try:
            self.client.connect("localhost", 1883, 60)
            
            # Listening to sensors on all towers
            self.client.subscribe("garden/+/sensors/data")
            
            self.client.on_message = self.on_message
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ Refill Start Error: {e}")

    def get_tower_memory(self, tower_id):
        """Create or retrieve memory for a tower"""
        if tower_id not in self.tower_states:
            self.tower_states[tower_id] = {
                "is_filling": False,
                "last_cmd": 0
            }
        return self.tower_states[tower_id]

    def send_command(self, tower_id, action):
        memory = self.get_tower_memory(tower_id)
        
        # Check Cooldown (only for power on, power off is immediate)
        if action == "ON" and (time.time() - memory["last_cmd"]) < self.COOLDOWN:
            return

        target = "pump_refill"
        topic = f"garden/{tower_id}/cmd/{target}"
        payload = {"target": target, "action": action, "timestamp": time.time()}
        
        self.client.publish(topic, json.dumps(payload))
        
        # Update time of last command
        memory["last_cmd"] = time.time()
        print(f"🚰 {tower_id}: Refill -> {action}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic = msg.topic
            
            # --- 1. Tower Identification ---
            parts = topic.split("/")
            if len(parts) < 3: return
            tower_id = parts[1] # tower_1
            
            # --- 2. Get water level and plant name ---
            level = payload.get("water_level") or payload.get("level_r1")
            plant_name = payload.get("plant") # Plant name
            
            if level is None: return
            current_level = float(level)
            
            #--- 3. Get Smart Allowances ---
            # Here we pass the plant name so that if there was a specific setting in Jason it would be applied.
            limits = get_limits("water_level", plant_name)
            
            min_level = limits.get("min", 20)
            max_target = limits.get("max", 95)

            # --- 4. Logic Control ---
            memory = self.get_tower_memory(tower_id)
            
            # Emergency braking and stopping
            if current_level >= max_target:
                # If we were filling up or it reached the dangerous level (99)
                if memory["is_filling"] or current_level >= 99:
                    print(f"✅ {tower_id} ({plant_name}): Tank Full ({current_level}%) -> STOP")
                    self.send_command(tower_id, "OFF")
                    memory["is_filling"] = False
            
            # Start filling
            elif current_level < min_level:
                if not memory["is_filling"]:
                    print(f"📉 {tower_id} ({plant_name}): Low Water ({current_level}% < {min_level}%) -> START")
                    self.send_command(tower_id, "ON")
                    memory["is_filling"] = True

        except Exception as e:
            print(f"⚠️ Refill Logic Error: {e}")

if __name__ == "__main__":
    UniversalRefillController().start()