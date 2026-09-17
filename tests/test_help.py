import json

import pytest
from pydantic import ValidationError

from app.language import detect_intent
from app.protocol import NLU
from app.responses import Responses
from tests.conftest import GROUP, envelope
from tests.test_dialog import chosen_state, post


@pytest.mark.parametrize("command", ["что ты умеешь", "что ты можешь", "что умеешь", "что можешь"])
def test_capabilities_are_not_the_help_instructions(command):
    assert detect_intent(command, NLU()) == "capabilities"
    assert detect_intent(command, NLU(intents={"YANDEX.HELP": {}})) == "capabilities"


@pytest.mark.parametrize("command", ["помощь", "помоги", "справка", "как пользоваться"])
def test_help_requests_use_instructions(command):
    assert detect_intent(command, NLU()) == "help"


def test_help_and_capabilities_are_distinct_without_loading_schedules(client, monkeypatch):
    async def unexpected(*args):
        pytest.fail("Auxiliary commands must not load the schedule")

    monkeypatch.setattr(client.app.state.skill.provider, "groups", unexpected)
    capabilities = post(client, "что ты умеешь", new=True)
    help_answer = post(client, "помощь", new=True)
    assert capabilities["response"]["text"] != help_answer["response"]["text"]
    assert "«смени группу»" in help_answer["response"]["text"]
    assert "«смени группу»" not in capabilities["response"]["text"]
    assert "application_state" not in help_answer
    assert "application_state" not in capabilities


def test_duplicate_configured_help_texts_are_rejected(responses):
    data = responses.model_dump()
    data["texts"]["capabilities"] = data["texts"]["help"]
    with pytest.raises(ValidationError, match="must have different texts"):
        Responses.model_validate(data)


@pytest.mark.parametrize("command", ["помощь", "что ты умеешь"])
def test_auxiliary_commands_keep_group_clarification_and_original_question(client, command):
    initial = post(client, "какой предмет на второй паре завтра", new=True)
    instructions = post(client, command, state={"session": initial["session_state"]})
    assert instructions["response"]["end_session"] is False
    assert instructions["session_state"] == initial["session_state"]
    selected = post(client, "103", state={"session": instructions["session_state"]})
    assert "2-я — Программирование" in selected["response"]["text"]
    assert selected["session_state"]["last_date"] == "2026-09-16"
    assert selected["response"]["end_session"] is True


@pytest.mark.parametrize("command", ["помощь", "что ты умеешь"])
def test_auxiliary_commands_preserve_current_day_and_followup(client, command):
    welcome = post(client, new=True, state=chosen_state())
    tomorrow = post(client, "завтра", state={"session": welcome["session_state"]})
    instructions = post(client, command, state={"session": tomorrow["session_state"]})
    assert instructions["response"]["end_session"] is False
    assert instructions["session_state"] == tomorrow["session_state"]
    first = post(client, "первая", state={"session": instructions["session_state"]})
    assert "Компьютерные сети" in first["response"]["text"]
    assert first["session_state"]["last_date"] == "2026-09-16"


def press(client, item, previous):
    payload = envelope(state={"session": previous["session_state"]}, payload=item["payload"])
    payload["request"]["type"] = "ButtonPressed"
    result = client.post("/webhook", json=payload)
    assert result.status_code == 200
    return result.json()


def buttons(result):
    items = result["response"]["buttons"]
    assert len({item["title"] for item in items}) == len(items)
    assert len({json.dumps(item["payload"], sort_keys=True) for item in items}) == len(items)
    return {item["payload"]["action"]: item for item in items}


def test_buttons_after_help_route_to_dates_and_group_selection(client):
    welcome = post(client, new=True, state=chosen_state())
    help_answer = post(client, "помощь", state={"session": welcome["session_state"]})
    actions = buttons(help_answer)
    tomorrow = press(client, actions["tomorrow"], help_answer)
    assert tomorrow["session_state"]["last_date"] == "2026-09-16"
    today = press(client, buttons(tomorrow)["today"], tomorrow)
    assert today["session_state"]["last_date"] == "2026-09-15"
    groups = press(client, buttons(today)["change"], today)
    assert groups["session_state"]["awaiting_group"]
    selected_button = next(
        item for item in groups["response"]["buttons"] if item["payload"].get("group_id") == GROUP
    )
    selected = press(client, selected_button, groups)
    assert selected["application_state"]["group_id"] == GROUP
    catalog = press(client, buttons(groups)["groups"], groups)
    assert "Группы:" in catalog["response"]["text"]
