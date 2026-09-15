import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import GROUP, TODAY
from tests.test_dialog import post

OTHER = "104-Д9-3ИСП"


@pytest.fixture
def targets_client(tmp_path, fixture_data):
    for day in fixture_data["days"]:
        for lesson in day["lessons"]:
            lesson["teacher"] = "Примерова Д. Е."
    fixture_data["days"].extend(
        [
            {
                "group_id": OTHER,
                "date": str(TODAY),
                "lessons": [
                    {"number": 2, "subject": "Физика", "teacher": "Примерова Д. Е.", "room": "999"},
                    {"number": 3, "subject": "История", "teacher": "Тестов А. Б."},
                    {"number": 4, "subject": "Русский", "teacher": "Тестов В. Г."},
                ],
            },
            {"group_id": OTHER, "date": "2026-09-18", "lessons": []},
        ]
    )
    schedule, overrides = tmp_path / "schedule.json", tmp_path / "overrides.json"
    schedule.write_text(json.dumps(fixture_data))
    overrides.write_text('{"schema_version":1,"replacements":[]}')
    settings = Settings(provider="file", schedule_file=schedule, overrides_file=overrides)
    with TestClient(create_app(settings, now=lambda: TODAY)) as client:
        yield client


@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "расписание группы 104 на завтра",
        "что у сто четвёртой группы завтра",
        "что у сто четыре завтра",
        "сколько пар у группы 104 завтра",
        "какая первая пара группы 104 завтра",
        "расписание 104-Д9-3ИСП завтра",
        "сколько пар 104-Д9-3ИСП завтра",
    ],
)
def test_other_group_is_one_off_and_preserves_own_group(targets_client, command, user):
    key = "user" if user else "application"
    state = {key: {"group_id": GROUP, "lesson_label": "subject"}}
    result = post(targets_client, command, state=state, user=user, new=True)
    assert result["session_state"]["query_group_id"] == OTHER
    assert result["session_state"]["group_id"] == GROUP
    assert "application_state" not in result and "user_state_update" not in result
    assert result["response"]["end_session"] is True
    own = post(targets_client, "что завтра", state=state, user=user, new=True)
    assert "Компьютерные сети" in own["response"]["text"]


def test_one_off_group_needs_no_saved_group(targets_client):
    result = post(targets_client, "расписание группы 104 завтра", new=True)
    assert "Примерова" in result["response"]["text"]
    assert "application_state" not in result
    assert result["response"]["end_session"] is True


@pytest.mark.parametrize("command", ["смени группу на 104", "моя группа 104", "запомни группу 104"])
def test_explicit_own_group_change_is_persistent(targets_client, command):
    result = post(targets_client, command, state={"application": {"group_id": GROUP}})
    assert result["application_state"]["group_id"] == OTHER


def test_unknown_group_clarification_does_not_overwrite_own_group(targets_client):
    result = post(
        targets_client, "расписание группы 999 завтра", state={"application": {"group_id": GROUP}}
    )
    selected = post(targets_client, "104", state={"session": result["session_state"]})
    assert selected["session_state"]["group_id"] == GROUP
    assert selected["session_state"]["last_date"] == "2026-09-16"
    assert "application_state" not in selected


def test_date_clarification_keeps_temporary_group_and_count(targets_client):
    asked = post(
        targets_client,
        "сколько пар группы 104 на 31.02.2026",
        state={"application": {"group_id": GROUP}},
    )
    result = post(targets_client, "завтра", state={"session": asked["session_state"]})
    assert result["session_state"]["query_group_id"] == OTHER
    assert "1 пара." in result["response"]["text"]
    assert "Примерова" not in result["response"]["text"]
    assert "application_state" not in result
    assert result["response"]["end_session"] is True


@pytest.mark.parametrize(
    "command",
    [
        "расписание преподавателя Примеровой на сегодня",
        "расписание у Примеровой",
        "что у Примеровой сегодня",
        "расписание Примерова",
    ],
)
def test_teacher_lookup_without_own_group_and_without_initials_or_rooms(targets_client, command):
    result = post(targets_client, command, new=True)
    text = result["response"]["text"]
    assert text.startswith("Примерова.")
    assert GROUP in text and OTHER in text
    assert "2 пары." in text  # A simultaneous lesson in two groups is one period.
    assert "Д." not in text and "Е." not in text and "999" not in text
    assert "Математика" not in text and "сентября" not in text
    assert result["response"]["end_session"] is True
    assert "application_state" not in result


def test_teacher_ambiguity_keeps_question_and_date(targets_client):
    asked = post(targets_client, "сколько пар у Тестова завтра", new=True)
    assert "однофамильцы" in asked["response"]["text"]
    assert "А." not in asked["response"]["text"]
    assert asked["response"]["end_session"] is False
    result = post(targets_client, "Тестов А. Б.", state={"session": asked["session_state"]})
    assert result["session_state"]["last_date"] == "2026-09-16"
    assert result["session_state"]["last_kind"] == "count"
    assert "Пар нет." in result["response"]["text"]
    assert result["response"]["end_session"] is True


