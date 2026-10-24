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

- `GROQ_API_KEY`: your Groq API key.
- `PULSE_ID_SALT`: a random secret used to hash review ids, so stored ids can't be traced back to Play reviews.
  Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`.
  Keep it stable: changing it changes every review id and breaks week-over-week matching and idempotent re-runs.

## Usage

```bash
python -m groww_pulse.ingest              # download the last 12 weeks of public reviews to data/raw/
python -m groww_pulse.stages normalize    # scrub + normalize the latest snapshot into data/reviews.sqlite
python -m groww_pulse.stages themes       # themes, assignments, quotes -> SQLite, data/runs/<week>/ (needs GROQ_API_KEY)
python -m groww_pulse.stages pulse        # LLM draft + validators -> data/runs/<week>/pulse.md, run.json
python -m groww_pulse.stages all          # the three stages in order; add --week YYYY-Www for another week
pytest                                    # run the test suite
```

Each stage reads what the previous one stored, so any stage can be re-run on its own.
`pulse.md` is written only when every validator passes; otherwise `run.json` lists the failed checks.

`data/` is gitignored. Raw snapshots may contain user names and are purged after 14 days;
only PII-scrubbed reviews are stored in `data/reviews.sqlite`.
