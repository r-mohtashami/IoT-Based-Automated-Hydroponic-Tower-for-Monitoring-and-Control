# Final stabilization pass

This package is based on the last ChatGPT-authored UX V2 line and keeps the one-page design, artwork, colors, typography, and core architecture unchanged. The latest consolidated evidence is in [`FINAL_RELEASE_AUDIT.md`](FINAL_RELEASE_AUDIT.md).

## User-facing corrections retained

- Single native X for dialogs; no duplicate Close control or duplicate generic Tower title.
- Explicit named plant actions; built-ins can be used as-is or copied; user-owned plants can be edited for future cycles.
- Active growing cycles keep their saved Planting snapshot.
- Reservoir level is labeled as a Tower sensor rather than a plant target; missing sensors remain **Not monitored**.
- Explicit end-growing-cycle flow, guided Add Tower flow, full EC unit rendering, and data/target-aware trend scaling.
- Tower settings include rename and confirmed removal.

## Final lifecycle/technical additions

- UI-created Towers receive stable internal device keys and are dynamically discovered by the stateful simulator.
- Explicit simulator-state reset is separate from database/demo reset.
- Tower removal also removes its local operational/history rows while preserving Plant Profiles.
- Customized-factor provenance is preserved only while the evidence-backed value remains unchanged.
- User-configured water/air temperature and humidity targets are manual-review conditions; configured PPFD can use the supervised grow-light path.
- Reservoir level has no invented low-water rule/threshold.
- Existing stale/future telemetry protection, delayed-reading handling, approve/execute separation, EC-high manual-only behavior, ACK-vs-recovery separation, and restart persistence remain intact.

## Automated verification in the final audit

- Backend: **125 passed**
- Streamlit: **38 passed**
- Simulator: **19 passed**
- Total: **182 passed, 0 failed**

The Owner completed the local Docker/browser supervised-control runtime gate described in `FINAL_RELEASE_AUDIT.md`. Local Ollama Assistant behavior, deterministic explicit action handling, ACK-vs-recovery separation, and telemetry-confirmed recovery are included in the final validation evidence.
