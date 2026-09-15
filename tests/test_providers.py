import asyncio
import json
from datetime import date

import httpx
import pytest

from app.config import Settings
from app.models import College, read_json
from app.providers import (
    FileProvider,
    KkepikProvider,
    NotPublished,
    Unavailable,
    WithOverrides,
    parse_api_day,
)
from tests.conftest import GROUP, TODAY


@pytest.fixture
def college():
    from pathlib import Path

    return College.model_validate(read_json(Path("data/college.json")))


def api_payload(lines=None):
    return {
        "group": GROUP,
        "date": TODAY.strftime("%d.%m.%Y"),
        "schedule": lines if lines is not None else ["▪️2 пара – Математика – 22"],
    }


def test_api_parser_skips_empty_pairs_preserves_subgroups_and_times(college):
    result = parse_api_day(
        api_payload(
            [
                "▪️1 пара – Нет",
                "▪️2 пара – Сети – 22\n▪️2 пара – Программирование – 35",
                "▪️4 пара – <b>Математика</b> – 40",
            ]
        ),
        GROUP,
        TODAY,
        college,
    )
    assert [item.number for item in result.lessons] == [2, 2, 4]
    assert result.lessons[0].start == "10:25"
    assert result.lessons[0].room == "22"
    assert result.lessons[2].subject == "Математика"


@pytest.mark.parametrize(
    "change",
    [
        {"date": "16.09.2026"},
        {"group": "wrong"},
        {"schedule": ["unknown format"]},
        {"schedule": [1]},
        {"schedule": "wrong"},
    ],
)
def test_bad_upstream_format_is_not_a_day_off(college, change):
    with pytest.raises(Unavailable):
        parse_api_day(api_payload() | change, GROUP, TODAY, college)


def test_cache_coalesces_concurrent_requests_and_passes_exact_date(college):
    async def run():
        count = 0

        async def handler(request):
            nonlocal count
            count += 1
            assert request.url.params["date"] == "15.09.2026"
            assert request.url.params["group"] == GROUP
            await asyncio.sleep(0.01)
            return httpx.Response(200, json=api_payload())

        async with httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        ) as client:
            provider = KkepikProvider(client, Settings(), college)
            results = await asyncio.gather(*(provider.day(GROUP, TODAY) for _ in range(30)))
            assert count == 1
            assert len(results) == 30
            await provider.day(GROUP, TODAY)
            assert count == 1

    asyncio.run(run())


def test_stale_cache_is_explicit_and_expires(college):
    async def run():
        now = [0.0]
        status = [200]
        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status[0], json=api_payload())
            ),
        ) as client:
            provider = KkepikProvider(
                client, Settings(cache_ttl=10, stale_ttl=20), college, lambda: now[0]
            )
            assert not (await provider.day(GROUP, TODAY)).stale
            status[0], now[0] = 503, 11
            assert (await provider.day(GROUP, TODAY)).stale
            now[0] = 31
            with pytest.raises(Unavailable):
                await provider.day(GROUP, TODAY)

    asyncio.run(run())


def test_404_is_cached_and_invalidates_old_schedule(college):
    async def run():
        now, status, calls = [0.0], [200], [0]

        def handler(request):
            calls[0] += 1
            return httpx.Response(status[0], json=api_payload())

        async with httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        ) as client:
            provider = KkepikProvider(client, Settings(cache_ttl=10), college, lambda: now[0])
            await provider.day(GROUP, TODAY)
            status[0], now[0] = 404, 11
            for _ in range(3):
                with pytest.raises(NotPublished):
                    await provider.day(GROUP, TODAY)
            assert calls[0] == 2

    asyncio.run(run())


def test_429_honors_retry_after_without_retry_storm(college):
    async def run():
        now, calls = [0.0], [0]

        def handler(request):
            calls[0] += 1
            return httpx.Response(429, headers={"Retry-After": "10"})

        async with httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        ) as client:
            provider = KkepikProvider(client, Settings(), college, lambda: now[0])
            for offset in (0, 1, 2, 3):
                now[0] = offset
                with pytest.raises(Unavailable):
                    await provider.day(GROUP, TODAY)
            assert calls[0] == 1

    asyncio.run(run())


def test_timeout_returns_a_controlled_error(college):
    async def run():
        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=api_payload())

        async with httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        ) as client:
            provider = KkepikProvider(client, Settings(upstream_timeout=0.01), college)
            with pytest.raises(Unavailable):
                await provider.day(GROUP, TODAY)

    asyncio.run(run())


def test_phantom_103_group_is_filtered_by_skill_config(college):
    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"groups": [GROUP, "103-Д3-3ИНС", "104-Д9-3ИСП"]}
                )
            ),
        ) as client:
            provider = KkepikProvider(client, Settings(), college)
            assert [group.id for group in await provider.groups()] == [GROUP, "104-Д9-3ИСП"]

    asyncio.run(run())


def test_overrides_replace_whole_day_and_reload_without_restart(tmp_path, fixture_data):
    async def run():
        schedule = tmp_path / "schedule.json"
        schedule.write_text(json.dumps(fixture_data))
        overrides = tmp_path / "overrides.json"
        overrides.write_text(json.dumps({"schema_version": 1, "replacements": []}))
        provider = WithOverrides(FileProvider(schedule), overrides)
        assert len((await provider.day(GROUP, TODAY)).lessons) == 2
        replacement = {
            "schema_version": 1,
            "replacements": [
                {
                    "group_id": GROUP,
                    "date": TODAY.isoformat(),
                    "lessons": [],
                    "note": "Отмена занятий",
                },
            ],
        }
        staged = tmp_path / "staged.json"
        staged.write_text(json.dumps(replacement))
        staged.replace(overrides)
        result = await provider.day(GROUP, TODAY)
        assert result.source == "override" and result.lessons == []
        overrides.write_text("{")
        with pytest.raises(Unavailable):
            await provider.day(GROUP, TODAY)
        with pytest.raises(NotPublished):
            await provider.upstream.day(GROUP, date(2026, 10, 1))

    asyncio.run(run())
