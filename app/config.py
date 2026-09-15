import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: Literal["development", "production"] = "development"
    skill_id: str = ""
    test_skill_id: str = ""
    provider: Literal["kkepik", "file"] = "kkepik"
    api_base_url: str = "https://kkepik.rub1kub.ru"
    college_file: Path = Path("data/college.json")
    responses_file: Path = Path("data/responses.json")
    schedule_file: Path = Path("data/schedule.example.json")
    overrides_file: Path = Path("data/overrides.json")
    cache_ttl: float = Field(default=60, ge=1, le=600)
    stale_ttl: float = Field(default=180, ge=0, le=600)
    upstream_timeout: float = Field(default=1.8, gt=0, le=2)
    upstream_rps: float = Field(default=4, gt=0, le=4)
    max_body_bytes: int = Field(default=65536, ge=1024, le=262144)

    @field_validator("skill_id", "test_skill_id")
    @classmethod
    def strip_skill_id(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_deployment(self):
        url = urlsplit(self.api_base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("API_BASE_URL must be an HTTPS origin")
        if url.username or url.password:
            raise ValueError("Credentials must not be embedded in API_BASE_URL")
        if self.environment == "production" and not self.skill_id.strip():
            raise ValueError("ALICE_SKILL_ID is required in production")
        return self

    @classmethod
    def from_env(cls):
        mapping = {
            "environment": "APP_ENV",
            "skill_id": "ALICE_SKILL_ID",
            "test_skill_id": "ALICE_TEST_SKILL_ID",
            "provider": "SCHEDULE_PROVIDER",
            "api_base_url": "API_BASE_URL",
            "college_file": "COLLEGE_FILE",
            "responses_file": "RESPONSES_FILE",
            "schedule_file": "SCHEDULE_FILE",
            "overrides_file": "OVERRIDES_FILE",
            "cache_ttl": "CACHE_TTL_SECONDS",
            "stale_ttl": "STALE_TTL_SECONDS",
            "upstream_timeout": "UPSTREAM_TIMEOUT_SECONDS",
            "upstream_rps": "UPSTREAM_RPS",
        }
        return cls(**{key: os.environ[name] for key, name in mapping.items() if name in os.environ})
