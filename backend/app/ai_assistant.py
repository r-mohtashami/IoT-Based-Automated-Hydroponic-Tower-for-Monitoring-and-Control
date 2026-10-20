from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.ai_tools import ACTION_TOOL_NAMES, dispatch_tool


DEFAULT_OLLAMA_MODEL = "qwen3.5:9b"
DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 120.0
MAX_TOOL_ROUNDS = 8

ASSISTANT_INSTRUCTIONS = """
You are the operational assistant for a simulated home hydroponic monitoring project.
Use application tools for all current project state. Do not rely on model knowledge for telemetry,
agronomic targets, alert status, recommendation status, approvals, or execution evidence.

Keep these distinctions explicit:
- telemetry is observed application data and may be simulated;
- targets come from the active planting profile snapshot and its stored references;
- alerts are derived application findings;
- recommendations are historical application proposals and are not current observations;
- approval is not execution;
- approval never automatically triggers execution; after approval, a separate explicit Apply/Execute application control is required;
- a command being sent is not the same as verified execution;
- every actuator execution in this project is simulated.

Never invent missing sensor values, unsupported targets, thresholds, dosing amounts, actuator durations,
safety limits, or actuator parameters. If a sensor is missing, say it is not monitored/unavailable.
If a profile has no target for a factor, say the target is unsupported/unset.
Never claim a condition is an emergency or safety violation from an optimum target range alone.

State-changing tools are exposed only when the application has authorized one exact action for one exact
Recommendation in this turn. Do not request a different mutation, a different Recommendation, or a
second mutation after an action result. Existing Phase 7/8 application logic remains the lifecycle and
execution authority. Report failed actions as failures. Never call an execution verified unless the
application result explicitly has execution_verified=true. Never say an approval, rejection, or execution happened
unless the corresponding state-changing application tool was actually authorized and completed in this turn. If a
user asks for a mutation in plain chat without an authorized action tool, state that no action was performed and that
the explicit application action control is required. Whenever application data marks telemetry or actuator evidence as
simulated, explicitly label that data as simulated in the final answer. A simulator acknowledgement or actuator state
of completed is execution evidence only; it is not physical recovery unless newer telemetry proves recovery.
Do not expose internal database IDs, tower keys, MQTT topics, rule codes, or internal version numbers
in normal user-facing answers. Use the tower's human-facing name exactly as returned by application tools; never
infer or invent an alias from its numeric ID, tower key, list position, or neighboring Tower names. Recommendation
numbers may be shown because they are part of the approval/rejection/execution workflow. Only expose technical
identifiers when the user explicitly asks for debugging or technical identifiers.
When the user asks to fix, resolve, correct, or make a Tower/condition okay, inspect current state and actionable
Recommendations with read-only tools before answering. In a plain-chat turn do not mutate state; explain the
current actionable Recommendation(s) so the application UI can offer explicit Approve/Reject/Apply controls.
Reply in the user's language and keep the answer concise and operational.
""".strip()


def _technical_identifier_requested(message: str) -> bool:
    normalized = message.casefold()
    markers = (
        "tower_id",
        "tower id",
        "tower_key",
        "tower key",
        "device key",
        "internal id",
        "internal identifier",
        "debug",
        "debugging",
    )
    return any(marker in normalized for marker in markers)


def _tower_identities_from_trace(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, str]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            tower_key = value.get("tower_key")
            tower_id = value.get("tower_id")
            name = value.get("tower_name") or value.get("name")

            if tower_id is None and tower_key is not None:
                tower_id = value.get("id")

            if isinstance(name, str) and name.strip() and (
                isinstance(tower_id, int) or isinstance(tower_key, str)
            ):
                identity = (tower_id, tower_key, name.strip())
                if identity not in seen:
                    seen.add(identity)
                    identities.append(
                        {
                            "tower_id": tower_id,
                            "tower_key": tower_key,
                            "name": name.strip(),
                        }
                    )

            for child in value.values():
                visit(child)

        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(trace)
    return identities


