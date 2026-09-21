"""Conversational regressions found by three independent student personas."""

from datetime import date

import pytest

from app.language import parse_date, parse_pair
from app.models import Group, Lesson, ScheduleResult, Teacher, TeacherScheduleResult
from app.protocol import NLU
from tests.conftest import GROUP, TODAY
from tests.test_dialog import post
from tests.test_schedule_targets import OTHER
from tests.test_schedule_targets import targets_client as targets_client


def saved(*, stay=False, label="subject"):
    return {"application": {"group_id": GROUP, "auto_exit": not stay, "lesson_label": label}}


def follow(answer):
    return {"session": answer["session_state"]}


def unchanged(answer):
    assert "application_state" not in answer and "user_state_update" not in answer
    assert answer["session_state"]["group_id"] == GROUP


@pytest.mark.parametrize("command", ["у 104", "а у 104", "104 завтра", "пары 104 завтра"])
def test_short_other_group_is_never_saved(targets_client, command):
    result = post(targets_client, command, state=saved(), new=True)
    assert result["session_state"]["query_group_id"] == OTHER
    assert result["response"]["end_session"]
    unchanged(result)


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("первая", "first"),
        ("а первая", "first"),
        ("первая завтра", "first"),
        ("а сколько", "count"),
        ("сколько завтра", "count"),
        ("чё по парам", "schedule"),
        ("последняя пара", "last"),
        ("когда первая", "first"),
        ("с какой пары завтра", "first"),
        ("ко скольки завтра", "start"),
        ("до скольки завтра", "finish"),
    ],
)
def test_short_queries_keep_tomorrow(targets_client, command, kind):
    first = post(targets_client, "что завтра", state=saved(stay=True))
    result = post(targets_client, command, state=follow(first))
    assert result["session_state"]["last_kind"] == kind
    assert result["session_state"]["last_date"] == "2026-09-16"
    assert "группы нет" not in result["response"]["text"]
    unchanged(result)


@pytest.mark.parametrize("command", ["в пт", "че там в пт", "расписание пожалуйста на пятницу"])
def test_informal_weekday_is_not_a_teacher(targets_client, command):
    result = post(targets_client, command, state=saved())
    assert result["session_state"]["last_date"] == "2026-09-18"
    assert "Пар нет" in result["response"]["text"]


@pytest.mark.parametrize(
    ("initial", "question", "target_key", "target"),
    [
        ("что завтра", "а у 104", "query_group_id", OTHER),
        ("расписание группы 104 завтра", "а в пятницу", "query_group_id", OTHER),
        ("что завтра", "а у Примеровой", "query_teacher_id", "Примерова Д. Е."),
        ("расписание группы 104 завтра", "а у Примеровой", "query_teacher_id", "Примерова Д. Е."),
        ("расписание у Примеровой завтра", "а у Тестова А. Б.", "query_teacher_id", "Тестов А. Б."),
        ("расписание группы 104 завтра", "а моя", "query_group_id", GROUP),
        ("расписание группы 104 завтра", "а у меня", "query_group_id", GROUP),
        ("расписание у Примеровой завтра", "а у моей группы", "query_group_id", GROUP),
    ],
)
def test_followup_changes_only_the_requested_part(
    targets_client, initial, question, target_key, target
):
    first = post(targets_client, initial, state=saved(stay=True))
    result = post(targets_client, question, state=follow(first))
    assert result["session_state"][target_key] == target
    assert result["session_state"]["last_date"] == (
        "2026-09-18" if "пятниц" in question else "2026-09-16"
    )
    unchanged(result)


@pytest.mark.parametrize(
    "command",
    [
        "какой преподаватель на второй паре завтра",
        "назови фамилию преподавателя второй пары завтра",
    ],
)
def test_teacher_of_a_lesson_is_not_a_teacher_search(targets_client, command):
    result = post(targets_client, command, state=saved())
    assert "2-я — Примерова" in result["response"]["text"]
    assert result["response"]["end_session"]
    unchanged(result)


def test_lesson_question_preserves_an_explicit_teacher_name(targets_client):
    result = post(
        targets_client,
        "какой предмет на второй паре преподавателя Примеровой завтра",
        state=saved(),
    )
    assert result["session_state"]["query_teacher_id"] == "Примерова Д. Е."
    assert result["session_state"]["last_pair"] == 2
    assert "Программирование" in result["response"]["text"]
    unchanged(result)


