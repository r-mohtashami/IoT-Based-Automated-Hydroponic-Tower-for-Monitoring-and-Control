from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.ai_action_tools import (
    approve_recommendation_tool,
    execute_recommendation_tool,
    reject_recommendation_tool,
)
from app.ai_context_tools import (
    explain_alert_tool,
    explain_recommendation_tool,
    get_action_history_tool,
    get_active_planting_tool,
    get_latest_telemetry_tool,
    get_open_alerts_tool,
    get_recommendation_status_tool,
    get_recommendations_tool,
    get_tower_status_tool,
    list_towers_tool,
)


READ_TOOL_NAMES = {
    "list_towers",
    "get_tower_status",
    "get_active_planting",
    "get_latest_telemetry",
    "get_open_alerts",
    "get_recommendations",
    "get_recommendation_status",
    "explain_alert",
    "explain_recommendation",
    "get_action_history",
}
ACTION_TOOL_NAMES = {
    "approve_recommendation",
    "reject_recommendation",
    "execute_recommendation",
}


def dispatch_tool(session: Session, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "list_towers":
        return list_towers_tool(session)
    if name == "get_tower_status":
        return get_tower_status_tool(session, int(arguments["tower_id"]))
    if name == "get_active_planting":
        return get_active_planting_tool(session, int(arguments["tower_id"]))
    if name == "get_latest_telemetry":
        return get_latest_telemetry_tool(session, int(arguments["tower_id"]))
    if name == "get_open_alerts":
        return get_open_alerts_tool(session, int(arguments["tower_id"]))
    if name == "get_recommendations":
        return get_recommendations_tool(session, int(arguments["tower_id"]))
    if name == "get_recommendation_status":
        return get_recommendation_status_tool(session, int(arguments["recommendation_id"]))
    if name == "explain_alert":
        return explain_alert_tool(session, int(arguments["alert_id"]))
    if name == "explain_recommendation":
        return explain_recommendation_tool(session, int(arguments["recommendation_id"]))
    if name == "approve_recommendation":
        return approve_recommendation_tool(session, int(arguments["recommendation_id"]))
    if name == "reject_recommendation":
        return reject_recommendation_tool(
            session,
            int(arguments["recommendation_id"]),
            arguments.get("reason"),
        )
    if name == "execute_recommendation":
        return execute_recommendation_tool(session, int(arguments["recommendation_id"]))
    if name == "get_action_history":
        return get_action_history_tool(session, int(arguments["tower_id"]))
    return {"error": "Tool is not available to the assistant.", "tool": name}
