import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.language import detect_intent, parse_date, parse_pair
from app.main import create_app
from app.protocol import NLU
from tests.conftest import GROUP, TODAY
from tests.test_dialog import post

OTHER = "104-Д9-3ИСП"


@pytest.fixture
def subjects_client(tmp_path, fixture_data):
    for day in fixture_data["days"]:
        for lesson in day["lessons"]:
            lesson["teacher"] = "Примерова Д. Е."
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(fixture_data))
    overrides = tmp_path / "overrides.json"
    overrides.write_text('{"schema_version":1,"replacements":[]}')
    settings = Settings(provider="file", schedule_file=path, overrides_file=overrides)
    with TestClient(create_app(settings, now=lambda: TODAY)) as client:
        yield client


def saved(*, stay=False):
    return {"application": {"group_id": GROUP, "lesson_label": "teacher", "auto_exit": not stay}}


def follow_state(result):
    return {"session": result["session_state"]}


@pytest.mark.parametrize(
    ("command", "number"),
    [
        ("какой предмет на второй паре", 2),
        ("что на 2-й паре", 2),
        ("какая третья пара", 3),
        ("а на второй", 2),
        ("что завтра на второй", 2),
        ("кто на 4 паре завтра", 4),
        ("какой предмет на первой паре", 1),
        ("что на двенадцатой паре завтра", 12),
    ],
)
def test_pair_number_is_not_a_date(command, number):
    assert parse_pair(command).value == number
    assert not parse_date(command, NLU(), TODAY).error


@pytest.mark.parametrize(
    "command",
    ["расписание на 16 сентября", "группа сто третья", "какая первая пара", "сколько пар завтра"],
)
def test_dates_groups_and_first_occupied_pair_keep_their_meaning(command):
    assert parse_pair(command).value is None


@pytest.mark.parametrize(
    "command", ["предметы", "а предметы", "а какие предметы", "какие предметы завтра"]
)
def test_subjects_are_one_off_and_same_teacher_can_have_different_subjects(
    subjects_client, command
):
    state = saved()
    answer = post(subjects_client, command, state=state, new=True)
    text = answer["response"]["text"]
    assert "Примерова" not in text and "Буду" not in text
    assert "application_state" not in answer and "user_state_update" not in answer
    assert answer["response"]["end_session"] is True
    ordinary = post(subjects_client, "что завтра", state=state, new=True)
    assert "Примерова" in ordinary["response"]["text"]
    assert "Программирование" not in ordinary["response"]["text"]


def test_same_session_subject_clarification_keeps_tomorrow(subjects_client):
    tomorrow = post(subjects_client, "что завтра", state=saved(stay=True))
    answer = post(subjects_client, "а какие предметы", state=follow_state(tomorrow))
    assert answer["session_state"]["last_date"] == "2026-09-16"
    assert "Компьютерные сети" in answer["response"]["text"]
    assert "Программирование" in answer["response"]["text"]
    assert "Математика" not in answer["response"]["text"]


@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("command", ["предметы", "какие предметы завтра"])
@pytest.mark.parametrize("exit_command", ["хватит", "спасибо"])
def test_plain_launch_allows_subject_and_pair_followups_until_exit(
    subjects_client, user, command, exit_command
):
    state_key = "user" if user else "application"
    state = {state_key: {"group_id": GROUP, "lesson_label": "teacher"}}
    welcome = post(subjects_client, new=True, user=user, state=state)
    state["session"] = welcome["session_state"]
    tomorrow = post(subjects_client, "завтра", user=user, state=state)
    assert "Примерова" in tomorrow["response"]["text"]
    assert "Компьютерные сети" not in tomorrow["response"]["text"]
    assert tomorrow["response"]["end_session"] is False
    state["session"] = tomorrow["session_state"]
    subjects = post(subjects_client, command, user=user, state=state)
    assert "Компьютерные сети" in subjects["response"]["text"]
    assert "Программирование" in subjects["response"]["text"]
    assert subjects["session_state"]["last_date"] == "2026-09-16"
    assert subjects["response"]["end_session"] is False
    assert "user_state_update" not in subjects and "application_state" not in subjects
    state["session"] = subjects["session_state"]
    pair = post(subjects_client, "а на второй", user=user, state=state)
    assert pair["response"]["text"] == "Это пример расписания. 2-я — Программирование."
    assert pair["response"]["end_session"] is False
    state["session"] = pair["session_state"]
    end = post(subjects_client, exit_command, user=user, state=state)
    assert end["response"]["end_session"] is True