@pytest.mark.parametrize("question", ["а преподаватель", "а кто"])
def test_teacher_followup_keeps_specific_pair(targets_client, question):
    first = post(targets_client, "какой предмет на второй паре завтра", state=saved(stay=True))
    result = post(targets_client, question, state=follow(first))
    assert result["response"]["text"] == "Это пример расписания. 2-я — Примерова."
    assert result["session_state"]["last_date"] == "2026-09-16"
    unchanged(result)


@pytest.mark.parametrize("command", ["преподаватели", "фамилии", "предметы и преподаватели"])
def test_bare_formats_do_not_change_preferences(targets_client, command):
    first = post(targets_client, "что завтра", state=saved(stay=True))
    result = post(targets_client, command, state=follow(first))
    assert "Примерова" in result["response"]["text"]
    assert "Буду" not in result["response"]["text"]
    assert result["session_state"]["lesson_label"] == "subject"
    unchanged(result)


@pytest.mark.parametrize(
    "command",
    [
        "расписание на завтра без фамилий",
        "не фамилии а предметы завтра",
        "не надо фамилий только предметы завтра",
    ],
)
def test_negative_teacher_preference_is_one_off(targets_client, command):
    result = post(targets_client, command, state=saved(label="teacher"))
    assert "Программирование" in result["response"]["text"]
    assert "Примерова" not in result["response"]["text"]
    unchanged(result)


def test_negative_subject_preference_is_one_off(targets_client):
    result = post(targets_client, "расписание без предметов завтра", state=saved())
    assert "Примерова" in result["response"]["text"]
    assert "Программирование" not in result["response"]["text"]
    unchanged(result)


@pytest.mark.parametrize(
    "command",
    [
        "расписание на пятницу 16 сентября",
        "расписание на завтра 17 сентября",
        "на 16.09.2026 и 17.09.2026",
        "на 16 и 17 сентября",
        "на 2026-09-16 и 2026-09-17",
        "расписание с понедельника по пятницу",
    ],
)
def test_multiple_dates_never_pick_one_silently(targets_client, command):
    result = post(targets_client, command, state=saved())
    assert result["response"]["text"] == "Назовите один день."
    assert not result["response"]["end_session"]


def test_consistent_date_clues_are_accepted():
    assert parse_date("в среду 16 сентября завтра", NLU(), TODAY).value == date(2026, 9, 16)


@pytest.mark.parametrize(
    "command",
    [
        "первые две пары завтра",
        "какой предмет на второй или третьей паре завтра",
        "что на третьей и четвертой парах завтра",
    ],
)
def test_multiple_pairs_ask_for_one_and_keep_date(targets_client, command):
    result = post(targets_client, command, state=saved())
    assert result["response"]["text"] == "Назовите одну пару."
    selected = post(targets_client, "вторая", state=follow(result))
    assert "2-я — Программирование" in selected["response"]["text"]
    unchanged(selected)


@pytest.mark.parametrize("command", ["завтра две пары", "сколько пар завтра две или три пары"])
def test_cardinal_count_is_not_a_pair_number(targets_client, command):
    assert parse_pair(command).value is None
    result = post(targets_client, command, state=saved())
    assert "2 пары" in result["response"]["text"]
    assert result["session_state"]["last_kind"] in {"schedule", "count"}


def test_invalid_pair_does_not_lose_saved_group(targets_client):
    asked = post(targets_client, "какой предмет на 13 паре завтра", state=saved())
    assert "номер пары от 1 до 12" in asked["response"]["text"]
    result = post(targets_client, "вторая", state=follow(asked))
    assert "2-я — Программирование" in result["response"]["text"]


@pytest.mark.parametrize(
    "command",
    [
        "расписание групп 103 и 999 завтра",
        "расписание Примеровой и Несуществующего завтра",
        "расписание моей группы и преподавателя Примеровой завтра",
    ],
)
def test_unknown_second_target_is_not_silently_ignored(targets_client, command):
    result = post(targets_client, command, state=saved())
    assert not result["response"]["end_session"]
    assert any(
        word in result["response"]["text"] for word in ("одной группе", "одного преподавателя")
    )
    unchanged(result)


@pytest.mark.parametrize("target", ["104", "Примерова", "а моя"])
def test_mixed_target_clarification_keeps_one_off_request(targets_client, target):
    asked = post(
        targets_client, "расписание моей группы и преподавателя Примеровой завтра", state=saved()
    )
    result = post(targets_client, target, state=follow(asked))
    unchanged(result)
    assert result["session_state"]["last_date"] == "2026-09-16"
    assert result["response"]["end_session"]
    if target == "104":
        assert result["session_state"]["query_group_id"] == OTHER
    elif target == "Примерова":
        assert result["session_state"]["query_teacher_id"] == "Примерова Д. Е."
    else:
        assert result["session_state"]["query_group_id"] == GROUP


