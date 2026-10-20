import ast
from pathlib import Path


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_ux_v2_is_one_page_and_contextual():
    source_path = Path(__file__).resolve().parents[1] / "app.py"
    source = source_path.read_text()
    tree = ast.parse(source)
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    assert "render_home" in names
    assert "render_tower_dialog_content" in names
    assert "page_dashboard" not in names
    assert "page_towers" not in names
    assert "page_plants" not in names
    assert "page_assistant" not in names
    assert "NAV_ITEMS" not in source
    assert "top-nav-link" not in source


def test_history_and_technical_details_are_separate_expanders():
    source_path = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source_path.read_text())
    function = _function(tree, "render_history")
    expander_calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "expander"
    ]
    labels = [
        call.args[0].value
        for call in expander_calls
        if call.args and isinstance(call.args[0], ast.Constant)
    ]
    assert "History" in labels
    assert "Technical details" in labels


def test_plain_language_replaces_ambiguous_old_labels():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert '"Observed only"' not in source
    assert '"Manage planting"' not in source
    assert "Tower sensor" in source
    assert "End this growing cycle" in source


def test_dialogs_use_native_single_close_and_dismiss_cleanup():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert 'st.button("Close"' not in source
    assert "on_dismiss=dismiss_overlay_query" in source
    assert "on_dismiss=dismiss_add_tower" in source
    assert '@st.dialog("Tower"' not in source


def test_add_tower_is_guided_and_plant_choice_is_explicit():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert "Do you want to start growing something now?" in source
    assert "Set up tower only" in source
    assert "Choose what to grow" in source
    assert "Save plant and use it" in source
    assert "Nothing is selected silently" in source


def test_tower_settings_expose_explicit_remove_and_user_plant_editing():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert 'with st.expander("Tower settings")' in source
    assert '"Remove tower permanently"' in source
    assert 'client.delete_tower(tower["id"])' in source
    assert 'f"Edit {name}"' in source
    assert 'f"Make my own {name}"' in source


def test_reservoir_copy_explains_device_sensor_vs_plant_target():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert '"Reservoir level"' in source
    assert '"Tower sensor"' in source
    assert "reservoir level is measured by the tower" in source.lower()


def test_add_tower_leaves_internal_device_key_assignment_to_backend():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert "next_sim_tower_key" not in source
    assert "tower = client.create_tower(name)" in source


def test_assistant_dialog_uses_chat_transcript_and_chat_input() -> None:
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    tree = ast.parse(source)
    function = _function(tree, "show_assistant_dialog")
    segment = ast.get_source_segment(source, function) or ""

    assert "st.chat_message" in segment
    assert "st.chat_input" in segment
    assert "assistant_popup_prompt" not in segment
    assert 'st.button("Send"' not in segment
    assert "Technical details" in segment
    assert "Typing “approve”, “reject”, or “execute” here does not perform an action." in segment


def test_assistant_styles_include_chat_specific_rules() -> None:
    styles = (Path(__file__).resolve().parents[1] / "styles.css").read_text()
    assert "assistant-chat-status" in styles
    assert '[data-testid="stChatMessage"]' in styles
    assert '[data-testid="stChatInput"]' in styles


def test_assistant_dialog_exposes_only_explicit_structured_action_controls() -> None:
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    tree = ast.parse(source)
    function = _function(tree, "show_assistant_dialog")
    segment = ast.get_source_segment(source, function) or ""

    assert "assistant_action_candidates" in segment
    assert '"Approve correction"' in segment
    assert '"Reject recommendation"' in segment
    assert '"Apply approved correction"' in segment
    assert 'action=pending_action["action"]' in segment
    assert 'recommendation_id=pending_action["recommendation_id"]' in segment
    assert "Typing “approve”, “reject”, or “execute” here does not perform an action." in segment