def test_thanks_with_a_subject_question_is_not_an_exit():
    assert detect_intent("спасибо а какие предметы завтра", NLU()) == "schedule"


def test_specific_pair_selects_subject_without_changing_preference(subjects_client):
    answer = post(subjects_client, "какой предмет на второй паре завтра", state=saved())
    assert answer["response"]["text"] == "Это пример расписания. 2-я — Программирование."
    assert answer["response"]["end_session"] is True
    assert "application_state" not in answer


def test_first_actual_pair_and_exact_slot_one_are_different(subjects_client):
    first = post(subjects_client, "какой предмет первая пара", state=saved())
    assert "2-я пара — Математика" in first["response"]["text"]
    slot = post(subjects_client, "какой предмет на первой паре", state=saved())
    assert "1-й пары нет" in slot["response"]["text"]
    assert slot["response"]["end_session"] is True


def test_pair_number_and_subject_mode_survive_initial_group_question(subjects_client):
    asked = post(subjects_client, "что на второй паре завтра", new=True)
    assert "группу или фамилию" in asked["response"]["text"]
    answer = post(subjects_client, "103", state=follow_state(asked))
    assert "2-я — Программирование" in answer["response"]["text"]
    assert "Компьютерные сети" not in answer["response"]["text"]
    assert answer["application_state"]["group_id"] == GROUP


def test_subject_followup_preserves_temporary_group(subjects_client):
    other = post(subjects_client, "расписание группы 104 завтра", state=saved(stay=True))
    answer = post(subjects_client, "а предметы", state=follow_state(other))
    assert answer["session_state"]["group_id"] == GROUP
    assert answer["session_state"]["query_group_id"] == OTHER
    assert "Разработка приложений" in answer["response"]["text"]
    assert "application_state" not in answer


def test_short_pair_followup_keeps_selected_day(subjects_client):
    tomorrow = post(subjects_client, "что завтра", state=saved(stay=True))
    answer = post(subjects_client, "а на второй", state=follow_state(tomorrow))
    assert answer["response"]["text"] == "Это пример расписания. 2-я — Программирование."


def test_subject_clarification_of_first_pair_does_not_expand_to_whole_day(subjects_client):
    first = post(subjects_client, "кто ведёт первую пару завтра", state=saved(stay=True))
    answer = post(subjects_client, "а какой предмет", state=follow_state(first))
    assert "1-я пара — Компьютерные сети" in answer["response"]["text"]
    assert "Программирование" not in answer["response"]["text"]


def test_subject_clarification_of_numbered_pair_keeps_slot(subjects_client):
    pair = post(subjects_client, "кто на второй паре завтра", state=saved(stay=True))
    assert "Примерова" in pair["response"]["text"]
    answer = post(subjects_client, "а предмет", state=follow_state(pair))
    assert answer["response"]["text"] == "Это пример расписания. 2-я — Программирование."


def test_invalid_date_clarification_keeps_pair_and_subject(subjects_client):
    asked = post(subjects_client, "какой предмет на второй паре на 31.02.2026", state=saved())
    assert "Такой даты нет" in asked["response"]["text"]
    answer = post(subjects_client, "завтра", state=follow_state(asked))
    assert "2-я — Программирование" in answer["response"]["text"]
    assert "1-я" not in answer["response"]["text"]


@pytest.mark.parametrize("query", ["на нулевой паре", "на 13 паре", "на 2 и 3 паре"])
def test_bad_pair_clarification_preserves_other_group_and_day(subjects_client, query):
    asked = post(subjects_client, "какой предмет " + query + " у группы 104 завтра", state=saved())
    assert asked["response"]["end_session"] is False
    assert "Назовите" in asked["response"]["text"]
    answer = post(subjects_client, "третья", state=follow_state(asked))
    assert answer["session_state"]["query_group_id"] == OTHER
    assert "3-я — Разработка приложений" in answer["response"]["text"]
    assert "application_state" not in answer


def test_teacher_subject_request_overrides_group_only_output(subjects_client):
    answer = post(subjects_client, "какие предметы у Примеровой завтра", new=True)
    assert "Компьютерные сети" in answer["response"]["text"]
    assert "Программирование" in answer["response"]["text"]
    assert "Разработка приложений" in answer["response"]["text"]
    assert "Д." not in answer["response"]["text"] and "22" not in answer["response"]["text"]
    assert answer["response"]["end_session"] is True


