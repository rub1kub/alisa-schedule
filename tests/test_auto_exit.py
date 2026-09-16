import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Group, Lesson, ScheduleResult
from app.providers import ProviderError
from tests.conftest import GROUP, TODAY, envelope
from tests.test_dialog import chosen_state, post


@pytest.mark.parametrize("user", [False, True])
def test_completed_first_answer_keeps_preferences_for_next_launch(client, user):
    state_key = "user" if user else "application"
    update_key = "user_state_update" if user else "application_state"
    preferences = {"lesson_label": "subject"}
    initial = post(client, "что завтра", new=True, user=user, state={state_key: preferences})
    assert initial["response"]["end_session"] is False
    result = post(
        client,
        "103",
        user=user,
        state={"session": initial["session_state"], state_key: preferences},
    )
    assert result["response"]["end_session"] is True
    assert result[update_key] == {"group_id": GROUP, **preferences}
    restarted = post(
        client, "что сегодня", new=True, user=user, state={state_key: result[update_key]}
    )
    assert "Математика" in restarted["response"]["text"]
    assert restarted["session_state"]["last_date"] == str(TODAY)
    assert restarted["response"]["end_session"] is True


@pytest.mark.parametrize(
    "command", ["", "смени группу", "список групп", "расписание на 31.02.2026"]
)
def test_questions_keep_listening(client, command):
    result = post(client, command, state=chosen_state())
    assert result["response"]["end_session"] is False


def test_unpublished_schedule_is_a_complete_answer(client):
    result = post(client, "суббота", state=chosen_state())
    assert "нет опубликованного расписания" in result["response"]["text"]
    assert result["response"]["end_session"] is True


def test_auto_exit_can_be_disabled(dialog_client):
    result = post(dialog_client, "завтра", state=chosen_state())
    assert result["response"]["end_session"] is False
    assert result["response"]["buttons"]


@pytest.mark.parametrize("user", [False, True])
def test_plain_launch_onboarding_keeps_dialogue_without_saving_exit_preference(client, user):
    state_key = "user" if user else "application"
    update_key = "user_state_update" if user else "application_state"
    welcome = post(client, new=True, user=user)
    selected = post(client, "103", user=user, state={"session": welcome["session_state"]})
    assert selected[update_key] == {"group_id": GROUP}
    state = {state_key: selected[update_key], "session": selected["session_state"]}
    tomorrow = post(client, "завтра", user=user, state=state)
    assert tomorrow["response"]["end_session"] is False
    assert tomorrow["session_state"]["last_date"] == "2026-09-16"
    assert update_key not in tomorrow
    # A new one-shot query must ignore the previous conversation's session state.
    state["session"] = tomorrow["session_state"]
    direct = post(client, "что сегодня", new=True, user=user, state=state)
    assert direct["response"]["end_session"] is True
    assert "Математика" in direct["response"]["text"]


@pytest.mark.parametrize("auto_exit", [False, True])
@pytest.mark.parametrize("plain_launch", [False, True])
def test_explicit_exit_preference_wins_for_both_launches(client, auto_exit, plain_launch):
    state = {"application": {"group_id": GROUP, "auto_exit": auto_exit}}
    if plain_launch:
        welcome = post(client, new=True, state=state)
        state["session"] = welcome["session_state"]
    result = post(client, "завтра", new=not plain_launch, state=state)
    assert result["response"]["end_session"] is auto_exit


def test_interactive_launch_can_be_disabled_in_config(client):
    client.app.state.skill.responses.options.interactive_launch = False
    welcome = post(client, new=True, state=chosen_state())
    result = post(client, "завтра", state={"session": welcome["session_state"]})
    assert result["response"]["end_session"] is True


def test_forgetting_group_keeps_current_conversation_open(client):
    welcome = post(client, new=True, state=chosen_state())
    forgotten = post(client, "забудь группу", state={"session": welcome["session_state"]})
    selected = post(client, "103", state={"session": forgotten["session_state"]})
    tomorrow = post(client, "завтра", state={"session": selected["session_state"]})
    assert tomorrow["response"]["end_session"] is False
    assert selected["application_state"] == {"group_id": GROUP}


def test_explicit_exit_command_overrides_current_conversation(client):
    welcome = post(client, new=True, state=chosen_state())
    result = post(client, "выходи после ответа", state={"session": welcome["session_state"]})
    assert result["response"]["end_session"] is True
    assert result["application_state"] == {"group_id": GROUP, "auto_exit": True}


@pytest.mark.parametrize("operation", ["groups", "day"])
def test_source_failure_keeps_an_open_conversation(client, monkeypatch, operation):
    welcome = post(client, new=True, state=chosen_state())

    async def unavailable(*args):
        raise ProviderError("test outage")

    monkeypatch.setattr(client.app.state.skill.provider, operation, unavailable)
    result = post(client, "завтра", state={"session": welcome["session_state"]})
    assert "Попробуйте позже" in result["response"]["text"]
    assert result["response"]["end_session"] is False


@pytest.mark.parametrize("operation", ["groups", "day"])
def test_unavailable_schedule_exits(client, monkeypatch, operation):
    async def unavailable(*args):
        raise ProviderError("test outage")

    monkeypatch.setattr(client.app.state.skill.provider, operation, unavailable)
    result = post(client, "завтра", state=chosen_state())
    assert "Попробуйте позже" in result["response"]["text"]
    assert result["response"]["end_session"] is True


def test_long_schedule_exits_only_after_the_last_page(tmp_path, responses):
    class Source:
        async def groups(self):
            return [Group(id=GROUP, name=GROUP)]

        async def day(self, group_id, target):
            return ScheduleResult(
                group_id=group_id,
                date=target,
                source="kkepik",
                lessons=[
                    Lesson(number=n, subject=f"Предмет {n}: " + "подробное название " * 8)
                    for n in range(1, 10)
                ],
            )

    responses.options.page_chars = 300
    response_file = tmp_path / "responses.json"
    response_file.write_text(responses.model_dump_json())
    settings = Settings(responses_file=response_file)
    with TestClient(create_app(settings, provider=Source(), now=lambda: TODAY)) as client:
        result = client.post("/webhook", json=envelope("завтра", state=chosen_state())).json()
        assert result["response"]["end_session"] is False
        pages = []
        for _ in range(20):
            pages.append(result["response"]["text"].removesuffix(" Скажите «дальше»."))
            if result["response"]["end_session"]:
                break
            assert "Скажите «дальше»." in result["response"]["text"]
            result = post(client, "дальше", state={"session": result["session_state"]})
        else:
            pytest.fail("The last schedule page did not end the session")
        assert len(pages) > 1
        assert "buttons" not in result["response"]
        assert all(f"Предмет {n}:" in "".join(pages) for n in range(1, 10))
