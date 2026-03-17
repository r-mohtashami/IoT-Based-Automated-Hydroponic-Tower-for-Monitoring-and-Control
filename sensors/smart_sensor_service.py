import time
import json
import random
import sys
import requests
import paho.mqtt.client as mqtt

# Basic (since we are local, the catalog is on port 8080)
CATALOG_URL = "http://localhost:8080"

class SmartTowerNode:
    def __init__(self, tower_id, plant_name="lettuce"):
        self.tower_id = tower_id
        self.plant_name = plant_name
        self.mqtt_client = None
        self.paused = False  # Pause situation
        
        # --- 1. Initial values ​​of sensors (exactly according to your code) ---
        self.data = {
            "ph": round(random.uniform(5.0, 7.5), 2),
            "ec": round(random.uniform(0.8, 3.0), 2),
            "air_temperature": round(random.uniform(20, 35), 1),
            "water_level": round(random.uniform(30, 90), 1),
            "light_intensity": random.randint(100, 500),
            "plant": plant_name
        }
        
        # --- 2. Status of all actuators ---
        self.actuators = {
            "pump_refill": "OFF",
            "nutrient_pump": "OFF",
            "pump_ph_up": "OFF",
            "pump_ph_down": "OFF",
            "cooling_fan": "OFF",
            "grow_light": "OFF"
        }

    def register_to_catalog(self):
        """Register in the catalog(Service Discovery)"""
        payload = {
            "id": self.tower_id,
            "type": "tower_system",
            "plant": self.plant_name,
            "endpoints": {
                "sensors": f"garden/{self.tower_id}/sensors",
                "actuators": f"garden/{self.tower_id}/cmd"
            }
        }
        try:
            # Submit a registration request
            res = requests.post(f"{CATALOG_URL}/register", json=payload)
            if res.status_code == 200:
                print(f"✅ Registered {self.tower_id} ({self.plant_name}) successfully.")
                config = res.json()
                # Returning the MQTT settings that the catalog gives us
                return config.get("mqtt", {})
            else:
                print(f"❌ Registration failed: {res.text}")
                sys.exit(1)
        except Exception as e:
            print(f"❌ Catalog connection error: {e}")
            sys.exit(1)

    def start(self):
        # 1. Register and get MQTT settings
        mqtt_config = self.register_to_catalog()
        broker = mqtt_config.get("host", "localhost")
        port = mqtt_config.get("port", 1883)

        # 2. Connecting to MQTT
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"node_{self.tower_id}")
        try:
            self.client.connect(broker, port)
        except:
            print(f"❌ Failed to connect to MQTT Broker at {broker}:{port}")
            return

        # 3. Listening to instructions specific to this tower
        cmd_topic = f"garden/{self.tower_id}/cmd/#"
        self.client.subscribe(cmd_topic)
        self.client.on_message = self.on_message
        self.client.loop_start()

        print(f"🚀 {self.tower_id} Simulation Running...")
        
        # 4. Starting the physics loop and generating data
        self.simulation_loop()

    def on_message(self, client, userdata, msg):
        """Execution of control commands + system management"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            # Extract device name
            device = topic.split("/")[-1] 
            action = payload.get("action") or payload.get("value")
            
            # --- 1. System commands (high priority) ---
            if action == "SHUTDOWN":
                print(f"💀 Received SHUTDOWN command. Stopping {self.tower_id}...")
                sys.exit(0)

            elif action == "PAUSE":
                self.paused = True
                print(f"⏸ {self.tower_id} PAUSED by User.")
                return # Exit the function so nothing changes.

            elif action == "RESUME":
                self.paused = False
                print(f"▶ {self.tower_id} RESUMED by User.")
                return # Exit the function.

            # --- 2. Hardware commands ---
            if action:
                sim_action = "ON" if action in ["ON", "DOSE"] else "OFF"
                
                if device in self.actuators:
                    self.actuators[device] = sim_action
                    
        except Exception as e:
            print(f"Msg Error: {e}")

    def simulation_loop(self):
        topic = f"garden/{self.tower_id}/sensors/data"
        status_topic = f"garden/{self.tower_id}/status" # New topic for Heartbeat

        while True:
            try:
                # ==========================================
                # 🛑 (Pause Logic)
                # ==========================================
                if self.paused:
                    # 1. Send a message to MQTT (so the dashboard knows we are online)
                    # This line keeps the Resume button active.
                    try:
                        self.client.publish(status_topic, json.dumps({"status": "Paused", "timestamp": time.time()}))
                    except: pass

                    # 2. Heartbeat to Catalog (for Discovery Service)
                    try:
                        hb_payload = {"id": self.tower_id, "type": "tower_system", "plant": self.plant_name}
                        requests.post(f"{CATALOG_URL}/register", json=hb_payload, timeout=1)
                    except: pass
                    
                    time.sleep(1)
                    continue # Here the loop goes around and the lower code (physics) is not executed.
                
                # ==========================================
                # ⚙️ Physics simulation
                # ==========================================
                
                # 1. Natural changes (plant consumption/environment)
                self.data["water_level"] -= 0.1   # Evaporation
                self.data["ec"] -= 0.005          # Fertilizer use
                self.data["ph"] += 0.002          # pH drift
                self.data["air_temperature"] += 0.05 # Tower environmental warming

                # 2. The impact of actuators
                if self.actuators["pump_refill"] == "ON":
                    self.data["water_level"] += 2.5
                
                if self.actuators["nutrient_pump"] == "ON":
                    self.data["ec"] += 0.15
                
                if self.actuators["pump_ph_down"] == "ON":
                    self.data["ph"] -= 0.1
                if self.actuators["pump_ph_up"] == "ON":
                    self.data["ph"] += 0.1
                    
                if self.actuators["cooling_fan"] == "ON":
                    self.data["air_temperature"] -= 0.4
                    
                if self.actuators["grow_light"] == "ON":
                    self.data["light_intensity"] = 800 + random.randint(-20, 20)
                else:
                    self.data["light_intensity"] = 100 + random.randint(-10, 10)

                # 3. Clamping numbers
                self.data["water_level"] = max(0.0, min(100.0, self.data["water_level"]))
                self.data["ec"] = max(0.0, self.data["ec"])
                self.data["ph"] = max(0.0, min(14.0, self.data["ph"]))
                self.data["air_temperature"] = max(10.0, self.data["air_temperature"])

                # 4. Round and send
                payload = {k: round(v, 2) if isinstance(v, float) else v for k, v in self.data.items()}
                payload["timestamp"] = time.time()

                self.client.publish(topic, json.dumps(payload))
                
                # Catalog heartbeat in normal mode
                try:
                    requests.post(f"{CATALOG_URL}/register", json={"id": self.tower_id, "type": "tower_system", "plant": self.plant_name})
                except: pass

                time.sleep(2) # Simulation speed

            except KeyboardInterrupt:
                sys.exit(0)
            except Exception as e:
                print(f"Sim Error: {e}")
                time.sleep(2)

if __name__ == "__main__":
    my_id = "tower_1" 
    my_plant = "lettuce"

    if len(sys.argv) > 1:
        my_id = sys.argv[1]
    
    if len(sys.argv) > 2:
        my_plant = sys.argv[2]
    
    node = SmartTowerNode(my_id, my_plant)
    node.start()