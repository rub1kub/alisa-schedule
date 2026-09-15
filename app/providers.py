"""Read-only data adapters. No imports, files or databases from KKEPIK_bot."""

import asyncio
import html
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.models import (
    College,
    Group,
    Lesson,
    Overrides,
    ScheduleFile,
    ScheduleResult,
    Teacher,
    TeacherLesson,
    TeacherScheduleResult,
    read_json,
)
from app.teachers import teacher_key, teacher_names


class ProviderError(Exception):
    pass


class NotPublished(ProviderError):
    pass


class Unavailable(ProviderError):
    pass


class ScheduleProvider(Protocol):
    async def groups(self) -> list[Group]: ...

    async def day(self, group_id: str, target: date) -> ScheduleResult: ...

    async def teachers(self) -> list[Teacher]: ...

    async def teacher_day(self, teacher_id: str, target: date) -> TeacherScheduleResult: ...


def plain(text: str) -> str:
    return " ".join(re.sub(r"<[^>]*>", "", html.unescape(text)).split())


PAIR = re.compile(r"^[\s▪️•]*([1-9]|1[0-2])\s*пара\s*[–—-]\s*(.+)$", re.I)
EMPTY_SUBJECTS = {"нет", "нет пары", "отмена", "отменена", "не будет"}
ROOM = re.compile(r"^(?:\d[\w\s,./-]*|(?:ауд\.?|каб\.?|вц|иц|тир|с/з|спорт|акт)[\w\s,./-]*)$", re.I)
GROUP_CODE = re.compile(r"\b\d{1,4}-[А-ЯЁA-Z0-9]+-[1-4][А-ЯЁA-Z]+\b", re.I)


def parse_api_day(payload: Any, group_id: str, target: date, college: College) -> ScheduleResult:
    """Reject unknown wire formats instead of silently undercounting pairs."""
    if not isinstance(payload, dict):
        raise Unavailable("Invalid upstream object")
    if payload.get("date") != target.strftime("%d.%m.%Y") or payload.get("group") != group_id:
        raise Unavailable("Upstream group/date mismatch")
    rows = payload.get("schedule")
    if not isinstance(rows, list) or len(rows) > 100:
        raise Unavailable("Invalid upstream schedule")
    table = college.bells.get("saturday" if target.weekday() == 5 else "weekday", {})
    lessons: list[Lesson] = []
    for row in rows:
        if not isinstance(row, str) or len(row) > 8000:
            raise Unavailable("Invalid upstream line")
        for line in row.splitlines():
            if not line.strip():
                continue
            match = PAIR.fullmatch(plain(line))
            if not match:
                raise Unavailable("Unrecognized upstream pair")
            number = int(match[1])
            parts = re.split(r"\s+[–—-]\s+", match[2])
            subject, *details = parts
            if subject.casefold().rstrip(".! ") in EMPTY_SUBJECTS:
                continue
            rooms = []
            while details and ROOM.fullmatch(details[-1]):
                rooms.insert(0, details.pop())
            bell = table.get(str(number))
            lessons.append(
                Lesson(
                    number=number,
                    subject=college.subject_aliases.get(subject, subject),
                    teacher="; ".join(details),
                    room=", ".join(rooms),
                    start=bell.start if bell else None,
                    end=bell.end if bell else None,
                )
            )
    # The upstream emits multiple lines with the same pair number for subgroups.
    # Its public API does not identify the subgroup: preserve all variants, not an invented mapping.
    for number in {lesson.number for lesson in lessons}:
        same_pair = [lesson for lesson in lessons if lesson.number == number]
        if len(same_pair) > 1:
            for index, lesson in enumerate(same_pair, 1):
                lesson.subgroup = f"вариант {index}"
    return ScheduleResult(group_id=group_id, date=target, lessons=lessons, source="kkepik")


