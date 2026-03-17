import json
import time
import sys
import requests
from pathlib import Path
import paho.mqtt.client as mqtt

CATALOG_URL = "http://localhost:8080"

class UniversalLoggerService:
    def __init__(self):
        self.broker = "localhost"
        self.port = 1883
        
        self.log_file = Path(__file__).parent / "system_events.log"
        
        self.ensure_config()

    def ensure_config(self):
        """Get network settings from catalog"""
        while True:
            try:
                res = requests.get(f"{CATALOG_URL}/config")
                if res.status_code == 200:
                    cfg = res.json()
                    self.broker = cfg["mqtt"]["host"]
                    self.port = cfg["mqtt"]["port"]
                    print("✅ Logger: Config loaded from Catalog.")
                    break
            except:
                print("⏳ Logger: Waiting for Catalog...")
                time.sleep(2)

    def start(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="univ_logger_service")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        print(f"📝 Logger connecting to {self.broker}...")
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_forever()
        except Exception as e:
            print(f"❌ Logger Connection Error: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"✅ Logger Connected. Saving to: {self.log_file.name}")
            

            client.subscribe([("garden/+/cmd/#", 0), ("garden/+/alerts", 0)])
        else:
            print(f"❌ Connection Failed code: {rc}")

    def on_message(self, client, userdata, msg):
        try:
            raw_payload = msg.payload.decode("utf-8")
            topic = msg.topic
            

            parts = topic.split("/")
            tower_id = "UNKNOWN"
            if len(parts) >= 2:
                tower_id = parts[1]

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            
            log_entry = {
                "time": timestamp,
                "tower_id": tower_id,
                "topic": topic,
                "event_type": "INFO",
                "details": {}
            }

            try:
                data = json.loads(raw_payload)
                
                if "cmd" in topic:
                    log_entry["event_type"] = "ACTION"
                    target = data.get("target") or topic.split("/")[-1]
                    action = data.get("action") or data.get("value")
                    log_entry["details"] = {"device": target, "action": action}
                    
                    print(f"💾 Log: [{tower_id}] {target} -> {action}")
                
                elif "alert" in topic:
                    log_entry["event_type"] = "ALERT"
                    message = data.get("msg") or str(data)
                    level = data.get("level", "WARNING")
                    log_entry["details"] = {"level": level, "message": message}
                    
                    print(f"💾 Log: [{tower_id}] ⚠️ ALERT: {message}")
                
                else:
                    log_entry["details"] = data

            except json.JSONDecodeError:
                log_entry["details"] = {"raw_message": raw_payload}

            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        except Exception as e:
            print(f"❌ Log Write Error: {e}")

if __name__ == "__main__":
    UniversalLoggerService().start()