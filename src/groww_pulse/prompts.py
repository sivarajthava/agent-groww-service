"""Prompt files live in <repo>/prompts/; their sha256s go into run.json so every run is auditable."""

import hashlib
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def prompt_sha256(name: str) -> str:
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()
