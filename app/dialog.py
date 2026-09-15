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
from app.models import College, Group, ScheduleResult
from app.protocol import AliceRequest
from app.providers import NotPublished, ProviderError, ScheduleProvider, plain
from app.responses import LessonLabel, Responses, lesson_label

Kind = Literal["schedule", "first", "count"]


class Memory(BaseModel):
    model_config = ConfigDict(extra="ignore")
    group_id: str | None = Field(default=None, max_length=200)
    awaiting_group: bool = False
    pending_kind: Kind | None = None
    pending_date: date | None = None
    last_kind: Kind | None = None
    last_date: date | None = None
    view: Literal["schedule", "groups"] | None = None
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
        return self.reply(request, memory_from(request), self.text("failure"))

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
            return self.reply(request, memory, self.text("help"))
        if intent == "forget":
            return self.reply(
                request, Memory(lesson_label=memory.lesson_label), self.text("forget"), persist=True
            )
        if intent in {"prefer_subject", "prefer_teacher", "prefer_both", "prefer_auto"}:
            memory.lesson_label = intent.removeprefix("prefer_")
            return self.reply(request, memory, self.text(intent), persist=True)
        if intent == "my_group":
            text = (
                self.text("my_group", group=memory.group_id)
                if memory.group_id
                else self.text("no_group")
            )
            return self.reply(request, memory, text)

        selection = parse_date(command, utterance.nlu, self.now())
        if selection.error:
            return self.reply(request, memory, self.text(selection.error))
        if selection.value and abs((selection.value - self.now()).days) > 366:
            return self.reply(request, memory, self.text("date_out_of_range"))
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
                request,
                memory,
                result,
                buttons=[button(self.text("button_more"), action="more")],
            )

        explicit_group = bool(re.search(r"\bгрупп\w*", normalize(command)))
        numeric_only = bool(re.fullmatch(r"\d+", number_words(command)))
        matches = group_matches(
            command, groups, memory.awaiting_group or explicit_group or numeric_only
        )
        if selected_id is not None:
            matches = [group_map[selected_id]] if selected_id in group_map else []

        kind = intent if intent in {"schedule", "first", "count"} else None
        query_label = requested_label(command)
        if selection.value and kind is None:
            kind = "schedule"
        if len(matches) > 1:
            memory.pending_kind = kind or memory.pending_kind
            memory.pending_label = query_label or memory.pending_label
            memory.pending_date = selection.value or memory.pending_date or self.now()
            return self.ask_group(request, memory, matches, "ambiguous_group")
        persist = False
        if matches:
            memory.group_id = matches[0].id
            memory.awaiting_group = False
            memory.cursor = 0
            memory.view = None
            persist = True
            kind = kind or memory.pending_kind
            if not kind:
                memory.pending_date = None
                memory.last_date = None
                memory.last_kind = None
                return self.reply(
                    request,
                    memory,
                    self.text("group_saved", group=plain(matches[0].name)),
                    persist=True,
                )
        elif intent == "change":
            memory.pending_kind = None
            memory.pending_date = None
            memory.pending_label = None
            return self.ask_group(request, memory, groups)
        elif selected_id is not None or explicit_group:
            memory.pending_kind = kind
            memory.pending_label = query_label
            memory.pending_date = selection.value or self.now()
            return self.ask_group(request, memory, groups, "unknown_group")
        elif memory.awaiting_group and not kind:
            return self.ask_group(request, memory, groups, "group_not_understood")

        if not memory.group_id or memory.awaiting_group:
            memory.pending_kind = kind or memory.pending_kind
            memory.pending_label = query_label or memory.pending_label
            memory.pending_date = selection.value or memory.pending_date or self.now()
            return self.ask_group(request, memory, groups)

        more = intent == "more" and memory.view == "schedule"
        if more:
            kind = memory.last_kind
        if not kind:
            prompt = "welcome" if not normalize(command) else "unknown_request"
            return self.reply(request, memory, self.text(prompt), persist=persist)
        target = selection.value or memory.pending_date
        if target is None and (kind in {"first", "count"} or more):
            target = memory.last_date
        target = target or self.now()
        memory.pending_kind = None
        memory.pending_date = None
        memory.last_date = target
        memory.last_kind = kind
        memory.view = "schedule"
        if not more:
            memory.announce_group = persist
            memory.last_label = query_label or memory.pending_label
        memory.pending_label = None
        try:
            result = await self.provider.day(memory.group_id, target)
            group = group_map[memory.group_id]
            label_mode = self.responses.label_mode(
                self.college, group, memory.last_label or memory.lesson_label
            )
            text = schedule_text(result, group, kind, self.responses, label_mode)
        except NotPublished:
            memory.cursor = 0
            text = self.text("not_published")
        except ProviderError:
            memory.cursor = 0
            text = self.text("failure")
        if memory.announce_group and not self.responses.options.show_group:
            text = (
                self.text("group_header", group=plain(group_map[memory.group_id].name)) + " " + text
            )
        text = page(text, memory, more, self.responses)
        buttons = None
        if memory.cursor:
            buttons = [
                button(self.text("button_more"), action="more"),
                button(self.text("button_change"), action="change"),
            ]
        return self.reply(request, memory, text, persist=persist, buttons=buttons)
