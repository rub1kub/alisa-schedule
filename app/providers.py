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
    read_json,
)


class ProviderError(Exception):
    pass


class NotPublished(ProviderError):
    pass


class Unavailable(ProviderError):
    pass


class ScheduleProvider(Protocol):
    async def groups(self) -> list[Group]: ...

    async def day(self, group_id: str, target: date) -> ScheduleResult: ...


def plain(text: str) -> str:
    return " ".join(re.sub(r"<[^>]*>", "", html.unescape(text)).split())


PAIR = re.compile(r"^[\s▪️•]*([1-9]|1[0-2])\s*пара\s*[–—-]\s*(.+)$", re.I)
EMPTY_SUBJECTS = {"нет", "нет пары", "отмена", "отменена", "не будет"}
ROOM = re.compile(r"^(?:\d[\w\s,./-]*|(?:ауд\.?|каб\.?|вц|иц|тир|с/з|спорт|акт)[\w\s,./-]*)$", re.I)


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
