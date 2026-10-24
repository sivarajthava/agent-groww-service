"""The `pulse` command line: `pulse run`, `pulse publish` and `pulse agent`.

Exit codes: 0 for ok / dry_run, 2 for partial (Doc or draft missing), 1 for failed (nothing published).
Output is one summary line of status, links and flags; it never prints review text.
"""

import asyncio
from datetime import date
from pathlib import Path

import typer

from groww_pulse.agent.loop import run_agent
from groww_pulse.agent.tools import AgentTools
from groww_pulse.config import Settings, load_settings
from groww_pulse.llm import GroqClient
from groww_pulse.publish.mcp_client import McpGateway
from groww_pulse.runner import STAGES, RunContext, RunDeps, RunResult, run_pipeline
from groww_pulse.weeks import current_iso_week, week_window

# Plain-text help: Windows consoles default to cp1252, which can't print Rich's box drawing or arrows.
app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode=None,
                  pretty_exceptions_enable=False, help="Groww weekly review pulse.")
EXIT_CODES = {"ok": 0, "dry_run": 0, "partial": 2, "failed": 1}


def _week(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        week_window(value, 1)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not an ISO week; expected YYYY-Www, e.g. 2026-W43") from None
    return value


def _stage(value: str) -> str:
    if value not in STAGES:
        raise typer.BadParameter(f"{value!r} is not a stage; expected one of {', '.join(STAGES)}")
    return value


ConfigOpt = typer.Option(Path("config.yaml"), "--config", help="Path to config.yaml.")


def _settings(config: Path) -> Settings:
    from dotenv import load_dotenv

    load_dotenv()  # GROQ_API_KEY and MCP header secrets for the SDKs; Settings reads PULSE_ID_SALT itself
    try:
        return load_settings(config)
    except Exception as e:  # missing file, bad YAML, failed validation
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(1) from None


def _run(week: str, dry_run: bool, from_stage: str, config: Path) -> None:
    settings = _settings(config)
    result = run_pipeline(settings, week, dry_run=dry_run, from_stage=from_stage)
    typer.echo(_summary(result))
    if result.error:
        typer.echo(f"error: {result.error}", err=True)
    raise typer.Exit(EXIT_CODES[result.status])


def _summary(r: RunResult) -> str:
    return (f"week={r.week} status={r.status} doc_url={r.doc_url or '-'} draft_id={r.draft_id or '-'} "
            f"flags={','.join(r.flags) or '-'}")


@app.command("run")
def run_cmd(
    week: str = typer.Option(None, "--week", callback=_week, help="ISO week YYYY-Www; default: the current week."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Write pulse.md and run.json, publish nothing."),
    stage: str = typer.Option("ingest", "--stage", callback=_stage,
                              help=f"Resume from this stage: {', '.join(STAGES)}."),
    config: Path = ConfigOpt,
) -> None:
    """Run the pipeline for a week: ingest, normalize, themes, pulse, publish."""
    _run(week or current_iso_week(date.today()), dry_run, stage, config)


@app.command("publish")
def publish_cmd(
    week: str = typer.Option(..., "--week", callback=_week, help="ISO week YYYY-Www."),
    config: Path = ConfigOpt,
) -> None:
    """Publish the week's validated pulse.md to Google Docs and a Gmail draft (same as run --stage publish)."""
    _run(week, False, "publish", config)


@app.command("agent")
def agent_cmd(
    week: str = typer.Option(None, "--week", callback=_week, help="ISO week YYYY-Www; default: the current week."),
    goal: str = typer.Option(None, "--goal", help="What to do, in words; default: produce and publish the pulse."),
    config: Path = ConfigOpt,
) -> None:
    """Let the Groq model drive the pipeline tools (bounded, validation-gated publishing)."""
    settings = _settings(config)
    week = week or current_iso_week(date.today())
    # Stage calls use the pipeline's reasoning effort; the loop passes settings.agent.reasoning_effort itself.
    llm = GroqClient(settings.model, settings.fallback_model, settings.reasoning_effort, settings.temperature)
    ctx = RunContext(settings, week, RunDeps(llm=llm))  # stages run by the agent use the same Groq client
    m = settings.mcp
    tools = AgentTools(ctx, lambda name: McpGateway(getattr(m, name), name, m.timeout_s))
    result = asyncio.run(run_agent(ctx, llm, tools, goal))
    typer.echo(result.final_message)
    typer.echo(f"week={result.week} status={result.status} doc_url={result.doc_url or '-'} "
               f"draft_id={result.draft_id or '-'} steps={result.steps}"
               + (f" reason={result.reason}" if result.reason else ""))
    raise typer.Exit(EXIT_CODES[result.status])


if __name__ == "__main__":
    app()
