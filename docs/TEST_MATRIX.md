# Final Test Matrix and Evidence Summary

This file records reproducible validation for the final repository. Automated results are engineering evidence for this student project, not production certification.

## Automated suites

| Area | Command / evidence | Coverage emphasis | Latest result |
| --- | --- | --- | --- |
| Backend | `docker compose exec -T backend python -m pytest -q` | API/domain models, Tower lifecycle, Plant/Profile snapshot rules, telemetry, rule engine, recommendation decisions/revalidation, controlled AI tools/orchestration, actuator execution, closed-loop outcomes, provenance | **125 passed, 0 failed** |
| Streamlit | `docker compose exec -T streamlit env PYTHONPATH=. python -m pytest tests -q` | REST-only client, one-page UI helpers/contracts, Assistant chat UX/client behavior, Tower removal controls, explicit plant choice/editing, stale/missing sensor semantics, chart/status helpers | **38 passed, 0 failed** |
| Simulator | `docker compose exec -T simulator python -m pytest tests -q` | persistent virtual state, dynamic backend Tower discovery, add/remove isolation, temporal continuity, actuator effects, missing sensors, command validation/ack, explicit state reset | **19 passed, 0 failed** |

Latest final local regression: **182 passed total, 0 failed**. The backend run emitted one dependency `DeprecationWarning` from Starlette/AnyIO; it did not fail a test.

These results do not replace the Owner's final Docker Compose/Mosquitto/browser acceptance for the exact delivered ZIP.

## Targeted integration/demo matrix

| Scenario | Expected evidence |
| --- | --- |
| Clean bootstrap | Explicit simulator-state reset + PostgreSQL demo reset; canonical built-ins only; requested positive Tower count |
| UI-created Tower | Backend assigns a stable internal `sim-{id}` key; dynamic simulator discovery starts telemetry without a manual simulator-count restart |
| Remove Tower | Explicit confirmation; Tower-local telemetry/Plantings/Alerts/Recommendations/ActuatorState/ActionLog removed; reusable Plant Profiles preserved; simulator stops that device after refresh |
| Plant choice | Named `Use <plant>` action; no silent selection |
| Built-in customization | Built-in remains immutable; customized copy is editable; unchanged evidence-backed factors retain evidence provenance; edited factors become user-configured |
| Active Planting snapshot | Later saved-plant edits do not silently alter an active growing cycle |
| Reservoir level | Tower/device sensor only; no plant-specific target or automatic low-water rule is invented |
| Custom environment target | Water/air temperature and humidity deviations generate manual-review recommendations only; no automatic actuator |
| PPFD target | If explicitly configured by the user, supervised grow-light recommendation path is available |
| Missing sensor | Missing value persists/renders as **Not monitored**; no fabricated value |
| Stale/future telemetry | Tower is not shown as healthy; approval/execution revalidation blocks unsafe stale/future readings |
| Reject | Recommendation rejected; no execution request/command |
| Recovery before approval | Old pending/approved recommendation becomes non-actionable/stale |
| Approve only | Decision recorded; no actuator action |
| Separate execute | Fresh execution-time revalidation before command publication |
| ACK vs recovery | Simulator acknowledgement is recorded separately; only newer telemetry can prove physical recovery |
| Duplicate execute | One `execution_requested` acquisition per Recommendation |
| Delayed telemetry | Older out-of-order readings remain in history but cannot rewrite current lifecycle state |
| 10-Tower configuration | Ten independent simulated Towers supported as a validation target, not a hard cap/benchmark |
| AI unavailable | Core non-AI monitoring/control remains usable; assistant reports unavailable |
| Real local Ollama AI | `qwen3.5:9b` demonstrated through `/assistant/chat`: grounded reads, unauthorized text mutation blocked, explicit approve, separate execute, simulator ACK, and later telemetry-confirmed recovery |

## Important evidence wording

- Simulated telemetry is not physical sensor evidence.
- `execution_verified` means correlated local simulator acknowledgement, not recovery.
- A configured model name alone is not proof of a working local AI runtime; runtime evidence was recorded separately.
- Deterministic AI tests validate application boundaries independently of model variability.
- No exactly-once MQTT/database, enterprise scale/authentication, or physical-device safety claim is made.
