# Final Release Audit

This audit consolidates the final stabilization work, local Ollama Assistant integration, and supervised agentic action UI. The one-page Tower-centered structure, visual language, and human-in-the-loop control model remain unchanged.

## Final gaps corrected

### Tower lifecycle and simulator membership
- Added explicit Tower rename/remove controls with destructive confirmation.
- `DELETE /towers/{id}` removes Tower-local operational/history rows in dependency-safe order while preserving reusable Plant Profiles.
- UI-created Towers may omit internal device keys; the backend assigns a stable `sim-{tower_id}` key after persistence.
- The simulator can discover current Tower keys from the backend and add/remove virtual device state dynamically. New UI-created Towers therefore begin simulated telemetry without editing a fixed simulator count.
- Removed Towers stop publishing after the simulator's next discovery refresh.

### Simulator source of truth and reset
- Persistent virtual physical/device state remains simulator-owned on its Docker volume.
- Added explicit `reset_state.py --reset`; a technical restart still restores state rather than resetting it.
- Demo documentation now resets simulator state separately from PostgreSQL application history.

### Plant/Profile lifecycle clarity
- Built-ins remain immutable and are presented as Use / Make my own.
- User-owned/customized plants are reusable and editable for future growing cycles.
- Active Plantings remain snapshot-based; editing a saved plant does not silently alter an active cycle.
- Factor-level provenance now preserves inherited evidence only for unchanged customized factors; an edited factor becomes user-configured.

### Sensor/rule consistency
- Reservoir level is presented as a Tower/device sensor, not as a Basil/Lettuce crop target. No low-water threshold or automatic refill recommendation is invented because none is approved/configured.
- User-configured water temperature, air temperature, and humidity target deviations are now evaluated as **manual review** recommendations with no automatic actuator.
- Explicitly configured PPFD can use the supervised grow-light path.
- Missing sensors remain **Not monitored** and stale/future readings cannot appear healthy or authorize actions.

### Recommendation/outcome correctness retained
- Pending/approved recommendations are invalidated when the condition recovers or planting context changes.
- Delayed older telemetry is retained for history but cannot rewrite current Alert/Recommendation state.
- Direction changes supersede previous Alerts without falsely claiming recovery.
- Approval and execution remain separate explicit steps with revalidation at both boundaries.
- EC-high and other manual-only recommendations cannot send actuator MQTT commands.
- Simulator ACK remains distinct from physical recovery; later telemetry is required.

## Automated verification in this artifact

- Backend: **125 passed, 0 failed**.
- Streamlit: **38 passed, 0 failed**.
- Simulator: **19 passed, 0 failed**.
- Total: **182 passed, 0 failed**.
- Python compilation: `python -m compileall -q backend/app streamlit simulator` completed successfully.

## Honest limitations / final runtime gates

- The Owner completed the final local Docker Compose/browser runtime gate on the working tree: dynamic simulated telemetry, explicit Assistant approval, separate execution, application revalidation, simulated command acknowledgement, later telemetry-confirmed recovery, blocked plain-text mutation, and Tower add/remove behavior were exercised successfully.
- Local Ollama execution was validated with `qwen3.5:9b`. Runtime evidence includes grounded read-only status, blocked unauthorized textual mutation, explicitly authorized approval, separate explicitly authorized execution, simulator acknowledgement, and later telemetry-confirmed recovery.
- Reservoir refill exists as a simulated device capability, but the application deliberately does not generate a low-water correction because no approved operational threshold is configured.
- Deleting a Tower is intentionally destructive for this local student-project workflow; reusable Plant Profiles remain.
- Saved user Plant Profiles are editable but there is no saved-profile deletion workflow in this MVP. This does not block Tower/Planting operation and avoids deleting profiles referenced by historical Plantings.

## Minimum Owner runtime gate after replacement

1. Rebuild/recreate backend, Streamlit, and simulator without deleting volumes.
2. Confirm all services healthy and a newly added Tower receives fresh simulated telemetry automatically.
3. Remove that test Tower and confirm it disappears and no longer receives telemetry.
4. Start one built-in Planting and one user/custom Planting; confirm the active snapshot stays stable after editing the saved profile.
5. Run one intentional pH closed loop: out of range → Approve (no command) → Apply → correlated ACK → newer telemetry → recovery.
6. Confirm an EC-high or configured environmental review condition stays manual/non-executable.

Those runtime gates and the local AI acceptance flow were completed successfully. The remaining release work is documentation/package identity and submission.

## Final boundary-noise stabilization

Runtime acceptance on 2026-10-12 exposed repeated EC-high Alert/Recommendation records when simulated EC oscillated by a few thousandths around the configured 1.6 mS/cm boundary. This was a monitoring lifecycle problem, not an agronomic target problem.

The rule engine keeps the first excursion behavior unchanged, but now debounces both sides of the lifecycle. Pending or approved-but-unexecuted conditions require three consecutive in-range readings before recovery is confirmed, and a recovered condition requires three consecutive readings in the same out-of-range direction before reopening. The application settings are `ALERT_RECOVERY_CONFIRMATION_READINGS` and `ALERT_REOPEN_CONFIRMATION_READINGS` (both default `3`). Neither setting widens, shrinks, or otherwise modifies plant target ranges.

Executed corrections are intentionally different: the first newer in-range reading may still verify physical recovery immediately, preserving the HITL Execute → Verify flow. Regression coverage includes the alternating EC boundary case so `1.599 ↔ 1.601` noise cannot repeatedly create stale/new Alert/Recommendation pairs.
