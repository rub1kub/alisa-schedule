import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings
from app.dialog import Skill
from app.models import College, read_json
from app.protocol import AliceRequest
from app.providers import FileProvider, KkepikProvider, ProviderError, WithOverrides
from app.responses import Responses

logger = logging.getLogger("alisa_schedule")


def create_app(settings: Settings | None = None, provider=None, now=None) -> FastAPI:
    settings = settings or Settings.from_env()
    allowed_skill_ids = {value for value in (settings.skill_id, settings.test_skill_id) if value}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        college = College.model_validate(read_json(settings.college_file))
        responses = Responses.load(settings.responses_file)
        async with httpx.AsyncClient(
            base_url=settings.api_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.upstream_timeout),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "AlisaCollegeSchedule/1.0"},
        ) as client:
            base = provider
            if base is None:
                base = (
                    KkepikProvider(client, settings, college)
                    if settings.provider == "kkepik"
                    else FileProvider(settings.schedule_file)
                )
            source = WithOverrides(base, settings.overrides_file)
            app.state.skill = Skill(source, college, responses, now)
            app.state.catalog_ready = False
            try:
                app.state.catalog_ready = bool(await source.groups())
            except ProviderError:
                logger.warning("Schedule catalog unavailable at startup")
            if isinstance(base, KkepikProvider):
                try:
                    await source.teachers()
                except ProviderError:
                    logger.warning("Teacher catalog unavailable at startup")
            yield
            if isinstance(base, KkepikProvider):
                await base.close()

    app = FastAPI(
        title="Alice college schedule",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "alisa-schedule"}

    @app.get("/readyz")
    async def readyz():
        try:
            await asyncio.to_thread(app.state.skill.provider.document.read)
            # After an upstream outage, a normal student request can recover readiness.
            ready = app.state.catalog_ready
        except (OSError, ValueError):
            ready = False
        return JSONResponse(
            {"status": "ready" if ready else "degraded"}, status_code=200 if ready else 503
        )

    @app.post("/webhook")
    async def webhook(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return JSONResponse({"error": "application/json required"}, status_code=415)
        try:
            async with asyncio.timeout(1):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > settings.max_body_bytes:
                        return JSONResponse({"error": "request too large"}, status_code=413)
            envelope = AliceRequest.model_validate_json(body)
        except TimeoutError:
            return JSONResponse({"error": "request timeout"}, status_code=408)
        except (ValueError, ValidationError):
            return JSONResponse({"error": "invalid Alice request"}, status_code=400)
        if allowed_skill_ids and envelope.session.skill_id not in allowed_skill_ids:
            return JSONResponse({"error": "unknown skill"}, status_code=403)
        try:
            async with asyncio.timeout(2.8):
                result = await app.state.skill.handle(envelope)
                # Readiness describes valid configuration/catalog, not publication for every date.
                base = app.state.skill.provider.upstream
                if isinstance(base, KkepikProvider):
                    app.state.catalog_ready = ("groups",) in base.cache
                else:
                    app.state.catalog_ready = True
                return result
        except TimeoutError:
            logger.warning("Skill response deadline reached")
        except Exception as exc:
            # Never log utterances, IDs, HTTP bodies, headers or upstream personal data.
            logger.error("Skill handler failure: %s", type(exc).__name__)
        return app.state.skill.failure(envelope)

    return app
