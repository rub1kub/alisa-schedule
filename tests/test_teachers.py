import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.dialog import teacher_schedule_text
from app.models import College, Teacher, TeacherLesson, TeacherScheduleResult, read_json
from app.providers import (
    FileProvider,
    KkepikProvider,
    NotPublished,
    Unavailable,
    WithOverrides,
    parse_api_teacher_day,
)
from app.teachers import teacher_matches, teacher_query
from tests.conftest import GROUP, TODAY

TEACHER = "Тестов А. Б."
OTHER = "104-Д9-3ИСП"


@pytest.fixture
def college():
    return College.model_validate(read_json(Path("data/college.json")))


def payload(lines=None):
    return {
        "teacher": TEACHER,
        "date": "15.09.2026",
        "schedule": lines
        if lines is not None
        else [
            f"▪️1 пара – Нет\n▪️2 пара – {GROUP} – Сети – ауд. 86-б",
            f"▪️2 пара – {OTHER} – Сети – ауд. 86-б",
            f"▪️4 пара – {GROUP} – Математика – ауд. 22",
        ],
    }


def test_teacher_api_preserves_groups_periods_and_discards_rooms(college, responses):
    result = parse_api_teacher_day(payload(), TEACHER, TODAY, college)
    assert [item.number for item in result.lessons] == [2, 2, 4]
    assert result.lessons[0].groups == [GROUP]
    assert result.lessons[0].subject == "Сети"
    assert result.lessons[0].start == "10:25"
    spoken = teacher_schedule_text(result, "schedule", responses)
    assert spoken.startswith("Тестов. 2 пары.")
    assert "86" not in spoken and "22" not in spoken and "А." not in spoken
    first = teacher_schedule_text(result, "first", responses)
    assert GROUP in first and OTHER in first and "4-я" not in first


def test_native_teacher_rows_without_subject_and_combined_groups(college):
    result = parse_api_teacher_day(
        payload(
            [
                f"▪️2 пара – {GROUP}, {OTHER} – 22",
                "\nСовмещенные пары:",
                f"▪️2 пара – {GROUP}, {OTHER} – 22",
                f"▪️3 пара – {GROUP} – 35",
            ]
        ),
        TEACHER,
        TODAY,
        college,
    )
    assert len(result.lessons) == 2
    assert result.lessons[0].groups == [GROUP, OTHER]
    assert result.lessons[0].subject == ""


@pytest.mark.parametrize(
    "change",
    [
        {"teacher": "wrong"},
        {"date": "16.09.2026"},
        {"schedule": [7]},
        {"schedule": "bad"},
        {"schedule": ["changed wire format"]},
        {"schedule": ["▪️1 пара – Математика – 22"]},
        {"schedule": [f"▪️1 пара – {GROUP} и неизвестная группа – 22"]},
    ],
)
def test_invalid_teacher_payload_is_never_a_day_off(college, change):
    with pytest.raises(Unavailable):
        parse_api_teacher_day(payload() | change, TEACHER, TODAY, college)


@pytest.mark.parametrize("phrase", ["расписание Тестова", "у Тестову", "Тестовым", "Тестове"])
def test_inflected_surname_matches_conservatively(phrase):
    teachers = [
        Teacher(id=TEACHER, name=TEACHER),
        Teacher(id="Примерова В. Г.", name="Примерова В. Г."),
    ]
    assert [t.id for t in teacher_matches(phrase, teachers)] == [TEACHER]


def test_namesakes_and_initials_and_configured_aliases():
    teachers = [
        Teacher(id=TEACHER, name=TEACHER, aliases=["Алексей Борисович Тестов"]),
        Teacher(id="Тестов В. Г.", name="Тестов В. Г."),
    ]
    assert len(teacher_matches("Тестова", teachers)) == 2
    for name in ("Тестов А. Б.", "у Тестова А. Б.", "Алексей Борисович Тестов"):
        assert [t.id for t in teacher_matches(name, teachers)] == [TEACHER]
    assert teacher_matches("Нетестова", teachers) == []


@pytest.mark.parametrize(
    "command",
    [
        "что у меня завтра",
        "кто ведёт первую пару",
        "называй преподавателей",
        "расписание на пятницу",
        "расписание у группы 103",
        "что у сто три завтра",
    ],
)
def test_group_requests_do_not_become_teacher_lookups(command):
    assert teacher_query(command) is None


