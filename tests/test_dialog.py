import json

import pytest

from app.dialog import Memory, page, schedule_text
from app.models import Group, Lesson, ScheduleResult
from tests.conftest import GROUP, TODAY, envelope


def post(client, command="", **kwargs):
    response = client.post("/webhook", json=envelope(command, **kwargs))
    assert response.status_code == 200
    value = response.json()
    assert value["version"] == "1.0"
    assert len(value["response"]["text"]) <= 1024
    assert len(json.dumps(value["session_state"], ensure_ascii=False).encode()) <= 1024
    return value


def chosen_state():
    return {"application": {"group_id": GROUP}}


def test_first_request_keeps_tomorrow_while_asking_for_group(client):
    initial = post(client, "что завтра", new=True)
    assert "Какая группа?" in initial["response"]["text"]
    assert initial["response"]["end_session"] is False
    result = post(client, "группа сто три", state={"session": initial["session_state"]})
    assert result["session_state"]["last_date"] == "2026-09-16"
    assert "Компьютерные сети" in result["response"]["text"]
    assert result["application_state"] == {"group_id": GROUP}
    assert result["response"]["end_session"] is True


def test_group_survives_new_session_and_unknown_commands(client):
    selected = post(client, "103", new=True)
    result = post(
        client, "что сегодня", new=True, state={"application": selected["application_state"]}
    )
    assert "Математика" in result["response"]["text"]
    unknown = post(client, "погода", new=True, state={"application": selected["application_state"]})
    assert unknown["session_state"]["group_id"] == GROUP


def test_authorized_user_state_wins_over_other_surface_and_forget_clears_it(client):
    result = post(
        client,
        "моя группа",
        user=True,
        new=True,
        state={
            "user": {"group_id": GROUP},
            "application": {"group_id": "104-Д9-3ИСП"},
        },
    )
    assert GROUP in result["response"]["text"]
    selected = post(client, "103", user=True)
    assert selected["user_state_update"] == {"group_id": GROUP}
    assert "application_state" not in selected
    forgotten = post(client, "забудь группу", user=True, state={"user": {"group_id": GROUP}})
    assert forgotten["user_state_update"] == {"group_id": None}
    assert forgotten["session_state"] == {}


def test_new_account_does_not_inherit_guest_choice(client):
    result = post(client, "что завтра", new=True, user=True, state=chosen_state())
    assert "Какая группа?" in result["response"]["text"]


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("что сегодня", "Математика"),
        ("что завтра", "Компьютерные сети"),
        ("расписание на пятницу", "Пар нет"),
        ("какая первая пара", "2-я пара"),
        ("сколько пар", "2 пары"),
    ],
)
def test_required_utterances(client, command, fragment):
    result = post(client, command, state=chosen_state())
    assert fragment in result["response"]["text"]
    assert result["response"]["end_session"] is True
    assert "buttons" not in result["response"]


def test_followup_first_and_count_reuse_selected_day(dialog_client):
    tomorrow = post(dialog_client, "что завтра", state=chosen_state())
    first = post(dialog_client, "какая первая пара", state={"session": tomorrow["session_state"]})
    assert first["session_state"]["last_date"] == "2026-09-16"
    assert "Компьютерные сети" in first["response"]["text"]


def test_yandex_normalized_first_pair_preserves_context(dialog_client):
    tomorrow = post(dialog_client, "что завтра", state=chosen_state())
    request = envelope("какая 1 пара", state={"session": tomorrow["session_state"]})
    request["request"]["original_utterance"] = "какая первая пара"
    response = dialog_client.post("/webhook", json=request).json()
    assert response["session_state"]["last_date"] == "2026-09-16"
    assert "1-я пара" in response["response"]["text"]
    assert "Программирование" not in response["response"]["text"]


def test_empty_day_and_missing_day_have_different_meanings(client):
    empty = post(client, "пятница", state=chosen_state())["response"]["text"]
    missing = post(client, "суббота", state=chosen_state())["response"]["text"]
    assert "Пар нет" in empty
    assert "нет опубликованного расписания" in missing


def test_button_selects_group_and_keeps_original_question(client):
    first = post(client, "сколько пар завтра", new=True)
    response = envelope(
        payload={"action": "select_group", "group_id": GROUP},
        state={"session": first["session_state"]},
    )
    response["request"]["type"] = "ButtonPressed"
    selected = client.post("/webhook", json=response).json()
    assert selected["session_state"]["last_date"] == "2026-09-16"
    assert "2 пары" in selected["response"]["text"]


def test_unknown_group_does_not_return_previous_group_schedule(client):
    value = post(client, "расписание группы 999 на завтра", state=chosen_state())
    assert "Такой группы нет в списке" in value["response"]["text"]
    assert "Компьютерные сети" not in value["response"]["text"]


def test_change_group_then_ask_question_before_selection(client):
    first = post(client, "смени группу", state=chosen_state())
    second = post(client, "что завтра", state={"session": first["session_state"]})
    third = post(client, "104", state={"session": second["session_state"]})
    assert "104-Д9-3ИСП" in third["response"]["text"]
    assert "Разработка приложений" in third["response"]["text"]


def test_invalid_date_is_not_silently_today(client):
    value = post(client, "расписание на 31.02.2026", state=chosen_state())
    assert "Такой даты нет" in value["response"]["text"]


def test_pagination_delivers_every_character_and_detects_changes(responses):
    memory = Memory()
    text = " ".join("Подробное занятие номер " + str(index) + "." for index in range(200))
    pages = []
    more = False
    while True:
        value = page(text, memory, more, responses)
        assert len(value) <= 1024
        pages.append(value.removesuffix(" Скажите «дальше»."))
        if not memory.cursor:
            break
        more = True
    assert "".join(pages) == text
    page(text, memory, False, responses)
    assert "обновилось" in page(text + " Изменение.", memory, True, responses)


def test_subgroups_are_counted_as_one_pair_and_first_keeps_both(responses):
    result = ScheduleResult(
        group_id=GROUP,
        date=TODAY,
        source="kkepik",
        lessons=[
            Lesson(number=2, subject="Английский", subgroup="вариант 1"),
            Lesson(number=2, subject="Немецкий", subgroup="вариант 2"),
            Lesson(number=4, subject="Математика"),
        ],
    )
    group = Group(id=GROUP, name=GROUP)
    assert "2 пары" in schedule_text(result, group, "count", responses)
    first = schedule_text(result, group, "first", responses)
    assert "Английский" in first and "Немецкий" in first and "Математика" not in first


def test_webhook_validation_does_not_echo_user_input(client):
    assert (
        client.post("/webhook", content="{}", headers={"content-type": "text/plain"}).status_code
        == 415
    )
    assert (
        client.post(
            "/webhook", content="{", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/webhook", content="x" * 70000, headers={"content-type": "application/json"}
        ).status_code
        == 413
    )
    payload = envelope("some private text")
    payload["session"]["skill_id"] = "other"
    response = client.post("/webhook", json=payload)
    assert response.status_code == 403
    assert "some private text" not in response.text
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
