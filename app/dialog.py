import hashlib
import json
import re
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.language import (
    date_label,
    detect_intent,
    group_matches,
    normalize,
    number_words,
    pair_count,
    parse_date,
    requested_label,
)
from app.models import College, Group, ScheduleResult, TeacherScheduleResult
from app.protocol import AliceRequest
from app.providers import NotPublished, ProviderError, ScheduleProvider, plain
from app.responses import LessonLabel, Responses, lesson_label
from app.teachers import surname, teacher_matches, teacher_query

Kind = Literal["schedule", "first", "count"]


class Memory(BaseModel):
    model_config = ConfigDict(extra="ignore")
    group_id: str | None = Field(default=None, max_length=200)
    awaiting_group: bool = False
    temporary_group: bool = False
    query_group_id: str | None = Field(default=None, max_length=200)
    query_teacher_id: str | None = Field(default=None, max_length=200)
    awaiting_teacher: bool = False
    awaiting_date: bool = False
    auto_exit: bool | None = None
    pending_kind: Kind | None = None
    pending_date: date | None = None
    last_kind: Kind | None = None
    last_date: date | None = None
    view: Literal["schedule", "groups", "teacher"] | None = None
    cursor: int = Field(default=0, ge=0, le=50000)
    digest: str = Field(default="", max_length=16)
    announce_group: bool = False
    lesson_label: LessonLabel | None = None
    pending_label: LessonLabel | None = None
    last_label: LessonLabel | None = None


def memory_from(request: AliceRequest) -> Memory:
    try:
        memory = Memory.model_validate({} if request.session.new else request.state.session)
    except ValidationError:
        memory = Memory()
    state = request.state.user if request.session.authorized else request.state.application
    if not memory.group_id:
        value = state.get("group_id")
        if isinstance(value, str) and 0 < len(value) <= 200:
            memory.group_id = value
    preference = state.get("lesson_label")
    if memory.auto_exit is None and type(state.get("auto_exit")) is bool:
        memory.auto_exit = state["auto_exit"]
    if (
        memory.lesson_label is None
        and isinstance(preference, str)
        and preference in {"auto", "subject", "teacher", "both"}
    ):
        memory.lesson_label = preference
    return memory


def button(title: str, **payload):
    return {"title": title[:64], "payload": payload, "hide": True}


def answer(
    request: AliceRequest,
    memory: Memory,
    text: str,
    *,
    buttons: list[dict] | None = None,
    persist: bool = False,
    end: bool = False,
):
    state = memory.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    if len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > 1024:
        raise ValueError("Session state exceeds protocol limit")
    if len(text) > 1024:
        raise ValueError("Response exceeds protocol limit")
    result = {
        "version": "1.0",
        "response": {"text": text, "end_session": end},
        "session_state": state,
    }
    if not end:
        result["response"]["buttons"] = buttons or []
    if persist:
        preferences = {"lesson_label": memory.lesson_label} if memory.lesson_label else {}
        if memory.auto_exit is not None:
            preferences["auto_exit"] = memory.auto_exit
        if request.session.authorized:
            result["user_state_update"] = {"group_id": memory.group_id, **preferences}
        else:
            result["application_state"] = {
                **({"group_id": memory.group_id} if memory.group_id else {}),
                **preferences,
            }
    return result


def page(text: str, memory: Memory, more: bool, responses: Responses) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    changed = more and memory.digest != digest
    start = memory.cursor if more and not changed else 0
    if start >= len(text):
        start = 0
    prefix = responses.text("page_changed") + " " if changed else ""
    suffix = " " + responses.text("page_more")
    limit = responses.options.page_chars - len(prefix)
    if len(text) - start <= limit:
        memory.cursor = 0
        memory.digest = digest
        return prefix + text[start:]
    end = start + limit - len(suffix)
    boundary = text.rfind(" ", start, end)
    if boundary > start:
        end = boundary
    memory.cursor = end
    memory.digest = digest
    return prefix + text[start:end] + suffix