def test_teacher_cache_coalesces_requests_passes_exact_date_and_honors_404(college):
    async def run():
        now, status, calls = [0.0], [200], []

        async def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/api/teachers":
                return httpx.Response(200, json={"teachers": [TEACHER]})
            assert dict(request.url.params) == {"teacher": TEACHER, "date": "15.09.2026"}
            await asyncio.sleep(0.001)
            return httpx.Response(status[0], json=payload())

        async with httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        ) as client:
            provider = KkepikProvider(client, Settings(cache_ttl=10), college, lambda: now[0])
            assert (await provider.teachers())[0].id == TEACHER
            now[0] = 1
            results = await asyncio.gather(
                *(provider.teacher_day(TEACHER, TODAY) for _ in range(12))
            )
            assert len(results) == 12
            assert calls.count("/api/schedule/teacher") == 1
            now[0], status[0] = 12, 404
            for _ in range(2):
                with pytest.raises(NotPublished):
                    await provider.teacher_day(TEACHER, TODAY)
            assert calls.count("/api/schedule/teacher") == 2
            await provider.close()

    asyncio.run(run())


def test_teacher_cache_is_stale_only_within_allowed_window(college):
    async def run():
        now, status = [0.0], [200]
        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(status[0], json=payload())),
        ) as client:
            provider = KkepikProvider(
                client, Settings(cache_ttl=10, stale_ttl=20), college, lambda: now[0]
            )
            assert not (await provider.teacher_day(TEACHER, TODAY)).stale
            now[0], status[0] = 11, 503
            assert (await provider.teacher_day(TEACHER, TODAY)).stale
            now[0] = 31
            with pytest.raises(Unavailable):
                await provider.teacher_day(TEACHER, TODAY)
            await provider.close()

    asyncio.run(run())


def test_group_replacement_removes_old_teacher_and_adds_new_without_fanout(tmp_path, fixture_data):
    async def run():
        for day in fixture_data["days"]:
            for lesson in day["lessons"]:
                lesson["teacher"] = TEACHER
        schedule, overrides = tmp_path / "schedule.json", tmp_path / "overrides.json"
        schedule.write_text(json.dumps(fixture_data))
        overrides.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "replacements": [
                        {
                            "group_id": GROUP,
                            "date": str(TODAY),
                            "lessons": [
                                {"number": 4, "subject": "Замена", "teacher": "Примерова В. Г."}
                            ],
                        }
                    ],
                }
            )
        )
        provider = WithOverrides(FileProvider(schedule), overrides)
        assert (await provider.teacher_day(TEACHER, TODAY)).lessons == []
        replacement = await provider.teacher_day("Примерова В. Г.", TODAY)
        assert [item.number for item in replacement.lessons] == [4]
        assert replacement.source == "override"
        assert "Примерова В. Г." in [item.id for item in await provider.teachers()]

    asyncio.run(run())


def test_combined_pair_override_preserves_unaffected_group(tmp_path):
    class Source:
        async def teacher_day(self, teacher_id, target):
            return TeacherScheduleResult(
                teacher_id=teacher_id,
                date=target,
                source="kkepik",
                lessons=[TeacherLesson(number=2, groups=[GROUP, OTHER])],
            )

    async def run():
        path = tmp_path / "overrides.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "replacements": [{"group_id": GROUP, "date": str(TODAY), "lessons": []}],
                }
            )
        )
        result = await WithOverrides(Source(), path).teacher_day(TEACHER, TODAY)
        assert result.lessons[0].groups == [OTHER]
        assert result.source == "override"

    asyncio.run(run())


def test_overrides_only_teacher_schedule_is_explicitly_partial(tmp_path, responses):
    class Missing:
        async def teacher_day(self, *args):
            raise NotPublished()

    async def run():
        path = tmp_path / "overrides.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "replacements": [
                        {
                            "group_id": GROUP,
                            "date": str(TODAY),
                            "lessons": [{"number": 2, "subject": "Сети", "teacher": TEACHER}],
                        }
                    ],
                }
            )
        )
        result = await WithOverrides(Missing(), path).teacher_day(TEACHER, TODAY)
        assert result.partial is True
        assert "Другие пары не проверены" in teacher_schedule_text(result, "count", responses)

    asyncio.run(run())


def test_wrong_initials_do_not_silently_match_a_surname():
    teachers = [Teacher(id=TEACHER, name=TEACHER)]
    assert teacher_matches("Тестова В. Г.", teachers) == []


@pytest.mark.parametrize(
    ("catalog", "utterance"),
    [
        ("Примеров А. Б.", "Примерова"),
        ("Примерова А. Б.", "Примеровой"),
        ("Тестовский А. Б.", "Тестовского"),
        ("Тестовская А. Б.", "Тестовской"),
        ("Тестар А. Б.", "Тестару"),
        ("Тестарь А. Б.", "Тестаря"),
    ],
)
def test_regular_surname_forms(catalog, utterance):
    assert teacher_matches(utterance, [Teacher(id=catalog, name=catalog)])
