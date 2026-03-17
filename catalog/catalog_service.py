import cherrypy
import json
import time
from pathlib import Path

class CatalogService:
    exposed = True

    def __init__(self):
        # This dictionary is the list of active devices (temporary/in-memory storage)
        self.registry = {} 
        
        # Loading the base configuration (config.json)
        self.config_path = Path(__file__).resolve().parent / "config.json"
        self.base_config = self._load_base_config()
        print("✅ Catalog Service Started (Service Discovery Mode)")

    def _load_base_config(self):
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Warning: Could not load config.json: {e}")
            return {}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def index(self):
        return {"status": "Catalog is Running", "docs": "/devices or /config"}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def register(self):
        # Registration method: Devices (sensors) introduce/register themselves here.
        try:
            data = cherrypy.request.json
            device_id = data.get("id")
            device_type = data.get("type")
            
            if not device_id or not device_type:
                return {"status": "error", "msg": "Missing ID or Type"}

            # Updating the last activity time (heartbeat).
            data["last_seen"] = time.time()
            
            # Store in memory
            # If it already exists, it gets updated (for example, the plant may have changed)
            is_new = device_id not in self.registry
            self.registry[device_id] = data
            
            if is_new:
                print(f"🆕 New Device Registered: {device_id} ({data.get('plant', 'unknown')})")
            
            # Return the MQTT settings to the device so it knows where to connect.
            return {"status": "registered", "mqtt": self.base_config.get("mqtt")}
            
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def devices(self):
        #List of all active devices (for dashboard use)
        # Devices that have not been heard from for more than 60 seconds are removed.
        current_time = time.time()
        # Filtering active (online) devices.
        active_devices = {
            k: v for k, v in self.registry.items() 
            if (current_time - v["last_seen"]) < 60
        }
        
        # Clearing memory of inactive (dead/offline) devices (to prevent RAM from filling up over time).
        self.registry = active_devices
        
        return active_devices

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def config(self):
       
        # Providing the full configuration file (for controllers that need the limits).
       
        # To be safe, we load it again so that if the file was changed manually, the system will detect it.
        # (For better performance you can just use self.base_config.)
        return self._load_base_config()

if __name__ == "__main__":
    # Server settings
    conf = {
        'global': {
            'server.socket_host': '0.0.0.0',
            'server.socket_port': 8080,
            # These lines are to prevent the terminal from getting cluttered:
            'log.screen': False,      # Do not print CherryPy logs to the console.
            'log.access_file': '',    # Do not create an access log.
            'log.error_file': ''      # Do not create an error log.
        }
    }
    cherrypy.quickstart(CatalogService(), '/', conf)