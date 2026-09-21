import json

import pytest

from app.dialog import Memory, answer, memory_from
from app.protocol import AliceRequest
from app.providers import ProviderError
from tests import test_schedule_targets
from tests.conftest import GROUP, TODAY, envelope
from tests.test_dialog import post

profile_client = test_schedule_targets.targets_client
OTHER = test_schedule_targets.OTHER

TEACHER = "Примерова Д. Е."


def carry(result, previous=None, *, user=False):
    key = "user" if user else "application"
    update = "user_state_update" if user else "application_state"
    saved = dict((previous or {}).get(key, {}))
    if update in result:
        saved = (saved | result[update]) if user else result[update]
        saved = {k: v for k, v in saved.items() if v is not None}
    return {key: saved, "session": result["session_state"]}


@pytest.mark.parametrize("user", [False, True])
def test_teacher_onboarding_restart_and_subject_preference(profile_client, user):
    welcome = post(profile_client, new=True, user=user)
    assert "группу или фамилию преподавателя" in welcome["response"]["text"]
    selected = post(profile_client, "Примерова", state=carry(welcome, user=user), user=user)
    assert "Запомнила: Примерова." in selected["response"]["text"]
    assert not selected["response"]["end_session"]
    state = carry(selected, user=user)
    key = "user" if user else "application"
    assert state[key] == {"teacher_id": TEACHER}
    tomorrow = post(profile_client, "что завтра", state=state, user=user, new=True)
    assert "3 пары." in tomorrow["response"]["text"]
    assert GROUP in tomorrow["response"]["text"] and OTHER in tomorrow["response"]["text"]
    assert tomorrow["response"]["end_session"]
    preference = post(profile_client, "называй предметы", state=state, user=user, new=True)
    subjects = post(
        profile_client, "что завтра", state=carry(preference, state, user=user), user=user, new=True
    )
    assert "Компьютерные сети" in subjects["response"]["text"]
    assert "Программирование" in subjects["response"]["text"]
    state = carry(preference, state, user=user)
    groups_only = post(profile_client, "называй группы", state=state, user=user, new=True)
    assert groups_only["response"]["text"] == "Буду называть группы."
    state = carry(groups_only, state, user=user)
    profile_client.app.state.skill.responses.options.teacher_lesson_label = "both"
    groups = post(profile_client, "что завтра", state=state, user=user, new=True)
    assert "Компьютерные сети" not in groups["response"]["text"]
    assert GROUP in groups["response"]["text"]


@pytest.mark.parametrize(
    "command,kind",
    [
        ("сколько пар завтра", "count"),
        ("какой предмет на второй паре завтра", "lesson"),
        ("ко скольки завтра", "start"),
    ],
)
@pytest.mark.parametrize("role_step", [False, True])
def test_question_survives_profile_choice_and_optional_role(
    profile_client, command, kind, role_step
):
    asked = post(profile_client, command, new=True)
    if role_step:
        asked = post(profile_client, "я преподаватель", state=carry(asked))
    selected = post(profile_client, "Примерова", state=carry(asked))
    assert selected["session_state"]["last_date"] == "2026-09-16"
    assert selected["session_state"]["last_kind"] == kind
    assert selected["application_state"]["teacher_id"] == TEACHER
    assert selected["response"]["end_session"]
    if kind == "lesson":
        assert "Программирование" in selected["response"]["text"]
        assert "Компьютерные сети" not in selected["response"]["text"]


@pytest.mark.parametrize(
    "command", ["Примерова завтра", "я преподаватель Примерова", "запомни фамилию Примерова"]
)
def test_teacher_selection_accepts_date_and_explicit_ownership(profile_client, command):
    welcome = post(profile_client, new=True)
    selected = post(profile_client, command, state=carry(welcome))
    assert selected["application_state"]["teacher_id"] == TEACHER
    assert not selected["response"]["end_session"]
    if "завтра" in command:
        assert selected["session_state"]["last_date"] == "2026-09-16"


