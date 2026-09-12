#!/usr/bin/env python3
"""Run eval reports from GitHub Actions via the agent-runtimes API.

This action talks to the Datalayer platform through the ``agent-runtimes``
client (``AgentClient``) and the ``agent-runtimes`` eval-report helpers
directly (no CLI subprocess), so the generated reports include the full
structured failure diagnostics that the report engine renders (per-run failure
causes, stages, types and detail excerpts). The action also aggregates those
failures into the GitHub step summary and exposes them as action outputs.

For ``execution_target=local``, runtime lifecycle/debug operations are expected
to be handled with the ``agent-runtimes`` CLI.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from agent_runtimes.client import AgentClient
from agent_runtimes.evals.remote import (
    average_latest_pass_rate,
    build_eval_report,
    collect_report_failures,
    benchmark_url,
    execute_evalset_spec,
    load_evalset_spec,
    make_client,
    now_iso,
    render_eval_report_markdown,
    timestamp_slug,
    write_eval_report_csv,
)


def _comparison_url(evalset_id: str, launch_ids: Sequence[str] = ()) -> str:
    """The cross-agentspec comparison of a benchmark (B6-03, B5-08).

    Composed from `benchmark_url` rather than built from a base of its own:
    where the product lives is `agent_runtimes.evals.links`' business, and two
    answers to that question is how a link starts pointing at the wrong
    deployment.

    The launches are named when they are known — an execution knows the ones it
    just submitted — and left off otherwise, in which case the page compares the
    two newest launches of the benchmark.
    """
    page = benchmark_url(evalset_id)
    if not page:
        return ""
    named = [str(item).strip() for item in launch_ids if str(item or "").strip()]
    if not named:
        return f"{page}/compare"
    return f"{page}/compare?launches={quote(','.join(named), safe=',')}"


def as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(raw: str) -> list[str]:
    values: list[str] = []
    for token in (raw or "").split(","):
        value = token.strip()
        if value and value not in values:
            values.append(value)
    return values


def parse_request_timeout_seconds(raw: str, default: int = 180) -> int:
    """Parse the request-timeout-seconds input. Returns default on bad values.

    The value is the per-agent-call timeout in seconds and is clamped to a
    minimum of 1 second.
    """
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = int(float(text))
    except ValueError:
        return default
    return max(1, value)


def git_context(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Where this launch comes from, read from what Actions sets (B6-02).

    The commit, the ref, the repository, the action run, and the pull
    request when there is one — from the event payload when it names it,
    from a `refs/pull/N/...` ref otherwise. Empty outside Actions, and the
    runner drops empty values, so a launch made by hand carries nothing.
    """
    env = environ if environ is not None else os.environ
    ref = str(env.get("GITHUB_REF") or "").strip()
    pr_number = ""
    event_path = str(env.get("GITHUB_EVENT_PATH") or "").strip()
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            number = (event.get("pull_request") or {}).get("number") if isinstance(event, dict) else None
            if number is not None:
                pr_number = str(number)
        except (OSError, ValueError):
            pr_number = ""
    if not pr_number:
        match = re.match(r"^refs/pull/(\d+)/", ref)
        if match:
            pr_number = match.group(1)
    server = str(env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
    run_id = str(env.get("GITHUB_RUN_ID") or "").strip()
    return {
        "sha": str(env.get("GITHUB_SHA") or "").strip(),
        "ref": ref,
        "repository": repository,
        "run_id": run_id,
        "pr_number": pr_number,
        "url": f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else "",
    }


def parse_gate(raw: str, name: str) -> float | None:
    """A gate input: a fraction in [0, 1], or nothing. Anything else is refused."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as error:
        raise ValueError(f"{name} must be a fraction between 0 and 1, got {text!r}") from error
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a fraction between 0 and 1, got {text!r}")
    return value


def quality_gate(
    report: Mapping[str, Any], *, pass_rate_threshold: float | None, max_regression: float | None
) -> tuple[str, list[str]]:
    """The gates' verdict from the scores (B6-05): `passed`, `failed`, or
    `skipped` when no gate is set; and the reasons, one per experiment that
    failed one. An experiment with no scored run does not pass a gate — a
    missing score is not a passing one."""
    if pass_rate_threshold is None and max_regression is None:
        return "skipped", []
    reasons: list[str] = []
    for experiment in report.get("experiments") or []:
        if not isinstance(experiment, dict):
            continue
        name = str(experiment.get("name") or experiment.get("id") or "experiment")
        latest = experiment.get("latest_pass_rate")
        drift = experiment.get("drift_delta")
        if pass_rate_threshold is not None:
            if not isinstance(latest, (int, float)):
                reasons.append(f"{name}: no scored run to hold to the pass-rate threshold")
            elif float(latest) < pass_rate_threshold:
                reasons.append(f"{name}: latest pass rate {float(latest):.1%} is below the threshold {pass_rate_threshold:.1%}")
        if max_regression is not None and isinstance(drift, (int, float)) and float(drift) < -max_regression:
            reasons.append(f"{name}: drift {float(drift):+.1%} exceeds the allowed regression of {max_regression:.1%}")
    return ("failed" if reasons else "passed"), reasons


def append_github_output(key: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT", "")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as stream:
        stream.write(f"{key}={value}\n")


def append_step_summary(text: str) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as stream:
        stream.write(text)


def _resolve_evalset_id(
    client: AgentClient,
    *,
    explicit_evalset_id: str,
    spec_file: str,
    billing_entity_uid: str,
    account_uid: str,
) -> str:
    """Return an evalset id, creating it from a spec file when needed."""
    evalset_id = explicit_evalset_id.strip()
    if evalset_id:
        return evalset_id

    spec_path = spec_file.strip()
    if not spec_path:
        raise ValueError("Provide evalset-id or evalset-spec-file.")

    spec = load_evalset_spec(spec_path)
    spec_name = str(spec.get("name") or "").strip() or "evalset"
    spec["name"] = f"{spec_name}-{timestamp_slug(now_iso())}"
    payload = client.evals_create_eval_from_spec(
        spec=spec,
        billing_entity_uid=billing_entity_uid or None,
        account_uid=account_uid or None,
    )
    created_id = str(((payload.get("evalset") or {}).get("id") or "")).strip()
    if not created_id:
        raise ValueError(f"Evalset create response did not contain an id: {payload}")
    return created_id


def _report_is_partial(report: dict[str, Any]) -> bool:
    experiments = [
        item for item in (report.get("experiments") or []) if isinstance(item, dict)
    ]
    if not experiments:
        return True
    for experiment in experiments:
        runs = [item for item in (experiment.get("runs") or []) if isinstance(item, dict)]
        if not runs:
            return True
    return False


def _partial_report_reason(report: dict[str, Any]) -> str:
    experiments = [
        item for item in (report.get("experiments") or []) if isinstance(item, dict)
    ]
    if not experiments:
        return "no experiments in report"

    empty_runs = 0
    for experiment in experiments:
        runs = [item for item in (experiment.get("runs") or []) if isinstance(item, dict)]
        if not runs:
            empty_runs += 1
    if empty_runs:
        return f"{empty_runs}/{len(experiments)} experiments have no runs"
    return "unknown partial state"


def _generate_report(
    client: AgentClient,
    *,
    evalset_id: str,
    billing_entity_uid: str,
    account_uid: str,
    run_limit: int,
    output_markdown: str,
    export_csv: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build and persist a report using the core eval-report helpers."""
    report = build_eval_report(
        client,
        evalset_id,
        run_limit=run_limit,
        billing_entity_uid=billing_entity_uid or None,
        account_uid=account_uid or None,
    )

    report_path = Path(output_markdown)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    markdown = render_eval_report_markdown(report, run_limit=run_limit, colorize=False)
    report_path.write_text(markdown + "\n", encoding="utf-8")

    # A log artifact carrying the full structured report (including every
    # run-level failure_cause) so failures are never lost in CI.
    log_path = report_path.with_suffix(report_path.suffix + ".log")
    log_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    csv_out = ""
    if export_csv:
        csv_path = report_path.with_name(report_path.stem + ".csv")
        write_eval_report_csv(report, csv_path)
        csv_out = str(csv_path)

    generated_at = report.get("generated_at")
    timestamp = timestamp_slug(str(generated_at or now_iso()))
    timestamped_md = Path(f"report-{timestamp}.md")
    timestamped_md.write_text(markdown + "\n", encoding="utf-8")
    timestamped_csv = Path(f"report-{timestamp}.csv")
    write_eval_report_csv(report, timestamped_csv)

    outputs = {
        "report_file": str(report_path),
        "csv_file": csv_out,
        "log_file": str(log_path),
        "timestamped_report_file": str(timestamped_md),
        "timestamped_csv_file": str(timestamped_csv),
    }
    return report, outputs


def _write_comparison_summary(
    *,
    path: Path,
    primary_label: str,
    secondary_label: str,
    primary_report: dict[str, Any],
    secondary_report: dict[str, Any],
    primary_comparison_url: str = "",
    secondary_comparison_url: str = "",
) -> None:
    primary_avg = average_latest_pass_rate(primary_report)
    secondary_avg = average_latest_pass_rate(secondary_report)
    primary_failures = collect_report_failures(primary_report)
    secondary_failures = collect_report_failures(secondary_report)

    lines: list[str] = []
    lines.append("# Evals Comparison Summary")
    lines.append("")
    lines.append(f"- Primary: {primary_label}")
    lines.append(f"- Secondary: {secondary_label}")
    lines.append("")
    lines.append("| Group | Avg latest pass rate | Failed runs |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| Primary | {f'{primary_avg * 100:.1f}%' if primary_avg is not None else 'n/a'} "
        f"| {primary_failures['failed_run_count']} |"
    )
    lines.append(
        f"| Secondary | {f'{secondary_avg * 100:.1f}%' if secondary_avg is not None else 'n/a'} "
        f"| {secondary_failures['failed_run_count']} |"
    )
    if primary_avg is not None and secondary_avg is not None:
        delta = secondary_avg - primary_avg
        lines.append(f"| Delta (Secondary - Primary) | {delta * 100:+.1f} pts | |")
    lines.append("")
    # Where to read each side in the product (B6-03): the file a reviewer is
    # handed should not make them go and find the pages themselves.
    if primary_comparison_url or secondary_comparison_url:
        lines.append("Compare in Datalayer:")
        if primary_comparison_url:
            lines.append(f"- Primary: {primary_comparison_url}")
        if secondary_comparison_url:
            lines.append(f"- Secondary: {secondary_comparison_url}")
        lines.append("")
    lines.append("Notes:")
    lines.append("- Use the same eval cases in both specs.")
    lines.append("- Keep only one controlled variable between primary and secondary.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_failure_summary(label: str, report: dict[str, Any]) -> int:
    """Render an aggregated failure section into the step summary. Returns count."""
    aggregate = collect_report_failures(report)
    failed = int(aggregate["failed_run_count"])
    append_step_summary(f"### {label} failures\n\n")
    if failed == 0:
        append_step_summary("- No failed runs detected.\n\n")
        return 0

    append_step_summary(f"- Failed runs: {failed}\n")
    type_counts = aggregate["type_counts"]
    if type_counts:
        breakdown = ", ".join(
            f"{ftype} ({count})"
            for ftype, count in sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        append_step_summary(f"- Failure types: {breakdown}\n")
    append_step_summary("\n")
    append_step_summary("| Experiment | Run ID | Status | Stage | Type | Message | Detail |\n")
    append_step_summary("|---|---|---|---|---|---|---|\n")
    for failure in aggregate["failures"]:
        message = str(failure["message"]).replace("|", "\\|")
        detail = str(failure["detail_excerpt"]).replace("|", "\\|")
        append_step_summary(
            f"| {failure['experiment']} | {failure['run_id']} | {failure['status']} "
            f"| {failure['stage']} | {failure['type']} | {message} | {detail} |\n"
        )
    append_step_summary("\n")
    return failed


def _prepare_lane_spec(
    *,
    source_path: str,
    run_environment: str,
    output_dir: str,
    output_file: str,
) -> Path:
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"Evalset spec file not found: {source}")

    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["run_environment"] = run_environment

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if output_file.strip():
        out_path = out_dir / output_file.strip()
    else:
        base = source.name.removesuffix(".evalset.json").removesuffix(".json")
        out_path = out_dir / f"{base}-{run_environment}.evalset.json"

    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path


def _run_prepare_spec_mode() -> int:
    evalset_spec_file = os.getenv("INPUT_EVALSET_SPEC_FILE", "").strip()
    run_environment = os.getenv("INPUT_RUN_ENVIRONMENT", "sdk").strip() or "sdk"
    output_dir = os.getenv("INPUT_PREPARED_SPEC_OUTPUT_DIR", "artifacts/specs").strip() or "artifacts/specs"
    output_file = os.getenv("INPUT_PREPARED_SPEC_OUTPUT_FILE", "").strip()

    if not evalset_spec_file:
        print("prepare-spec requires evalset-spec-file", file=sys.stderr)
        return 2

    try:
        prepared = _prepare_lane_spec(
            source_path=evalset_spec_file,
            run_environment=run_environment,
            output_dir=output_dir,
            output_file=output_file,
        )
    except Exception as exc:
        print(f"Failed to prepare evalset spec: {exc}", file=sys.stderr)
        append_step_summary("## Datalayer Evals Report\n\n")
        append_step_summary(f"- Error: `Failed to prepare evalset spec: {exc}`\n")
        return 1

    append_github_output("prepared_spec_path", str(prepared))
    append_github_output("spec_path", str(prepared))

    append_step_summary("## Datalayer Evals Report\n\n")
    append_step_summary("- Mode: prepare-spec\n")
    append_step_summary(f"- Lane: {run_environment}\n")
    append_step_summary(f"- Prepared evalset spec: {prepared}\n\n")
    return 0


def _run_execute_runs_mode() -> int:
    api_key = os.getenv("INPUT_API_KEY", "").strip()
    evalset_spec_file = os.getenv("INPUT_EVALSET_SPEC_FILE", "").strip()
    ai_agents_url = os.getenv("INPUT_AI_AGENTS_URL", "").strip()
    billing_entity_uid = os.getenv("INPUT_BILLING_ENTITY_UID", "").strip()
    account_uid = os.getenv("INPUT_ACCOUNT_UID", "").strip()
    run_limit_raw = os.getenv("INPUT_RUN_LIMIT", "50").strip() or "50"
    iam_url = os.getenv("INPUT_IAM_URL", "").strip()
    runtimes_url = os.getenv("INPUT_RUNTIMES_URL", "").strip()
    run_environment = os.getenv("INPUT_RUN_ENVIRONMENT", "sdk").strip() or "sdk"
    agent_environment_name = os.getenv("INPUT_AGENT_ENVIRONMENT_NAME", "ai-agents-env").strip() or "ai-agents-env"
    execution_target = os.getenv("INPUT_EXECUTION_TARGET", "cloud").strip().lower() or "cloud"
    auto_start_local_agent_runtime = as_bool(
        os.getenv("INPUT_AUTO_START_LOCAL_AGENT_RUNTIME", "false")
    )
    local_agent_base_url = os.getenv("INPUT_LOCAL_AGENT_BASE_URL", "").strip()
    local_agent_name = os.getenv("INPUT_LOCAL_AGENT_NAME", "").strip()
    agent_spec_ids = parse_csv(os.getenv("INPUT_AGENT_SPEC_IDS", "").strip())
    request_timeout_seconds = parse_request_timeout_seconds(
        os.getenv("INPUT_REQUEST_TIMEOUT_SECONDS", "180")
    )
    concurrency_raw = os.getenv("INPUT_CONCURRENCY", "4").strip() or "4"
    budget_raw = os.getenv("INPUT_BUDGET", "").strip()

    if not api_key:
        print("Missing required input: api-key", file=sys.stderr)
        return 2
    if not evalset_spec_file:
        print("execute-runs requires evalset-spec-file", file=sys.stderr)
        return 2
    try:
        concurrency = max(1, int(concurrency_raw))
    except ValueError:
        print(f"execute-runs concurrency must be a whole number, got {concurrency_raw!r}", file=sys.stderr)
        return 2
    budget: float | None = None
    if budget_raw:
        try:
            budget = float(budget_raw)
        except ValueError:
            print(f"execute-runs budget must be a number of credits, got {budget_raw!r}", file=sys.stderr)
            return 2
    if not agent_spec_ids:
        print("execute-runs requires agentspec-ids", file=sys.stderr)
        return 2
    if execution_target not in {"cloud", "local"}:
        print("execute-runs execution-target must be 'cloud' or 'local'", file=sys.stderr)
        return 2

    client = make_client(
        api_key=api_key,
        ai_agents_url=ai_agents_url,
        iam_url=iam_url,
        runtimes_url=runtimes_url,
    )

    try:
        execution = _execute_eval_runs(
            client=client,
            evalset_spec_file=evalset_spec_file,
            agent_spec_ids=agent_spec_ids,
            run_limit_raw=run_limit_raw,
            run_environment=run_environment,
            agent_environment_name=agent_environment_name,
            execution_target=execution_target,
            auto_start_local_agent_runtime=auto_start_local_agent_runtime,
            local_agent_base_url=local_agent_base_url,
            local_agent_name=local_agent_name,
            billing_entity_uid=billing_entity_uid,
            account_uid=account_uid,
            request_timeout_seconds=request_timeout_seconds,
            concurrency=concurrency,
            budget=budget,
        )
    except Exception as exc:
        message = f"Failed to execute eval runs: {exc}"
        print(message, file=sys.stderr)
        append_step_summary("## Datalayer Evals Report\n\n")
        append_step_summary(f"- Error: `{message}`\n")
        return 1

    executed_evalset_id = str(execution.get("evalset_id") or "")
    live_report_url = str(execution.get("view_url") or benchmark_url(executed_evalset_id))
    append_github_output("executed_evalset_id", executed_evalset_id)
    append_github_output("evalset_id", executed_evalset_id)
    append_github_output("live_report_url", live_report_url)
    # Where several agentspecs are compared against each other (B6-03): the
    # launches this execution submitted, named, so the link is this run's
    # comparison rather than whatever is newest by the time somebody opens it.
    launch_ids = [str(item) for item in (execution.get("launch_ids") or []) if str(item or "").strip()]
    experiments = [str(item) for item in (execution.get("experiment_ids") or []) if str(item or "").strip()]
    comparison_url = (
        _comparison_url(executed_evalset_id, launch_ids)
        if len(agent_spec_ids) > 1 or len(experiments) > 1 or len(launch_ids) > 1
        else ""
    )
    append_github_output("comparison_url", comparison_url)
    append_github_output("secondary_comparison_url", "")

    append_step_summary("## Datalayer Evals Report\n\n")
    # The first line is where to read it (B6-01).
    append_step_summary(f"**Live report:** {live_report_url}\n\n")
    if comparison_url:
        append_step_summary(f"**Comparison:** {comparison_url}\n\n")
    append_step_summary("- Mode: execute-runs\n")
    append_step_summary(f"- Lane: {run_environment}\n")
    append_step_summary(f"- Execution target: {execution_target}\n")
    if execution_target == "cloud":
        append_step_summary(f"- Concurrency: {concurrency}\n")
        append_step_summary(f"- Budget: {'none' if budget is None else f'{budget:g} credits'}\n")
    if execution_target == "local":
        append_step_summary(
            f"- Local runtime endpoint: {local_agent_base_url or 'http://127.0.0.1:8765'}\n"
        )
        append_step_summary(
            "- Local runtime CLI: `agent-runtimes serve --port 8765 --find-free-port`\n"
        )
    append_step_summary(f"- Executed evalset id: {executed_evalset_id}\n")
    append_step_summary(f"- Agentspec ids: {', '.join(agent_spec_ids)}\n")
    launch_ids = [str(item) for item in (execution.get("launch_ids") or [])]
    if launch_ids:
        append_step_summary(f"- Launches: {', '.join(launch_ids)}\n")
    append_step_summary("\n")

    return 0


def _execute_eval_runs(
    *,
    client: AgentClient,
    evalset_spec_file: str,
    agent_spec_ids: list[str],
    run_limit_raw: str,
    run_environment: str,
    agent_environment_name: str,
    execution_target: str,
    auto_start_local_agent_runtime: bool,
    local_agent_base_url: str,
    local_agent_name: str,
    billing_entity_uid: str,
    account_uid: str,
    request_timeout_seconds: int,
    concurrency: int = 4,
    budget: float | None = None,
) -> dict[str, Any]:
    """The runner's answer, whole: the evalset it made, the launches it
    submitted and where to read them (`view_url`, B6-01)."""
    try:
        execution_run_limit = max(1, int(run_limit_raw))
    except ValueError:
        execution_run_limit = 1

    spec = load_evalset_spec(evalset_spec_file)
    exec_kwargs: dict[str, Any] = {
        "spec": spec,
        "agentspec_ids": agent_spec_ids,
        "run_limit": execution_run_limit,
        "run_environment": run_environment,
        "environment_name": agent_environment_name,
        "local_agent_base_url": local_agent_base_url or None,
        "auto_start_local_agent_runtime": bool(auto_start_local_agent_runtime),
        "billing_entity_uid": billing_entity_uid or None,
        "account_uid": account_uid or None,
        "launch_source": "datalayer-github-actions",
        "execution_target": execution_target,
        "request_timeout_seconds": request_timeout_seconds,
        # A cloud execution is a launch the platform runs (BENCHMARK.md,
        # B2-15): how many sandboxes per experiment, and what it may spend.
        "concurrency": concurrency,
        "credits_limit": budget,
        "log": print,
    }

    # The runner names the local agent `agent_name`. A filter used to drop
    # any keyword the installed runner did not know, which is how this input
    # went unused for months without a word; the action now pins a runner
    # that takes every keyword it sends, and a mismatch fails loudly.
    if local_agent_name:
        exec_kwargs["agent_name"] = local_agent_name
    exec_kwargs = {key: value for key, value in exec_kwargs.items() if value is not None}

    # Where the launch comes from rides on it (B6-02); empty outside Actions.
    exec_kwargs["git"] = git_context()

    execution = execute_evalset_spec(client, **exec_kwargs)
    executed_evalset_id = str(execution.get("evalset_id") or "").strip()
    if not executed_evalset_id:
        raise RuntimeError("Runner did not return an evalset id.")
    return dict(execution)


def main() -> int:
    mode = os.getenv("INPUT_MODE", "run-report").strip().lower() or "run-report"

    if mode == "prepare-spec":
        return _run_prepare_spec_mode()
    if mode == "execute-runs":
        return _run_execute_runs_mode()
    if mode != "run-report":
        print(f"Unsupported mode: {mode}", file=sys.stderr)
        return 2

    evalset_id = os.getenv("INPUT_EVALSET_ID", "").strip()
    evalset_spec_file = os.getenv("INPUT_EVALSET_SPEC_FILE", "").strip()
    secondary_evalset_id = os.getenv("INPUT_SECONDARY_EVALSET_ID", "").strip()
    secondary_evalset_spec_file = os.getenv("INPUT_SECONDARY_EVALSET_SPEC_FILE", "").strip()
    api_key = os.getenv("INPUT_API_KEY", "").strip()
    ai_agents_url = os.getenv("INPUT_AI_AGENTS_URL", "").strip()
    billing_entity_uid = os.getenv("INPUT_BILLING_ENTITY_UID", "").strip()
    account_uid = os.getenv("INPUT_ACCOUNT_UID", "").strip()
    run_limit_raw = os.getenv("INPUT_RUN_LIMIT", "50").strip() or "50"
    output_markdown = os.getenv("INPUT_OUTPUT_MARKDOWN", "evals-report.md").strip() or "evals-report.md"
    secondary_output_markdown = os.getenv("INPUT_SECONDARY_OUTPUT_MARKDOWN", "").strip()
    comparison_summary_output = os.getenv("INPUT_COMPARISON_SUMMARY_OUTPUT", "").strip()
    export_csv = as_bool(os.getenv("INPUT_EXPORT_CSV", "true"))
    iam_url = os.getenv("INPUT_IAM_URL", "").strip()
    runtimes_url = os.getenv("INPUT_RUNTIMES_URL", "").strip()

    if not api_key:
        print("Missing required input: api-key", file=sys.stderr)
        return 2
    try:
        pass_rate_threshold = parse_gate(os.getenv("INPUT_PASS_RATE_THRESHOLD", ""), "pass-rate-threshold")
        max_regression = parse_gate(os.getenv("INPUT_MAX_REGRESSION", ""), "max-regression")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        run_limit = max(1, min(200, int(run_limit_raw)))
    except ValueError:
        run_limit = 50

    client = make_client(
        api_key=api_key,
        ai_agents_url=ai_agents_url,
        iam_url=iam_url,
        runtimes_url=runtimes_url,
    )

    executed_evalset_id = ""

    try:
        resolved_evalset_id = _resolve_evalset_id(
            client,
            explicit_evalset_id=evalset_id,
            spec_file=evalset_spec_file,
            billing_entity_uid=billing_entity_uid,
            account_uid=account_uid,
        )
    except Exception as exc:
        message = f"Failed to resolve primary evalset: {exc}"
        print(message, file=sys.stderr)
        append_step_summary("## Datalayer Evals Report\n\n")
        append_step_summary(f"- Error: `{message}`\n")
        return 2

    resolved_secondary_evalset_id = ""
    if secondary_evalset_id or secondary_evalset_spec_file:
        try:
            resolved_secondary_evalset_id = _resolve_evalset_id(
                client,
                explicit_evalset_id=secondary_evalset_id,
                spec_file=secondary_evalset_spec_file,
                billing_entity_uid=billing_entity_uid,
                account_uid=account_uid,
            )
        except Exception as exc:
            message = f"Failed to resolve secondary evalset: {exc}"
            print(message, file=sys.stderr)
            append_step_summary("## Datalayer Evals Report\n\n")
            append_step_summary(f"- Error: `{message}`\n")
            return 2

    try:
        primary_report, primary_outputs = _generate_report(
            client,
            evalset_id=resolved_evalset_id,
            billing_entity_uid=billing_entity_uid,
            account_uid=account_uid,
            run_limit=run_limit,
            output_markdown=output_markdown,
            export_csv=export_csv,
        )
    except Exception as exc:
        message = f"Failed to generate primary report: {exc}"
        print(message, file=sys.stderr)
        append_step_summary("## Datalayer Evals Report\n\n")
        append_step_summary(f"- Error: `{message}`\n")
        return 1

    # A benchmark with more than one experiment has a cross-agentspec
    # comparison to read (B6-03). No launches are named: this mode reports on
    # runs that already exist, so the page compares the two newest launches.
    comparison_url = (
        _comparison_url(resolved_evalset_id)
        if len((primary_report or {}).get("experiments") or []) > 1
        else ""
    )
    secondary_comparison_url = ""

    secondary_outputs = {
        "report_file": "",
        "csv_file": "",
        "log_file": "",
        "timestamped_report_file": "",
        "timestamped_csv_file": "",
    }
    secondary_report: dict[str, Any] = {}
    comparison_summary_file = ""

    if resolved_secondary_evalset_id:
        if not secondary_output_markdown:
            primary_path = Path(output_markdown)
            secondary_output_markdown = str(
                primary_path.with_name(primary_path.stem + "-secondary" + primary_path.suffix)
            )
        try:
            secondary_report, secondary_outputs = _generate_report(
                client,
                evalset_id=resolved_secondary_evalset_id,
                billing_entity_uid=billing_entity_uid,
                account_uid=account_uid,
                run_limit=run_limit,
                output_markdown=secondary_output_markdown,
                export_csv=export_csv,
            )
        except Exception as exc:
            message = f"Failed to generate secondary report: {exc}"
            print(message, file=sys.stderr)
            append_step_summary("## Datalayer Evals Report\n\n")
            append_step_summary(f"- Error: `{message}`\n")
            return 1

        secondary_comparison_url = (
            _comparison_url(resolved_secondary_evalset_id)
            if len((secondary_report or {}).get("experiments") or []) > 1
            else ""
        )

        summary_path = (
            Path(comparison_summary_output)
            if comparison_summary_output
            else Path(output_markdown).with_name("comparison-summary.md")
        )
        _write_comparison_summary(
            path=summary_path,
            primary_label=resolved_evalset_id,
            secondary_label=resolved_secondary_evalset_id,
            primary_report=primary_report,
            secondary_report=secondary_report,
            primary_comparison_url=comparison_url,
            secondary_comparison_url=secondary_comparison_url,
        )
        comparison_summary_file = str(summary_path)

    primary_failures = collect_report_failures(primary_report)
    secondary_failures = (
        collect_report_failures(secondary_report)
        if resolved_secondary_evalset_id
        else {"failed_run_count": 0}
    )
    total_failed = int(primary_failures["failed_run_count"]) + int(secondary_failures["failed_run_count"])

    live_report_url = benchmark_url(resolved_evalset_id)
    gate_status, gate_reasons = quality_gate(
        primary_report, pass_rate_threshold=pass_rate_threshold, max_regression=max_regression
    )

    append_github_output("live_report_url", live_report_url)
    append_github_output("comparison_url", comparison_url)
    append_github_output("secondary_comparison_url", secondary_comparison_url)
    append_github_output("gate_status", gate_status)
    append_github_output("report_file", primary_outputs["report_file"])
    append_github_output("csv_file", primary_outputs["csv_file"])
    append_github_output("log_file", primary_outputs["log_file"])
    append_github_output("timestamped_report_file", primary_outputs["timestamped_report_file"])
    append_github_output("timestamped_csv_file", primary_outputs["timestamped_csv_file"])
    append_github_output("secondary_report_file", secondary_outputs["report_file"])
    append_github_output("secondary_csv_file", secondary_outputs["csv_file"])
    append_github_output("secondary_log_file", secondary_outputs["log_file"])
    append_github_output("secondary_timestamped_report_file", secondary_outputs["timestamped_report_file"])
    append_github_output("secondary_timestamped_csv_file", secondary_outputs["timestamped_csv_file"])
    append_github_output("comparison_summary_file", comparison_summary_file)
    append_github_output("evalset_id", resolved_evalset_id)
    append_github_output("executed_evalset_id", executed_evalset_id)
    append_github_output("failed_run_count", str(total_failed))
    append_github_output("primary_failed_run_count", str(primary_failures["failed_run_count"]))
    append_github_output("secondary_failed_run_count", str(secondary_failures["failed_run_count"]))

    if primary_outputs["report_file"]:
        append_step_summary("## Datalayer Evals Report\n\n")
        append_step_summary(f"**Live report:** {live_report_url}\n\n")
        if comparison_url:
            # Where several subjects are read against each other (B6-03).
            append_step_summary(f"**Comparison:** {comparison_url}\n\n")
        if gate_status != "skipped":
            append_step_summary(f"- Quality gate: **{gate_status}**\n")
            for reason in gate_reasons:
                append_step_summary(f"  - {reason}\n")
        append_step_summary(f"- Primary evalset: {resolved_evalset_id}\n")
        if executed_evalset_id:
            append_step_summary(f"- Executed evalset (real runs): {executed_evalset_id}\n")
            append_step_summary(f"- Execution target: {execution_target}\n")
        append_step_summary(f"- Primary markdown report: {primary_outputs['report_file']}\n")
        if primary_outputs["csv_file"]:
            append_step_summary(f"- Primary CSV report: {primary_outputs['csv_file']}\n")
        if primary_outputs["timestamped_report_file"]:
            append_step_summary(f"- Primary timestamped markdown: {primary_outputs['timestamped_report_file']}\n")
        if primary_outputs["timestamped_csv_file"]:
            append_step_summary(f"- Primary timestamped CSV: {primary_outputs['timestamped_csv_file']}\n")
        if primary_outputs["log_file"]:
            append_step_summary(f"- Primary report log (full JSON): {primary_outputs['log_file']}\n")
        if resolved_secondary_evalset_id:
            append_step_summary(f"- Secondary evalset: {resolved_secondary_evalset_id}\n")
            append_step_summary(f"- Secondary markdown report: {secondary_outputs['report_file']}\n")
            if secondary_outputs["csv_file"]:
                append_step_summary(f"- Secondary CSV report: {secondary_outputs['csv_file']}\n")
            if comparison_summary_file:
                append_step_summary(f"- Comparison summary: {comparison_summary_file}\n")
        append_step_summary(f"- Total failed runs: {total_failed}\n")
        append_step_summary("\n")

        _append_failure_summary("Primary", primary_report)
        if resolved_secondary_evalset_id:
            _append_failure_summary("Secondary", secondary_report)

    primary_partial = _report_is_partial(primary_report)
    secondary_partial = resolved_secondary_evalset_id and _report_is_partial(secondary_report)
    if primary_partial or secondary_partial:
        reasons: list[str] = []
        if primary_partial:
            reasons.append(f"primary: {_partial_report_reason(primary_report)}")
        if secondary_partial:
            reasons.append(f"secondary: {_partial_report_reason(secondary_report)}")
        reason_text = "; ".join(reasons) if reasons else "missing experiments or runs"
        message = f"Partial results detected ({reason_text}). Failing the action."
        print(message, file=sys.stderr)
        append_step_summary(f"- Error: {message}\n")
        return 1

    if gate_status == "failed":
        # Every output is written above; the gate fails the step, not the report.
        print("Quality gate failed: " + "; ".join(gate_reasons), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
