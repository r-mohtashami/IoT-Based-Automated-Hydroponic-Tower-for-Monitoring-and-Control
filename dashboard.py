import streamlit as st
import json
import time
import pandas as pd
import requests
import paho.mqtt.client as mqtt
from datetime import datetime
from pathlib import Path

# --- Page Config ---
st.set_page_config(page_title="Smart Farm Dashboard", page_icon="🏭", layout="wide")

CONFIG_PATH = Path("catalog/config.json")
LOG_FILE_PATH = Path("logger/system_events.log")

# ==========================================
# Central memory (Farm State)
# ==========================================
# Structure: { "tower_1": { "sensors": {}, "status": "Running", "last_seen": time }, ... }
if 'farm_store' not in st.session_state:
    st.session_state.farm_store = {}

if 'selected_tower' not in st.session_state:
    st.session_state.selected_tower = None

# --- Support functions---
def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def send_system_command(tower_id, command):
    # Send a system command (PAUSE/RESUME) to a specific tower
    if 'mqtt_client' in st.session_state:
        topic = f"garden/{tower_id}/cmd/system"
        payload = json.dumps({"target": "system", "action": command})
        st.session_state.mqtt_client.publish(topic, payload)
        
        # Update the status in the dashboard memory
        if tower_id in st.session_state.farm_store:
            st.session_state.farm_store[tower_id]["status"] = "Paused" if command == "PAUSE" else "Running"

def load_system_logs():
    if not LOG_FILE_PATH.exists(): return pd.DataFrame()
    data_rows = []
    
    # A dictionary for converting technical names to user-friendly names in the logs
    NAME_MAP = {
        "pump_refill": "💧 Refill Pump",
        "nutrient_pump": "🧪 Nutrient Pump",
        "pump_ph_up": "⬆️ pH Up",
        "pump_ph_down": "⬇️ pH Down",
        "cooling_fan": "💨 Cooling Fan",
        "grow_light": "💡 Grow Light",
        "system": "🖥️ System Control"
    }

    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-100:] 
            
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                details = entry.get("details", {})
                event_type = entry.get("event_type", "INFO")
                
                # Assign an icon and category
                icon = "ℹ️"
                category = "General"
                
                if event_type == "ALERT":
                    icon = "🚨"
                    category = "Alert"
                elif event_type == "ACTION":
                    icon = "⚙️"
                    category = "Action"
                
                # --- The main fix is here: build the smart message text ---
                target = details.get("target")
                action = details.get("action")
                msg_text = details.get("message") or details.get("msg")
                
                final_message = str(details) # Default

                # Case 1: If it is a control command (Target + Action)
                if target and action:
                    friendly_name = NAME_MAP.get(target, target) # Convert the name into a nicer (more user-friendly) format
                    final_message = f"{friendly_name} ➔ {action}"
                
                # Case 2: If it is a text message (like alerts)
                elif msg_text:
                    final_message = msg_text
                
                row = {
                    "Icon": icon,
                    "Time": datetime.strptime(entry.get("time"), "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S"),
                    "Tower": entry.get("tower_id", "System"),
                    "Category": category,
                    "Message": final_message  # Corrected message
                }
                data_rows.append(row)
            except: continue
            
        return pd.DataFrame(data_rows)
    except: return pd.DataFrame()

@st.cache_data(ttl=60)
def get_thingspeak_history():
    cfg = load_config()
    ts = cfg.get("thingspeak", {})
    if not ts.get("enabled"): return None
    try:
        url = f"https://api.thingspeak.com/channels/{ts['channel_id']}/feeds.json?results=50&api_key={ts['read_api_key']}"
        resp = requests.get(url).json()
        df = pd.DataFrame(resp['feeds'])
        df['created_at'] = pd.to_datetime(df['created_at'])
        
        # Mapping Fields
        rename_map = {f"field{v}": k for k, v in ts.get("field_map", {}).items()}
        df.rename(columns=rename_map, inplace=True)
        for c in rename_map.values():
            if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
        return df.set_index('created_at')
    except: return None

