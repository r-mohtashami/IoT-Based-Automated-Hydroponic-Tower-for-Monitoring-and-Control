import json
import time
import requests

# Catalog service URL
CATALOG_URL = "http://localhost:8080"

class CatalogClient:
    def get_config(self) -> dict:
        try:
            # Attempt to retrieve the full configuration file from the catalog.
            # We set a short timeout so that if it is down, it will retry quickly.
            response = requests.get(f"{CATALOG_URL}/config", timeout=2)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"⚠️ Utils: Error connecting to Catalog: {e}")
        return {}

client = CatalogClient()
config = {}

def refresh_config():
    # Attempt to update the config.
    global config
    new_cfg = client.get_config()
    if new_cfg:
        config = new_cfg
        # print("✅ Config Updated from JSON.")

def get_limits(sensor_type, plant_name=None):
    # Retrieving the allowed limits from the JSON file.
    # If a plant name (plant_name) is provided, that plant’s settings take priority.
    global config
    
    # If the config is empty, try to fetch it.
    if not config:
        refresh_config()
        if not config:
            # If the catalog is not up yet, return a safe default range for now so the program does not crash.
            return {"min": 0, "max": 1000}

    # Standardizing sensor names (to match the keys in the JSON file).
    key_map = {
        "air_temp": "air_temperature",
        "level_r1": "water_level", 
        "level_r2": "water_level",
        "ph": "ph", 
        "ec": "ec",
        "light": "light_intensity"
    }
    lookup_key = key_map.get(sensor_type, sensor_type)

    # 1. Retrieve the general (default) settings from the thresholds section.
    thresholds = config.get("thresholds", {})
    final_limits = thresholds.get(lookup_key, {"min": 0, "max": 100}).copy()

    # 2. If we have a plant name, find its specific settings.
    if plant_name:
        all_plants = config.get("plants", {})
        target_plant_data = None
        
        # Search within the categories (leafy_greens, etc.).
        for category, plants_dict in all_plants.items():
            if plant_name in plants_dict:
                target_plant_data = plants_dict[plant_name]
                break
        
        # If the plant is found, override with its values.
        if target_plant_data:
            # pH logic
            if lookup_key == "ph" and "optimal_ph" in target_plant_data:
                final_limits = {
                    "min": target_plant_data["optimal_ph"][0], 
                    "max": target_plant_data["optimal_ph"][1]
                }
            # EC logic
            elif lookup_key == "ec" and "optimal_ec" in target_plant_data:
                final_limits = {
                    "min": target_plant_data["optimal_ec"][0], 
                    "max": target_plant_data["optimal_ec"][1]
                }
            # Temperature logic
            elif lookup_key == "air_temperature" and "max_air_temp" in target_plant_data:
                # Usually plants do not have a minimum temperature; only the maximum matters.
                final_limits["max"] = target_plant_data["max_air_temp"]
            # Water level logic
            elif lookup_key == "water_level" and "min_water_level_cm" in target_plant_data:
                # Note: In the JSON, min_water_level_cm is a list
                val_list = target_plant_data["min_water_level_cm"]
                if val_list:
                    final_limits["min"] = val_list[0] # Minimum water height

    return final_limits