import os
from typing import Any

import requests


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BackendClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = 8.0,
        assistant_timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("BACKEND_BASE_URL", "http://backend:8000")).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.assistant_timeout_seconds = assistant_timeout_seconds or float(
            os.getenv("ASSISTANT_REQUEST_TIMEOUT_SECONDS", "120")
        )
        self.session = requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_not_found: bool = False,
        timeout_seconds: float | None = None,
    ) -> Any:
        request_timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                params=params,
                timeout=request_timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"Backend is unavailable: {exc}") from exc

        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise ApiError(
                detail or f"Backend request failed with HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        return response.json()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def list_towers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/towers")

    def create_tower(
        self,
        name: str,
        *,
        tower_key: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if tower_key is not None:
            payload["tower_key"] = tower_key
        if description is not None:
            payload["description"] = description
        return self._request("POST", "/towers", json=payload)

    def update_tower(self, tower_id: int, *, name: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        return self._request("PATCH", f"/towers/{tower_id}", json=payload)

    def delete_tower(self, tower_id: int) -> None:
        self._request("DELETE", f"/towers/{tower_id}")

    def latest_telemetry(self, tower_id: int) -> dict[str, Any] | None:
        return self._request("GET", f"/towers/{tower_id}/telemetry/latest", allow_not_found=True)

    def telemetry_history(self, tower_id: int, limit: int = 200) -> list[dict[str, Any]]:
        return self._request("GET", f"/towers/{tower_id}/telemetry/history", params={"limit": limit})

    def active_planting(self, tower_id: int) -> dict[str, Any] | None:
        return self._request("GET", f"/towers/{tower_id}/planting", allow_not_found=True)

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._request("GET", "/plant-profiles")

    def start_planting(self, tower_id: int, profile_id: int, growth_stage: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"plant_profile_id": profile_id}
        if growth_stage:
            payload["growth_stage"] = growth_stage
        return self._request("POST", f"/towers/{tower_id}/planting", json=payload)

    def end_planting(self, tower_id: int, status: str) -> dict[str, Any]:
        return self._request("POST", f"/towers/{tower_id}/planting/end", json={"status": status})

    def customized_copy(self, profile_id: int, name: str) -> dict[str, Any]:
        return self._request("POST", f"/plant-profiles/{profile_id}/customized-copy", json={"name": name})


    def create_custom_profile(self, name: str, targets: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/plant-profiles/custom", json={"name": name, "targets": targets, "source_references": []})

    def update_profile(self, profile_id: int, *, name: str | None = None, targets: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if targets is not None:
            payload["targets"] = targets
        return self._request("PATCH", f"/plant-profiles/{profile_id}", json=payload)

    def list_alerts(self, tower_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/towers/{tower_id}/alerts", params={"limit": limit})

    def list_recommendations(self, tower_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/towers/{tower_id}/recommendations", params={"limit": limit})

    def authoritative_recommendation_state(
        self,
        tower_id: int,
        recommendation_id: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Refresh Recommendation and history from backend after a mutation."""
        recommendations = self.list_recommendations(tower_id, limit=500)
        recommendation = next(
            (item for item in recommendations if item.get("id") == recommendation_id),
            None,
        )
        if recommendation is None:
            raise ApiError("Recommendation is no longer available from the backend.")
        history = self.action_history(tower_id, limit=200)
        return recommendation, history

    def approve_recommendation(self, recommendation_id: int) -> dict[str, Any]:
        return self._request("POST", f"/recommendations/{recommendation_id}/approve")

    def reject_recommendation(self, recommendation_id: int, reason: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/recommendations/{recommendation_id}/reject",
            json={"reason": reason or None},
        )

    def execute_recommendation(self, recommendation_id: int) -> dict[str, Any]:
        return self._request("POST", f"/recommendations/{recommendation_id}/execute")

    def actuator_states(self, tower_id: int) -> list[dict[str, Any]]:
        return self._request("GET", f"/towers/{tower_id}/actuator-states")

    def action_history(self, tower_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/towers/{tower_id}/actions", params={"limit": limit})

    def assistant_status(self) -> dict[str, Any]:
        return self._request("GET", "/assistant/status")

    def assistant_chat(
        self,
        message: str,
        *,
        action: str | None = None,
        recommendation_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"message": message}
        if action is not None:
            if recommendation_id is None:
                raise ValueError("recommendation_id is required for an AI mutation action")
            payload["action_request"] = {
                "action": action,
                "recommendation_id": recommendation_id,
            }
        return self._request(
            "POST",
            "/assistant/chat",
            json=payload,
            timeout_seconds=self.assistant_timeout_seconds,
        )