# ==========================================
# MQTT Logic (Multi-Tower)
# ==========================================
def on_message(client, userdata, msg):
    try:
        topic = msg.topic # garden/tower_1/sensors/data
        payload = json.loads(msg.payload.decode())
        store = userdata 
        
        parts = topic.split("/")
        if len(parts) < 3: return
        
        tower_id = parts[1] # tower_1
        msg_type = parts[2] # sensors, cmd, alerts

        # Register a new tower in memory (if it wasn’t already there)
        if tower_id not in store:
            store[tower_id] = {
                "sensors": {}, 
                "actuators": {}, 
                "alerts": [],
                "status": "Running",
                "last_seen": datetime.now()
            }
        
        store[tower_id]["last_seen"] = datetime.now()

        if msg_type == "sensors":
            clean = {k: v for k, v in payload.items() if isinstance(v, (int, float))}
            store[tower_id]["sensors"] = clean
            
            # --- Important fix: respect the Pause state ---
            # Only set the status to Running if it has not been manually paused
            current_status = store[tower_id].get("status", "Running")
            if current_status != "Paused":
                store[tower_id]["status"] = "Running"
            
        elif msg_type == "cmd":
            dev = payload.get("target") or parts[-1]
            act = payload.get("action")
            if dev and act: store[tower_id]["actuators"][dev] = act
            
        elif msg_type == "alerts":
            txt = payload.get('msg')
            if txt:
                store[tower_id]["alerts"].insert(0, {"time": datetime.now().strftime('%H:%M'), "text": txt})

    except: pass

if 'mqtt_connected' not in st.session_state:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=st.session_state.farm_store)
    client.on_message = on_message
    try:
        client.connect("localhost", 1883)
        client.subscribe("garden/#")
        client.loop_start()
        st.session_state['mqtt_connected'] = True
        st.session_state['mqtt_client'] = client
    except: pass

# ==========================================
# Dashboard UI
# ==========================================

# --- Section 1: Farm-wide management (Overview) ---
st.title("🏭 Farm Control Center")

towers = list(st.session_state.farm_store.keys())

if not towers:
    st.info("📡 Waiting for towers... (Start runner.py)")
else:
    st.subheader("📍 Active Towers Management")
    # Display management cards for each tower
    cols = st.columns(len(towers)) if len(towers) <= 4 else st.columns(4)
    
    for idx, t_id in enumerate(towers):
        data = st.session_state.farm_store[t_id]
        status = data.get("status", "Running")
        
        # Calculate the time since the last message
        last = data.get("last_seen")
        is_online = (datetime.now() - last).seconds < 10 if last else False
        
        col = cols[idx % 4]
        with col:
            with st.container(border=True):
                st.markdown(f"### 🏗️ {t_id.upper()}")
                
                if is_online:
                    st.caption("🟢 Online")
                else:
                    st.caption("🔴 Offline")

                # Control buttons (Stop / Continue)
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("⏸ Pause", key=f"pause_{t_id}", disabled=(status=="Paused" or not is_online)):
                        send_system_command(t_id, "PAUSE")
                        st.rerun()
                with c2:
                    if st.button("▶ Resume", key=f"resume_{t_id}", disabled=(status=="Running" or not is_online)):
                        send_system_command(t_id, "RESUME")
                        st.rerun()

                if status == "Paused":
                    st.warning("⚠️ Tower Paused")
                else:
                    st.success("✅ Operational")

    st.divider()

# --- Section 2: Details and charts ---
with st.sidebar:
    st.header("🔍 Inspection")
    if towers:
        selected = st.selectbox("Select Tower to Inspect:", towers, index=0)
        st.session_state.selected_tower = selected
    else:
        st.warning("No towers available.")
        selected = None

