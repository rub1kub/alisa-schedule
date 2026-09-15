from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProtocolModel(BaseModel):
    # Yandex may add fields; only the fields used by this skill are interpreted.
    model_config = ConfigDict(extra="ignore")


class NLU(ProtocolModel):
    tokens: list[str] = Field(default_factory=list, max_length=500)
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    intents: dict[str, Any] = Field(default_factory=dict)


class Utterance(ProtocolModel):
    type: Literal["SimpleUtterance", "ButtonPressed"]
    command: str = Field(default="", max_length=4096)
    original_utterance: str = Field(default="", max_length=4096)
    payload: dict[str, Any] = Field(default_factory=dict)
    nlu: NLU = Field(default_factory=NLU)


class Session(ProtocolModel):
    session_id: str = Field(min_length=1, max_length=128)
    message_id: int = Field(ge=0)
    skill_id: str = Field(min_length=1, max_length=128)
    new: bool
    user: dict[str, Any] | None = None
    application: dict[str, Any] = Field(default_factory=dict)

    @property
    def authorized(self) -> bool:
        return bool(self.user and self.user.get("user_id"))


class State(ProtocolModel):
    session: dict[str, Any] = Field(default_factory=dict)
    user: dict[str, Any] = Field(default_factory=dict)
    application: dict[str, Any] = Field(default_factory=dict)


class AliceRequest(ProtocolModel):
    version: Literal["1.0"]
    request: Utterance
    session: Session
    state: State = Field(default_factory=State)