def schedule_text(
    result: ScheduleResult, group: Group, kind: Kind, responses: Responses, label_mode="subject"
) -> str:
    text = responses.text
    options = responses.options
    lines = [text("demo")] if result.demo else []
    if result.stale:
        lines.append(text("stale"))
    if options.show_group:
        lines.append(text("group_header", group=plain(group.name)))
    if result.source == "override":
        lines.append(text("replacement"))
    if result.note:
        lines.append(plain(result.note).rstrip(".!?") + ".")
    if options.date_format != "none":
        lines.append(
            text("date_header", date=date_label(result.date, full=options.date_format == "full"))
        )
    lessons = sorted(result.lessons, key=lambda item: (item.number, item.subgroup))
    if not lessons:
        return " ".join([*lines, text("day_empty")])
    count = len({lesson.number for lesson in lessons})
    if kind == "count":
        return " ".join([*lines, text("day_count", count=pair_count(count))])
    if kind == "first":
        lessons = [lesson for lesson in lessons if lesson.number == lessons[0].number]
    else:
        lines.append(text("day_count", count=pair_count(count)))
    time_mode = options.first_time if kind == "first" else options.schedule_time
    for lesson in lessons:
        parts = [
            text(
                "first_lesson" if kind == "first" else "lesson",
                number=lesson.number,
                label=lesson_label(lesson, label_mode),
            )
        ]
        if lesson.subgroup:
            parts.append(plain(lesson.subgroup))
        if time_mode == "range" and lesson.start and lesson.end:
            parts.append(text("time_range", start=lesson.start, end=lesson.end))
        elif time_mode == "start" and lesson.start:
            parts.append(text("time_start", start=lesson.start))
        lines.append(", ".join(parts) + ".")
    if any(lesson.subgroup.startswith("вариант ") for lesson in lessons):
        lines.append(text("unknown_subgroups"))
    return " ".join(lines)


def teacher_schedule_text(result: TeacherScheduleResult, kind: Kind, responses: Responses) -> str:
    text, options = responses.text, responses.options
    lines = [text("teacher_header", teacher=plain(surname(result.teacher_id)))]
    for enabled, key in (
        (result.demo, "demo"),
        (result.stale, "stale"),
        (result.source == "override", "replacement"),
        (result.partial, "teacher_partial"),
    ):
        if enabled:
            lines.append(text(key))
    if options.date_format != "none":
        lines.append(
            text("date_header", date=date_label(result.date, full=options.date_format == "full"))
        )
    lessons = sorted(result.lessons, key=lambda item: (item.number, item.groups, item.subgroup))
    if not lessons:
        return " ".join([*lines, text("teacher_partial_empty" if result.partial else "day_empty")])
    if kind != "first":
        lines.append(text("day_count", count=pair_count(len({item.number for item in lessons}))))
    if kind == "count":
        return " ".join(lines)
    if kind == "first":
        lessons = [item for item in lessons if item.number == lessons[0].number]
    time_mode = options.first_time if kind == "first" else options.schedule_time
    # One period with several groups counts once, but every group remains audible.
    for lesson in lessons:
        label = ", ".join(plain(group) for group in lesson.groups)
        if options.teacher_lesson_label == "both" and lesson.subject:
            label += ", " + plain(lesson.subject)
        parts = [text("teacher_lesson", number=lesson.number, label=label)]
        if lesson.subgroup:
            parts.append(plain(lesson.subgroup))
        if time_mode == "range" and lesson.start and lesson.end:
            parts.append(text("time_range", start=lesson.start, end=lesson.end))
        elif time_mode == "start" and lesson.start:
            parts.append(text("time_start", start=lesson.start))
        lines.append(", ".join(parts) + ".")
    return " ".join(lines)


