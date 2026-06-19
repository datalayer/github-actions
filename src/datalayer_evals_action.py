#!/usr/bin/env python3
"""Run datalayer evals reports from GitHub Actions via the datalayer-core API.

This action talks to the Datalayer platform through the ``datalayer-core``
Python client and its eval-report helpers directly (no CLI subprocess), so the
generated reports include the full structured failure diagnostics that the core
report renders (per-run failure causes, stages, types and detail excerpts). The
action also aggregates those failures into the GitHub step summary and exposes
them as action outputs.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path
from typing import Any

from datalayer_core.cli.commands.agents import _load_agent_spec
from datalayer_core.evals import (
    average_latest_pass_rate,
    build_eval_report,
    collect_report_failures,
    execute_evalset_spec,
    load_evalset_spec,
    make_client,
    now_iso,
    render_eval_report_markdown,
    timestamp_slug,
    write_eval_report_csv,
)
from datalayer_core.client.client import DatalayerClient
try:
    from datalayer_core.agents import (
        create_cloud_agent_runtime,
        teardown_agent_execution_resources,
    )
except Exception:  # pragma: no cover - compatibility with older datalayer-core
    from datalayer_core.runtimes.agent_runtime import (  # type: ignore
        create_cloud_agent_runtime,
        teardown_agent_execution_resources,
    )


def as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(raw: str) -> list[str]:
    values: list[str] = []
    for token in (raw or "").split(","):
        value = token.strip()
        if value and value not in values:
            values.append(value)
    return values


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


def _create_agent_runtime(
    client: DatalayerClient,
    *,
    environment_name: str,
    given_name: str,
    time_reservation: str,
    agent_spec_id: str,
    agent_spec: str,
    billable_account_uid: str,
) -> tuple[str, str]:
    """Create an agent runtime via the core client. Returns (pod_name, ingress)."""
    if agent_spec_id and agent_spec:
        raise ValueError("Use only one of agentspec-id or agentspec.")

    spec_payload: dict[str, Any] | None = None
    resolved_spec_id: str | None = None
    if agent_spec.strip():
        spec_payload = _load_agent_spec(agent_spec.strip())
    else:
        resolved_spec_id = agent_spec_id.strip() or "example-simple"

    try:
        reservation = int(str(time_reservation).strip() or "10")
    except ValueError:
        reservation = 10

    runtime = create_cloud_agent_runtime(
        client,
        environment_name=environment_name,
        name=given_name.strip() or None,
        agent_spec_id=resolved_spec_id,
        agent_spec=spec_payload,
        time_reservation=reservation,
        billable_account_uid=(billable_account_uid or "").strip() or None,
    )
    return str(runtime.pod_name or ""), str(runtime.ingress or "")


def _resolve_evalset_id(
    client: DatalayerClient,
    *,
    explicit_evalset_id: str,
    spec_file: str,
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
    client: DatalayerClient,
    *,
    evalset_id: str,
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


def main() -> int:
    evalset_id = os.getenv("INPUT_EVALSET_ID", "").strip()
    evalset_spec_file = os.getenv("INPUT_EVALSET_SPEC_FILE", "").strip()
    secondary_evalset_id = os.getenv("INPUT_SECONDARY_EVALSET_ID", "").strip()
    secondary_evalset_spec_file = os.getenv("INPUT_SECONDARY_EVALSET_SPEC_FILE", "").strip()
    api_key = os.getenv("INPUT_API_KEY", "").strip()
    ai_agents_url = os.getenv("INPUT_AI_AGENTS_URL", "").strip()
    account_uid = os.getenv("INPUT_BILLABLE_ACCOUNT_UID", "").strip()
    run_limit_raw = os.getenv("INPUT_RUN_LIMIT", "50").strip() or "50"
    output_markdown = os.getenv("INPUT_OUTPUT_MARKDOWN", "evals-report.md").strip() or "evals-report.md"
    secondary_output_markdown = os.getenv("INPUT_SECONDARY_OUTPUT_MARKDOWN", "").strip()
    comparison_summary_output = os.getenv("INPUT_COMPARISON_SUMMARY_OUTPUT", "").strip()
    export_csv = as_bool(os.getenv("INPUT_EXPORT_CSV", "true"))
    iam_url = os.getenv("INPUT_IAM_URL", "").strip()
    runtimes_url = os.getenv("INPUT_RUNTIMES_URL", "").strip()
    agent_spec_id = os.getenv("INPUT_AGENT_SPEC_ID", "").strip()
    agent_spec_ids_raw = os.getenv("INPUT_AGENT_SPEC_IDS", "").strip()
    agent_spec = os.getenv("INPUT_AGENT_SPEC", "").strip()
    agent_environment_name = os.getenv("INPUT_AGENT_ENVIRONMENT_NAME", "ai-agents-env").strip() or "ai-agents-env"
    agent_given_name = os.getenv("INPUT_AGENT_GIVEN_NAME", "").strip()
    agent_time_reservation = os.getenv("INPUT_AGENT_TIME_RESERVATION", "10").strip() or "10"
    billable_account_uid = os.getenv("INPUT_BILLABLE_ACCOUNT_UID", "").strip()
    execute_runs = as_bool(os.getenv("INPUT_EXECUTE_RUNS", "false"))
    run_environment = os.getenv("INPUT_RUN_ENVIRONMENT", "sdk").strip() or "sdk"

    if not api_key:
        print("Missing required input: api-key", file=sys.stderr)
        return 2

    try:
        run_limit = max(2, min(200, int(run_limit_raw)))
    except ValueError:
        run_limit = 50

    try:
        execution_run_limit = max(1, int(run_limit_raw))
    except ValueError:
        execution_run_limit = 1

    client = make_client(
        api_key=api_key,
        ai_agents_url=ai_agents_url,
        iam_url=iam_url,
        runtimes_url=runtimes_url,
    )

    created_agent_runtime_pod_name = ""
    created_agent_runtime_ingress = ""
    created_agent_runtime_pod_names: list[str] = []
    created_agent_runtime_ingresses: list[str] = []
    runtime_termination_attempted = False
    agent_spec_ids = parse_csv(agent_spec_ids_raw)

    def _ensure_runtime_terminated() -> bool:
        nonlocal runtime_termination_attempted
        if runtime_termination_attempted:
            return False
        runtime_termination_attempted = True
        terminated_any = False
        runtime_ids = [
            runtime_id
            for runtime_id in created_agent_runtime_pod_names
            if runtime_id
        ]
        if not runtime_ids and created_agent_runtime_pod_name:
            runtime_ids = [created_agent_runtime_pod_name]
        for runtime_id in runtime_ids:
            cleanup = teardown_agent_execution_resources(
                client,
                execution_target="cloud",
                cloud_runtime_or_pod_name=runtime_id,
            )
            terminated_any = bool(cleanup.get("cloud_runtime_terminated")) or terminated_any
        return terminated_any

    if agent_spec_ids and any([agent_spec_id, agent_spec]):
        print(
            "agentspec-ids cannot be combined with agentspec-id or agentspec",
            file=sys.stderr,
        )
        return 2

    executed_evalset_id = ""
    if execute_runs:
        if not evalset_spec_file:
            print("execute-runs requires evalset-spec-file", file=sys.stderr)
            return 2
        if not agent_spec_ids:
            print("execute-runs requires agentspec-ids", file=sys.stderr)
            return 2
        try:
            spec = load_evalset_spec(evalset_spec_file)
            execution = execute_evalset_spec(
                client,
                spec=spec,
                agentspec_ids=agent_spec_ids,
                run_limit=execution_run_limit,
                run_environment=run_environment,
                environment_name=agent_environment_name,
                account_uid=account_uid or None,
                launch_source="datalayer-github-actions",
                log=print,
            )
            executed_evalset_id = str(execution.get("evalset_id") or "")
            if not executed_evalset_id:
                raise RuntimeError("Runner did not return an evalset id.")
            evalset_id = executed_evalset_id
        except Exception as exc:
            message = f"Failed to execute eval runs: {exc}"
            print(message, file=sys.stderr)
            append_step_summary("## Datalayer Evals Report\n\n")
            append_step_summary(f"- Error: `{message}`\n\n")
            return 1

    if agent_spec_ids and not execute_runs:
        try:
            for idx, variant_id in enumerate(agent_spec_ids):
                pod_name, ingress = _create_agent_runtime(
                    client,
                    environment_name=agent_environment_name,
                    given_name=(
                        f"{agent_given_name.strip()}-{idx + 1}"
                        if agent_given_name.strip()
                        else ""
                    ),
                    time_reservation=agent_time_reservation,
                    agent_spec_id=variant_id,
                    agent_spec="",
                    billable_account_uid=billable_account_uid,
                )
                if pod_name:
                    created_agent_runtime_pod_names.append(pod_name)
                if ingress:
                    created_agent_runtime_ingresses.append(ingress)
            if created_agent_runtime_pod_names:
                created_agent_runtime_pod_name = created_agent_runtime_pod_names[0]
            if created_agent_runtime_ingresses:
                created_agent_runtime_ingress = created_agent_runtime_ingresses[0]
            if created_agent_runtime_pod_names:
                atexit.register(_ensure_runtime_terminated)
        except Exception as exc:
            print(f"Failed to create runtimes for agentspec-ids: {exc}", file=sys.stderr)
            append_step_summary("## Datalayer Evals Report\n\n")
            append_step_summary(f"- Multi-runtime creation failed: `{exc}`\n\n")
            return 1
    elif any([agent_spec_id, agent_spec]):
        try:
            created_agent_runtime_pod_name, created_agent_runtime_ingress = _create_agent_runtime(
                client,
                environment_name=agent_environment_name,
                given_name=agent_given_name,
                time_reservation=agent_time_reservation,
                agent_spec_id=agent_spec_id,
                agent_spec=agent_spec,
                billable_account_uid=billable_account_uid,
            )
            if created_agent_runtime_pod_name:
                created_agent_runtime_pod_names = [created_agent_runtime_pod_name]
            if created_agent_runtime_ingress:
                created_agent_runtime_ingresses = [created_agent_runtime_ingress]
            if created_agent_runtime_pod_name:
                atexit.register(_ensure_runtime_terminated)
        except Exception as exc:
            print(f"Failed to create agent runtime: {exc}", file=sys.stderr)
            append_step_summary("## Datalayer Evals Report\n\n")
            append_step_summary(f"- Agent runtime creation failed: `{exc}`\n\n")
            return 1

    try:
        resolved_evalset_id = _resolve_evalset_id(
            client,
            explicit_evalset_id=evalset_id,
            spec_file=evalset_spec_file,
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
        )
        comparison_summary_file = str(summary_path)

    primary_failures = collect_report_failures(primary_report)
    secondary_failures = (
        collect_report_failures(secondary_report)
        if resolved_secondary_evalset_id
        else {"failed_run_count": 0}
    )
    total_failed = int(primary_failures["failed_run_count"]) + int(secondary_failures["failed_run_count"])

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
    append_github_output("agent_runtime_pod_name", created_agent_runtime_pod_name)
    append_github_output("agent_runtime_ingress", created_agent_runtime_ingress)
    append_github_output("agent_runtime_pod_names", json.dumps(created_agent_runtime_pod_names))
    append_github_output("agent_runtime_ingresses", json.dumps(created_agent_runtime_ingresses))
    append_github_output("failed_run_count", str(total_failed))
    append_github_output("primary_failed_run_count", str(primary_failures["failed_run_count"]))
    append_github_output("secondary_failed_run_count", str(secondary_failures["failed_run_count"]))

    if primary_outputs["report_file"]:
        append_step_summary("## Datalayer Evals Report\n\n")
        append_step_summary(f"- Primary evalset: {resolved_evalset_id}\n")
        if executed_evalset_id:
            append_step_summary(f"- Executed evalset (real runs): {executed_evalset_id}\n")
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
        if created_agent_runtime_pod_names:
            append_step_summary(
                f"- Agent runtime pods: {', '.join(created_agent_runtime_pod_names)}\n"
            )
        elif created_agent_runtime_pod_name:
            append_step_summary(f"- Agent runtime pod: {created_agent_runtime_pod_name}\n")
        if created_agent_runtime_ingresses:
            append_step_summary(
                f"- Agent runtime ingresses: {', '.join(created_agent_runtime_ingresses)}\n"
            )
        elif created_agent_runtime_ingress:
            append_step_summary(f"- Agent runtime ingress: {created_agent_runtime_ingress}\n")
        append_step_summary(f"- Total failed runs: {total_failed}\n")
        append_step_summary("\n")

        if created_agent_runtime_pod_names or created_agent_runtime_pod_name:
            if _ensure_runtime_terminated():
                append_step_summary(
                    "- Agent runtime termination attempted for all created runtimes.\n"
                )
            else:
                append_step_summary(
                    "- Warning: agent runtime termination was not confirmed for one or more runtimes.\n"
                )

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
