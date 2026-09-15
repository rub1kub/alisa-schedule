from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.dialog import Memory, page, schedule_text
from app.main import create_app
from app.models import College, Group, Lesson, ScheduleResult, read_json
from app.responses import Responses
from tests.conftest import GROUP, TODAY, envelope


@pytest.fixture
def named_client(tmp_path, responses):
    class Source:
        async def groups(self):
            return [Group(id=GROUP, name=GROUP), Group(id="141-Д9-1КСК", name="141-Д9-1КСК")]

        async def day(self, group_id, target):
            return ScheduleResult(
                group_id=group_id,
                date=target,
                source="kkepik",
                lessons=[
                    Lesson(
                        number=2,
                        subject="Математика",
                        teacher="Тестова А. Б.",
                        room="999",
                        start="10:25",
                        end="11:45",
                    ),
                    Lesson(number=3, subject="История", teacher="Примеров В.Г.", room="888"),
                ],
            )

    response_file = tmp_path / "responses.json"
    response_file.write_text(responses.model_dump_json())
    overrides = tmp_path / "overrides.json"
    overrides.write_text('{"schema_version":1,"replacements":[]}')
    settings = Settings(responses_file=response_file, overrides_file=overrides)
    with TestClient(create_app(settings, provider=Source(), now=lambda: TODAY)) as client:
        yield client


def ask(client, command, state=None, **kwargs):
    response = client.post("/webhook", json=envelope(command, state=state, **kwargs))
    assert response.status_code == 200
    return response.json()


def test_first_course_gets_subjects_and_senior_gets_surnames(named_client):
    junior = ask(named_client, "что завтра", {"application": {"group_id": "141-Д9-1КСК"}})
    senior = ask(named_client, "что завтра", {"application": {"group_id": GROUP}})
    assert junior["response"]["text"] == "2 пары. 2-я — Математика. 3-я — История."
    assert senior["response"]["text"] == "2 пары. 2-я — Тестова. 3-я — Примеров."


def test_spoken_preference_survives_a_new_session_and_group_forget(named_client):
    chosen = ask(named_client, "называй предметы", {"user": {"group_id": GROUP}}, user=True)
    state = {"user": chosen["user_state_update"]}
    result = ask(named_client, "что завтра", state, user=True, new=True)
    assert "Математика" in result["response"]["text"]
    assert "Тестова" not in result["response"]["text"]
    forgotten = ask(named_client, "забудь группу", state, user=True)
    assert forgotten["user_state_update"] == {"group_id": None, "lesson_label": "subject"}


def test_guest_preference_and_account_isolation(named_client):
    chosen = ask(named_client, "называй предметы", {"application": {"group_id": GROUP}})
    state = {"application": chosen["application_state"]}
    assert "Математика" in ask(named_client, "завтра", state, new=True)["response"]["text"]
    state["user"] = {"group_id": GROUP}
    assert "Тестова" in ask(named_client, "завтра", state, user=True, new=True)["response"]["text"]


def test_explicit_question_overrides_mode_without_changing_preference(named_client):
    state = {"application": {"group_id": GROUP}}
    subjects = ask(named_client, "какие предметы завтра", state)
    assert "Математика" in subjects["response"]["text"]
    assert "application_state" not in subjects
    first = ask(named_client, "кто ведёт первую пару", state, new=True)
    assert first["response"]["text"] == "2-я пара — Тестова, в 10:25."
    teachers = ask(named_client, "что завтра", state, new=True)
    assert "Тестова" in teachers["response"]["text"]


def test_explicit_mode_kept_while_asking_for_group(named_client):
    initial = ask(named_client, "какие предметы завтра", new=True)
    response = ask(named_client, "103", {"session": initial["session_state"]})
    assert "Математика" in response["response"]["text"]
    assert "Тестова" not in response["response"]["text"]


def test_course_comes_from_final_code_component_and_can_be_overridden(responses):
    college = College.model_validate(read_json(Path("data/college.json")))
    for group_id, course in [(GROUP, 3), ("141-Д9-1КСК", 1), ("122-Д9-2КСК", 2), ("84-Д9-4КСК", 4)]:
        assert college.course(group_id) == course
    assert college.course("103") is None
    college.group_courses[GROUP] = 1
    assert responses.label_mode(college, Group(id=GROUP, name=GROUP), None) == "subject"


@pytest.mark.parametrize("teacher", ["Тестова А.Б.", "А. Б. Тестова", "ТестоваА.Б."])
def test_initials_removed_and_absent_teacher_falls_back_to_subject(teacher, responses):
    result = ScheduleResult(
        group_id=GROUP,
        date=TODAY,
        source="kkepik",
        lessons=[
            Lesson(number=1, subject="Математика", teacher=teacher, room="999"),
            Lesson(number=2, subject="История"),
        ],
    )
    value = schedule_text(result, Group(id=GROUP, name=GROUP), "schedule", responses, "teacher")
    assert value == "2 пары. 1-я — Тестова. 2-я — История."


def test_templates_and_detail_options_are_loaded_from_file(tmp_path, responses):
    responses.texts["welcome"] = "Сегодня или завтра?"
    responses.options.show_group = True
    responses.options.schedule_time = "range"
    responses.options.date_format = "full"
    path = tmp_path / "responses.json"
    path.write_text(responses.model_dump_json())
    with TestClient(
        create_app(Settings(provider="file", responses_file=path), now=lambda: TODAY)
    ) as client:
        state = {"application": {"group_id": GROUP}}
        assert ask(client, "", state)["response"]["text"] == "Сегодня или завтра?"
        schedule = ask(client, "сегодня", state)["response"]["text"]
        assert GROUP in schedule and "2026 года" in schedule and "до 11:45" in schedule


@pytest.mark.parametrize(
    "template",
    ["{label.__class__}", "{label[0]}", "{label!r}", "{label:>9999}", "{other}", "", "x" * 700],
)
def test_bad_templates_are_rejected_before_startup(template, responses):
    data = responses.model_dump()
    data["texts"]["lesson"] = template
    with pytest.raises(ValidationError):
        Responses.model_validate(data)


def test_configured_pagination_and_required_notices(responses):
    responses.options.page_chars = 300
    result = ScheduleResult(
        group_id=GROUP,
        date=date(2026, 9, 16),
        source="override",
        stale=True,
        demo=True,
        note="Занятия перенесены",
        lessons=[
            Lesson(number=n, subject="Очень длинное название учебного предмета " * 4)
            for n in range(1, 10)
        ],
    )
    full = schedule_text(result, Group(id=GROUP, name=GROUP), "schedule", responses)
    first = page(full, Memory(), False, responses)
    assert len(first) <= 300
    for key in ["demo", "stale", "replacement", "page_more"]:
        assert responses.text(key) in first


def test_invalid_persisted_preference_is_ignored(named_client):
    result = ask(
        named_client, "что завтра", {"application": {"group_id": GROUP, "lesson_label": {}}}
    )
    assert "Тестова" in result["response"]["text"]
