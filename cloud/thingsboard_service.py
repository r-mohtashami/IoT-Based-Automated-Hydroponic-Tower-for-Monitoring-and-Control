import json
import time
import requests
import paho.mqtt.client as mqtt

# Configs
CATALOG_URL = "http://localhost:8080"
TB_HOST = "eu.thingsboard.cloud"

class ThingsBoardBridge:
    def __init__(self):
        self.tb_tokens = {} 
        self.tb_clients = {} 
        self.load_config()

    def load_config(self):
        while True:
            try:
                res = requests.get(f"{CATALOG_URL}/config", timeout=2)
                if res.status_code == 200:
                    cfg = res.json()
                    self.broker = cfg["mqtt"]["host"]
                    self.port = cfg["mqtt"]["port"]
                    
                    tb_cfg = cfg.get("thingsboard", {})
                    if tb_cfg.get("enabled"):
                        self.tb_tokens = tb_cfg.get("tokens", {})
                        print(f"✅ Bridge Config Loaded. Tokens found: {list(self.tb_tokens.keys())}")
                        break
            except:
                print("⏳ Bridge: Waiting for Catalog...")
                time.sleep(2)

    def get_tb_client(self, tower_key):
        # Connect to the cloud for a specific tower
        if tower_key not in self.tb_tokens: return None

        if tower_key in self.tb_clients: return self.tb_clients[tower_key]

        token = self.tb_tokens[tower_key]
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bridge_{tower_key}_{int(time.time())}")
        client.username_pw_set(token)
        
        try:
            client.connect(TB_HOST, 1883, 60)
            client.loop_start()
            self.tb_clients[tower_key] = client
            print(f"☁️ Connected {tower_key} to Cloud.")
            return client
        except Exception as e:
            print(f"❌ Cloud Conn Error ({tower_key}): {e}")
            return None

    def on_local_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            raw_payload = msg.payload.decode()
            data = json.loads(raw_payload)
            
            # 1. Extract the tower name from the topic (garden/tower_1/...)
            parts = topic.split("/")
            if len(parts) < 2: return
            tower_key = parts[1] # tower_1
            
            # 2. Clean the data (send only numbers)
            telemetry = {}
            for k, v in data.items():
                if isinstance(v, (int, float)):
                    telemetry[k] = v
                elif isinstance(v, str) and v.replace('.','',1).isdigit():
                    telemetry[k] = float(v)

            if not telemetry: return

            # 3. Send to ThingsBoard
            tb_client = self.get_tb_client(tower_key)
            if tb_client:
                # A fixed, standard ThingsBoard topic
                tb_client.publish("v1/devices/me/telemetry", json.dumps(telemetry))
                # print(f"⬆️ Sent {len(telemetry)} keys for {tower_key}")

        except Exception as e:
            print(f"Bridge Error: {e}")

    def start(self):
        self.local = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tb_bridge_main_listener")
        self.local.on_connect = self.on_connect
        self.local.on_message = self.on_local_message
        try:
            self.local.connect(self.broker, self.port, 60)
            self.local.loop_forever()
        except Exception as e:
            print(f"❌ Local MQTT Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("✅ Bridge Listening to Local Garden...")
            client.subscribe("garden/+/sensors/data")

if __name__ == "__main__":
    ThingsBoardBridge().start()