if selected:
    tower_data = st.session_state.farm_store[selected]
    
    st.header(f"📊 Details: {selected.upper()}")
    
    tab1, tab2, tab3 = st.tabs(["📡 Live Monitor", "☁️ History", "📜 Logs"])

# === TAB 1: Live ===
    with tab1:
        # Sensors
        sens = tower_data["sensors"]
        c1, c2, c3, c4, c5 = st.columns(5)
        
        # Fixed version (without key) to resolve the TypeError
        c1.metric("pH", sens.get("ph", "--"))
        c2.metric("EC", sens.get("ec", "--"))
        c3.metric("Temp", f"{sens.get('air_temperature', '--')} °C")
        c4.metric("Water", f"{sens.get('water_level', '--')} %")
        c5.metric("Light", sens.get("light_intensity", "--"))

        # Actuators
        st.subheader("⚙️ Actuators")
        acts = tower_data["actuators"]
        ac1, ac2, ac3, ac4, ac5, ac6 = st.columns(6)
        
        dev_map = {
            "pump_refill": "💧 Refill", "nutrient_pump": "🧪 Nutrient",
            "pump_ph_up": "⬆️ pH Up", "pump_ph_down": "⬇️ pH Down",
            "cooling_fan": "💨 Fan", "grow_light": "💡 Light"
        }
        
        col_list = [ac1, ac2, ac3, ac4, ac5, ac6]
        for i, (key_name, label) in enumerate(dev_map.items()):
            state = acts.get(key_name, "OFF")
            # رنگی کردن وضعیت
            delta_val = "ON" if state in ["ON", "DOSE"] else None
            
            col_list[i].metric(
                label, 
                state, 
                delta=delta_val, 
                delta_color="normal"
            )
# === TAB 2: Cloud History ===
    with tab2:
        st.header("☁️ ThingsBoard Cloud Dashboard")
        st.info("Historical data and advanced analytics are hosted on ThingsBoard.")
        
        st.markdown(f"""
        each tower has its own dedicated device panel.
        
        **Access your Cloud Dashboard here:**
        [👉 Open eu.thingsboard.cloud](https://eu.thingsboard.cloud/dashboards)
        
        **Active Device:** `{selected.upper() if selected else 'None'}`
        """)
        
# === TAB 3: Smart Logs ===
    with tab3:
        st.subheader("📜 Event Log & Alerts")
        
        df_logs = load_system_logs()
        
        if not df_logs.empty:
            # 1. Filter logs for the selected tower
            tower_logs = df_logs[df_logs["Tower"] == selected].copy()
            
            if not tower_logs.empty:
                # --- Filtering section---
                c_filter1, c_filter2 = st.columns([3, 1])
                with c_filter1:
                    # Select the message type (alert or action)
                    selected_cats = st.multiselect(
                        "Filter by Type:",
                        options=["Alert", "Action", "General"],
                        default=["Alert", "Action"]
                    )
                with c_filter2:
                    # Refresh button
                    if st.button("🔄 Refresh Logs"):
                        st.rerun()

                # Apply the user’s filter
                filtered_df = tower_logs[tower_logs["Category"].isin(selected_cats)]

                # --- Display a status summary (metrics) ---
                alert_count = len(tower_logs[tower_logs["Category"] == "Alert"])
                if alert_count > 0:
                    st.warning(f"⚠️ Found {alert_count} recent alerts for {selected}!")
                
                # --- Display a nice/clean table ---
                # Use column_config to configure the appearance of the columns
                st.dataframe(
                    filtered_df[["Icon", "Time", "Category", "Message"]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Icon": st.column_config.TextColumn("Type", width="small", help="Event Type"),
                        "Time": st.column_config.TextColumn("Time", width="medium"),
                        "Category": st.column_config.TextColumn("Category", width="medium"),
                        "Message": st.column_config.TextColumn("Details", width="large"),
                    }
                )
            else:
                st.info(f"No logs found for {selected}.")
        else:
            st.info("Log file is empty.")

time.sleep(1)
st.rerun()