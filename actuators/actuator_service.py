import time
import json
import threading
import requests
import cherrypy
import paho.mqtt.client as mqtt

# Web server port to view actuator status
SERVICE_PORT = 9090
CATALOG_URL = "http://localhost:8080"

class ActuatorService:
    exposed = True

    def __init__(self):
       # New structure: { "tower_1": { "pump": "ON", ... }, "tower_2": ... }
        self.farm_state = {} 
        self.ensure_config()

    def ensure_config(self):
        """Receive network settings from the Catalog"""
        self.broker = "localhost"
        self.port = 1883
        
        while True:
            try:
                # Attempting to fetch config from the central Catalog
                res = requests.get(f"{CATALOG_URL}/config")
                if res.status_code == 200:
                    cfg = res.json()
                    self.broker = cfg["mqtt"]["host"]
                    self.port = cfg["mqtt"]["port"]
                    print("✅ Config loaded from Catalog.")
                    break
            except:
                print("⏳ Actuator: Waiting for Catalog...")
                time.sleep(2)

    def start_mqtt(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_actuator_monitor")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        print(f"🔌 Actuator Monitor connecting to {self.broker}...")
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ MQTT Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        print(f"✅ Actuator Service Ready (Multi-Tower Mode).")
        # Listening to commands from all towers
        # Format: garden/+/cmd/#
        topic = "garden/+/cmd/#"
        client.subscribe(topic)
        print(f"👂 Listening on: {topic}")

    def on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            # --- 1. Extract tower name and device ---
            # garden/tower_1/cmd/pump_refill
            parts = topic.split("/")
            if len(parts) < 4: return
            
            tower_id = parts[1]  # tower_1
            device_name = parts[3] # pump_refill

            # --- 2. Receive command ---
            action = payload.get("action") or payload.get("value")

            if action:
                # --- 3. Update farm state ---
                if tower_id not in self.farm_state:
                    self.farm_state[tower_id] = {}
                
                self.farm_state[tower_id][device_name] = action

                # --- 4. Pretty print to terminal ---
                
                color_action = f"\033[92m[ {action} ]\033[0m" if action in ["ON", "DOSE"] else f"\033[91m[ {action} ]\033[0m"
                color_tower = f"\033[93m{tower_id}\033[0m"
                
                print(f"⚡ {color_tower} : {device_name} -> {color_action}")

        except Exception as e:
            print(f"❌ Msg Error: {e}")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def index(self):
        """Display total farm status in the browser"""
        return {
            "status": "Active",
            "total_towers": len(self.farm_state),
            "farm_data": self.farm_state
        }

if __name__ == "__main__":
    app = ActuatorService()
    
    # Running MQTT in a separate thread
    t = threading.Thread(target=app.start_mqtt)
    t.daemon = True
    t.start()

    # Running CherryPy web server
    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': SERVICE_PORT,
        'log.screen': False # Prevent terminal clutter with web logs
    })
    print(f"🌍 Actuator Dashboard: http://localhost:{SERVICE_PORT}")
    cherrypy.quickstart(app)