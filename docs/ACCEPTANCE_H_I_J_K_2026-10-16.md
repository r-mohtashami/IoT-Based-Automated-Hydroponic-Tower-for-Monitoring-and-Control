Acceptance audit H–K, performed 16 October 2026 on the local Mac (Europe/Rome).

The release does **not** pass all acceptance gates. The complete stopped stack starts, but simultaneous startup loses saved simulator state for Towers beyond the three-Tower fallback. Packaging passes. Setup documentation needs corrections. Visual acceptance was unavailable.

| Gate | Result | Reason |
| --- | --- | --- |
| H — startup / restart / persistence | **FAIL** | Startup succeeds and PostgreSQL persists; simulator startup can discard saved device state. |
| I — canonical ZIP | **PASS** | ZIP integrity, required contents, prohibited-entry checks, and SHA-256 verified without modification. |
| J — reproducibility / setup | **FAIL** | Demo sequence fails with current persistent simulator and debounce semantics; Ollama installation/startup and ordinary restart instructions are incomplete. |
| K — visual browser acceptance | **NOT PERFORMED** | No connected browser or computer-use surface was available. |

**Scope and preservation**

The audited artifact is `/Users/rouyanm/MyProject/IoT_Hydroponic_FINAL_CANONICAL.zip`. All 101 archived files matched the workspace before this separate report was added. Runtime comparisons also matched 81 files across backend, simulator, and Streamlit, including application code, tests, requirements, and UI assets. No release source files were edited. The archive was neither rebuilt nor overwritten.

No `docker compose down -v`, volume removal, demo bootstrap reset, simulator reset utility, production SQL deletion, or volume recreation was performed. Existing tests and the demo replay used disposable test databases; the replay explicitly used SQLite in memory. The persistent volumes remained `iot_hydroponic_postgres_data` and `iot_hydroponic_simulator_state`. Container IDs and mount identities were identical before and after the stopped-stack test.

The existing deployment contained 11 Towers, four Plant Profiles, six Plantings, and historical recommendations/actions. It was deliberately not converted to a clean three-Tower demo.

**H — observed startup and persistence**

The non-destructive operational sequence was:

```bash
docker compose stop simulator
# Read and fingerprint the simulator state and all eight PostgreSQL tables.
docker compose restart backend
# Compare database fingerprints with simulator traffic still stopped.
docker compose start simulator
# Compare restored virtual state after an isolated simulator restart.
docker compose stop simulator
# Save another simulator/database baseline.
docker compose stop
# Confirm every service is stopped.
docker compose start --wait --wait-timeout 60
```

All five services were confirmed stopped before the final command. That command returned exit code 0 and brought the complete stack back up. This verifies existing-container startup from a stopped state; it does not claim a clean-image build or container recreation was tested. `docker compose images` separately reported missing metadata for the original simulator image SHA, although the existing container started successfully.

| Service | Final Docker status | Additional runtime evidence |
| --- | --- | --- |
| PostgreSQL | Running, healthy | `pg_isready` accepts connections; backend SQL queries succeed. |
| Simulator | Running, healthy | All 11 Towers receive telemetry, approximately five seconds old at the probe. |
| Streamlit | Running, healthy | `/` returns HTTP 200; `/_stcore/health` returns HTTP 200 and `ok`. |
| Backend | Running; no Docker healthcheck configured | `/health` returns HTTP 200 with `status=ok`, `database=connected`; API docs and domain endpoints respond. |
| Mosquitto | Running; no Docker healthcheck configured | Independent QoS 1 publish/subscribe round trip succeeds on a unique, non-retained diagnostic topic. |

The isolated backend restart preserved every row fingerprint and the aggregate SHA-256 of every table. The isolated simulator restart, performed with the backend already ready, restored all 11 Towers: pH, EC, actuator flags, and transient-effect collections were preserved; reservoir levels decreased by the expected simulated consumption.

The simultaneous full-stack startup failed virtual-state persistence. At 12:39:05 Europe/Rome, initial backend discovery returned connection refused. The simulator loaded only fallback keys `sim-1` through `sim-3` and saved that subset. At 12:39:10, discovery succeeded and it created `sim-4` through `sim-11` as new virtual devices. Their previous persisted values were lost despite retaining the volume.