@pytest.mark.parametrize("user", [False, True])
def test_switch_profile_clear_old_target_and_forget(profile_client, user):
    key = "user" if user else "application"
    state = {key: {"group_id": GROUP, "auto_exit": False, "lesson_label": "both"}}
    change = post(profile_client, "смени расписание", state=state, user=user, new=True)
    state = carry(change, state, user=user)
    teacher = post(profile_client, "Примерова", state=state, user=user)
    state = carry(teacher, state, user=user)
    assert state[key] == {"teacher_id": TEACHER, "auto_exit": False, "lesson_label": "both"}
    status = post(profile_client, "что у меня сохранено", state=state, user=user, new=True)
    assert status["response"]["text"] == "Сохранён преподаватель Примерова."
    change = post(profile_client, "смени расписание", state=state, user=user, new=True)
    group = post(profile_client, "104", state=carry(change, state, user=user), user=user)
    state = carry(group, state, user=user)
    assert state[key] == {"group_id": OTHER, "auto_exit": False, "lesson_label": "both"}
    teacher = post(profile_client, "я преподаватель Примерова", state=state, user=user, new=True)
    state = carry(teacher, state, user=user)
    forgotten = post(profile_client, "забудь моё расписание", state=state, user=user, new=True)
    state = carry(forgotten, state, user=user)
    assert state[key] == {"auto_exit": False, "lesson_label": "both"}
    restarted = post(profile_client, "что завтра", state=state, user=user, new=True)
    assert "группу или фамилию" in restarted["response"]["text"]


@pytest.mark.parametrize(
    "command", ["расписание у Тестова А. Б. завтра", "расписание группы 104 завтра"]
)
def test_temporary_target_and_return_to_personal_teacher(profile_client, command):
    state = {"application": {"teacher_id": TEACHER, "auto_exit": False}}
    other = post(profile_client, command, state=state, new=True)
    assert "application_state" not in other
    own = post(profile_client, "а у меня", state=carry(other, state))
    assert "Примерова." in own["response"]["text"]
    assert "3 пары." in own["response"]["text"]
    assert own["session_state"]["last_date"] == "2026-09-16"


@pytest.mark.parametrize(
    "command", ["расписание группы 104 завтра", "расписание у Примеровой завтра"]
)
def test_explicit_query_during_onboarding_is_not_saved(profile_client, command):
    welcome = post(profile_client, new=True)
    answer = post(profile_client, command, state=carry(welcome))
    assert "application_state" not in answer
    assert not answer["session_state"].get("group_id")
    assert not answer["session_state"].get("teacher_id")
    restarted = post(profile_client, "завтра", new=True)
    assert "группу или фамилию" in restarted["response"]["text"]


def test_surname_ambiguity_preserves_question_until_initials(profile_client):
    asked = post(profile_client, "сколько пар завтра", new=True)
    ambiguous = post(profile_client, "Тестов", state=carry(asked))
    assert "однофамильцы" in ambiguous["response"]["text"]
    assert "application_state" not in ambiguous
    assert not ambiguous["response"]["end_session"]
    selected = post(profile_client, "Тестов А. Б.", state=carry(ambiguous))
    assert selected["application_state"]["teacher_id"] == "Тестов А. Б."
    assert selected["session_state"]["last_date"] == "2026-09-16"
    assert selected["session_state"]["last_kind"] == "count"


@pytest.mark.parametrize("name", ["Несуществующий", "не Примерова", "Примерова и Несуществующий"])
def test_uncertain_selection_never_overwrites_saved_profile(profile_client, name):
    state = {"application": {"group_id": GROUP}}
    change = post(profile_client, "смени расписание", state=state, new=True)
    result = post(profile_client, name, state=carry(change, state))
    assert not result["response"]["end_session"]
    assert "application_state" not in result
    assert result["session_state"]["group_id"] == GROUP


def test_profile_selection_survives_help_settings_and_bad_date(profile_client):
    asked = post(profile_client, "какая первая пара на 31.02.2026", new=True)
    for command in ("помощь", "выходи после ответа", "называй предметы"):
        result = post(profile_client, command, state=carry(asked))
        assert not result["response"]["end_session"]
        asked = result
    selected = post(profile_client, "Примерова", state=carry(asked))
    assert not selected["response"]["end_session"]
    fixed = post(profile_client, "завтра", state=carry(selected))
    assert fixed["response"]["end_session"]
    assert fixed["session_state"]["last_kind"] == "first"
    assert "Компьютерные сети" in fixed["response"]["text"]