class Skill:
    def __init__(
        self, provider: ScheduleProvider, college: College, responses: Responses, now=None
    ):
        self.provider = provider
        self.college = college
        self.responses = responses
        self.text = responses.text
        self.now = now or (lambda: datetime.now(ZoneInfo(college.timezone)).date())

    def reply(self, request, memory, text, **kwargs):
        if kwargs.get("buttons") is None:
            kwargs["buttons"] = [
                button(self.text("button_today"), action="today"),
                button(self.text("button_tomorrow"), action="tomorrow"),
                button(self.text("button_change"), action="change"),
            ]
        return answer(request, memory, text, **kwargs)

    def failure(self, request: AliceRequest):
        memory = memory_from(request)
        return self.reply(
            request,
            memory,
            self.text("failure"),
            end=self.auto_exit(memory),
        )

    def auto_exit(self, memory: Memory) -> bool:
        return self.responses.options.auto_exit if memory.auto_exit is None else memory.auto_exit

    def ask_group(self, request, memory, groups, prompt="ask_group", persist=False):
        examples = groups[:3]
        text = (
            self.text(prompt, groups=", ".join(plain(g.name) for g in examples))
            if prompt == "ambiguous_group"
            else self.text(prompt)
        )
        memory.awaiting_group = True
        return self.reply(
            request,
            memory,
            text,
            persist=persist,
            buttons=[
                *[button(g.name, action="select_group", group_id=g.id) for g in examples],
                button(self.text("button_groups"), action="groups"),
            ],
        )

    async def handle_teacher(self, request, memory, command, intent, selection):
        kind = intent if intent in {"schedule", "first", "count"} else None
        more = intent == "more" and memory.view == "teacher"
        selected_id = request.request.payload.get("teacher_id")
        try:
            teachers = await self.provider.teachers()
        except ProviderError:
            return self.failure(request)
        teachers = [
            item.model_copy(
                update={"aliases": [*item.aliases, *self.college.teacher_aliases.get(item.id, [])]}
            )
            for item in teachers
        ]
        fragment = teacher_query(command)
        lookup = fragment is not None or memory.awaiting_teacher or selected_id is not None
        if lookup:
            matches = teacher_matches(command, teachers)
            if selected_id is not None:
                matches = [item for item in teachers if item.id == selected_id]
            if len(matches) != 1:
                memory.awaiting_teacher = True
                memory.awaiting_group = False
                memory.query_teacher_id = None
                memory.query_group_id = None
                memory.pending_kind = kind or memory.pending_kind or "schedule"
                memory.pending_date = selection.value or memory.pending_date
                memory.cursor = 0
                memory.view = "teacher"
                prompt = "ambiguous_teacher" if matches else "unknown_teacher"
                if len({surname(item.name) for item in matches}) > 1:
                    prompt = "multiple_teachers"
                if fragment == "":
                    prompt = "ask_teacher"
                return self.reply(request, memory, self.text(prompt), buttons=[])
            teacher = matches[0]
        else:
            teacher = next((item for item in teachers if item.id == memory.query_teacher_id), None)
            if teacher is None:
                memory.awaiting_teacher = True
                return self.reply(request, memory, self.text("ask_teacher"), buttons=[])
        memory.query_teacher_id = teacher.id
        memory.query_group_id = None
        memory.awaiting_teacher = False
        memory.awaiting_group = False
        memory.temporary_group = False
        kind = kind or memory.pending_kind or (memory.last_kind if more else "schedule")
        memory.view = "teacher"
        if selection.error or (memory.awaiting_date and not selection.value):
            memory.awaiting_date = True
            memory.pending_kind = kind
            return self.reply(request, memory, self.text(selection.error or "date_required"))
        target = selection.value or memory.pending_date
        if target is None and (more or (kind in {"first", "count"} and not lookup)):
            target = memory.last_date
        target = target or self.now()
        memory.awaiting_date = False
        memory.pending_kind = None
        memory.pending_date = None
        memory.pending_label = None
        memory.last_label = None
        memory.last_date, memory.last_kind = target, kind
        try:
            result = await self.provider.teacher_day(teacher.id, target)
            text = teacher_schedule_text(result, kind, self.responses)
        except NotPublished:
            text = self.text("teacher_not_published")
        except ProviderError:
            text = self.text("failure")
        text = page(text, memory, more, self.responses)
        return self.reply(
            request,
            memory,
            text,
            buttons=[button(self.text("button_more"), action="more")] if memory.cursor else [],
            end=self.auto_exit(memory) and not memory.cursor,
        )

    async def handle(self, request: AliceRequest):
        memory = memory_from(request)
        utterance = request.request
        command = utterance.command or utterance.original_utterance
        actions = {
            "today": "сегодня",
            "tomorrow": "завтра",
            "change": "сменить группу",
            "groups": "список групп",
            "more": "дальше",
        }
        action = utterance.payload.get("action")
        selected_id = None
        if isinstance(action, str) and action in actions:
            command = actions[action]
        elif action == "select_group" and isinstance(utterance.payload.get("group_id"), str):
            selected_id = utterance.payload["group_id"]
            command = ""
        intent = detect_intent(command, utterance.nlu)
        if intent == "exit":
            return self.reply(request, memory, self.text("exit"), end=True)
        if intent == "help":
            return self.reply(request, memory, self.text("help"), end=self.auto_exit(memory))
        if intent == "forget":
            return self.reply(
                request,
                Memory(lesson_label=memory.lesson_label, auto_exit=memory.auto_exit),
                self.text("forget"),
                persist=True,
            )
        if intent in {"prefer_exit", "prefer_stay"}:
            memory.auto_exit = intent == "prefer_exit"
            return self.reply(
                request, memory, self.text(intent), persist=True, end=self.auto_exit(memory)
            )
        if intent in {"prefer_subject", "prefer_teacher", "prefer_both", "prefer_auto"}:
            memory.lesson_label = intent.removeprefix("prefer_")
            return self.reply(
                request, memory, self.text(intent), persist=True, end=self.auto_exit(memory)
            )
        if intent == "my_group":
            text = (
                self.text("my_group", group=memory.group_id)
                if memory.group_id
                else self.text("no_group")
            )
            return self.reply(
                request, memory, text, end=bool(memory.group_id) and self.auto_exit(memory)
            )

        selection = parse_date(command, utterance.nlu, self.now())
        if selection.value and abs((selection.value - self.now()).days) > 366:
            selection = type(selection)(error="date_out_of_range")
        normalized = normalize(command)
        own_group_reference = bool(re.search(r"\b(?:моей|нашей)\s+групп\w*\b", normalized))
        explicit_group = bool(re.search(r"\bгрупп\w*", normalized)) and not own_group_reference
        numeric_only = bool(re.fullmatch(r"\d+", number_words(command)))
        numeric_owner = bool(re.search(r"\bу\s+\d+\b", number_words(command)))
        numeric_schedule = bool(re.search(r"\bрасписание\s+\d+\b", number_words(command)))
        group_target = (
            explicit_group
            or numeric_only
            or numeric_owner
            or numeric_schedule
            or selected_id is not None
        )
        if group_target and teacher_query(command) is not None:
            return self.reply(request, memory, self.text("one_target"), buttons=[])
        teacher_followup = memory.view == "teacher" and (
            intent in {"more", "first", "count"}
            or (selection.value and intent in {None, "schedule"})
            or memory.awaiting_date
        )
        if (
            intent not in {"groups", "change"}
            and not group_target
            and (
                teacher_query(command) is not None
                or memory.awaiting_teacher
                or teacher_followup
                or action == "select_teacher"
            )
        ):
            return await self.handle_teacher(request, memory, command, intent, selection)
        try:
            groups = await self.provider.groups()
        except ProviderError:
            return self.failure(request)
        group_map = {group.id: group for group in groups}
        if memory.group_id not in group_map:
            memory.group_id = None

        if intent == "groups" or (intent == "more" and memory.view == "groups"):
            text = self.text("groups", groups="; ".join(plain(group.name) for group in groups))
            result = page(text, memory, intent == "more", self.responses)
            memory.view = "groups"
            memory.awaiting_group = True
            return self.reply(
                request, memory, result, buttons=[button(self.text("button_more"), action="more")]
            )

        matches = group_matches(command, groups, memory.awaiting_group or group_target)
        if selected_id is not None:
            matches = [group_map[selected_id]] if selected_id in group_map else []
        kind = intent if intent in {"schedule", "first", "count"} else None
        query_label = requested_label(command)
        if selection.value and kind is None:
            kind = memory.pending_kind or "schedule"
        save_requested = intent == "change" or bool(
            re.search(r"\b(?:моя группа|учусь в группе)\b", normalized)
        )
        # A question about another group is a temporary target. Selecting one's own
        # group, including the answer to onboarding, is the only persistent change.
        if save_requested:
            memory.temporary_group = False
            memory.awaiting_group = False
            memory.pending_kind = None
            memory.pending_date = None
            memory.pending_label = None
        elif not memory.awaiting_group and (group_target or matches) and kind:
            memory.temporary_group = True
        if len(matches) > 1:
            memory.pending_kind = kind or memory.pending_kind
            memory.pending_label = query_label or memory.pending_label
            memory.pending_date = selection.value or memory.pending_date
            return self.ask_group(request, memory, matches, "ambiguous_group")
        persist = False
        if matches:
            memory.query_group_id = matches[0].id
            if not memory.temporary_group:
                memory.group_id = matches[0].id
                persist = True
            memory.awaiting_group = False
            memory.awaiting_teacher = False
            memory.query_teacher_id = None
            memory.cursor = 0
            memory.view = None
            kind = kind or memory.pending_kind
            if not kind:
                memory.pending_date = None
                memory.last_date = None
                memory.last_kind = None
                if not memory.temporary_group:
                    return self.reply(
                        request,
                        memory,
                        self.text("group_saved", group=plain(matches[0].name)),
                        persist=True,
                    )
                kind = "schedule"
        elif intent == "change":
            memory.query_group_id = None
            memory.query_teacher_id = None
            memory.awaiting_teacher = False
            memory.awaiting_date = False
            return self.ask_group(request, memory, groups)
        elif group_target:
            memory.pending_kind = kind
            memory.pending_label = query_label
            memory.pending_date = selection.value
            return self.ask_group(request, memory, groups, "unknown_group")
        elif memory.awaiting_group and not kind:
            return self.ask_group(request, memory, groups, "group_not_understood")

        more = intent == "more" and memory.view == "schedule"
        continuing = (
            more
            or memory.awaiting_date
            or (kind in {"first", "count"} and memory.view == "schedule")
        )
        query_group_id = memory.query_group_id if matches or continuing else memory.group_id
        if not query_group_id or memory.awaiting_group:
            memory.pending_kind = kind or memory.pending_kind
            memory.pending_label = query_label or memory.pending_label
            memory.pending_date = selection.value or memory.pending_date
            return self.ask_group(request, memory, groups)
        if more:
            kind = memory.last_kind
        kind = kind or (memory.pending_kind if memory.awaiting_date else None)
        if selection.error or (memory.awaiting_date and not selection.value):
            memory.query_group_id = query_group_id
            memory.awaiting_date = True
            memory.pending_kind = kind or "schedule"
            memory.pending_label = query_label or memory.pending_label
            return self.reply(
                request, memory, self.text(selection.error or "date_required"), persist=persist
            )
        if not kind:
            prompt = "welcome" if not normalized else "unknown_request"
            return self.reply(
                request,
                memory,
                self.text(prompt),
                persist=persist,
                end=bool(normalized) and self.auto_exit(memory),
            )
        target = selection.value or memory.pending_date
        if target is None and continuing:
            target = memory.last_date
        target = target or self.now()
        memory.awaiting_date = False
        memory.awaiting_teacher = False
        memory.query_teacher_id = None
        memory.query_group_id = query_group_id
        memory.pending_kind = None
        memory.pending_date = None
        memory.last_date = target
        memory.last_kind = kind
        memory.view = "schedule"
        if not more:
            memory.announce_group = persist or query_group_id != memory.group_id
            memory.last_label = query_label or memory.pending_label
        memory.pending_label = None
        memory.temporary_group = False
        try:
            result = await self.provider.day(query_group_id, target)
            group = group_map[query_group_id]
            label_mode = self.responses.label_mode(
                self.college, group, memory.last_label or memory.lesson_label
            )
            text = schedule_text(result, group, kind, self.responses, label_mode)
        except NotPublished:
            text = self.text("not_published")
        except ProviderError:
            text = self.text("failure")
        if memory.announce_group and not self.responses.options.show_group:
            text = (
                self.text("group_header", group=plain(group_map[query_group_id].name)) + " " + text
            )
        text = page(text, memory, more, self.responses)
        buttons = None
        if memory.cursor:
            buttons = [
                button(self.text("button_more"), action="more"),
                button(self.text("button_change"), action="change"),
            ]
        return self.reply(
            request,
            memory,
            text,
            persist=persist,
            buttons=buttons,
            end=self.auto_exit(memory) and not memory.cursor,
        )
