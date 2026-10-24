"""Typed settings: config.yaml for structure, environment / .env for secrets."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# An allowlisted action whose name contains any of these is rejected: Gmail is draft-only, nothing destructive.
BANNED_ACTION_WORDS = ("send", "delete", "trash", "label")


class ToolSpec(BaseModel):
    """Maps one logical action to a real MCP tool and renames our argument keys to its parameters."""

    name: str
    args: dict[str, str] = {}
    result: dict[str, str] = {}


class McpServer(BaseModel):
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: str | None = None
    header_env: dict[str, str] = {}  # header name -> env var NAME, never the value
    tools: dict[str, ToolSpec]
    allowlist: list[str]

    @model_validator(mode="after")
    def _check(self) -> "McpServer":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport == "http" and not self.url:
            raise ValueError("http transport requires 'url'")
        unknown = [a for a in self.allowlist if a not in self.tools]
        if unknown:
            raise ValueError(f"allowlist actions not in tools: {unknown}")
        banned = [a for a in self.allowlist if any(w in a.lower() for w in BANNED_ACTION_WORDS)]
        if banned:
            raise ValueError(f"allowlist may not contain send/delete/trash/label actions: {banned}")
        return self


class McpConfig(BaseModel):
    timeout_s: float = 30
    docs: McpServer
    gmail: McpServer


class AgentConfig(BaseModel):
    max_steps: int = 12
    max_repeat: int = 3
    reasoning_effort: str = "medium"


class Settings(BaseSettings):
    # The PULSE_ prefix keeps generic OS variables (LANG, MODEL, ...) from overriding settings.
    model_config = SettingsConfigDict(env_prefix="PULSE_", env_file=".env", extra="ignore", populate_by_name=True)

    app_id: str = "com.nextbillion.groww"
    lang: str = "en"
    country: str = "in"
    window_weeks: int = 10
    max_window_weeks: int = 12
    min_reviews: int = 30
    max_themes: int = 5
    rank_weights: tuple[float, float, float] = (0.6, 0.3, 0.1)
    model: str = "openai/gpt-oss-120b"
    fallback_model: str = "openai/gpt-oss-20b"
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    temperature: float = 0.2
    data_dir: Path = Path("data")
    recipient: str
    id_salt: str = Field(validation_alias=AliasChoices("PULSE_ID_SALT", "id_salt"), min_length=1)
    mcp: McpConfig
    agent: AgentConfig = AgentConfig()

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if not 8 <= self.window_weeks <= self.max_window_weeks <= 12:
            raise ValueError(
                "window_weeks/max_window_weeks must satisfy 8 <= window_weeks <= max_window_weeks <= 12, "
                f"got {self.window_weeks}/{self.max_window_weeks}"
            )
        if not 1 <= self.max_themes <= 5:
            raise ValueError(f"max_themes must be between 1 and 5, got {self.max_themes}")
        return self


def load_settings(path: Path = Path("config.yaml")) -> Settings:
    """Load config.yaml; secrets (PULSE_ID_SALT) come from the environment or .env."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Settings(**data)