def test_teacher_subject_followup_and_pair_selection_keep_context(subjects_client):
    teacher = post(subjects_client, "расписание у Примеровой завтра", state=saved(stay=True))
    subjects = post(subjects_client, "а предметы", state=follow_state(teacher))
    assert subjects["session_state"]["view"] == "teacher"
    assert "Компьютерные сети" in subjects["response"]["text"]
    pair = post(subjects_client, "какой предмет на второй паре", state=follow_state(subjects))
    assert "Программирование" in pair["response"]["text"]
    assert "Компьютерные сети" not in pair["response"]["text"]
    assert pair["session_state"]["last_date"] == "2026-09-16"


def test_explicit_setting_command_still_changes_preference(subjects_client):
    assert detect_intent("называй предметы", NLU()) == "prefer_subject"
    answer = post(subjects_client, "называй предметы", state=saved())
    assert answer["application_state"]["lesson_label"] == "subject"


def test_numbered_pair_keeps_both_subgroups(subjects_client, monkeypatch):
    from app.models import Lesson, ScheduleResult

    async def day(group_id, target):
        return ScheduleResult(
            group_id=group_id,
            date=target,
            source="kkepik",
            lessons=[
                Lesson(number=2, subject="Английский", subgroup="первая подгруппа", room="999"),
                Lesson(number=2, subject="Немецкий", subgroup="вторая подгруппа", room="888"),
                Lesson(number=3, subject="История"),
            ],
        )

    monkeypatch.setattr(subjects_client.app.state.skill.provider, "day", day)
    answer = post(subjects_client, "какой предмет на второй паре", state=saved())
    text = answer["response"]["text"]
    assert "Английский" in text and "Немецкий" in text
    assert "История" not in text and "999" not in text and "888" not in text


def test_teacher_subject_missing_is_explicit(subjects_client, monkeypatch):
    from app.models import TeacherLesson, TeacherScheduleResult

    async def day(teacher_id, target):
        return TeacherScheduleResult(
            teacher_id=teacher_id,
            date=target,
            source="kkepik",
            lessons=[TeacherLesson(number=2, groups=[GROUP])],
        )

    monkeypatch.setattr(subjects_client.app.state.skill.provider, "teacher_day", day)
    answer = post(subjects_client, "какой предмет на второй паре у Примеровой завтра")
    assert "предмет не указан" in answer["response"]["text"]
    assert answer["response"]["end_session"] is True


def test_teacher_one_off_subjects_survive_pagination(subjects_client, monkeypatch):
    from app.models import TeacherLesson, TeacherScheduleResult

    async def day(teacher_id, target):
        return TeacherScheduleResult(
            teacher_id=teacher_id,
            date=target,
            source="kkepik",
            lessons=[
                TeacherLesson(number=n, groups=[GROUP], subject=f"Предмет {n}: " + "название " * 15)
                for n in range(1, 9)
            ],
        )

    skill = subjects_client.app.state.skill
    skill.responses.options.page_chars = 300
    monkeypatch.setattr(skill.provider, "teacher_day", day)
    answer = post(subjects_client, "какие предметы у Примеровой завтра")
    pages = []
    for _ in range(15):
        pages.append(answer["response"]["text"].removesuffix(" Скажите «дальше»."))
        if answer["response"]["end_session"]:
            break
        answer = post(subjects_client, "дальше", state=follow_state(answer))
    else:
        pytest.fail("Pagination did not finish")
    assert len(pages) > 1
    assert all(f"Предмет {n}:" in "".join(pages) for n in range(1, 9))


@pytest.mark.parametrize(
    "command",
    [
        "какой предмет на второй паре в субботу",
        "какой предмет на второй паре у Примеровой в субботу",
    ],
)
def test_missing_publication_is_not_an_empty_pair(subjects_client, command):
    answer = post(subjects_client, command, state=saved())
    assert "нет опубликованного расписания" in answer["response"]["text"]
    assert "2-й пары нет" not in answer["response"]["text"]


def test_closed_session_does_not_silently_reuse_yesterdays_question(subjects_client):
    first = post(subjects_client, "какой предмет на второй паре завтра", state=saved(), new=True)
    assert first["response"]["end_session"] is True
    state = {**saved(), **follow_state(first)}
    next_launch = post(subjects_client, "какой предмет на второй паре", state=state, new=True)
    assert "Математика" in next_launch["response"]["text"]
    assert "Программирование" not in next_launch["response"]["text"]
