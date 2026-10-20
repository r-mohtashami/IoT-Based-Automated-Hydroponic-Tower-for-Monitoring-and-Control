# Concise Owner Demo Script

Use this as a short explanation while demonstrating the project. Keep all hardware claims explicitly simulated.

## 1. Opening

“This is an AI-assisted IoT platform for **simulated** home hydroponic towers. The important design choice is that AI is not the controller. FastAPI owns the domain rules, PostgreSQL stores authoritative state, MQTT carries simulated device messages, and every actuator execution requires an explicit user action plus backend revalidation.”

## 2. Architecture

“Each simulated Tower publishes telemetry to Mosquitto. The FastAPI backend validates and persists it, evaluates the active Planting snapshot, and exposes management/state through REST. Streamlit talks only to the backend. The optional local Ollama Assistant also goes through controlled backend tools; it has no raw SQL or MQTT access.”

## 3. Plant Profile vs Planting

“A Plant Profile is reusable crop configuration. A Planting is what is actually growing in one Tower and stores a snapshot of the selected profile. That prevents later profile edits from silently changing an active planting.”

## 4. Evidence-backed targets

“The official demo intentionally uses only two built-ins: Basil and Lettuce. Their pH and EC ranges come from Oklahoma State University Extension HLA-6722. Other factors are left unset rather than invented.”

## 5. Monitoring and isolation

“Here are three simulated Towers. Only Demo Tower 1 has an active Basil Planting, so crop-derived alerts belong only to that Tower. If a sensor is omitted, the UI says ‘Not monitored’; it never fabricates a zero.”

## 6. Human-in-the-Loop flow

“I create a controlled pH deviation. The backend detects that the observed value is outside the active Planting snapshot and creates an Alert and Recommendation. I can reject it, or approve it. Approval is deliberately not execution.”

Show an approved Recommendation with `executed_at` still empty and no execution events.

“Execution is a second explicit action. Before execution, the backend revalidates current telemetry again. If the condition recovered or changed, execution is blocked.”

## 7. Verification

“When execution is allowed, the backend sends an application-defined simulated hardware command over MQTT, such as the pH Up or pH Down pump. The simulator returns a correlated acknowledgement, but that acknowledgement only proves command acceptance. The Alert stays open until newer simulated telemetry demonstrates the physical state moving back into the configured range.”

Show Action History and `Review Indicator: On` / `verification_source=simulator_ack`.

## 8. AI boundary

“The assistant uses local Ollama with `qwen3.5:9b` and controlled tool calling. It can explain current application state, but it is not the controller. We demonstrated a real local AI flow where plain-text mutation requests were blocked, explicit approval was separate from execution, execution was revalidated, the simulated command was acknowledged, and only later telemetry confirmed recovery.”

## 9. Close

“The project is deliberately a reproducible student MVP: Docker Compose, modular monolith, no unnecessary infrastructure, validated with up to ten simulated Towers for the demo. It does not claim enterprise scale, exactly-once messaging, production authentication, or hardware safety. The final UX refinement and supervised local-AI interaction are included in this delivered version.”
