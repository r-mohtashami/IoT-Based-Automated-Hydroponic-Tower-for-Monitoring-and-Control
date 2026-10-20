# Reproducible Final Demo

This procedure starts from a clean official demo dataset and avoids historical database IDs. All telemetry and actuator behavior are simulated.

## 1. Build and start the base services

```bash
cp .env.example .env
docker compose build
docker compose up -d postgres mosquitto backend streamlit
docker compose exec -T backend python -m app.create_schema
```

## 2. Bootstrap the official three-Tower dataset

Reset the simulator-owned virtual physical state and the PostgreSQL demo data explicitly:

```bash
docker compose run --rm --no-deps simulator python reset_state.py --reset
docker compose exec -T backend \
  python -m app.demo_bootstrap --reset --tower-count 3
```

Expected logical dataset:

- `sim-1` / Demo Tower 1 — active `Basil — Hydroponic` Planting snapshot;
- `sim-2` / Demo Tower 2 — no active Planting;
- `sim-3` / Demo Tower 3 — no active Planting;
- official built-ins only: `Basil — Hydroponic`, `Lettuce — Hydroponic`.

Start continuous telemetry:

```bash
docker compose up -d simulator
```

Open Streamlit at http://localhost:8501.

## 3. Verify clean state without fixed IDs

```bash
python3 - <<'PY'
import json, urllib.request
BASE = "http://localhost:8000"
towers = json.load(urllib.request.urlopen(BASE + "/towers"))
profiles = json.load(urllib.request.urlopen(BASE + "/plant-profiles"))
print("towers:", [(t["tower_key"], t["name"]) for t in towers])
print("builtins:", [p["name"] for p in profiles if p["profile_type"] == "system_builtin"])
PY
```

Resolve Demo Tower 1 dynamically when needed:

```bash
TOWER_ID=$(python3 - <<'PY'
import json, urllib.request
rows = json.load(urllib.request.urlopen("http://localhost:8000/towers"))
print(next(r["id"] for r in rows if r["tower_key"] == "sim-1"))
PY
)
echo "$TOWER_ID"
```

## 4. Missing-sensor demonstration

Stop the continuous simulator so a one-shot reading remains the latest state:

```bash
docker compose stop simulator
```

Publish PPFD as missing only for `sim-2`:

```bash
docker compose run --rm \
  -e SIM_MISSING_SENSORS='sim-2:ppfd_umol_m2_s' \
  -e SIM_RUN_ONCE=true \
  simulator
```

In Streamlit, Demo Tower 2 must display PPFD as **Not monitored**, not zero.

## 5. Prepare a clean pH episode

First publish a recovery reading for Demo Tower 1. The offset is a simulator test input, not a crop target:

```bash
docker compose run --rm \
  -e SIM_RUN_ONCE=true \
  -e SIM_DEVIATION_TOWER=sim-1 \
  -e SIM_DEVIATION_FACTOR=ph \
  -e SIM_DEVIATION_OFFSET=-0.3 \
  simulator
```

Then create a fresh above-range episode:

```bash
docker compose run --rm \
  -e SIM_RUN_ONCE=true \
  -e SIM_DEVIATION_TOWER=sim-1 \
  -e SIM_DEVIATION_FACTOR=ph \
  -e SIM_DEVIATION_OFFSET=0.5 \
  simulator
```

Resolve the newest pending Recommendation dynamically:

```bash
REC_ID=$(python3 - <<'PY'
import json, urllib.request
BASE = "http://localhost:8000"
towers = json.load(urllib.request.urlopen(BASE + "/towers"))
tower = next(t for t in towers if t["tower_key"] == "sim-1")
rows = json.load(urllib.request.urlopen(BASE + f"/towers/{tower['id']}/recommendations?status=pending"))
print(max(rows, key=lambda r: r["id"])["id"])
PY
)
echo "$REC_ID"
```

## 6. Reject path

Reject the pending Recommendation:

```bash
curl -s -X POST "http://localhost:8000/recommendations/$REC_ID/reject" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"Rejected during final demo."}'
```

Verify in Streamlit Action History that the Recommendation is rejected and has no `execution_requested`, command-sent, or verification event.

## 7. Recovery / stale revalidation path

Create another fresh above-range episode using Step 5, resolve its new `REC_ID`, then publish recovery **before approval**:

```bash
docker compose run --rm \
  -e SIM_RUN_ONCE=true \
  -e SIM_DEVIATION_TOWER=sim-1 \
  -e SIM_DEVIATION_FACTOR=ph \
  -e SIM_DEVIATION_OFFSET=-0.3 \
  simulator
```

