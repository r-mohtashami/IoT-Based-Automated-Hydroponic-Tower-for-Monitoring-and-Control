# Portfolio Summary

## Short version

Built a Dockerized, AI-assisted IoT platform for **simulated hydroponic towers** using FastAPI, PostgreSQL, Mosquitto, Streamlit, Ollama, and pytest. The system persists multi-Tower MQTT telemetry, applies evidence-backed Basil/Lettuce pH and EC targets, creates deterministic Alerts/Recommendations, and enforces a human-in-the-loop workflow where approval and execution are separate actions with backend revalidation. Simulated actuator verification requires a correlated MQTT acknowledgement, while later telemetry is required to confirm recovery. A local `qwen3.5:9b` Assistant can access only controlled application tools and cannot bypass application authority.

## Defensible claims

- modular-monolith backend with PostgreSQL persistence;
- configurable simulated multi-Tower telemetry over MQTT;
- evidence-backed crop targets with stored source references;
- Plant Profile / Planting snapshot separation;
- deterministic Alerts and Recommendations;
- approve/reject plus stale/current-state revalidation;
- separate execution action with execution-time revalidation;
- correlated simulated MQTT acknowledgement and persisted `ActuatorState`;
- explicit command-sent vs verified distinction;
- controlled AI tool boundary with no raw SQL/MQTT access;
- Docker Compose reproducibility and automated tests;
- ten-Tower demo validation without a hard-coded ten-Tower maximum.

## Claims intentionally not made

- physical-hardware validation or safety;
- autonomous nutrient/pH dosing;
- exactly-once MQTT/PostgreSQL execution;
- production authentication/RBAC;
- enterprise/commercial greenhouse scale;
- ML prediction or computer-vision diagnosis;
- autonomous LLM control or unrestricted model access to SQL/MQTT.
