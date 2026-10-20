from __future__ import annotations

import base64
import os
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from api_client import ApiError, BackendClient
from ui_helpers import (
    SENSOR_SPECS,
    assistant_action_candidates,
    chart_domain,
    chart_points,
    execution_evidence,
    factor_label,
    parse_target_range,
    profile_origin_label,
    sensor_number_and_unit,
    sensor_text,
    target_for_factor,
    target_text,
    telemetry_age_seconds,
    telemetry_is_stale,
)


st.set_page_config(
    page_title="Hydroponic Platform",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={"About": "AI-Assisted IoT Platform for Hydroponic Farm Monitoring and Automation"},
)

STYLE_PATH = Path(__file__).with_name("styles.css")
ASSET_DIR = Path(__file__).with_name("assets")
if STYLE_PATH.exists():
    st.html(STYLE_PATH)

client = BackendClient()
TELEMETRY_STALE_SECONDS = float(os.getenv("TELEMETRY_MAX_AGE_SECONDS", "120"))
TELEMETRY_MAX_FUTURE_SKEW_SECONDS = float(os.getenv("RECOMMENDATION_MAX_FUTURE_CLOCK_SKEW_SECONDS", "5"))

USER_EVENT_LABELS = {
    "recommendation_approved": "You approved a correction",
    "recommendation_rejected": "You rejected a recommendation",
    "recommendation_stale": "Recommendation no longer needed",
    "execution_requested": "You asked the system to apply the correction",
    "actuator_command_sent": "Command sent to the simulated device",
    "execution_verified": "Simulated device accepted the command",
    "awaiting_physical_outcome": "Waiting for newer readings",
    "physical_outcome_observed": "New post-action reading received",
    "physical_outcome_recovered": "Back in target range",
    "physical_outcome_follow_up_required": "Another review is needed",
    "execution_failed": "Action could not be confirmed",
    "execution_blocked": "Correction was not applied",
}

EVENT_DETAILS = {
    "recommendation_approved": "Approval was recorded; nothing was applied at this step.",
    "recommendation_rejected": "No correction was applied.",
    "recommendation_stale": "Conditions changed, so the previous recommendation is no longer actionable.",
    "execution_requested": "The backend re-checked current state before attempting the approved step.",
    "actuator_command_sent": "The command left the application; this does not prove physical recovery.",
    "execution_verified": "The simulator acknowledged the command. New telemetry still has to prove the result.",
    "awaiting_physical_outcome": "The system is waiting for newer simulated sensor readings.",
    "physical_outcome_observed": "A newer reading arrived after the correction step.",
    "physical_outcome_recovered": "New telemetry returned to the configured target range.",
    "physical_outcome_follow_up_required": "The condition remained outside target after the observation window.",
    "execution_failed": "Current application evidence could not confirm the simulated action.",
    "execution_blocked": "The application blocked the correction because current state did not permit execution.",
}

EVENT_TONES = {
    "recommendation_approved": "success",
    "recommendation_rejected": "danger",
    "recommendation_stale": "neutral",
    "execution_requested": "info",
    "actuator_command_sent": "warning",
    "execution_verified": "info",
    "awaiting_physical_outcome": "warning",
    "physical_outcome_observed": "info",
    "physical_outcome_recovered": "success",
    "physical_outcome_follow_up_required": "warning",
    "execution_failed": "danger",
    "execution_blocked": "warning",
}

ACTION_COPY = {
    "increase_ph": ("Raise the pH", "One bounded simulated pH Up correction step is available."),
    "decrease_ph": ("Lower the pH", "One bounded simulated pH Down correction step is available."),
    "increase_ec": ("Increase nutrient strength", "One bounded simulated nutrient dosing step is available."),
    "manual_ec_high_intervention": (
        "Manual action needed",
        "This project has no automatic EC-high correction. Adjust the solution manually, then let later readings confirm recovery.",
    ),
    "manual_environment_review": (
        "Manual review needed",
        "Review this environmental condition manually. No automatic actuator is defined for this factor in the MVP.",
    ),
}


