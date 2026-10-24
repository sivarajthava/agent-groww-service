from pathlib import Path

import pytest
from pydantic import ValidationError

from groww_pulse.config import Settings, load_settings

MINIMAL_MCP_YAML = """
mcp:
  docs:
    transport: stdio
    command: npx
    args: ["-y", "docs-server"]
    tools:
      find_doc:   {name: search_documents, args: {title: query}}
      create_doc: {name: create_document, args: {title: title, body: content}, result: {id: documentId, url: url}}
    allowlist: [find_doc, create_doc]
  gmail:
    transport: http
    url: "https://gmail.example/mcp"
    header_env: {Authorization: GMAIL_MCP_AUTH}
    tools:
      list_drafts:  {name: list_drafts, args: {subject: query}}
      create_draft: {name: create_draft, result: {id: draftId}}
    allowlist: [list_drafts, create_draft]
"""


@pytest.fixture(autouse=True)
def _salt(monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_minimal_config_loads_with_defaults(tmp_path):
    s = load_settings(_write(tmp_path, "recipient: a@b.c\n" + MINIMAL_MCP_YAML))
    assert isinstance(s, Settings)
    assert s.app_id == "com.nextbillion.groww"
    assert s.window_weeks == 10 and s.max_window_weeks == 12
    assert s.max_themes == 5
    assert s.rank_weights == (0.6, 0.3, 0.1)
    assert s.model == "openai/gpt-oss-120b"
    assert s.fallback_model == "openai/gpt-oss-20b"
    assert s.id_salt == "test-salt"
    assert s.data_dir == Path("data")
    assert s.mcp.timeout_s == 30
    assert s.mcp.docs.tools["create_doc"].args == {"title": "title", "body": "content"}
    assert s.mcp.gmail.header_env == {"Authorization": "GMAIL_MCP_AUTH"}
    assert s.agent.max_steps == 12 and s.agent.max_repeat == 3


def test_generic_os_env_vars_do_not_override_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("MODEL", "some-other-model")
    s = load_settings(_write(tmp_path, "recipient: a@b.c\n" + MINIMAL_MCP_YAML))
    assert s.lang == "en"
    assert s.model == "openai/gpt-oss-120b"


def test_repo_config_yaml_is_valid():
    s = load_settings(Path(__file__).parent.parent / "config.yaml")
    assert set(s.mcp.docs.allowlist) == {"find_doc", "create_doc", "update_doc"}
    assert set(s.mcp.gmail.allowlist) == {"list_drafts", "create_draft", "update_draft"}


def test_missing_salt_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("PULSE_ID_SALT")
    monkeypatch.chdir(tmp_path)  # keep a developer's real .env out of this test
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, "recipient: a@b.c\n" + MINIMAL_MCP_YAML))


def test_settings_rejects_window_outside_8_to_12(tmp_path):
    p = _write(tmp_path, "window_weeks: 20\nrecipient: a@b.c\n" + MINIMAL_MCP_YAML)
    with pytest.raises(ValidationError, match="window_weeks"):
        load_settings(p)


def test_settings_rejects_window_below_8(tmp_path):
    p = _write(tmp_path, "window_weeks: 6\nrecipient: a@b.c\n" + MINIMAL_MCP_YAML)
    with pytest.raises(ValidationError, match="window_weeks"):
        load_settings(p)


def test_settings_rejects_max_themes_above_5(tmp_path):
    p = _write(tmp_path, "max_themes: 6\nrecipient: a@b.c\n" + MINIMAL_MCP_YAML)
    with pytest.raises(ValidationError, match="max_themes"):
        load_settings(p)


def test_allowlist_must_be_subset_of_tools(tmp_path):
    yaml = MINIMAL_MCP_YAML.replace(
        "allowlist: [list_drafts, create_draft]", "allowlist: [list_drafts, create_draft, send_message]"
    )
    with pytest.raises(ValidationError, match="send_message"):
        load_settings(_write(tmp_path, "recipient: a@b.c\n" + yaml))


def test_allowlist_rejects_send_actions(tmp_path):
    yaml = MINIMAL_MCP_YAML.replace(
        "      create_draft: {name: create_draft, result: {id: draftId}}\n",
        "      create_draft: {name: create_draft, result: {id: draftId}}\n"
        "      send_email: {name: send_message}\n",
    ).replace("allowlist: [list_drafts, create_draft]", "allowlist: [list_drafts, create_draft, send_email]")
    with pytest.raises(ValidationError, match="send_email"):
        load_settings(_write(tmp_path, "recipient: a@b.c\n" + yaml))


@pytest.mark.parametrize("action", ["delete_doc", "trash_draft", "label_message"])
def test_allowlist_rejects_destructive_actions(tmp_path, action):
    yaml = MINIMAL_MCP_YAML.replace(
        "      create_doc:", f"      {action}: {{name: x}}\n      create_doc:"
    ).replace("allowlist: [find_doc, create_doc]", f"allowlist: [find_doc, create_doc, {action}]")
    with pytest.raises(ValidationError, match=action):
        load_settings(_write(tmp_path, "recipient: a@b.c\n" + yaml))


def test_http_server_requires_url(tmp_path):
    yaml = MINIMAL_MCP_YAML.replace('    url: "https://gmail.example/mcp"\n', "")
    with pytest.raises(ValidationError, match="url"):
        load_settings(_write(tmp_path, "recipient: a@b.c\n" + yaml))


def test_stdio_server_requires_command(tmp_path):
    yaml = MINIMAL_MCP_YAML.replace("    command: npx\n", "")
    with pytest.raises(ValidationError, match="command"):
        load_settings(_write(tmp_path, "recipient: a@b.c\n" + yaml))