Attempt approval:

```bash
curl -i -X POST "http://localhost:8000/recommendations/$REC_ID/approve"
```

Expected behavior: backend returns `409`, the Recommendation becomes stale/blocked because the latest telemetry returned to the configured range, and no execution event is created.

## 8. Successful supervised execution

Create one more fresh above-range episode using Step 5 and resolve its new `REC_ID`.

Approve:

```bash
curl -s -X POST "http://localhost:8000/recommendations/$REC_ID/approve"
```

Before execution, verify in Streamlit or REST that:

- Recommendation status is `approved`;
- `executed_at` is null;
- Action History contains `recommendation_approved`;
- no execution event exists yet.

Start continuous simulator traffic so the backend has fresh telemetry and the simulator is available for the command acknowledgement:

```bash
docker compose up -d simulator
```

Execute as a **separate** action:

```bash
curl -s -X POST "http://localhost:8000/recommendations/$REC_ID/execute"
```

Required successful evidence:

1. execution-time revalidation evaluates current telemetry against the active Planting snapshot;
2. `execution_requested` is recorded;
3. MQTT command is published and `actuator_command_sent` is recorded;
4. simulator returns a correlated acknowledgement;
5. only then is `execution_verified` recorded and `ActuatorState` updated.

Read verified simulated actuator state dynamically:

```bash
curl -s "http://localhost:8000/towers/$TOWER_ID/actuator-states"
```

Expected demonstration actuator:

- `actuator_type = ph_up_pump` or `ph_down_pump` for the pH demo
- actuator acknowledgement is not physical recovery; wait for newer telemetry
- `state = on`
- `simulated = true`
- `verification_source = simulator_ack`

This is a demonstration indicator, not an agronomic dosing/pump/light action.

## 9. Ten-Tower configuration validation

Ten is a project validation target, not a maximum or performance benchmark.

```bash
docker compose stop simulator
docker compose run --rm --no-deps simulator python reset_state.py --reset

docker compose exec -T backend \
  python -m app.demo_bootstrap --reset --tower-count 10

docker compose run --rm \
  -e SIM_DISCOVER_TOWERS_FROM_BACKEND=false \
  -e SIMULATED_TOWER_COUNT=10 \
  -e SIM_RUN_ONCE=true \
  simulator
```

Verify `/towers` returns ten Towers and each has independent latest simulated telemetry.

Restore the standard three-Tower final demo afterward:

```bash
docker compose run --rm --no-deps simulator python reset_state.py --reset
docker compose exec -T backend \
  python -m app.demo_bootstrap --reset --tower-count 3

docker compose up -d simulator
```

## 10. AI evidence boundary

The validated Assistant uses local Ollama with `qwen3.5:9b`.

Check availability:

```bash
curl -s http://localhost:8000/assistant/status
```

For the validated local demo, `available` should be `true` and the reported model should be `qwen3.5:9b`.

If `available` is false, the core demo remains valid; start Ollama on the host and ensure the model is installed:

```bash
ollama pull qwen3.5:9b
```

AI acceptance evidence should distinguish model output from application authority: plain chat text cannot authorize mutations; approve/reject/execute require explicit application action controls, and approval remains separate from execution.

## 11. Tests and final health

```bash
docker compose exec -T backend pytest -q
docker compose exec -T streamlit python -m pytest -q
docker compose exec -T simulator python -m pytest -q
docker compose ps
```

## 12. Shutdown

```bash
docker compose down
```

This preserves the PostgreSQL volume.


## Corrective closed-loop simulator semantics

The simulator owns persistent per-Tower virtual physical/device state on the `simulator_state` Docker volume. A technical simulator restart restores that state; a reset is an explicit demo/planting action, not an incidental restart side effect. Sensor noise is observation noise only and does not replace the underlying physical state.

The supervised flow is: **Detect → Explain → Recommend → Approve/Reject → Revalidate → Execute → Actuator ACK → New telemetry → Outcome verification**. ACK never closes an Alert. If a bounded correction step does not recover the condition within the deterministic observation window, the application creates one new follow-up Recommendation; it does not repeat execution autonomously.

EC-low may use the simulated nutrient dosing pump. EC-high is manual dilution / solution-maintenance only and has no executable actuator in the MVP. Built-in target evidence is stored at factor level; editing a customized factor changes that factor to user-configured provenance and removes the inherited system-evidence claim for the edited value.

All numeric simulator drift, sensor-noise envelopes and actuator-effect coefficients are **SIMULATED / ASSUMED APPLICATION CONFIGURATION**. They are not agronomic evidence, dosing instructions, or physical-device safety guidance.