def _sanitize_user_facing_identifiers(
    message: str,
    text: str,
    trace: list[dict[str, Any]],
) -> str:
    sanitized = text
    identities = _tower_identities_from_trace(trace)

    # A model must not decorate an application-provided Tower name with an inferred alias
    # such as `01 (Demo Tower 11)`. Strip only a parenthetical suffix on an explicit Name field.
    for identity in identities:
        name = identity["name"]
        name_pattern = re.escape(name)
        sanitized = re.sub(
            rf'(?im)((?:\*\*)?Name\s*:(?:\*\*)?\s*["\']?{name_pattern}["\']?)\s*\([^\n)]*\)',
            r"\1",
            sanitized,
        )

    if _technical_identifier_requested(message):
        return sanitized

    for identity in identities:
        tower_id = identity.get("tower_id")
        tower_key = identity.get("tower_key")
        name = identity["name"]

        display_name = name

        if isinstance(tower_id, int):
            name_pattern = re.escape(name)

            # Remove a DB ID shown directly beside a known user-facing Tower name,
            # for example: "Tower 1 (ID: 49)".
            sanitized = re.sub(
                rf'(?i)({name_pattern})\s*\(\s*(?:tower\s*)?id\s*[:#]?\s*{tower_id}\s*\)',
                r"\1",
                sanitized,
            )

            sanitized = re.sub(
                rf"(?i)\btower[_ ]?id\s*[:=]\s*{tower_id}\b",
                display_name,
                sanitized,
            )
            sanitized = re.sub(
                rf"(?i)\bTower\s+{tower_id}\b",
                display_name,
                sanitized,
            )

        if isinstance(tower_key, str) and tower_key:
            key_pattern = re.escape(tower_key)
            name_pattern = re.escape(name)

            sanitized = re.sub(
                rf'(?i)\s*\(\s*key\s*:\s*["\']?{key_pattern}["\']?\s*,\s*name\s*:\s*["\']?{name_pattern}["\']?\s*\)',
                "",
                sanitized,
            )
            sanitized = re.sub(
                rf'(?i)\b(?:tower[_ ]?key|device key|key)\s*[:=]\s*["\']?{key_pattern}["\']?',
                "",
                sanitized,
            )
            sanitized = re.sub(
                rf"(?i)\b{key_pattern}\b",
                "",
                sanitized,
            )

    sanitized = re.sub(r"[ \t]+([,.;:])", r"\1", sanitized)
    sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)

    return sanitized.strip()


def _integer_tool(name: str, description: str, field: str) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {field: {"type": "integer", "minimum": 1}},
            "required": [field],
            "additionalProperties": False,
        },
        "strict": True,
    }


READ_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_towers",
        "description": "List application-managed hydroponic towers. Read-only.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    _integer_tool("get_tower_status", "Get current application status for one tower. Read-only.", "tower_id"),
    _integer_tool("get_active_planting", "Get the active planting snapshot and configured targets for one tower. Read-only.", "tower_id"),
    _integer_tool("get_latest_telemetry", "Get latest persisted telemetry. Missing sensors are explicitly not_monitored. Read-only.", "tower_id"),
    _integer_tool("get_open_alerts", "Get current open derived alerts for one tower. Read-only.", "tower_id"),
    _integer_tool("get_recommendations", "Get recommendation history and statuses for one tower. Read-only.", "tower_id"),
    _integer_tool("get_recommendation_status", "Get one recommendation plus separately queried current alert, planting, and latest telemetry context. Read-only.", "recommendation_id"),
    _integer_tool("explain_alert", "Get traceable observed value, configured target range, direction, and stored target source for one alert. Read-only.", "alert_id"),
    _integer_tool("explain_recommendation", "Get a recommendation explanation while preserving that its reason is historical proposal context. Read-only.", "recommendation_id"),
    _integer_tool(
        "get_action_history",
        "Get recent application decision/execution history for one tower. Read-only.",
        "tower_id",
    ),
]

