# Groww Weekly Review Pulse

Turns the last 8–12 weeks of public Google Play reviews of Groww into a ≤ 250-word weekly pulse
(top 3 themes, 3 verbatim quotes, 3 action ideas), published to Google Docs and a Gmail draft via MCP.

Design: [`doc/architecture.md`](doc/architecture.md) · Plan: [`doc/implementation-plan.md`](doc/implementation-plan.md)

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
python -m spacy download en_core_web_sm   # local NER model used by the PII scrubber
cp .env.example .env
```

Then fill in `.env`:

- `GROQ_API_KEY`: your Groq API key, from [console.groq.com/keys](https://console.groq.com/keys).
- `PULSE_ID_SALT`: a random secret used to hash review ids, so stored ids can't be traced back to Play reviews.
  Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`.
  Keep it stable: changing it changes every review id and breaks week-over-week matching and idempotent re-runs.
- `GMAIL_MCP_AUTH` (optional): the auth header value for an HTTP Gmail MCP server, if yours needs one.

Set `recipient` in `config.yaml` to the address the weekly draft is addressed to.

## MCP connectors (Google Docs and Gmail)

The pipeline never talks to Google APIs and holds no Google credentials. It publishes through two MCP servers that
you choose and configure under `mcp:` in `config.yaml`:

1. **Pick the servers.** Any Google Docs MCP server and any Gmail MCP server that can search/create/update docs and
   list/create/update drafts will do. Each one is either
   - `transport: stdio`: set `command` and `args` (e.g. `npx -y <server-package>`); the server runs as a child
     process during the publish step, or
   - `transport: http`: set `url`, and `header_env` to map a header name to the *name* of an environment variable
     holding its value (e.g. `Authorization: GMAIL_MCP_AUTH`). Tokens never go in `config.yaml`.
2. **Map the tools.** The code only calls logical actions (`find_doc`, `create_doc`, `update_doc`, `list_drafts`,
   `create_draft`, `update_draft`). Under `tools:`, map each one to the server's real tool `name`, rename our
   argument keys to its parameter names with `args`, and say where the id/url sit in its result with `result`.
   On connect, the run stops before any write if a mapped tool doesn't exist or a required parameter isn't
   mapped; the error lists the tools the server does offer, so mismatches are quick to fix.
3. **Authorize once, inside the MCP server.** Each server does its own Google OAuth (usually a browser consent on
   first start). The pipeline never sees the token.
4. **Keep it draft-only.** `allowlist:` lists the actions the run may call. Send, delete, trash and label actions
   are rejected by config validation and blocked in code, so the Gmail step can only create or update a draft.

## Usage

```bash
pulse run --dry-run                 # this week: ingest -> normalize -> themes -> pulse; writes data/runs/<week>/
pulse run                           # the same, then publishes the Google Doc and the Gmail draft
pulse run --week 2026-W43           # a specific ISO week (YYYY-Www)
pulse run --stage pulse             # resume from a stage: ingest, normalize, themes, pulse, publish
pulse publish --week 2026-W43       # publish an already validated pulse.md (same as run --stage publish)
pulse agent                         # the same steps, chosen by a Groq model (see Agent mode)
pytest                              # run the test suite
```

Each run prints one line, e.g. `week=2026-W43 status=ok doc_url=... draft_id=... flags=-`, and exits with
**0** for `ok` / `dry_run`, **2** for `partial` (the Doc or the draft could not be written; the other was),
and **1** for `failed` (nothing was published; the reason is printed and stored in `run.json`).

What a run guarantees:

- **Idempotent per week.** The ISO week is in the Doc title (`Groww Weekly Pulse — 2026-W43`) and the email
  subject (`[Groww Pulse] 2026-W43 — weekly review pulse`); a re-run updates the same Doc and draft.
- **Only validated content is published.** `pulse.md` is written only when every validator passes (3 quotes
  verbatim from reviews, 3 actions, ≤ 250 words, no PII, no URLs/ids); the publish step reads it from disk and
  refuses to run otherwise. With fewer than 3 themes in the data the pulse is reduced and flagged `few_themes`.
- **Resilient.** Play Store errors are retried, then the latest raw snapshot is used (flag `source_fallback`).
  A sparse window widens from 10 to 12 weeks (flags `window_widened`, `low_volume`). If the Docs server fails,
  the draft still carries the full pulse (`partial`).
- **Auditable.** `data/runs/<week>/run.json` records counts, flags, the Groq model used, prompt hashes, the
  validator result, `doc_url`, `draft_id` and every MCP call (tool and argument names only, never values).

The stages can also be run one at a time without the CLI: `python -m groww_pulse.ingest` downloads a raw
snapshot, and `python -m groww_pulse.stages {normalize,themes,pulse,all}` runs the stored-data stages.

`data/` is gitignored. Raw snapshots may contain user names and are purged after 14 days;
only PII-scrubbed reviews are stored in `data/reviews.sqlite`.

## Agent mode (`pulse agent`)

```bash
pulse agent                                   # this week: check the run report, run what's missing, publish
pulse agent --week 2026-W43 --goal "The Doc failed last time; re-publish it and fix the draft link"
```

`pulse agent` runs the same pipeline, but a Groq model (`openai/gpt-oss-120b`) decides **which step to run
next** through tool calling. It reads the week's run report, reuses completed stages, runs the missing ones,
publishes the Doc and then the draft, and ends with a 3-line summary plus the usual status line and exit code
(0 ok, 2 partial, 1 failed). Every step is recorded in `data/runs/<week>/run.json` under `agent_trace`.

**When to use it:** interactive or recovery runs, e.g. after a `partial` run, to re-publish, or to have the
model explain what happened. The scheduled weekly job keeps using the deterministic `pulse run`.

**Guardrails** (enforced in code, not in the prompt):

- The model never sees raw MCP tools. It has five tools — `get_run_report`, `run_stage`, `get_pulse`,
  `publish_doc`, `upsert_gmail_draft` — that take only the week (and a stage name), and only the run's own week.
- The write tools publish the validated `pulse.md` from disk and refuse until validation has passed, so the
  model can't change the content, add PII, or publish an unvalidated pulse. Gmail stays draft-only.
- At most 12 model steps, and the same call at most 3 times in a row; hitting a limit ends the run as `failed`.
- The status comes from the tool results, never from the model's own text.
- If Groq is unavailable, the agent stops and tells you to run `pulse run` instead.

## Scheduling (Mondays 09:00 IST)

- **Windows:** `.\scripts\schedule_windows.ps1` registers a Task Scheduler job that runs `pulse run` from the repo
  every Monday at 09:00 IST (converted to the machine's local time). Add `-DryRun` to only write `pulse.md`, or
  `-Unregister` to remove it. Output is appended to `data/scheduled-run.log`.
- **GitHub Actions:** `.github/workflows/weekly-pulse.yml` runs on `cron: "30 3 * * 1"` (09:00 IST) or by hand.
  Add the repository secrets `GROQ_API_KEY`, `PULSE_ID_SALT` (and `GMAIL_MCP_AUTH` if used). It runs with
  `--dry-run` and uploads `data/runs/` as an artifact until you set the repository variable `PULSE_PUBLISH=true`.
  Only the scrubbed SQLite database is cached between runs; raw snapshots are never stored.