| Tower | Reservoir before stop (%) | Reservoir after startup (%) | Observation |
| --- | ---: | ---: | --- |
| sim-1 | 79.32083333333598 | 79.32020833333598 | Expected consumption; retained state. |
| sim-4 | 82.08333333333337 | 82.11958333333334 | Increased toward initial state without a refill. |
| sim-11 | 82.20333333333336 | 82.23958333333333 | Increased toward initial state without a refill. |

The same reset pattern affected all eight keys `sim-4` through `sim-11`. The recorded actuator flags happened to equal defaults, so this run does not independently demonstrate loss of a non-default actuator flag. The startup path discards each affected Tower's complete saved state object.

Relevant implementation: [simulator/app.py](../simulator/app.py#L33) falls back after failed discovery and saves the reduced state at line 90; [load_states](../simulator/simulator.py#L247) loads only requested keys; later discovery initializes missing keys through `sync_states_to_tower_keys`. [Compose](../docker-compose.yml#L72) requires the backend only to be started, with no backend readiness healthcheck.

The database did not reset during full startup:

| Table | Before | After startup | Existing rows lost | Existing rows changed |
| --- | ---: | ---: | ---: | ---: |
| towers | 11 | 11 | 0 | 0 |
| plant_profiles | 4 | 4 | 0 | 0 |
| plantings | 6 | 6 | 0 | 0 |
| telemetry_readings | 215209 | 215231 | 0 | 0 |
| alerts | 263 | 263 | 0 | 2 |
| recommendations | 263 | 263 | 0 | 0 |
| actuator_states | 2 | 2 | 0 | 0 |
| action_logs | 290 | 290 | 0 | 0 |

Two Alert rows changed while telemetry resumed. Towers, profiles, planting snapshots, recommendations, actuator records, and action history retained identical hashes. Existing telemetry retained its individual row hashes and new readings were appended.

Post-restart application checks succeeded for profile listing, active planting, Alerts, Recommendations, actuator state, and Action History (`/towers/{id}/actions`). Streamlit's nonvisual `AppTest` ran the dashboard and Assistant dialog with no exceptions or error elements; the dialog exposed a chat input and Clear button. A real read-only `/assistant/chat` request returned HTTP 200 in 25.75 seconds, called `list_towers`, correctly reported 11 Towers, and labelled the data simulated. These checks establish service and application execution evidence, while the visual gate remains open.

**I — exact artifact identity and contents**

```text
Filename: IoT_Hydroponic_FINAL_CANONICAL.zip
Size: 20098409 bytes
SHA-256: 8a27c23b89e943797a4a9c8e3ad4b0feca7fdef6279ca10e6edd032b25c17050
Archive root: IoT_Hydroponic/
Entries: 114 (101 files, 13 directories)
```

Python `zipfile.ZipFile` opened the archive and `testzip()` returned `None`, confirming all entries decompressed and passed CRC checks. Every archive path was inspected. None contained `.env`, `.git`, `__pycache__`, `.pytest_cache`, `.DS_Store`, a `.pyc` file, or a nested ZIP.

All required entries were present under the single archive root: `README.md`, `.env.example`, `docker-compose.yml`, `backend/`, `streamlit/`, `simulator/`, `mosquitto/`, and `docs/`. The SHA-256 was recomputed after runtime acceptance and was unchanged.

**J — setup accuracy and reproducibility findings**

| Evaluator need | Finding |
| --- | --- |
| Prerequisites | Docker Desktop, Compose, shell, optional Git/Python/Ollama are listed. Ollama installation instructions and model resource expectations are absent. |
| Initial stack setup | Build, staged infrastructure startup, schema creation, explicit destructive demo bootstrap, simulator startup, and URLs match the Compose layout. These destructive setup commands were inspected, not run during this audit. |
| Resume existing state | Shutdown preserves volumes, but there is no dedicated ordinary restart procedure. The fresh-start recipe includes a destructive bootstrap and should not be reused to resume existing work. The unconditional persistence claim at README line 242 is contradicted by H. |
| Ollama | Current runtime matches README and Compose: host Ollama `0.40.4`, `OLLAMA_BASE_URL=http://host.docker.internal:11434`, `OLLAMA_MODEL=qwen3.5:9b`, timeout 120 seconds. The installed model reports ID `d63f830f28d5`, size 7.6 GB. Backend status reports available and live inference works. README provides `ollama pull qwen3.5:9b`, but no installation link or explicit runtime startup command/app instructions. |
| Tests | All three README commands execute successfully as written; results below. |
| Demo | Linked procedure exists, but its sequential pH/reject/recovery instructions fail with current simulator persistence and debounce. |
| Simulation | Clearly describes simulated telemetry, virtual devices, actuator effects/ACKs, assumed dynamics, and absence of physical-device validation. |
| AI authority | Clearly describes controlled reads, explicit authorized single-Recommendation mutations, separate approval/execution, backend revalidation, no arbitrary thresholds/doses, and no direct SQL/MQTT access. |

The main reproducibility defect is in [docs/DEMO.md](DEMO.md#L83), Steps 5–8. A read-only audit of production modules was supplemented by replaying the documented sequence against an isolated in-memory database and virtual state:

| Step | Underlying pH | Observed pH | Application result |
| --- | ---: | ---: | --- |
| Initial reading | 6.08 | 6.084 | Pending recommendation, open Alert. |
| Step 5 recovery offset −0.3 | 5.78 | 5.784 | Single in-range sample; Alert stays open. |
| Step 5 deviation +0.5 | 6.28 | 6.284 | Original recommendation remains pending. |
| Step 6 reject | — | — | Original recommendation becomes rejected. |
| Step 7 repeats recovery −0.3 | 5.98 | 5.984 | Still only one in-range sample. |
| Step 7 repeats deviation +0.5 | 6.48 | 6.484 | Alert stays open; no new pending recommendation exists. |
| Step 7 claimed recovery −0.3 | 6.18 | 6.184 | Still above Basil's configured pH maximum of 6.0. |

The subsequent `max()` over pending recommendations has an empty list. Each one-shot invocation modifies persistent physical state, so offsets accumulate. Current recovery/reopen defaults also require three consecutive confirming samples, while the guide relies on individual one-shot readings. The guide must use deliberate, state-aware recovery episodes with enough confirming samples and freshly resolved recommendation IDs. Step 8's expected actuator state `on` is also stale for pH dosing: the current bounded `apply_step` command uses `completed`.

Additional documentation inconsistencies:

- README's ten-Tower recipe omits the simulator-state reset that `docs/DEMO.md` includes for that recipe. Reused keys can retain earlier virtual state.
- `docs/DEMO.md` begins an explicit reset without first stopping a previously running simulator; it is suitable only when that simulator is already stopped, an assumption that should be stated.
- README repeats the “Controlled tools implemented” bullet at lines 144–145.
- Prior release-audit prose both leaves runtime gates to the Owner and later says they were completed. This new audit establishes that the persistence gate currently fails; previous acceptance wording must not override that evidence.

Executed regression results:

```text
docker compose exec -T backend pytest -q                 124 passed
docker compose exec -T streamlit python -m pytest -q       35 passed
docker compose exec -T simulator python -m pytest -q       19 passed
Total                                                   178 passed
```

Backend emitted one Starlette/AnyIO deprecation warning. Passing tests did not detect the simultaneous-start persistence failure or the sequential documentation failure.

**K — visual acceptance status**

**Visual UI acceptance NOT performed**

The Browser skill was loaded and its supported connection setup attempted for `http://localhost:8501/`. It returned “No browser is available.” The documented troubleshooting discovery returned an empty browser list. No connected computer-use capability was exposed.

Consequently, broken layout, clipped/overflowing text, awkward empty states, dialog usability, button behavior in a real browser, visible raw exceptions, normal-UI identifier leakage, Assistant transcript/input behavior, loading/error presentation, and normal laptop viewport responsiveness are all **unverified visually**. No screenshot or browser interaction is claimed. HTTP checks, AppTest, source inspection, and unit tests do not produce a K PASS.

One additional response-quality observation came from the real Assistant API request: its otherwise correct reply contained empty quoted examples after “tower keys like”. This was observed in API output; its visual presentation was not inspected.

**Disposition**

Keep H and J open until simulator startup preserves all saved devices during backend unavailability and the demo/setup instructions are corrected and replayed. Keep K open until a connected browser can perform the requested visual checklist. I passes for exactly the SHA-256 above. All five services were left running; the canonical artifact and release source files remain unchanged.

Detailed temporary command records, row fingerprints, state snapshots, and probe outputs are stored at `/private/tmp/hydroponic-hijk-20261016/`. This report is a separate workspace addition and is not part of the canonical ZIP.