ACTION_TOOLS: list[dict[str, Any]] = [
    _integer_tool(
        "approve_recommendation",
        "Approve the one Recommendation explicitly authorized by the application for this turn, using existing Phase 7 revalidation.",
        "recommendation_id",
    ),
    {
        "type": "function",
        "name": "reject_recommendation",
        "description": "Reject the one Recommendation explicitly authorized by the application for this turn, using existing Phase 7 decision logic.",
        "parameters": {
            "type": "object",
            "properties": {
                "recommendation_id": {"type": "integer", "minimum": 1},
                "reason": {"type": ["string", "null"], "maxLength": 1000},
            },
            "required": ["recommendation_id", "reason"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    _integer_tool(
        "execute_recommendation",
        "Execute the one Recommendation explicitly authorized by the application for this turn, only through existing Phase 8 approval checks, revalidation, and simulated actuator execution.",
        "recommendation_id",
    ),
]
_ACTION_TOOL_BY_NAME = {tool["name"]: tool for tool in ACTION_TOOLS}


@dataclass(frozen=True)
class TurnAuthorization:
    tool_name: str
    recommendation_id: int


@dataclass(frozen=True)
class AssistantResult:
    message: str
    tool_trace: list[dict[str, Any]]
    model: str


class AssistantUnavailableError(RuntimeError):
    pass


_TARGET_FACTORS: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    ("humidity_pct", "humidity", (re.compile(r"\bhumidity\b", re.IGNORECASE), re.compile(r"رطوبت"))),
    ("ppfd_umol_m2_s", "PPFD", (re.compile(r"\bppfd\b", re.IGNORECASE), re.compile(r"\blight\b", re.IGNORECASE), re.compile(r"نور"))),
    ("water_temp_c", "water temperature", (re.compile(r"\bwater\s+temp(?:erature)?\b", re.IGNORECASE), re.compile(r"دمای\s*آب"))),
    ("air_temp_c", "air temperature", (re.compile(r"\bair\s+temp(?:erature)?\b", re.IGNORECASE), re.compile(r"دمای\s*هوا"))),
    ("water_level_pct", "water level", (re.compile(r"\bwater\s+level\b", re.IGNORECASE), re.compile(r"سطح\s*آب"))),
    ("ec_ms_cm", "EC", (re.compile(r"\bec\b", re.IGNORECASE),)),
    ("ph", "pH", (re.compile(r"\bph\b", re.IGNORECASE),)),
)
_TARGET_QUERY_TERMS = (
    re.compile(r"\btarget\b", re.IGNORECASE),
    re.compile(r"\brange\b", re.IGNORECASE),
    re.compile(r"\bconfigured\b", re.IGNORECASE),
    re.compile(r"\brecommended\b", re.IGNORECASE),
    re.compile(r"هدف"),
    re.compile(r"محدوده"),
)


def assistant_tools(authorization: TurnAuthorization | None = None) -> list[dict[str, Any]]:
    tools = [*READ_TOOLS]
    if authorization is None:
        return tools

    action_tool = copy.deepcopy(_ACTION_TOOL_BY_NAME[authorization.tool_name])
    action_tool["parameters"]["properties"]["recommendation_id"] = {
        "type": "integer",
        "enum": [authorization.recommendation_id],
    }
    tools.append(action_tool)
    return tools


def _ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).strip().rstrip("/")


def _ollama_timeout_seconds() -> float:
    raw = os.getenv("OLLAMA_TIMEOUT_SECONDS", str(DEFAULT_OLLAMA_TIMEOUT_SECONDS)).strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_OLLAMA_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_OLLAMA_TIMEOUT_SECONDS


def assistant_model() -> str:
    return os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL


def _ollama_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        },
    }


class OllamaClient:
    def __init__(self, base_url: str | None = None, timeout_seconds: float | None = None) -> None:
        self.base_url = (base_url or _ollama_base_url()).rstrip("/")
        self.timeout_seconds = timeout_seconds or _ollama_timeout_seconds()

    def chat(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        import httpx

        response = httpx.post(
            f"{self.base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "think": False,
                "keep_alive": "10m",
                "options": {"temperature": 0},
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Ollama returned a non-object response.")
        return payload

    def model_available(self, model: str) -> bool:
        import httpx

        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return False
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return False
        names = {item.get("name") for item in models if isinstance(item, dict)}
        return model in names


def assistant_available(client: Any | None = None, model: str | None = None) -> bool:
    selected_model = model or assistant_model()
    checker = client or OllamaClient()
    try:
        return bool(checker.model_available(selected_model))
    except Exception:
        return False


_MUTATION_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:(?:please|can you|could you|would you)\s+)?approve\s+(?:recommendation\b|it\b|this\b)", re.IGNORECASE),
    re.compile(r"\b(?:(?:please|can you|could you|would you)\s+)?reject\s+(?:recommendation\b|it\b|this\b)", re.IGNORECASE),
    re.compile(r"\b(?:(?:please|can you|could you|would you)\s+)?execute\s+(?:recommendation\b|it\b|this\b)", re.IGNORECASE),
    re.compile(r"(?:تأیید|تا[یي]ید)\s*(?:کن|ش\s*کن|کنید|بکن)"),
    re.compile(r"رد\s*(?:کن|ش\s*کن|کنید|بکن)"),
    re.compile(r"اجرا\s*(?:کن|ش\s*کن|کنید|بکن)"),
)


