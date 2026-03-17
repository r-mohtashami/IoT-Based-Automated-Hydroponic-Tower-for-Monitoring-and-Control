import subprocess
import time
import sys
import os
import json
import signal

# Config
CONFIG_PATH = os.path.join("catalog", "config.json")
DASHBOARD_PATH = "dashboard.py"

# List of all processes started by the runner (so they can be terminated together)
all_processes = []

def load_plants_from_config():
    # Reading the list of plants from the config file
    if not os.path.exists(CONFIG_PATH): return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("plants", {})
    except: return {}

def setup_towers():
    # Wizard to ask the user about the number and type of towers.
    print("\n🌿 --- Smart Farm Setup Wizard --- 🌿")
    
    # Number of towers
    while True:
        try:
            num = input("🏗️  How many towers do you want to start? (e.g. 1, 2, 5): ").strip()
            if not num: num = "1" # Default
            num_towers = int(num)
            if num_towers > 0: break
            print("Please enter a number greater than 0.")
        except ValueError:
            print("Invalid number.")

    plants_data = load_plants_from_config()
    towers_config = []

    # 2. Configure each tower
    print("\n📋 Available Plants:")
    plant_list = []
    idx = 1
    for cat, p_names in plants_data.items():
        for name in p_names:
            print(f"   [{idx}] {name.capitalize()} ({cat})")
            plant_list.append(name)
            idx += 1
            
    for i in range(1, num_towers + 1):
        tower_id = f"tower_{i}"
        print(f"\n⚙️  Configuring {tower_id.upper()}...")
        
        # Select the plant for this tower
        choice = input(f"   🌱 Select plant number for {tower_id} (Enter for random): ").strip()
        selected_plant = "lettuce" # Default
        
        if choice.isdigit():
            c_idx = int(choice) - 1
            if 0 <= c_idx < len(plant_list):
                selected_plant = plant_list[c_idx]
        
        towers_config.append({"id": tower_id, "plant": selected_plant})
        print(f"   ✅ {tower_id} set to grow {selected_plant.upper()}")

    return towers_config

def start_process(cmd, name):
    # Run a process and add it to the list.
    try:
        # Set up the Python environment
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd()
        
        # Run the process
        p = subprocess.Popen(cmd, env=env)
        all_processes.append(p)
        print(f"   ✅ Started: {name}")
        time.sleep(0.5) # A short delay to prevent interference
    except Exception as e:
        print(f"   ❌ Failed to start {name}: {e}")

def stop_all(signum, frame):
    print("\n🛑 Shutting down the entire farm...")
    for p in all_processes:
        try:
            p.terminate()
        except:
            pass
    sys.exit(0)

# ==========================================
# Main Execution
# ==========================================
if __name__ == "__main__":
    signal.signal(signal.SIGINT, stop_all)

    # 1. Run the wizard
    towers_to_run = setup_towers()

    print(f"\n🚀 Starting Infrastructure Services (Universal)...")
    
    # 2. General services (all controllers and core/base services)
    universal_services = [
        ("Catalog Service", ["catalog/catalog_service.py"]),
        ("Logger Service", ["logger/logger_service.py"]),
        ("Actuator Monitor", ["actuators/actuator_service.py"]),
        ("Alert Manager", ["controller/alert_manager.py"]),
        ("Cloud Bridge", ["cloud/thingsboard_service.py"]),    
        ("Universal Refill Ctrl", ["controller/refill_control.py"]),
        ("Universal pH Ctrl", ["controller/ph_control.py"]),
        ("Universal EC Ctrl", ["controller/ec_control.py"]),
        ("Universal Env Ctrl", ["controller/env_control.py"]),       
        ("Universal Light Ctrl", ["controller/lighting_control.py"])
    ]

    for name, script_path in universal_services:
        full_path = script_path[0]
        if os.path.exists(full_path):
            start_process([sys.executable, full_path], name)
        else:
            print(f"⚠️ Skipped {name} (File not found: {full_path})")

    print(f"\n🚀 Starting {len(towers_to_run)} Towers...")

    # 3. Run the towers (sensors and physical components)
    for t in towers_to_run:
        # Run command: python -m sensors.smart_sensor_service [TOWER_ID] [PLANT_NAME]
        cmd = [
            sys.executable, 
            "-m", "sensors.smart_sensor_service", 
            t["id"], 
            t["plant"]
        ]
        start_process(cmd, f"Node: {t['id']}")

    # 4. Run the dashboard
    print(f"\n📊 Launching Dashboard...")
    if os.path.exists(DASHBOARD_PATH):
        cmd = [sys.executable, "-m", "streamlit", "run", DASHBOARD_PATH]
        start_process(cmd, "Dashboard")
    else:
        print(f"❌ Dashboard file not found at: {DASHBOARD_PATH}")

    print("\n✅ FARM IS LIVE! Monitor via Dashboard.")
    print("👉 Dashboard: http://localhost:8501")
    print("👉 Actuators: http://localhost:9090")
    print("👉 Catalog:   http://localhost:8080")
    print("-----------------------------------")
    print("Press Ctrl+C to stop everything.\n")

    # Keep the runner alive
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            stop_all(None, None)