def e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def asset_uri(name: str) -> str:
    path = ASSET_DIR / name
    if not path.exists():
        return ""
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
    }.get(path.suffix.lower(), "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def asset_image(name: str, alt: str, css_class: str = "visual-photo") -> str:
    uri = asset_uri(name)
    if not uri:
        return ""
    return f'<img class="{e(css_class)}" src="{uri}" alt="{e(alt)}" loading="eager">'


def tower_visual_index(tower: dict[str, Any]) -> int:
    key = str(tower.get("tower_key") or "")
    try:
        if key.startswith("sim-"):
            return ((int(key.split("-", 1)[1]) - 1) % 3) + 1
    except (TypeError, ValueError):
        pass
    return ((int(tower.get("id") or 1) - 1) % 3) + 1


def tower_asset(tower: dict[str, Any]) -> str:
    return f"dashboard_tower_{tower_visual_index(tower)}.png"


def tower_display_name(tower: dict[str, Any]) -> str:
    name = tower.get("name") or "Tower"
    key = tower.get("tower_key") or ""
    if key.startswith("sim-") and name.startswith("Demo "):
        return name[5:]
    return name


def profile_display_name(name: str | None) -> str:
    value = name or "Plant"
    return value.split(" — ", 1)[0]


def relative_time(value: str | None) -> str:
    age = telemetry_age_seconds(value)
    if age is None:
        return "No recent data"
    if age < 60:
        return "just now"
    minutes = int(age // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def format_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%b %d, %Y")
    except ValueError:
        return value


def set_flash(kind: str, message: str) -> None:
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    flash = st.session_state.pop("flash", None)
    if not flash:
        return
    kind, message = flash
    getattr(st, kind, st.info)(message)


def close_overlay() -> None:
    st.query_params.clear()
    st.rerun()


def render_header(online: bool = True) -> None:
    online_class = "online" if online else "offline"
    online_label = "App Online" if online else "App Offline"
    st.html(
        f"""
        <div class="product-header single-page-header">
          <div class="brand-lockup">
            <div class="brand-leaf">{asset_image('brand_leaf.svg', 'Hydroponic leaf logo', 'brand-leaf-img')}</div>
            <div>
              <div class="brand-title">Hydroponic</div>
              <div class="brand-subtitle">Smart Growing Platform</div>
            </div>
          </div>
          <div class="header-actions">
            <span class="mode-chip">Simulated Hardware</span>
            <span class="online-chip {online_class}"><i></i>{online_label}</span>
          </div>
        </div>
        """
    )


def render_section(title: str, subtitle: str | None = None) -> None:
    copy = f"<p>{e(subtitle)}</p>" if subtitle else ""
    st.html(f'<div class="section-heading"><div><h2>{e(title)}</h2>{copy}</div></div>')


def render_empty(title: str, copy: str) -> None:
    st.html(
        f'<div class="empty-card"><div class="empty-title">{e(title)}</div><div class="empty-copy">{e(copy)}</div></div>'
    )


def profile_target(profile: dict[str, Any], factor: str) -> dict[str, Any] | None:
    target = (profile.get("targets") or {}).get(factor)
    return target if isinstance(target, dict) else None


def current_operational_state(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    planting = snapshot.get("planting")
    if not planting:
        return [], []
    planting_id = planting.get("planting_id")
    alerts = [
        alert
        for alert in snapshot.get("alerts", [])
        if alert.get("planting_id") == planting_id and alert.get("status") == "open"
    ]
    alert_ids = {alert.get("id") for alert in alerts}
    recommendations = [
        rec
        for rec in snapshot.get("recommendations", [])
        if rec.get("planting_id") == planting_id and rec.get("alert_id") in alert_ids
    ]
    return alerts, recommendations


def snapshot_is_stale(snapshot: dict[str, Any]) -> bool:
    latest = snapshot.get("latest")
    return telemetry_is_stale(
        latest.get("timestamp") if latest else None,
        stale_after_seconds=TELEMETRY_STALE_SECONDS,
        max_future_clock_skew_seconds=TELEMETRY_MAX_FUTURE_SKEW_SECONDS,
    )


def recommendation_still_applies(
    rec: dict[str, Any],
    alert: dict[str, Any],
    latest: dict[str, Any] | None,
    planting: dict[str, Any] | None,
) -> bool:
    if not latest or not planting:
        return False
    factor = alert.get("factor")
    if not factor:
        return False
    current = latest.get(factor)
    target = target_for_factor(planting, factor)
    if current is None or not target:
        return False
    minimum, maximum = target.get("min"), target.get("max")
    if not isinstance(current, (int, float)) or minimum is None or maximum is None:
        return False
    direction = (alert.get("details") or {}).get("direction")
    return (direction == "below" and current < minimum) or (direction == "above" and current > maximum)


def tower_ui_state(snapshot: dict[str, Any]) -> tuple[str, str]:
    if not snapshot.get("available"):
        return "Unavailable", "neutral"
    if snapshot_is_stale(snapshot):
        return "Readings not updating", "warning"

    planting = snapshot.get("planting")
    if not planting:
        return "Ready for a plant", "neutral"

    alerts, recommendations = current_operational_state(snapshot)
    active = [rec for rec in recommendations if rec.get("status") in {"pending", "approved"}]
    manual = next(
        (
            rec
            for rec in active
            if not bool((rec.get("action_parameters") or {}).get("executable", True))
        ),
        None,
    )
    approved = next(
        (
            rec
            for rec in active
            if rec.get("status") == "approved" and bool((rec.get("action_parameters") or {}).get("executable", True))
        ),
        None,
    )
    pending = next((rec for rec in active if rec.get("status") == "pending"), None)

    # Actionable conditions always take precedence over incomplete monitoring.
    if manual:
        return "Manual action needed", "warning"
    if approved:
        return "Ready to apply", "info"
    if pending:
        return "Waiting for your decision", "warning"
    if alerts:
        executed = any(
            rec.get("status") == "executed" and rec.get("alert_id") in {alert.get("id") for alert in alerts}
            for rec in recommendations
        )
        if executed:
            return "Checking the result", "info"
        return "Needs your attention", "warning"

    latest = snapshot.get("latest") or {}
    configured_targets = (planting.get("profile_snapshot") or {}).get("targets") or {}
    monitored_target_factors = [factor for factor in configured_targets if factor in SENSOR_SPECS]
    if any(latest.get(factor) is None for factor in monitored_target_factors):
        return "Some checks unavailable", "neutral"

    if not configured_targets:
        return "No crop targets set", "neutral"
    return "Everything looks good", "success"

def collect_tower_snapshot(tower: dict[str, Any]) -> dict[str, Any]:
    try:
        latest = client.latest_telemetry(tower["id"])
        planting = client.active_planting(tower["id"])
        alerts = client.list_alerts(tower["id"], limit=100)
        recommendations = client.list_recommendations(tower["id"], limit=100)
        history = client.action_history(tower["id"], limit=100)
        return {
            "tower": tower,
            "latest": latest,
            "planting": planting,
            "alerts": alerts,
            "recommendations": recommendations,
            "history": history,
            "available": True,
        }
    except ApiError:
        return {
            "tower": tower,
            "latest": None,
            "planting": None,
            "alerts": [],
            "recommendations": [],
            "history": [],
            "available": False,
        }


def render_home(snapshots: list[dict[str, Any]]) -> None:
    growing = sum(bool(snapshot.get("planting")) for snapshot in snapshots)
    states = [tower_ui_state(snapshot)[0] for snapshot in snapshots]
    needs_attention = sum(
        state in {"Needs your attention", "Waiting for your decision", "Ready to apply", "Manual action needed"}
        for state in states
    )
    not_updating = sum(state == "Readings not updating" for state in states)

    hero_photo = asset_image("home_hero.png", "Smart indoor hydroponic garden", "home-hero-illustration")
    subline_parts = [f"{growing} growing"]
    subline_parts.append(f"{needs_attention} need your attention" if needs_attention else "no decisions waiting")
    if not_updating:
        subline_parts.append(f"{not_updating} not updating")
    st.html(
        f"""
        <section class="home-visual-hero">
          <div class="home-visual-copy">
            <div class="sunline"><span>☀</span> Your garden, at a glance</div>
            <h1 class="display-title">Your hydroponic garden</h1>
            <p class="display-copy">{e(' · '.join(subline_parts))}</p>
            <div class="hero-facts">
              <span><b>{len(snapshots)}</b> towers</span>
              <span><b>{growing}</b> growing now</span>
              <span><b>{needs_attention}</b> need attention</span>
              <span><b>{not_updating}</b> not updating</span>
            </div>
          </div>
          <div class="home-visual-media">
            {hero_photo}
            <div class="hero-art-bubble">Everything you need,<br><strong>inside each tower.</strong></div>
          </div>
        </section>
        """
    )

    render_section("Your towers", "Open a tower to see its plant, readings, next steps, trends, and history in one place.")
    cards: list[str] = []
    for snapshot in snapshots:
        tower = snapshot["tower"]
        latest = snapshot.get("latest") or {}
        planting = snapshot.get("planting")
        label, tone = tower_ui_state(snapshot)
        plant_name = profile_display_name(planting.get("profile_name")) if planting else "No plant yet"
        updated = relative_time(latest.get("timestamp")) if latest else "No recent data"
        ph_num, ph_unit = sensor_number_and_unit(latest.get("ph"), "") if latest else ("—", "")
        ec_num, ec_unit = sensor_number_and_unit(latest.get("ec_ms_cm"), "mS/cm") if latest else ("—", "")
        water_num, water_unit = sensor_number_and_unit(latest.get("water_level_pct"), "%") if latest else ("—", "")
        art = asset_image(tower_asset(tower), f"Illustration of {tower_display_name(tower)}", "tower-card-illustration")
        href = f"?tower={tower['id']}"
        cards.append(
            f"""
            <a class="tower-card product-card tower-card-link uxv2-tower-card" href="{href}" aria-label="Open {e(tower_display_name(tower))}">
              <div class="tower-card-visual">{art}</div>
              <div class="tower-card-body">
                <div class="card-title-row">
                  <div class="card-title-stack">
                    <div class="card-title">{e(tower_display_name(tower))}</div>
                    <div class="card-subtitle">Updated {e(updated)}</div>
                  </div>
                  <span class="pill {tone}">{e(label)}</span>
                </div>
                <div class="tower-crop"><span class="crop-dot">●</span>{e(plant_name)}</div>
                <div class="reading-grid">
                  <div class="reading-mini"><div class="label">pH</div><div class="value"><strong>{e(ph_num)}</strong><span>{e(ph_unit)}</span></div></div>
                  <div class="reading-mini"><div class="label">EC</div><div class="value"><strong>{e(ec_num)}</strong><span>{e(ec_unit)}</span></div></div>
                  <div class="reading-mini"><div class="label">Water</div><div class="value"><strong>{e(water_num)}</strong><span>{e(water_unit)}</span></div></div>
                </div>
                <div class="card-cta">Open tower <span>→</span></div>
              </div>
            </a>
            """
        )

    all_cards = cards + [
        """
        <a class="product-card tower-card-link add-tower-card" href="?action=add">
          <div class="add-tower-plus">＋</div>
          <div class="add-tower-title">Add a tower</div>
          <div class="add-tower-copy">Set up a new tower and choose a plant now or later.</div>
        </a>
        """
    ]
    for start in range(0, len(all_cards), 3):
        row = all_cards[start : start + 3]
        columns = st.columns(len(row), gap="large")
        for column, html in zip(columns, row):
            with column:
                st.html(html)

def render_primary_readings(latest: dict[str, Any] | None, planting: dict[str, Any] | None, stale: bool) -> None:
    latest = latest or {}
    specs = [
        ("ph", "pH", ""),
        ("ec_ms_cm", "EC", "mS/cm"),
        ("water_level_pct", "Reservoir level", "%"),
    ]
    columns = st.columns(3, gap="medium")
    for column, (factor, label, unit) in zip(columns, specs):
        value = latest.get(factor)
        target = target_for_factor(planting, factor)
        support = ""
        if stale:
            state, tone = "Reading is old", "warning"
            support = "Waiting for a newer reading."
        elif value is None:
            state, tone = "Not monitored", "neutral"
            support = "No sensor reading is available."
        elif not target:
            state, tone = "Tower sensor", "neutral"
            if factor == "water_level_pct":
                support = "Reservoir level is measured by the tower. It is not a plant-specific growing target."
            else:
                support = "This value is measured by the tower, but this plant has no target range for it."
        else:
            minimum, maximum = target.get("min"), target.get("max")
            if minimum is not None and value < minimum:
                state, tone = "Below target", "warning"
            elif maximum is not None and value > maximum:
                state, tone = "Above target", "warning"
            else:
                state, tone = "Within range", "success"
        number, shown_unit = sensor_number_and_unit(value, unit)
        with column:
            st.html(
                f"""
                <div class="sensor-card uxv2-primary-reading">
                  <div class="sensor-label">{e(label)}</div>
                  <div class="sensor-value"><strong>{e(number)}</strong>{f'<span>{e(shown_unit)}</span>' if shown_unit else ''}</div>
                  <span class="pill {tone}">{e(state)}</span>
                  {f'<div class="sensor-support">{e(support)}</div>' if support else ''}
                </div>
                """
            )

def render_planting(planting: dict[str, Any] | None, profiles: list[dict[str, Any]], tower_id: int) -> None:
    render_section("Growing now")
    if not planting:
        render_empty(
            "This tower is ready for a plant",
            "Choose a saved plant or create a new one. You can change this later by ending the growing cycle.",
        )
        render_choose_plant(tower_id, profiles, key_prefix=f"tower_{tower_id}")
        return

    name = profile_display_name(planting.get("profile_name"))
    snapshot = planting.get("profile_snapshot") or {}
    targets = snapshot.get("targets") or {}
    ph_text = target_text(targets.get("ph"))
    ec_text = target_text(targets.get("ec_ms_cm"))
    art_name = "active_basil.png" if name.lower().startswith("basil") else "active_generic.png"
    st.html(
        f"""
        <div class="planting-summary-card">
          <div class="planting-summary-art">{asset_image(art_name, f'{name} planting', 'active-planting-illustration')}</div>
          <div class="planting-summary-copy">
            <h3>{e(name)}</h3>
            <div class="inline-stat">Started {e(format_date(planting.get('started_at')))}</div>
            <div class="planting-target-line">
              <span><b>pH</b> {e(ph_text)}</span>
              <span><b>EC</b> {e(ec_text)}</span>
            </div>
          </div>
        </div>
        """
    )

    with st.expander("About these growing targets"):
        profile_type = snapshot.get("profile_type")
        if profile_type == "system_builtin":
            st.caption("These built-in targets are evidence-backed and protected from direct editing.")
        elif profile_type == "user_customized":
            st.caption("This is your customized plant. Any values you changed are treated as settings chosen by you.")
        else:
            st.caption("These targets were entered by you; the app does not treat them as evidence-backed recommendations.")
        st.caption(
            "These targets were saved when this growing cycle started. Later plant-setting changes do not change this active cycle automatically."
        )
        references = snapshot.get("source_references") or []
        for ref in references:
            institution = ref.get("institution") or ref.get("publisher") or "Source"
            publication = ref.get("publication_id") or ref.get("title") or "Reference"
            st.write(f"**{institution}** — {publication}")
            if ref.get("url"):
                st.markdown(f"[View source]({ref['url']})")

    with st.expander("End this growing cycle"):
        st.caption(
            "Use this when this crop is finished or you are stopping it early. The tower becomes ready for another plant. "
            "Your saved plant settings are not deleted."
        )
        reason = st.radio(
            "Why are you ending it?",
            ["The crop finished normally", "I am stopping it early"],
            key=f"end_reason_{tower_id}",
        )
        confirmed = st.checkbox(
            "I understand this ends the active growing cycle.",
            key=f"end_confirm_{tower_id}",
        )
        if st.button(
            "End growing cycle",
            key=f"end_planting_{tower_id}",
            disabled=not confirmed,
            use_container_width=True,
        ):
            try:
                status = "completed" if reason == "The crop finished normally" else "cancelled"
                client.end_planting(tower_id, status)
                set_flash(
                    "success",
                    "Growing cycle ended. This tower is ready for another plant.",
                )
                st.rerun()
            except ApiError as exc:
                st.error(str(exc))


def render_profile_choices(
    profiles: list[dict[str, Any]],
    *,
    key_prefix: str,
    on_use,
    on_customize,
    on_edit=None,
) -> None:
    groups = [
        ("Evidence-backed plants", [p for p in profiles if p.get("profile_type") == "system_builtin"]),
        ("Your plants", [p for p in profiles if p.get("profile_type") != "system_builtin"]),
    ]
    for heading, items in groups:
        if not items:
            continue
        st.markdown(f"#### {heading}")
        if heading == "Evidence-backed plants":
            st.caption("Built-in plants are protected. Use them as-is, or make your own editable version.")
        else:
            st.caption("These are plants you saved. You can use them again or edit their settings for future growing cycles.")
        for start in range(0, len(items), 2):
            row = items[start : start + 2]
            columns = st.columns(len(row), gap="medium")
            for column, profile in zip(columns, row):
                with column:
                    with st.container(border=True):
                        name = profile_display_name(profile.get("name"))
                        st.markdown(f"**{name}**")
                        st.caption(profile_origin_label(profile, profiles))
                        ph = profile_target(profile, "ph")
                        ec = profile_target(profile, "ec_ms_cm")
                        st.caption(f"pH: {target_text(ph)}")
                        st.caption(f"EC: {target_text(ec)}")
                        if st.button(
                            f"Use {name}",
                            key=f"{key_prefix}_use_{profile['id']}",
                            type="primary",
                            use_container_width=True,
                        ):
                            on_use(profile)
                        if profile.get("profile_type") == "system_builtin":
                            if st.button(
                                f"Make my own {name}",
                                key=f"{key_prefix}_customize_{profile['id']}",
                                use_container_width=True,
                            ):
                                on_customize(profile)
                        elif on_edit is not None:
                            if st.button(
                                f"Edit {name}",
                                key=f"{key_prefix}_edit_{profile['id']}",
                                use_container_width=True,
                            ):
                                on_edit(profile)


def _custom_targets_from_inputs(key_prefix: str) -> tuple[str, dict[str, Any]]:
    name = str(st.session_state.get(f"{key_prefix}_name") or "").strip()
    if not name:
        raise ValueError("Enter a plant name.")
    targets: dict[str, Any] = {}
    if st.session_state.get(f"{key_prefix}_use_ph"):
        targets["ph"] = parse_target_range(
            str(st.session_state.get(f"{key_prefix}_ph_min") or ""),
            str(st.session_state.get(f"{key_prefix}_ph_max") or ""),
            unit="pH",
        )
    if st.session_state.get(f"{key_prefix}_use_ec"):
        targets["ec_ms_cm"] = parse_target_range(
            str(st.session_state.get(f"{key_prefix}_ec_min") or ""),
            str(st.session_state.get(f"{key_prefix}_ec_max") or ""),
            unit="mS/cm",
        )
    return name, targets


def render_custom_plant_form(*, key_prefix: str, on_created) -> None:
    st.markdown("### Create your plant")
    st.caption(
        "Only enter target ranges you already know and trust. The app will not invent growing targets for custom plants."
    )
    st.text_input("Plant name", key=f"{key_prefix}_name", placeholder="Mint")
    st.markdown("#### Growing targets")
    use_ph = st.checkbox("Add pH target", key=f"{key_prefix}_use_ph")
    if use_ph:
        left, right = st.columns(2)
        with left:
            st.text_input("Minimum", key=f"{key_prefix}_ph_min", placeholder="e.g. 5.5")
        with right:
            st.text_input("Maximum", key=f"{key_prefix}_ph_max", placeholder="e.g. 6.0")
        st.caption("Unit: pH")

    use_ec = st.checkbox("Add EC target", key=f"{key_prefix}_use_ec")
    if use_ec:
        left, right = st.columns(2)
        with left:
            st.text_input("Minimum", key=f"{key_prefix}_ec_min", placeholder="e.g. 1.0")
        with right:
            st.text_input("Maximum", key=f"{key_prefix}_ec_max", placeholder="e.g. 1.6")
        st.caption("Unit: mS/cm")

    if not use_ph and not use_ec:
        st.info(
            "No crop targets will be saved. The sensors can still show readings, but the app cannot create crop-specific alerts until targets are added."
        )

    if st.button("Save plant and use it", key=f"{key_prefix}_save", type="primary", use_container_width=True):
        try:
            name, targets = _custom_targets_from_inputs(key_prefix)
            on_created(name, targets)
        except ValueError as exc:
            st.error(str(exc))
        except ApiError as exc:
            st.error(str(exc))


def render_customize_builtin_form(
    profile: dict[str, Any],
    profiles: list[dict[str, Any]],
    *,
    key_prefix: str,
    on_ready,
) -> None:
    name = profile_display_name(profile.get("name"))
    st.markdown(f"### Customize {name}")
    st.caption(
        f"Start from {name}'s evidence-backed targets and change only what you need. The original {name} profile will stay unchanged."
    )
    copy_name = st.text_input("Plant name", value=f"My {name}", key=f"{key_prefix}_name")
    original_targets = deepcopy(profile.get("targets") or {})
    values: dict[str, tuple[float, float]] = {}
    for factor, label in [("ph", "pH"), ("ec_ms_cm", "EC")]:
        target = original_targets.get(factor)
        if not isinstance(target, dict):
            continue
        minimum = float(target.get("min", 0.0))
        maximum = float(target.get("max", 0.0))
        unit = target.get("unit") or SENSOR_SPECS[factor][1]
        st.markdown(f"#### {label}")
        left, right = st.columns(2)
        with left:
            new_min = st.number_input("Minimum", value=minimum, key=f"{key_prefix}_{factor}_min")
        with right:
            new_max = st.number_input("Maximum", value=maximum, key=f"{key_prefix}_{factor}_max")
        st.caption(f"Unit: {unit or label}")
        values[factor] = (float(new_min), float(new_max))
        if float(new_min) != minimum or float(new_max) != maximum:
            st.caption("Changed by you")

    button_name = copy_name.strip() or f"My {name}"
    if st.button(f"Save as {button_name}", key=f"{key_prefix}_save", type="primary", use_container_width=True):
        if not copy_name.strip():
            st.error("Enter a plant name.")
            return
        try:
            updated_targets = deepcopy(original_targets)
            for factor, (new_min, new_max) in values.items():
                original = original_targets.get(factor) or {}
                old_min = float(original.get("min", new_min))
                old_max = float(original.get("max", new_max))
                if new_min > new_max:
                    raise ValueError(f"{factor_label(factor)} minimum cannot be greater than maximum.")
                if new_min != old_min or new_max != old_max:
                    updated_targets[factor] = {"min": new_min, "max": new_max}
            on_ready(copy_name.strip(), updated_targets)
        except ValueError as exc:
            st.error(str(exc))
        except ApiError as exc:
            st.error(str(exc))

def render_edit_user_profile_form(
    profile: dict[str, Any],
    *,
    key_prefix: str,
    on_saved,
) -> None:
    name = profile_display_name(profile.get("name"))
    st.markdown(f"### Edit {name}")
    st.caption(
        "Changes update this saved plant for future growing cycles. Any tower already growing it keeps the target snapshot saved when that cycle started."
    )
    new_name = st.text_input("Plant name", value=name, key=f"{key_prefix}_name")
    original_targets = deepcopy(profile.get("targets") or {})
    updated_targets = {
        factor: deepcopy(value)
        for factor, value in original_targets.items()
        if factor not in {"ph", "ec_ms_cm"}
    }

    for factor, label, unit in [("ph", "pH", "pH"), ("ec_ms_cm", "EC", "mS/cm")]:
        existing = original_targets.get(factor) if isinstance(original_targets.get(factor), dict) else None
        enabled = st.checkbox(
            f"Set {label} target",
            value=existing is not None,
            key=f"{key_prefix}_{factor}_enabled",
        )
        if not enabled:
            continue
        left, right = st.columns(2)
        default_min = "" if existing is None else str(existing.get("min", ""))
        default_max = "" if existing is None else str(existing.get("max", ""))
        with left:
            minimum = st.text_input(
                "Minimum", value=default_min, key=f"{key_prefix}_{factor}_min"
            )
        with right:
            maximum = st.text_input(
                "Maximum", value=default_max, key=f"{key_prefix}_{factor}_max"
            )
        st.caption(f"Unit: {unit}")
        try:
            updated_targets[factor] = parse_target_range(minimum, maximum, unit=unit)
        except ValueError:
            # Validation is shown when Save is pressed so partially typed values are allowed.
            pass

    if st.button("Save changes", key=f"{key_prefix}_save", type="primary", use_container_width=True):
        if not new_name.strip():
            st.error("Enter a plant name.")
            return
        try:
            final_targets = {
                factor: deepcopy(value)
                for factor, value in original_targets.items()
                if factor not in {"ph", "ec_ms_cm"}
            }
            for factor, label, unit in [("ph", "pH", "pH"), ("ec_ms_cm", "EC", "mS/cm")]:
                if not st.session_state.get(f"{key_prefix}_{factor}_enabled"):
                    continue
                final_targets[factor] = parse_target_range(
                    str(st.session_state.get(f"{key_prefix}_{factor}_min") or ""),
                    str(st.session_state.get(f"{key_prefix}_{factor}_max") or ""),
                    unit=unit,
                )
            updated = client.update_profile(profile["id"], name=new_name.strip(), targets=final_targets)
            on_saved(updated)
        except (ValueError, ApiError) as exc:
            st.error(str(exc))


def render_choose_plant(tower_id: int, profiles: list[dict[str, Any]], key_prefix: str) -> None:
    mode_key = f"{key_prefix}_plant_mode"
    selected_key = f"{key_prefix}_customize_profile_id"
    mode = st.session_state.get(mode_key, "choose")

    if mode == "custom":
        if st.button("← Back to plant choices", key=f"{key_prefix}_custom_back"):
            st.session_state[mode_key] = "choose"
            st.rerun()

        def on_custom_created(name: str, targets: dict[str, Any]) -> None:
            profile = client.create_custom_profile(name, targets)
            client.start_planting(tower_id, profile["id"])
            set_flash("success", f"{name} is now growing in this tower.")
            st.session_state.pop(mode_key, None)
            st.rerun()

        render_custom_plant_form(key_prefix=f"{key_prefix}_new", on_created=on_custom_created)
        return

    if mode == "customize":
        selected_id = st.session_state.get(selected_key)
        selected = next((profile for profile in profiles if profile.get("id") == selected_id), None)
        if selected is None:
            st.session_state[mode_key] = "choose"
            st.rerun()
        if st.button("← Back to plant choices", key=f"{key_prefix}_customize_back"):
            st.session_state[mode_key] = "choose"
            st.rerun()

        def on_copy_ready(copy_name: str, updated_targets: dict[str, Any]) -> None:
            copied = client.customized_copy(selected["id"], copy_name)
            copied = client.update_profile(copied["id"], targets=updated_targets)
            client.start_planting(tower_id, copied["id"])
            set_flash("success", f"{profile_display_name(copied.get('name'))} is now growing in this tower.")
            st.session_state.pop(mode_key, None)
            st.session_state.pop(selected_key, None)
            st.rerun()

        render_customize_builtin_form(
            selected,
            profiles,
            key_prefix=f"{key_prefix}_copy_{selected_id}",
            on_ready=on_copy_ready,
        )
        return

    if mode == "edit":
        selected_id = st.session_state.get(selected_key)
        selected = next((profile for profile in profiles if profile.get("id") == selected_id), None)
        if selected is None or selected.get("profile_type") == "system_builtin":
            st.session_state[mode_key] = "choose"
            st.rerun()
        if st.button("← Back to plant choices", key=f"{key_prefix}_edit_back"):
            st.session_state[mode_key] = "choose"
            st.rerun()

        def on_edit_saved(updated: dict[str, Any]) -> None:
            set_flash("success", f"{profile_display_name(updated.get('name'))} was updated for future growing cycles.")
            st.session_state[mode_key] = "choose"
            st.session_state.pop(selected_key, None)
            st.rerun()

        render_edit_user_profile_form(
            selected,
            key_prefix=f"{key_prefix}_edit_{selected_id}",
            on_saved=on_edit_saved,
        )
        return

    st.markdown("### Choose what to grow")
    st.caption("Pick a saved plant. Nothing starts until you press a Use button.")

    def use_profile(profile: dict[str, Any]) -> None:
        try:
            client.start_planting(tower_id, profile["id"])
            set_flash("success", f"{profile_display_name(profile.get('name'))} is now growing in this tower.")
            st.rerun()
        except ApiError as exc:
            st.error(str(exc))

    def customize_profile(profile: dict[str, Any]) -> None:
        st.session_state[mode_key] = "customize"
        st.session_state[selected_key] = profile["id"]
        st.rerun()

    def edit_profile(profile: dict[str, Any]) -> None:
        st.session_state[mode_key] = "edit"
        st.session_state[selected_key] = profile["id"]
        st.rerun()

    if profiles:
        render_profile_choices(
            profiles,
            key_prefix=key_prefix,
            on_use=use_profile,
            on_customize=customize_profile,
            on_edit=edit_profile,
        )
    else:
        render_empty("No saved plants yet", "Create your first plant below.")

    if st.button("＋ Create a new plant", key=f"{key_prefix}_new_plant", use_container_width=True):
        st.session_state[mode_key] = "custom"
        st.rerun()

def alert_heading(alert: dict[str, Any]) -> str:
    direction = (alert.get("details") or {}).get("direction")
    label = factor_label(alert.get("factor", "condition"))
    if direction == "below":
        return f"{label} is too low"
    if direction == "above":
        return f"{label} is too high"
    return f"{label} needs attention"


def render_attention(snapshot: dict[str, Any]) -> None:
    planting = snapshot.get("planting")
    latest = snapshot.get("latest") or {}
    stale = snapshot_is_stale(snapshot)
    alerts, recommendations = current_operational_state(snapshot)
    render_section("Needs your attention" if stale or alerts else "Current status")
    rec_by_alert: dict[int, list[dict[str, Any]]] = {}
    for rec in recommendations:
        rec_by_alert.setdefault(rec.get("alert_id"), []).append(rec)

    if stale:
        st.warning(
            f"Readings are not updating. The latest reading is {relative_time(latest.get('timestamp'))}; "
            "new telemetry is required before a correction can be reviewed or applied safely."
        )
        return

    if not planting:
        st.info("Choose a plant before crop-specific target checks can run.")
        return

    if not alerts:
        configured_targets = (planting.get("profile_snapshot") or {}).get("targets") or {}
        missing = [
            factor_label(factor)
            for factor in configured_targets
            if factor in SENSOR_SPECS and latest.get(factor) is None
        ]
        if missing:
            st.info(
                "Some crop checks cannot run because these readings are unavailable: "
                + ", ".join(missing)
                + ". Missing readings are never fabricated."
            )
        elif not configured_targets:
            st.info(
                "This plant has no crop target ranges set. Sensor readings can still be displayed, but crop-specific alerts cannot be created."
            )
        else:
            st.html(
                '<div class="calm-state"><div class="calm-check">✓</div><div><strong>Everything looks good right now</strong><span>No current crop-target condition needs your decision.</span></div></div>'
            )
        return

    for alert in alerts:
        related = sorted(rec_by_alert.get(alert.get("id"), []), key=lambda item: item.get("id", 0), reverse=True)
        active = next((rec for rec in related if rec.get("status") in {"pending", "approved"}), None)
        executed = next((rec for rec in related if rec.get("status") == "executed"), None)
        factor = alert.get("factor")
        unit = SENSOR_SPECS.get(factor, ("", ""))[1]
        current = latest.get(factor)
        target = target_for_factor(planting, factor)

        if active and not recommendation_still_applies(active, alert, latest, planting):
            st.info(
                f"{alert_heading(alert)} was detected earlier, but the latest {factor_label(factor)} reading is now "
                f"{sensor_text(current, unit)}. The old correction is no longer presented as actionable."
            )
            continue

        st.html(
            f"""
            <div class="attention-panel">
              <div class="eyebrow">Current condition</div>
              <h3>{e(alert_heading(alert))}</h3>
              <div class="attention-reading-row">
                <div><span>Current</span><strong>{e(sensor_text(current, unit))}</strong></div>
                <div><span>Target</span><strong>{e(target_text(target))}</strong></div>
              </div>
            </div>
            """
        )

        if active:
            action_title, action_description = ACTION_COPY.get(
                active.get("action_type"),
                ("Review the suggested next step", "The application keeps this action supervised."),
            )
            params = active.get("action_parameters") or {}
            if not bool(params.get("executable", True)):
                st.warning(f"**{action_title}.** {action_description}")
                if active.get("action_type") == "manual_ec_high_intervention":
                    st.caption("There is no automatic dilution or drain actuator in this MVP.")
                else:
                    st.caption("No device command is available for this recommendation; later readings can still show whether the condition recovered.")
                continue

            detected = params.get("observed_value_at_creation")
            if detected is not None:
                st.caption(f"Reading when detected: {sensor_text(detected, unit)}")
            st.write(f"**Recommended next step: {action_title}**")
            st.caption(action_description)

            if active.get("status") == "pending":
                approve_col, reject_col = st.columns(2)
                with approve_col:
                    if st.button("Approve correction", key=f"approve_{active['id']}", type="primary", use_container_width=True):
                        try:
                            client.approve_recommendation(active["id"])
                            refreshed, _history = client.authoritative_recommendation_state(snapshot["tower"]["id"], active["id"])
                            if refreshed.get("status") == "approved":
                                set_flash("success", "Approved. Nothing has been applied yet.")
                            else:
                                set_flash("warning", "Conditions changed; the recommendation is no longer actionable.")
                            st.rerun()
                        except ApiError as exc:
                            st.error(str(exc))
                with reject_col:
                    if st.button("Reject recommendation", key=f"reject_{active['id']}", use_container_width=True):
                        try:
                            client.reject_recommendation(active["id"], "Rejected by user from the tower view.")
                            refreshed, _history = client.authoritative_recommendation_state(snapshot["tower"]["id"], active["id"])
                            if refreshed.get("status") == "rejected":
                                set_flash("success", "Recommendation rejected. No action was applied.")
                            else:
                                set_flash("warning", f"Current recommendation status: {refreshed.get('status', 'unknown')}.")
                            st.rerun()
                        except ApiError as exc:
                            st.error(str(exc))
            elif active.get("status") == "approved":
                st.success("Correction approved. Nothing has been applied yet.")
                if st.button("Apply approved correction", key=f"execute_{active['id']}", type="primary", use_container_width=True):
                    try:
                        client.execute_recommendation(active["id"])
                        refreshed, history = client.authoritative_recommendation_state(snapshot["tower"]["id"], active["id"])
                        evidence = execution_evidence(history, active["id"])
                        if refreshed.get("status") == "executed" and evidence.get("execution_verified"):
                            set_flash("info", "The simulated device accepted the correction. Waiting for newer readings to verify the result.")
                        elif refreshed.get("status") == "stale":
                            set_flash("warning", "Conditions changed before execution, so the approved correction was not applied.")
                        else:
                            set_flash("warning", evidence.get("label", "The correction could not be confirmed."))
                        st.rerun()
                    except ApiError as exc:
                        st.error(str(exc))
            continue

        if executed:
            evidence = execution_evidence(snapshot.get("history", []), executed["id"])
            if evidence.get("physical_recovery_verified"):
                st.success("Back in target range. Newer telemetry verified recovery.")
            elif evidence.get("follow_up_required"):
                st.warning("The condition has not recovered yet. A new recommendation is required before another correction can be applied.")
            elif evidence.get("execution_verified"):
                st.info("Checking the result. The simulated device accepted the command; newer telemetry must verify what changed.")
            elif evidence.get("execution_failed"):
                st.error("Action could not be confirmed by current system evidence.")
            else:
                st.info("A correction was requested. The system is waiting for authoritative execution evidence.")
        else:
            st.warning("This condition needs review, but there is no current actionable recommendation.")

        with st.expander("Why am I seeing this?"):
            st.write(alert.get("message") or "The latest reading is outside the configured target range.")
            details = alert.get("details") or {}
            provenance = details.get("target_provenance")
            if provenance == "system_evidence_backed":
                st.caption("The target came from an evidence-backed built-in plant setting captured by this planting.")
            elif provenance == "user_configured":
                st.caption("The target was set by the user and captured by this planting.")


def render_more_readings(latest: dict[str, Any] | None, planting: dict[str, Any] | None, stale: bool) -> None:
    latest = latest or {}
    with st.expander("More readings"):
        for factor in ["water_temp_c", "air_temp_c", "humidity_pct", "ppfd_umol_m2_s", "circulation_on"]:
            label, unit = SENSOR_SPECS[factor]
            value = latest.get(factor)
            target = target_for_factor(planting, factor)
            st.write(f"**{label}:** {sensor_text(value, unit)}")
            if stale:
                st.caption("Reading is old — waiting for newer telemetry.")
            elif value is None:
                st.caption("Not monitored — no sensor reading is available.")
            elif target:
                st.caption(f"Target for this plant: {target_text(target)}")
            else:
                st.caption("Tower sensor — measured by the tower; this plant does not define a target range for it.")

def render_trends(tower_id: int, planting: dict[str, Any] | None) -> None:
    with st.expander("View trends"):
        try:
            history = client.telemetry_history(tower_id, limit=120)
        except ApiError as exc:
            st.error(str(exc))
            return
        factor = st.selectbox(
            "Reading",
            ["ph", "ec_ms_cm", "water_level_pct", "water_temp_c", "air_temp_c", "humidity_pct", "ppfd_umol_m2_s"],
            format_func=lambda value: SENSOR_SPECS[value][0],
            key=f"trend_factor_{tower_id}",
        )
        points = chart_points(history, factor)
        if len(points) < 2:
            st.caption("Not enough monitored readings are available for a trend yet.")
            return
        data = pd.DataFrame(points)
        data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
        target = target_for_factor(planting, factor)
        unit = SENSOR_SPECS[factor][1]
        minimum_spans = {
            "ph": 0.6,
            "ec_ms_cm": 0.6,
            "water_level_pct": 10.0,
            "water_temp_c": 5.0,
            "air_temp_c": 5.0,
            "humidity_pct": 10.0,
            "ppfd_umol_m2_s": 200.0,
        }
        try:
            import altair as alt

            domain = chart_domain(data["value"].tolist(), target, minimum_span=minimum_spans.get(factor, 0.4))
            scale = alt.Scale(domain=list(domain), zero=False, nice=False) if domain else alt.Scale(zero=False, nice=False)
            y_encoding = alt.Y(
                "value:Q",
                title=f"{SENSOR_SPECS[factor][0]}{f' ({unit})' if unit else ''}",
                scale=scale,
            )
            line = (
                alt.Chart(data)
                .mark_line(point=True)
                .encode(
                    x=alt.X("timestamp:T", title=None, axis=alt.Axis(format="%H:%M", labelOverlap=True, tickCount=8)),
                    y=y_encoding,
                    tooltip=[
                        alt.Tooltip("timestamp:T", title="Time"),
                        alt.Tooltip("value:Q", title=SENSOR_SPECS[factor][0]),
                    ],
                )
            )
            layers = [line]
            if target and isinstance(target.get("min"), (int, float)) and isinstance(target.get("max"), (int, float)):
                target_data = pd.DataFrame([{"target": target["min"]}, {"target": target["max"]}])
                rules = (
                    alt.Chart(target_data)
                    .mark_rule(strokeDash=[5, 4], opacity=0.55)
                    .encode(y=alt.Y("target:Q", scale=scale))
                )
                layers.insert(0, rules)
                st.caption(f"Target for this plant: {target_text(target)}")
            else:
                st.caption("No target range is set for this reading.")
            st.altair_chart(alt.layer(*layers).properties(height=300), use_container_width=True)
        except ImportError:
            st.line_chart(data.set_index("timestamp")[["value"]], use_container_width=True)
            if target:
                st.caption(f"Target for this plant: {target_text(target)}")

def render_history(snapshot: dict[str, Any]) -> None:
    history = snapshot.get("history", [])
    with st.expander("History"):
        if not history:
            st.caption("No decisions or confirmed action events yet.")
        else:
            rows = []
            for event in history[:20]:
                event_type = event.get("event_type", "event")
                label = USER_EVENT_LABELS.get(event_type, "System activity")
                detail = EVENT_DETAILS.get(event_type, "A recorded application event occurred.")
                details = event.get("details") or {}
                if event_type == "physical_outcome_observed":
                    outcome = details.get("outcome")
                    if outcome == "improving":
                        label = "Condition is improving"
                        detail = "A newer reading moved toward the configured target. Recovery still requires later telemetry."
                    elif outcome == "wrong_direction":
                        label = "Condition moved the wrong way"
                        detail = "A newer reading moved farther from the configured target. No automatic repeat is performed."
                    else:
                        label = "New reading received"
                        detail = "A newer reading arrived after the correction step; the system is still checking the result."
                if event_type == "execution_blocked":
                    reason = str(details.get("reason") or "")
                    if details.get("manual_or_non_executable") or "manual" in reason.lower() or "no automatic" in reason.lower():
                        label = "Manual action required"
                        detail = "No automatic actuator exists for this recommendation, so no device command was sent."
                    elif reason:
                        detail = reason
                rows.append(
                    f"""
                    <div class="timeline-item">
                      <span class="timeline-dot {EVENT_TONES.get(event_type, 'neutral')}"></span>
                      <div class="timeline-title">{e(label)}</div>
                      <div class="timeline-meta">{e(relative_time(event.get('occurred_at')))}</div>
                      <div class="timeline-detail">{e(detail)}</div>
                    </div>
                    """
                )
            st.html(f'<div class="activity-card"><div class="timeline">{"".join(rows)}</div></div>')

    with st.expander("Technical details"):
        st.caption("Debug evidence only. These internal fields are intentionally hidden from the normal workflow.")
        if not history:
            st.caption("No technical action events are recorded yet.")
        for event in history[:20]:
            st.json(
                {
                    "event_type": event.get("event_type"),
                    "status": event.get("status"),
                    "actuator_type": event.get("actuator_type"),
                    "occurred_at": event.get("occurred_at"),
                    "details": event.get("details"),
                },
                expanded=False,
            )


def render_tower_settings(tower: dict[str, Any]) -> None:
    with st.expander("Tower settings"):
        st.caption("Rename this tower, or remove it from the local project when you no longer need it.")
        new_name = st.text_input(
            "Tower name",
            value=tower_display_name(tower),
            key=f"tower_settings_name_{tower['id']}",
        )
        if st.button("Save tower name", key=f"tower_settings_save_{tower['id']}"):
            if not new_name.strip():
                st.error("Enter a tower name.")
            else:
                try:
                    client.update_tower(tower["id"], name=new_name.strip())
                    set_flash("success", "Tower name updated.")
                    st.rerun()
                except ApiError as exc:
                    st.error(str(exc))

        st.divider()
        st.markdown("**Remove tower**")
        st.caption(
            "This permanently removes this tower and its local telemetry, growing-cycle history, alerts, recommendations, and action history. "
            "Saved plants remain available. The simulator will stop publishing this tower after its next device-list refresh."
        )
        confirmed = st.checkbox(
            f"I understand that removing {tower_display_name(tower)} deletes its local tower history.",
            key=f"tower_settings_delete_confirm_{tower['id']}",
        )
        if st.button(
            "Remove tower permanently",
            key=f"tower_settings_delete_{tower['id']}",
            disabled=not confirmed,
        ):
            try:
                name = tower_display_name(tower)
                client.delete_tower(tower["id"])
                set_flash("success", f"{name} was removed.")
                close_overlay()
            except ApiError as exc:
                st.error(str(exc))


def render_tower_dialog_content(tower: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
    snapshot = collect_tower_snapshot(tower)
    latest = snapshot.get("latest") or {}
    planting = snapshot.get("planting")
    label, tone = tower_ui_state(snapshot)
    stale = snapshot_is_stale(snapshot)
    plant_name = profile_display_name(planting.get("profile_name")) if planting else "No plant yet"

    st.caption(f"{plant_name} · Updated {relative_time(latest.get('timestamp')) if latest else 'no recent data'}")
    st.html(f'<span class="pill {tone}">{e(label)}</span>')

    if not snapshot.get("available"):
        st.error("This tower could not be loaded from the backend.")
        return

    render_primary_readings(latest, planting, stale)
    if stale:
        st.caption("Old sensor values are shown only for context. New telemetry is required before the tower can be treated as current.")

    render_attention(snapshot)
    render_planting(planting, profiles, tower["id"])
    render_tower_settings(tower)

    render_section("Current readings", "Primary readings stay visible above; less common measurements are available here.")
    render_more_readings(latest, planting, stale)
    render_trends(tower["id"], planting)
    render_history(snapshot)


def dismiss_overlay_query() -> None:
    st.query_params.clear()


def clear_add_tower_state() -> None:
    for key in list(st.session_state.keys()):
        if str(key).startswith("add_tower_"):
            st.session_state.pop(key, None)


def dismiss_add_tower() -> None:
    clear_add_tower_state()
    st.query_params.clear()


def show_tower_dialog(tower: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
    @st.dialog(tower_display_name(tower), width="large", on_dismiss=dismiss_overlay_query)
    def _dialog() -> None:
        render_tower_dialog_content(tower, profiles)

    _dialog()


def ensure_add_tower_created(name: str) -> int:
    existing = st.session_state.get("add_tower_created_id")
    if existing is not None:
        return int(existing)
    # Device identity is assigned by the backend; the normal UI never needs to
    # predict or expose MQTT/device mapping keys.
    tower = client.create_tower(name)
    st.session_state["add_tower_created_id"] = tower["id"]
    return int(tower["id"])


def finish_add_tower(message: str) -> None:
    set_flash("success", message)
    clear_add_tower_state()
    close_overlay()


def use_profile_for_new_tower(name: str, profile: dict[str, Any]) -> None:
    try:
        tower_id = ensure_add_tower_created(name)
        client.start_planting(tower_id, profile["id"])
        finish_add_tower(f"{name} was created and {profile_display_name(profile.get('name'))} is now growing. Simulated readings will begin automatically.")
    except ApiError as exc:
        st.error(str(exc))
        if st.session_state.get("add_tower_created_id"):
            st.info("The tower was created successfully. Fix the planting issue and retry; this screen will reuse the same tower instead of creating a duplicate.")


def _reuse_or_create_custom_profile(name: str, targets: dict[str, Any]) -> dict[str, Any]:
    signature = {"name": name, "targets": targets}
    if st.session_state.get("add_tower_pending_profile_signature") == signature:
        profile_id = st.session_state.get("add_tower_pending_profile_id")
        if profile_id is not None:
            return {"id": int(profile_id), "name": name, "targets": targets, "profile_type": "user_custom"}
    profile = client.create_custom_profile(name, targets)
    st.session_state["add_tower_pending_profile_signature"] = signature
    st.session_state["add_tower_pending_profile_id"] = profile["id"]
    return profile


def _reuse_or_create_customized_profile(
    source: dict[str, Any],
    copy_name: str,
    targets: dict[str, Any],
) -> dict[str, Any]:
    signature = {"source_id": source["id"], "name": copy_name, "targets": targets}
    if st.session_state.get("add_tower_pending_profile_signature") == signature:
        profile_id = st.session_state.get("add_tower_pending_profile_id")
        if profile_id is not None:
            return {"id": int(profile_id), "name": copy_name, "targets": targets, "profile_type": "user_customized"}
    copied = client.customized_copy(source["id"], copy_name)
    copied = client.update_profile(copied["id"], targets=targets)
    st.session_state["add_tower_pending_profile_signature"] = signature
    st.session_state["add_tower_pending_profile_id"] = copied["id"]
    return copied


def render_add_tower_flow(towers: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> None:
    step = st.session_state.get("add_tower_step", "name")
    name = st.session_state.get("add_tower_name")

    if step == "name":
        st.markdown("### Name your tower")
        tower_name = st.text_input("Tower name", placeholder="Kitchen Tower", key="add_tower_name_input")
        st.caption("You can choose a plant on the next step or leave the tower empty for now.")
        if st.button("Continue", key="add_tower_continue", type="primary", use_container_width=True):
            if not tower_name.strip():
                st.error("Enter a tower name.")
            else:
                st.session_state["add_tower_name"] = tower_name.strip()
                st.session_state["add_tower_step"] = "intent"
                st.rerun()
        return

    if not name:
        st.session_state["add_tower_step"] = "name"
        st.rerun()

    created_id = st.session_state.get("add_tower_created_id")
    st.caption(f"Tower: **{name}**" + (" · already created, waiting for a plant" if created_id else ""))

    if step == "intent":
        st.markdown("### Do you want to start growing something now?")
        st.caption("Choose a plant now, or create the tower without one and come back later.")
        if st.button("Choose a plant", key="add_tower_choose_now", type="primary", use_container_width=True):
            st.session_state["add_tower_step"] = "choose"
            st.rerun()
        if st.button("Set up tower only", key="add_tower_only", use_container_width=True):
            try:
                ensure_add_tower_created(name)
                finish_add_tower(f"{name} was created without a plant. Simulated readings will begin automatically, and you can choose a plant later.")
            except ApiError as exc:
                st.error(str(exc))
        if created_id is None and st.button("← Change tower name", key="add_tower_change_name"):
            st.session_state["add_tower_step"] = "name"
            st.session_state.pop("add_tower_name", None)
            st.rerun()
        return

    if step == "custom":
        if st.button("← Back to plant choices", key="add_tower_custom_back"):
            st.session_state["add_tower_step"] = "choose"
            st.rerun()

        def on_custom_ready(plant_name: str, targets: dict[str, Any]) -> None:
            profile = _reuse_or_create_custom_profile(plant_name, targets)
            use_profile_for_new_tower(name, profile)

        render_custom_plant_form(key_prefix="add_tower_custom", on_created=on_custom_ready)
        return

    if step == "customize":
        selected_id = st.session_state.get("add_tower_customize_profile_id")
        selected = next((profile for profile in profiles if profile.get("id") == selected_id), None)
        if selected is None:
            st.session_state["add_tower_step"] = "choose"
            st.rerun()
        if st.button("← Back to plant choices", key="add_tower_customize_back"):
            st.session_state["add_tower_step"] = "choose"
            st.rerun()

        def on_copy_ready(copy_name: str, targets: dict[str, Any]) -> None:
            copied = _reuse_or_create_customized_profile(selected, copy_name, targets)
            use_profile_for_new_tower(name, copied)

        render_customize_builtin_form(
            selected,
            profiles,
            key_prefix=f"add_tower_copy_{selected_id}",
            on_ready=on_copy_ready,
        )
        return

    if step == "edit":
        selected_id = st.session_state.get("add_tower_edit_profile_id")
        selected = next((profile for profile in profiles if profile.get("id") == selected_id), None)
        if selected is None or selected.get("profile_type") == "system_builtin":
            st.session_state["add_tower_step"] = "choose"
            st.rerun()
        if st.button("← Back to plant choices", key="add_tower_edit_back"):
            st.session_state["add_tower_step"] = "choose"
            st.rerun()

        def on_edit_ready(updated: dict[str, Any]) -> None:
            st.session_state["add_tower_step"] = "choose"
            st.session_state.pop("add_tower_edit_profile_id", None)
            set_flash("success", f"{profile_display_name(updated.get('name'))} was updated.")
            st.rerun()

        render_edit_user_profile_form(
            selected,
            key_prefix=f"add_tower_edit_{selected_id}",
            on_saved=on_edit_ready,
        )
        return

    # Plant choice step.
    st.markdown("### Choose what to grow")
    st.caption("Each Use button starts that plant in this tower. Nothing is selected silently.")

    def use_existing(profile: dict[str, Any]) -> None:
        use_profile_for_new_tower(name, profile)

    def customize(profile: dict[str, Any]) -> None:
        st.session_state["add_tower_customize_profile_id"] = profile["id"]
        st.session_state["add_tower_step"] = "customize"
        st.rerun()

    def edit_saved(profile: dict[str, Any]) -> None:
        st.session_state["add_tower_edit_profile_id"] = profile["id"]
        st.session_state["add_tower_step"] = "edit"
        st.rerun()

    render_profile_choices(
        profiles,
        key_prefix="add_tower_profile",
        on_use=use_existing,
        on_customize=customize,
        on_edit=edit_saved,
    )
    if st.button("＋ Create a new plant", key="add_tower_new_custom", use_container_width=True):
        st.session_state["add_tower_step"] = "custom"
        st.rerun()
    if st.button("← Back", key="add_tower_choose_back"):
        st.session_state["add_tower_step"] = "intent"
        st.rerun()


def show_add_tower_dialog(towers: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> None:
    @st.dialog("Add a tower", width="large", on_dismiss=dismiss_add_tower)
    def _dialog() -> None:
        render_add_tower_flow(towers, profiles)

    _dialog()


def show_assistant_dialog() -> None:
    @st.dialog("Hydroponic Assistant", width="large", on_dismiss=dismiss_overlay_query)
    def _dialog() -> None:
        try:
            state = client.assistant_status()
        except ApiError as exc:
            st.error("The Assistant cannot reach the local application right now.")
            with st.expander("Technical details"):
                st.caption(str(exc))
            return

        if not state.get("available"):
            st.html(
                f"""
                <div class="offline-feature uxv2-assistant-offline">
                  <div class="offline-art">{asset_image('assistant_offline.png', 'Offline smart assistant', 'assistant-offline-illustration')}</div>
                  <div class="offline-copy"><div class="eyebrow">Assistant status</div><h3>Assistant is currently offline</h3><p>The monitoring and supervised control system still works normally. Start local Ollama and ensure the configured model is installed to enable the Assistant.</p></div>
                </div>
                """
            )
            return

        header_left, header_right = st.columns([5, 1])
        with header_left:
            st.html(
                '<div class="assistant-chat-status"><span class="assistant-status-dot"></span>'
                '<strong>Local AI ready</strong><span>Ask about towers, plants, alerts, recommendations, or recent actions.</span></div>'
            )
        with header_right:
            if st.button("Clear", key="assistant_clear_chat", use_container_width=True):
                st.session_state["assistant_messages"] = []
                st.session_state.pop("assistant_last_error", None)
                st.session_state.pop("assistant_pending_prompt", None)
                st.session_state.pop("assistant_pending_action", None)
                st.rerun()

        messages = st.session_state.setdefault("assistant_messages", [])
        last_error = st.session_state.get("assistant_last_error")
        pending_prompt = st.session_state.get("assistant_pending_prompt")
        pending_action = st.session_state.get("assistant_pending_action")

        with st.container(height=430, border=True):
            if not messages:
                with st.chat_message("assistant", avatar="🌿"):
                    st.markdown(
                        "Hi. I can read the current simulated tower data, explain alerts and recommendations, "
                        "and help you carry out a supervised correction through explicit controls. What would you like to check?"
                    )

            for message_index, message in enumerate(messages):
                avatar = "🌿" if message["role"] == "assistant" else None
                with st.chat_message(message["role"], avatar=avatar):
                    st.markdown(message["content"])
                    if message["role"] == "assistant":
                        for candidate in message.get("actions") or []:
                            recommendation_id = candidate["recommendation_id"]
                            actions = candidate.get("actions") or []
                            st.caption(f"Supervised action · Recommendation #{recommendation_id}")
                            if "approve" in actions and "reject" in actions:
                                approve_col, reject_col = st.columns(2)
                                with approve_col:
                                    if st.button(
                                        "Approve correction",
                                        key=f"assistant_approve_{message_index}_{recommendation_id}",
                                        type="primary",
                                        use_container_width=True,
                                    ):
                                        for item in messages:
                                            item.pop("actions", None)
                                        action_message = f"Approve Recommendation #{recommendation_id}."
                                        messages.append({"role": "user", "content": action_message})
                                        st.session_state["assistant_pending_action"] = {
                                            "action": "approve",
                                            "recommendation_id": recommendation_id,
                                            "message": action_message,
                                        }
                                        st.rerun()
                                with reject_col:
                                    if st.button(
                                        "Reject recommendation",
                                        key=f"assistant_reject_{message_index}_{recommendation_id}",
                                        use_container_width=True,
                                    ):
                                        for item in messages:
                                            item.pop("actions", None)
                                        action_message = f"Reject Recommendation #{recommendation_id}."
                                        messages.append({"role": "user", "content": action_message})
                                        st.session_state["assistant_pending_action"] = {
                                            "action": "reject",
                                            "recommendation_id": recommendation_id,
                                            "message": action_message,
                                        }
                                        st.rerun()
                            elif "execute" in actions:
                                if st.button(
                                    "Apply approved correction",
                                    key=f"assistant_execute_{message_index}_{recommendation_id}",
                                    type="primary",
                                    use_container_width=True,
                                ):
                                    for item in messages:
                                        item.pop("actions", None)
                                    action_message = f"Apply approved Recommendation #{recommendation_id}."
                                    messages.append({"role": "user", "content": action_message})
                                    st.session_state["assistant_pending_action"] = {
                                        "action": "execute",
                                        "recommendation_id": recommendation_id,
                                        "message": action_message,
                                    }
                                    st.rerun()

            if pending_prompt:
                with st.chat_message("assistant", avatar="🌿"):
                    try:
                        with st.spinner("Checking the latest application data…"):
                            response = client.assistant_chat(pending_prompt)
                        action_candidates = assistant_action_candidates(response.get("tool_trace") or [])
                        assistant_message = {"role": "assistant", "content": response["message"]}
                        if action_candidates:
                            assistant_message["actions"] = action_candidates
                        messages.append(assistant_message)
                        st.session_state.pop("assistant_last_error", None)
                    except ApiError as exc:
                        st.session_state["assistant_last_error"] = str(exc)
                    finally:
                        st.session_state.pop("assistant_pending_prompt", None)
                    st.rerun()

            if pending_action:
                with st.chat_message("assistant", avatar="🌿"):
                    try:
                        with st.spinner("Revalidating the supervised action…"):
                            response = client.assistant_chat(
                                pending_action["message"],
                                action=pending_action["action"],
                                recommendation_id=pending_action["recommendation_id"],
                            )
                        action_candidates = assistant_action_candidates(response.get("tool_trace") or [])
                        assistant_message = {"role": "assistant", "content": response["message"]}
                        if action_candidates:
                            assistant_message["actions"] = action_candidates
                        messages.append(assistant_message)
                        st.session_state.pop("assistant_last_error", None)
                    except ApiError as exc:
                        st.session_state["assistant_last_error"] = str(exc)
                    finally:
                        st.session_state.pop("assistant_pending_action", None)
                    st.rerun()

            if last_error:
                st.warning("I couldn't get a response from the local Assistant. You can try again.")
                with st.expander("Technical details"):
                    st.caption(last_error)

        prompt = st.chat_input(
            "Message the Hydroponic Assistant…",
            key="assistant_chat_input",
        )
        st.caption(
            "Control actions still require explicit application approval. Typing “approve”, “reject”, or “execute” here does not perform an action. Use the explicit buttons shown in this chat for supervised control actions."
        )

        if prompt and prompt.strip():
            clean_prompt = prompt.strip()
            st.session_state.pop("assistant_last_error", None)
            messages.append({"role": "user", "content": clean_prompt})
            st.session_state["assistant_pending_prompt"] = clean_prompt
            st.rerun()

    _dialog()

def render_assistant_launcher() -> None:
    st.html('<a class="floating-assistant" href="?assistant=1" aria-label="Open Hydroponic Assistant" title="Open Hydroponic Assistant">✦</a>')


# Application frame
try:
    client.health()
except ApiError as exc:
    render_header(False)
    st.error("Application services are unavailable. Start the local system and try again.")
    st.caption(str(exc))
    st.stop()

render_header(True)
show_flash()

try:
    towers = client.list_towers()
    profiles = client.list_profiles()
except ApiError as exc:
    st.error(str(exc))
    st.stop()

snapshots = [collect_tower_snapshot(tower) for tower in towers]
render_home(snapshots)
render_assistant_launcher()

query_tower = st.query_params.get("tower")
query_action = st.query_params.get("action")
query_assistant = st.query_params.get("assistant")

if query_tower:
    try:
        tower_id = int(query_tower)
    except (TypeError, ValueError):
        tower_id = -1
    selected_tower = next((tower for tower in towers if tower.get("id") == tower_id), None)
    if selected_tower:
        show_tower_dialog(selected_tower, profiles)
    else:
        st.query_params.clear()
        set_flash("warning", "That tower is no longer available.")
        st.rerun()
elif query_action == "add":
    show_add_tower_dialog(towers, profiles)
elif query_assistant == "1":
    show_assistant_dialog()
