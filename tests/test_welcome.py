import pytest

from tests.conftest import GROUP
from tests.test_dialog import post


@pytest.mark.parametrize("known_group", [False, True])
@pytest.mark.parametrize("user", [False, True])
def test_blank_launch_explains_purpose_and_next_command(client, known_group, user):
    state = {"user" if user else "application": {"group_id": GROUP}} if known_group else {}
    result = post(client, new=True, user=user, state=state)
    text = result["response"]["text"]
    assert "Кэпик" in text and "расписание колледжа" in text
    assert ("что завтра" if known_group else "группу или фамилию") in text
    assert result["response"]["end_session"] is False
    assert "application_state" not in result and "user_state_update" not in result
    if not known_group:
        assert result["session_state"]["awaiting_group"]
        selected = post(client, "103", state={"session": result["session_state"]})
        assert selected["application_state"]["group_id"] == GROUP


def test_direct_question_does_not_repeat_intro_and_keeps_requested_day(client):
    result = post(client, "что завтра", new=True)
    assert "Кэпик" not in result["response"]["text"]
    selected = post(client, "103", state={"session": result["session_state"]})
    assert selected["session_state"]["last_date"] == "2026-09-16"
    assert selected["response"]["end_session"] is True