def test_teacher_missing_name_and_unrecognized_name_ask_not_own_schedule(targets_client):
    for command in ("расписание преподавателя", "расписание у Несуществующего"):
        result = post(targets_client, command, state={"application": {"group_id": GROUP}})
        assert result["response"]["end_session"] is False
        assert "фамилию" in result["response"]["text"]
        assert "Математика" not in result["response"]["text"]


def test_teacher_date_clarification_keeps_first_pair(targets_client):
    asked = post(targets_client, "первая пара у Примеровой на 31.02.2026")
    result = post(targets_client, "завтра", state={"session": asked["session_state"]})
    assert result["session_state"]["last_kind"] == "first"
    assert "1-я" in result["response"]["text"] and "2-я" not in result["response"]["text"]
    assert result["response"]["end_session"] is True


def test_teacher_missing_publication_is_not_a_day_off(targets_client):
    result = post(targets_client, "расписание у Примеровой на субботу")
    assert "нет опубликованного расписания преподавателя" in result["response"]["text"]
    assert "Пар нет" not in result["response"]["text"]
    assert result["response"]["end_session"] is True


def test_who_teaches_first_pair_is_still_own_group(targets_client):
    result = post(
        targets_client, "кто ведёт первую пару", state={"application": {"group_id": GROUP}}
    )
    assert result["response"]["text"] == "Это пример расписания. 2-я пара — Примерова, в 10:25."


@pytest.mark.parametrize("user", [False, True])
def test_auto_exit_preference_is_saved_and_isolated(targets_client, user):
    key = "user" if user else "application"
    update = "user_state_update" if user else "application_state"
    state = {key: {"group_id": GROUP}}
    stay = post(targets_client, "оставайся в навыке", state=state, user=user)
    state = {key: stay[update]}
    assert state[key]["auto_exit"] is False
    result = post(targets_client, "что завтра", state=state, user=user, new=True)
    assert result["response"]["end_session"] is False
    exit_setting = post(targets_client, "выходи после ответа", state=state, user=user, new=True)
    assert exit_setting[update]["auto_exit"] is True
    result = post(
        targets_client, "что завтра", state={key: exit_setting[update]}, user=user, new=True
    )
    assert result["response"]["end_session"] is True


def test_teacher_subjects_are_configurable_and_count_ignores_them(targets_client):
    targets_client.app.state.skill.responses.options.teacher_lesson_label = "both"
    result = post(targets_client, "расписание Примеровой")
    assert "Физика" in result["response"]["text"]
    result = post(targets_client, "сколько пар у Примеровой")
    assert "Физика" not in result["response"]["text"]


def test_teacher_pagination_keeps_target_and_exits_on_last_page(targets_client, monkeypatch):
    from app.models import TeacherLesson, TeacherScheduleResult

    async def long_day(teacher_id, target):
        return TeacherScheduleResult(
            teacher_id=teacher_id,
            date=target,
            source="kkepik",
            lessons=[
                TeacherLesson(number=n, groups=[OTHER], subject=f"Предмет {n}: " + "название " * 15)
                for n in range(1, 10)
            ],
        )

    skill = targets_client.app.state.skill
    skill.responses.options.page_chars = 300
    skill.responses.options.teacher_lesson_label = "both"
    monkeypatch.setattr(skill.provider, "teacher_day", long_day)
    result = post(targets_client, "расписание Примеровой завтра")
    pages = []
    for _ in range(20):
        pages.append(result["response"]["text"].removesuffix(" Скажите «дальше»."))
        if result["response"]["end_session"]:
            break
        result = post(targets_client, "дальше", state={"session": result["session_state"]})
    else:
        pytest.fail("Teacher pagination never ended")
    assert len(pages) > 1
    assert all(f"Предмет {n}:" in "".join(pages) for n in range(1, 10))
    assert result["session_state"]["last_date"] == str(TODAY + timedelta(days=1))


@pytest.mark.parametrize("command", ["расписание моей группы завтра", "что у моей группы завтра"])
def test_own_group_reference_uses_saved_group(targets_client, command):
    result = post(targets_client, command, state={"application": {"group_id": GROUP}})
    assert result["session_state"]["query_group_id"] == GROUP
    assert result["response"]["end_session"] is True


def test_teacher_alias_works_with_file_source(targets_client):
    targets_client.app.state.skill.college.teacher_aliases["Примерова Д. Е."] = ["Дарья Евгеньевна"]
    result = post(targets_client, "расписание преподавателя Дарья Евгеньевна")
    assert result["session_state"]["query_teacher_id"] == "Примерова Д. Е."
    assert result["response"]["end_session"] is True