def _plain_chat_mutation_request(message: str, authorization: TurnAuthorization | None) -> bool:
    if authorization is not None:
        return False
    return any(pattern.search(message) for pattern in _MUTATION_REQUEST_PATTERNS)


def _contains_simulated_marker(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("simulated") is True or value.get("provenance") == "simulated":
            return True
        return any(_contains_simulated_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_simulated_marker(item) for item in value)
    return False


def _trace_contains_simulated_data(trace: list[dict[str, Any]]) -> bool:
    return any(_contains_simulated_marker(item.get("result")) for item in trace)


def _simulation_disclosure(message: str, text: str, trace: list[dict[str, Any]]) -> str:
    if not _trace_contains_simulated_data(trace):
        return text
    lowered = text.lower()
    if "simulat" in lowered or "شبیه" in text:
        return text
    if re.search(r"[\u0600-\u06FF]", message):
        return text + "\n\nتوجه: داده‌های تله‌متری/اکچویتور اشاره‌شده در این پاسخ شبیه‌سازی‌شده‌اند."
    return text + "\n\nNote: telemetry/actuator data referenced in this response is simulated."


def _authorization_error(
    authorization: TurnAuthorization | None,
    tool_name: str,
    arguments: dict[str, Any],
    mutation_consumed: bool,
) -> dict[str, Any] | None:
    if tool_name not in ACTION_TOOL_NAMES:
        return None
    if authorization is None:
        return {"error": "No mutation is authorized for this assistant turn.", "requested_tool": tool_name}
    if tool_name != authorization.tool_name:
        return {
            "error": "This mutation tool is not authorized for this assistant turn.",
            "requested_tool": tool_name,
            "authorized_tool": authorization.tool_name,
            "authorized_recommendation_id": authorization.recommendation_id,
        }
    if arguments.get("recommendation_id") != authorization.recommendation_id:
        return {
            "error": "This Recommendation is not authorized for mutation in this assistant turn.",
            "requested_recommendation_id": arguments.get("recommendation_id"),
            "authorized_recommendation_id": authorization.recommendation_id,
            "authorized_tool": authorization.tool_name,
        }
    if mutation_consumed:
        return {
            "error": "The authorized mutation has already been attempted in this assistant turn; no chained mutation is allowed.",
            "authorized_tool": authorization.tool_name,
            "authorized_recommendation_id": authorization.recommendation_id,
        }
    return None


def _authorization_failure_message(result: dict[str, Any]) -> str:
    return f"Action not performed: {result.get('error', 'the requested mutation was not authorized.')}"


def _mutation_result_message(tool_name: str, recommendation_id: int, result: dict[str, Any]) -> str:
    if "error" in result:
        action = tool_name.removesuffix("_recommendation").replace("_", " ").capitalize()
        return f"{action} failed for Recommendation {recommendation_id}: {result['error']}"

    if tool_name == "approve_recommendation":
        decision = result.get("decision", {})
        if decision.get("status") == "approved" and decision.get("revalidation_passed") is True:
            return f"Recommendation {recommendation_id} was approved after application revalidation. It has not been executed."
        status = decision.get("status", "unknown")
        reason = decision.get("reason") or "The application did not approve it."
        return f"Approval did not succeed for Recommendation {recommendation_id} (status: {status}). {reason}"

    if tool_name == "reject_recommendation":
        decision = result.get("decision", {})
        if decision.get("status") == "rejected":
            return f"Recommendation {recommendation_id} was rejected. No execution was created."
        status = decision.get("status", "unknown")
        return f"Rejection did not succeed for Recommendation {recommendation_id} (status: {status})."

    execution = result.get("execution", {})
    reason = execution.get("reason") or "No verified execution evidence was returned."
    if execution.get("execution_verified") is True:
        return (
            f"Recommendation {recommendation_id} has verified simulated execution. "
            f"Command sent: {bool(execution.get('command_sent'))}; execution verified: true. {reason}"
        )
    if execution.get("command_sent") is True:
        return (
            f"A simulated command was sent for Recommendation {recommendation_id}, but execution was not verified. "
            f"Do not treat it as successful verified execution. {reason}"
        )
    return f"Execution did not succeed for Recommendation {recommendation_id}. {reason}"


def _target_factor_from_message(message: str) -> tuple[str, str] | None:
    if not any(pattern.search(message) for pattern in _TARGET_QUERY_TERMS):
        return None
    matches: list[tuple[str, str]] = []
    for factor, label, patterns in _TARGET_FACTORS:
        if any(pattern.search(message) for pattern in patterns):
            matches.append((factor, label))
    return matches[0] if len(matches) == 1 else None


def _snapshots_from_trace(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for item in trace:
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        for candidate in (
            result.get("active_planting"),
            result.get("configured_target_context"),
            result.get("current_context", {}).get("active_planting") if isinstance(result.get("current_context"), dict) else None,
        ):
            if isinstance(candidate, dict):
                snapshots.append(candidate)
    return snapshots


def _ground_target_query(message: str, trace: list[dict[str, Any]]) -> str | None:
    factor_request = _target_factor_from_message(message)
    if factor_request is None:
        return None
    factor, label = factor_request
    snapshots = _snapshots_from_trace(trace)
    if not snapshots:
        return (
            f"I cannot state a configured {label} target because no active-planting target context "
            "was retrieved from the application. I will not invent one."
        )

    snapshot = snapshots[-1]
    targets = snapshot.get("configured_targets") if isinstance(snapshot.get("configured_targets"), dict) else {}
    target = targets.get(factor)
    if not isinstance(target, dict):
        return (
            f"No configured/evidence-backed {label} target is available for the active planting. "
            "I will not invent an application target."
        )

    minimum = target.get("min")
    maximum = target.get("max")
    unit = target.get("unit") or ""
    source_reference_id = target.get("source_reference_id")
    source_text = f" Source reference: {source_reference_id}." if source_reference_id else ""
    return f"The active planting's configured {label} target is {minimum}–{maximum} {unit}." + source_text


def run_assistant(
    session: Session,
    message: str,
    *,
    authorization: TurnAuthorization | None = None,
    client: Any | None = None,
    model: str | None = None,
) -> AssistantResult:
    selected_model = model or assistant_model()
    if _plain_chat_mutation_request(message, authorization):
        return AssistantResult(
            "No action was performed. Approval, rejection, or execution requires the application's explicit action "
            "control so the exact action and Recommendation can be authorized and revalidated.",
            [],
            selected_model,
        )

    # An explicit application action control is already the user's authorization.
    # Do not ask the language model to decide whether to perform that exact mutation again.
    # Inspect current recommendation context first, then invoke exactly one controlled
    # application tool. The underlying approve/execute implementations retain their
    # existing revalidation and execution safeguards.
    if authorization is not None:
        context_arguments = {"recommendation_id": authorization.recommendation_id}
        context_result = dispatch_tool(
            session,
            "get_recommendation_status",
            context_arguments,
        )
        trace = [
            {
                "tool": "get_recommendation_status",
                "arguments": context_arguments,
                "result": context_result,
            }
        ]

        if isinstance(context_result, dict) and "error" in context_result:
            return AssistantResult(
                f"Action not performed: {context_result['error']}",
                trace,
                selected_model,
            )

        action_arguments: dict[str, Any] = {
            "recommendation_id": authorization.recommendation_id,
        }
        result = dispatch_tool(
            session,
            authorization.tool_name,
            action_arguments,
        )
        trace.append(
            {
                "tool": authorization.tool_name,
                "arguments": action_arguments,
                "result": result,
            }
        )
        if not isinstance(result, dict):
            return AssistantResult(
                f"Action not performed for Recommendation {authorization.recommendation_id}: "
                "the application returned no valid action result.",
                trace,
                selected_model,
            )
        return AssistantResult(
            _mutation_result_message(
                authorization.tool_name,
                authorization.recommendation_id,
                result,
            ),
            trace,
            selected_model,
        )

    if client is None:
        client = OllamaClient()
        if not assistant_available(client, selected_model):
            raise AssistantUnavailableError(
                f"AI Assistant is unavailable because local Ollama model {selected_model!r} is not reachable. "
                "Core monitoring remains available."
            )

    tools = assistant_tools(authorization)
    ollama_tools = [_ollama_tool(tool) for tool in tools]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ASSISTANT_INSTRUCTIONS},
        {"role": "user", "content": message},
    ]
    trace: list[dict[str, Any]] = []
    inspected_recommendations: set[int] = set()
    mutation_consumed = False

    for _round in range(MAX_TOOL_ROUNDS):
        try:
            response = client.chat(model=selected_model, messages=messages, tools=ollama_tools)
        except Exception as exc:
            raise AssistantUnavailableError(
                "Local Ollama request failed; core monitoring is still available."
            ) from exc

        assistant_message = response.get("message") if isinstance(response, dict) else None
        if not isinstance(assistant_message, dict):
            raise AssistantUnavailableError("Ollama returned no assistant message.")

        tool_calls = assistant_message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise AssistantUnavailableError("Ollama returned invalid tool-call data.")

        messages.append({
            "role": "assistant",
            "content": assistant_message.get("content") or "",
            **({"tool_calls": tool_calls} if tool_calls else {}),
        })

        if not tool_calls:
            if authorization is not None and not mutation_consumed:
                return AssistantResult(
                    f"No mutation was performed for Recommendation {authorization.recommendation_id}; "
                    f"the authorized action {authorization.tool_name} was not completed.",
                    trace,
                    selected_model,
                )
            grounded_target = _ground_target_query(message, trace)
            if grounded_target is not None:
                return AssistantResult(grounded_target, trace, selected_model)
            text = str(assistant_message.get("content") or "").strip()
            if not text:
                raise AssistantUnavailableError("Ollama returned no assistant message.")
            text = _simulation_disclosure(message, text, trace)
            text = _sanitize_user_facing_identifiers(message, text, trace)
            return AssistantResult(text, trace, selected_model)

        for call in tool_calls:
            arguments: dict[str, Any] = {}
            call_name = ""
            try:
                if not isinstance(call, dict):
                    raise ValueError("Tool call must be an object.")
                function = call.get("function")
                if not isinstance(function, dict):
                    raise ValueError("Tool call is missing function data.")
                call_name = str(function.get("name") or "").strip()
                if not call_name:
                    raise ValueError("Tool call is missing a function name.")
                raw_arguments = function.get("arguments", {})
                if isinstance(raw_arguments, str):
                    parsed = json.loads(raw_arguments)
                else:
                    parsed = raw_arguments
                if not isinstance(parsed, dict):
                    raise ValueError("Tool arguments must be a JSON object.")
                arguments = parsed

                auth_error = _authorization_error(authorization, call_name, arguments, mutation_consumed)
                if auth_error is not None:
                    result = auth_error
                    trace.append({"tool": call_name, "arguments": arguments, "result": result})
                    return AssistantResult(_authorization_failure_message(result), trace, selected_model)

                recommendation_id = arguments.get("recommendation_id")
                if (
                    call_name in {"approve_recommendation", "execute_recommendation"}
                    and recommendation_id not in inspected_recommendations
                ):
                    result = {
                        "error": "Current recommendation context must be inspected before approve or execute.",
                        "required_tool": "get_recommendation_status",
                        "recommendation_id": recommendation_id,
                    }
                else:
                    result = dispatch_tool(session, call_name, arguments)
                    if call_name in ACTION_TOOL_NAMES:
                        mutation_consumed = True

                if (
                    call_name in {"get_recommendation_status", "explain_recommendation"}
                    and isinstance(recommendation_id, int)
                ):
                    inspected_recommendations.add(recommendation_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                result = {"error": f"Invalid tool arguments: {exc}"}

            trace.append({"tool": call_name or "unknown", "arguments": arguments, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

            if call_name in ACTION_TOOL_NAMES and mutation_consumed:
                assert authorization is not None
                return AssistantResult(
                    _mutation_result_message(call_name, authorization.recommendation_id, result),
                    trace,
                    selected_model,
                )

    raise AssistantUnavailableError("Assistant tool-call limit reached before a final response.")