def parse_api_teacher_day(
    payload: Any, teacher_id: str, target: date, college: College
) -> TeacherScheduleResult:
    if not isinstance(payload, dict):
        raise Unavailable("Invalid upstream object")
    if payload.get("date") != target.strftime("%d.%m.%Y") or payload.get("teacher") != teacher_id:
        raise Unavailable("Upstream teacher/date mismatch")
    rows = payload.get("schedule")
    if not isinstance(rows, list) or len(rows) > 100:
        raise Unavailable("Invalid upstream schedule")
    table = college.bells.get("saturday" if target.weekday() == 5 else "weekday", {})
    lessons, seen = [], set()
    for row in rows:
        if not isinstance(row, str) or len(row) > 8000:
            raise Unavailable("Invalid upstream line")
        for raw in row.splitlines():
            line = plain(raw)
            if not line or line.casefold() == "совмещенные пары:":
                continue
            match = PAIR.fullmatch(line)
            if not match:
                raise Unavailable("Unrecognized upstream teacher pair")
            number, detail = int(match[1]), match[2]
            if detail.casefold().rstrip(".! ") in EMPTY_SUBJECTS:
                continue
            parts = re.split(r"\s+[–—-]\s+", detail)
            group_part, *rest = parts
            groups = list(dict.fromkeys(GROUP_CODE.findall(group_part)))
            remainder = GROUP_CODE.sub("", group_part).strip(" ,;/+и")
            if not groups or remainder:
                raise Unavailable("Unrecognized upstream teacher groups")
            while rest and ROOM.fullmatch(rest[-1]):
                rest.pop()
            if len(rest) > 1:
                raise Unavailable("Unrecognized upstream teacher details")
            subject = rest[0] if rest else ""
            groups = [group for group in groups if group not in college.excluded_groups]
            if not groups:
                continue
            key = number, tuple(sorted(groups)), subject
            if key in seen:
                continue
            seen.add(key)
            bell = table.get(str(number))
            lessons.append(
                TeacherLesson(
                    number=number,
                    groups=groups,
                    subject=college.subject_aliases.get(subject, subject),
                    start=bell.start if bell else None,
                    end=bell.end if bell else None,
                )
            )
    return TeacherScheduleResult(
        teacher_id=teacher_id, date=target, lessons=lessons, source="kkepik"
    )


def teacher_lessons(days, teacher_id: str) -> list[TeacherLesson]:
    result = []
    for day in days:
        for lesson in day.lessons:
            if teacher_key(teacher_id) in {
                teacher_key(name) for name in teacher_names(lesson.teacher)
            }:
                result.append(
                    TeacherLesson(
                        number=lesson.number,
                        groups=[day.group_id],
                        subject=lesson.subject,
                        subgroup=lesson.subgroup,
                        start=lesson.start,
                        end=lesson.end,
                    )
                )
    return result


@dataclass
class CacheEntry:
    value: Any
    created: float
    ttl: float
    missing: bool = False