@pytest.mark.parametrize("initials", ["И. Б.", "И Б", "И. И.", "И И"])
def test_initial_i_is_not_a_second_teacher(targets_client, monkeypatch, initials):
    teacher = Teacher(
        id="Примеров И. " + ("И." if initials.count("И") == 2 else "Б."),
        name="Примеров " + initials,
    )

    async def teachers():
        return [teacher]

    async def day(teacher_id, target):
        return TeacherScheduleResult(
            teacher_id=teacher_id, date=target, lessons=[], source="kkepik"
        )

    monkeypatch.setattr(targets_client.app.state.skill.provider, "teachers", teachers)
    monkeypatch.setattr(targets_client.app.state.skill.provider, "teacher_day", day)
    result = post(targets_client, "расписание Примеров " + initials + " завтра", state=saved())
    assert "Пар нет" in result["response"]["text"]
    assert result["response"]["end_session"]


def test_last_pair_followup_is_not_the_whole_day(targets_client):
    asked = post(targets_client, "что завтра", state=saved(stay=True))
    result = post(targets_client, "какой предмет на последней паре", state=follow(asked))
    assert result["session_state"]["last_kind"] == "last"
    assert "Программирование" in result["response"]["text"]
    assert "Компьютерные сети" not in result["response"]["text"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ко скольки завтра", "К 08:45, 1-я пара."),
        ("до скольки завтра", "В 11:45, после 2-й пары."),
        ("во сколько вторая пара завтра", "К 10:25, 2-я пара."),
    ],
)
def test_time_queries_are_brief(targets_client, command, expected):
    result = post(targets_client, command, state=saved())
    assert result["response"]["text"] == "Это пример расписания. " + expected
    assert result["response"]["end_session"]
    unchanged(result)


def test_missing_time_is_not_guessed(targets_client):
    result = post(targets_client, "ко скольки у 104 завтра", state=saved())
    assert "Время не указано" in result["response"]["text"]


def test_date_number_cannot_select_group_during_onboarding(targets_client, monkeypatch):
    original = targets_client.app.state.skill.provider.groups

    async def groups():
        return [*await original(), Group(id="16-Д9-1ТСТ", name="16-Д9-1ТСТ")]

    monkeypatch.setattr(targets_client.app.state.skill.provider, "groups", groups)
    asked = post(targets_client, "расписание на 16 сентября", new=True)
    assert "группу или фамилию" in asked["response"]["text"]
    assert "application_state" not in asked
    result = post(targets_client, "103", state=follow(asked))
    assert result["session_state"]["last_date"] == "2026-09-16"


def test_invalid_date_survives_initial_group_selection(targets_client):
    asked = post(targets_client, "сколько пар на 31.02.2026", new=True)
    selected = post(targets_client, "103", state=follow(asked))
    assert selected["response"]["text"] == "На какой день?"
    result = post(targets_client, "завтра", state=follow(selected))
    assert result["response"]["text"] == "Это пример расписания. 2 пары."
    assert result["session_state"]["last_date"] == "2026-09-16"


def test_unknown_teacher_does_not_discard_invalid_date(targets_client):
    asked = post(targets_client, "сколько пар у Несуществующего на 31.02.2026", state=saved())
    selected = post(targets_client, "Примерова", state=follow(asked))
    assert selected["response"]["text"] == "На какой день?"
    result = post(targets_client, "завтра", state=follow(selected))
    assert result["session_state"]["last_kind"] == "count"
    assert result["session_state"]["last_date"] == "2026-09-16"
    unchanged(result)


def test_time_respects_all_subgroups(targets_client, monkeypatch):
    async def day(group_id, target):
        return ScheduleResult(
            group_id=group_id,
            date=target,
            source="kkepik",
            lessons=[
                Lesson(number=2, subject="Английский", subgroup="1", start="10:25", end="11:45"),
                Lesson(number=2, subject="Немецкий", subgroup="2", start="10:30", end="12:00"),
            ],
        )

    monkeypatch.setattr(targets_client.app.state.skill.provider, "day", day)
    assert (
        post(targets_client, "ко скольки", state=saved())["response"]["text"]
        == "К 10:25, 2-я пара."
    )
    assert (
        post(targets_client, "до скольки", state=saved())["response"]["text"]
        == "В 12:00, после 2-й пары."
    )