def test_teacher_profile_has_interactive_followups_and_plain_fallback(profile_client):
    state = {"application": {"teacher_id": TEACHER}}
    welcome = post(profile_client, state=state, new=True)
    assert "расписание колледжа" in welcome["response"]["text"]
    tomorrow = post(profile_client, "завтра", state=carry(welcome, state))
    subjects = post(profile_client, "а предметы", state=carry(tomorrow, state))
    assert "Программирование" in subjects["response"]["text"]
    assert not subjects["response"]["end_session"]
    unrelated = post(profile_client, "погода", state=state, new=True)
    assert unrelated["response"]["text"].startswith("Спросите расписание")
    assert unrelated["session_state"]["teacher_id"] == TEACHER


def test_catalog_failure_keeps_selection_open_and_saved_profile(profile_client, monkeypatch):
    state = {"application": {"group_id": GROUP}}
    change = post(profile_client, "смени расписание", state=state)

    async def unavailable():
        raise ProviderError("test outage")

    monkeypatch.setattr(profile_client.app.state.skill.provider, "teachers", unavailable)
    failed = post(profile_client, "Примерова", state=carry(change, state))
    assert not failed["response"]["end_session"]
    assert failed["session_state"]["group_id"] == GROUP
    assert "application_state" not in failed


def test_authorized_account_does_not_inherit_guest_teacher(profile_client):
    result = post(
        profile_client,
        "что завтра",
        state={"application": {"teacher_id": TEACHER}},
        user=True,
        new=True,
    )
    assert "группу или фамилию" in result["response"]["text"]


@pytest.mark.parametrize(
    "first,last", [("я преподаватель", "Примерова"), ("Тестов", "Тестов А. Б.")]
)
def test_selecting_teacher_without_a_question_asks_for_day(profile_client, first, last):
    welcome = post(profile_client, new=True)
    clarify = post(profile_client, first, state=carry(welcome))
    chosen = post(profile_client, last, state=carry(clarify))
    assert "На какой день расписание?" in chosen["response"]["text"]
    assert "last_date" not in chosen["session_state"]
    assert not chosen["response"]["end_session"]


def test_changed_catalog_requires_new_choice_without_losing_requested_day(profile_client):
    state = {"application": {"teacher_id": "Удалённый А. Б."}}
    question = post(profile_client, "сколько пар завтра", state=state, new=True)
    assert "Не нашла преподавателя" in question["response"]["text"]
    chosen = post(profile_client, "Примерова", state=carry(question, state))
    assert chosen["application_state"]["teacher_id"] == TEACHER
    assert chosen["session_state"]["last_date"] == "2026-09-16"
    assert chosen["session_state"]["last_kind"] == "count"


def test_teacher_date_correction_stays_on_personal_teacher(profile_client):
    state = {"application": {"teacher_id": TEACHER}}
    question = post(profile_client, "на 31 февраля", state=state, new=True)
    assert not question["response"]["end_session"]
    fixed = post(profile_client, "завтра", state=carry(question, state))
    assert fixed["session_state"]["last_date"] == "2026-09-16"
    assert "Примерова." in fixed["response"]["text"]


def test_cancelled_change_keeps_teacher_for_next_launch(profile_client):
    state = {"application": {"teacher_id": TEACHER}}
    change = post(profile_client, "смени расписание", state=state, new=True)
    cancel = post(profile_client, "хватит", state=carry(change, state))
    assert cancel["response"]["end_session"]
    assert "application_state" not in cancel
    again = post(profile_client, "что завтра", state=carry(cancel, state), new=True)
    assert "Примерова." in again["response"]["text"]


def test_long_catalog_names_fit_session_limit_and_restore_own_teacher():
    own, other = "А" * 200, "Б" * 200
    stored = {"application": {"teacher_id": own}}
    request = AliceRequest.model_validate(envelope("что завтра", state=stored))
    memory = Memory(
        teacher_id=own,
        query_teacher_id=other,
        view="teacher",
        last_date=TODAY,
        last_kind="schedule",
        last_label="both",
        pending_kind="lesson",
        pending_pair=2,
        awaiting_pair=True,
        cursor=300,
        digest="1" * 16,
    )
    response = answer(request, memory, "Скажите «дальше».")
    assert len(json.dumps(response["session_state"], ensure_ascii=False).encode()) <= 1024
    followup = AliceRequest.model_validate(envelope("а у меня", state=carry(response, stored)))
    restored = memory_from(followup)
    assert restored.teacher_id == own
    assert restored.query_teacher_id == other