class KkepikProvider:
    """Bounded per-process cache, coalesced requests, negative caching and rate budget."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        college: College,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.settings = settings
        self.college = college
        self.clock = clock
        self.cache: OrderedDict[tuple, CacheEntry] = OrderedDict()
        self.inflight: dict[tuple, asyncio.Task] = {}
        self.next_request_at = clock()
        self.cooldown_until = 0.0

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _get(self, path: str, params: dict[str, str] | None = None):
        now = self.clock()
        delay = max(0.0, self.next_request_at - now)
        if now < self.cooldown_until or delay > 0.5:
            raise Unavailable("Upstream budget exhausted")
        # Reserve a slot without an await: concurrent misses cannot create a burst.
        self.next_request_at = max(now, self.next_request_at) + 1 / self.settings.upstream_rps
        if delay:
            await asyncio.sleep(delay)
        try:
            async with asyncio.timeout(self.settings.upstream_timeout):
                async with self.client.stream("GET", path, params=params) as response:
                    if response.status_code == 404:
                        raise NotPublished()
                    if response.status_code == 429:
                        retry = response.headers.get("Retry-After", "5")
                        seconds = int(retry) if retry.isdigit() else 5
                        self.cooldown_until = self.clock() + max(1, min(seconds, 60))
                        raise Unavailable("Upstream throttled")
                    if response.status_code >= 400:
                        self.cooldown_until = self.clock() + 2
                        raise Unavailable("Upstream unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 512 * 1024:
                            raise Unavailable("Upstream response too large")
                    return json.loads(body)
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            self.cooldown_until = self.clock() + 2
            raise Unavailable("Upstream request failed") from exc

    def _remember(self, key: tuple, entry: CacheEntry):
        self.cache[key] = entry
        self.cache.move_to_end(key)
        while len(self.cache) > 2048:
            self.cache.popitem(last=False)

    async def _load(self, key: tuple, loader, ttl: float):
        try:
            value = await loader()
            self._remember(key, CacheEntry(value, self.clock(), ttl))
            return value
        except NotPublished:
            self._remember(key, CacheEntry(None, self.clock(), 20, missing=True))
            raise
        except (ValidationError, TypeError, KeyError) as exc:
            raise Unavailable("Upstream schema mismatch") from exc
        finally:
            self.inflight.pop(key, None)

    async def _cached(self, key: tuple, loader, ttl: float, stale_ttl: float):
        entry = self.cache.get(key)
        if entry and self.clock() - entry.created < entry.ttl:
            self.cache.move_to_end(key)
            if entry.missing:
                raise NotPublished()
            return entry.value, False
        try:
            if key not in self.inflight:
                if len(self.inflight) >= 16:
                    raise Unavailable("Too many upstream requests")
                task = asyncio.create_task(self._load(key, loader, ttl))
                # Consume eventual exceptions even if the HTTP client disconnected.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                self.inflight[key] = task
            return await asyncio.shield(self.inflight[key]), False
        except NotPublished:
            # An authoritative 404 invalidates previously cached lessons.
            raise
        except Unavailable:
            if entry and not entry.missing and self.clock() - entry.created < entry.ttl + stale_ttl:
                return entry.value, True
            raise

    async def groups(self) -> list[Group]:
        async def load():
            data = await self._get("/api/groups")
            names = data.get("groups") if isinstance(data, dict) else None
            if not isinstance(names, list) or not names or len(names) > 5000:
                raise Unavailable("Group catalog unavailable")
            if any(not isinstance(name, str) or not name.strip() for name in names):
                raise Unavailable("Invalid group catalog")
            return [
                Group(id=name, name=name, aliases=self.college.group_aliases.get(name, []))
                for name in sorted(set(names) - set(self.college.excluded_groups))
            ]

        value, _ = await self._cached(("groups",), load, 600, 86400)
        return value

    async def day(self, group_id: str, target: date) -> ScheduleResult:
        async def load():
            data = await self._get(
                "/api/schedule/group", {"group": group_id, "date": target.strftime("%d.%m.%Y")}
            )
            return parse_api_day(data, group_id, target, self.college)

        value, stale = await self._cached(
            (group_id, target.isoformat()), load, self.settings.cache_ttl, self.settings.stale_ttl
        )
        return value.model_copy(update={"stale": stale})

    async def teachers(self) -> list[Teacher]:
        async def load():
            data = await self._get("/api/teachers")
            names = data.get("teachers") if isinstance(data, dict) else None
            if not isinstance(names, list) or not names or len(names) > 5000:
                raise Unavailable("Teacher catalog unavailable")
            if any(not isinstance(name, str) or not name.strip() for name in names):
                raise Unavailable("Invalid teacher catalog")
            return [
                Teacher(id=name, name=name, aliases=self.college.teacher_aliases.get(name, []))
                for name in sorted(set(names))
            ]

        value, _ = await self._cached(("teachers",), load, 600, 86400)
        return value

    async def teacher_day(self, teacher_id: str, target: date) -> TeacherScheduleResult:
        async def load():
            data = await self._get(
                "/api/schedule/teacher",
                {"teacher": teacher_id, "date": target.strftime("%d.%m.%Y")},
            )
            return parse_api_teacher_day(data, teacher_id, target, self.college)

        value, stale = await self._cached(
            ("teacher", teacher_id, target.isoformat()),
            load,
            self.settings.cache_ttl,
            self.settings.stale_ttl,
        )
        return value.model_copy(update={"stale": stale})


class JsonDocument:
    """Reload atomically replaced files; a broken update is reported, never hidden."""

    def __init__(self, path: Path, model):
        self.path = path
        self.model = model
        self.signature = None
        self.value = None
        self.read()

    def read(self):
        stat = self.path.stat()
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if signature != self.signature:
            value = self.model.model_validate(read_json(self.path))
            self.value, self.signature = value, signature
        return self.value


class FileProvider:
    def __init__(self, path: Path):
        self.document = JsonDocument(path, ScheduleFile)

    async def groups(self):
        try:
            data = await asyncio.to_thread(self.document.read)
            return data.groups
        except (OSError, ValueError) as exc:
            raise Unavailable("Schedule file invalid") from exc

    async def day(self, group_id: str, target: date):
        try:
            data = await asyncio.to_thread(self.document.read)
        except (OSError, ValueError) as exc:
            raise Unavailable("Schedule file invalid") from exc
        for day in data.days:
            if day.group_id == group_id and day.date == target:
                return ScheduleResult(**day.model_dump(), source="file", demo=data.demo)
        raise NotPublished()

    async def teachers(self):
        try:
            data = await asyncio.to_thread(self.document.read)
        except (OSError, ValueError) as exc:
            raise Unavailable("Schedule file invalid") from exc
        names = {
            name
            for day in data.days
            for lesson in day.lessons
            for name in teacher_names(lesson.teacher)
        }
        return [Teacher(id=name, name=name) for name in sorted(names)]

    async def teacher_day(self, teacher_id: str, target: date):
        try:
            data = await asyncio.to_thread(self.document.read)
        except (OSError, ValueError) as exc:
            raise Unavailable("Schedule file invalid") from exc
        days = [day for day in data.days if day.date == target]
        if not days:
            raise NotPublished()
        return TeacherScheduleResult(
            teacher_id=teacher_id,
            date=target,
            lessons=teacher_lessons(days, teacher_id),
            source="file",
            demo=data.demo,
            partial={day.group_id for day in days} != {group.id for group in data.groups},
        )


class WithOverrides:
    def __init__(self, upstream: ScheduleProvider, path: Path):
        self.upstream = upstream
        self.document = JsonDocument(path, Overrides)

    async def groups(self):
        return await self.upstream.groups()

    async def day(self, group_id: str, target: date):
        try:
            data = await asyncio.to_thread(self.document.read)
        except (OSError, ValueError) as exc:
            raise Unavailable("Overrides invalid") from exc
        for day in data.replacements:
            if day.group_id == group_id and day.date == target:
                return ScheduleResult(**day.model_dump(), source="override")
        return await self.upstream.day(group_id, target)

    async def teachers(self):
        try:
            data = await asyncio.to_thread(self.document.read)
        except (OSError, ValueError) as exc:
            raise Unavailable("Overrides invalid") from exc
        catalog = {teacher_key(item.id): item for item in await self.upstream.teachers()}
        for day in data.replacements:
            for lesson in day.lessons:
                for name in teacher_names(lesson.teacher):
                    catalog.setdefault(teacher_key(name), Teacher(id=name, name=name))
        return list(catalog.values())

    async def teacher_day(self, teacher_id: str, target: date):
        try:
            data = await asyncio.to_thread(self.document.read)
        except (OSError, ValueError) as exc:
            raise Unavailable("Overrides invalid") from exc
        days = [day for day in data.replacements if day.date == target]
        additions = teacher_lessons(days, teacher_id)
        try:
            result = await self.upstream.teacher_day(teacher_id, target)
        except NotPublished:
            if not additions:
                raise
            return TeacherScheduleResult(
                teacher_id=teacher_id,
                date=target,
                lessons=additions,
                source="override",
                partial=True,
            )
        if not days:
            return result
        replaced = {day.group_id for day in days}
        lessons, changed = [], bool(additions)
        for lesson in result.lessons:
            groups = [group for group in lesson.groups if group not in replaced]
            changed = changed or groups != lesson.groups
            if groups:
                lessons.append(lesson.model_copy(update={"groups": groups}))
        return result.model_copy(
            update={
                "lessons": lessons + additions,
                "source": "override" if changed else result.source,
            }
        )
