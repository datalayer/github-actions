import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def action_module(monkeypatch):
    """Load the action module with lightweight stubs for datalayer_core imports."""

    # Build module tree stubs required at import time.
    datalayer_core = types.ModuleType("datalayer_core")
    cli_mod = types.ModuleType("datalayer_core.cli")
    commands_mod = types.ModuleType("datalayer_core.cli.commands")

    agents_cmd_mod = types.ModuleType("datalayer_core.cli.commands.agents")
    agents_cmd_mod._load_agent_spec = lambda _: {"name": "spec"}

    evals_pkg = types.ModuleType("agent_runtimes.evals.remote")
    evals_pkg.build_eval_report = lambda *_args, **_kwargs: {
        "generated_at": "2026-01-01T00:00:00Z",
        "experiments": [],
    }
    evals_pkg.average_latest_pass_rate = lambda *_args, **_kwargs: None
    evals_pkg.collect_report_failures = lambda *_args, **_kwargs: {
        "failed_run_count": 0,
        "failed_status_runs": 0,
        "type_counts": {},
        "failures": [],
    }
    evals_pkg.benchmark_url = lambda evalset_id: f"https://datalayer.app/benchmarks/{evalset_id}"
    evals_pkg.execute_evalset_spec = lambda *_args, **_kwargs: {
        "evalset_id": "evalset-executed",
        "evalset_name": "spec-sdk",
        "experiment_ids": [],
        "run_ids": [],
    }
    evals_pkg.load_evalset_spec = lambda *_args, **_kwargs: {"name": "spec", "cases": []}
    evals_pkg.make_client = lambda *_args, **_kwargs: _StubClient()
    evals_pkg.now_iso = lambda: "2026-01-01T00:00:00Z"
    evals_pkg.render_eval_report_markdown = lambda *_args, **_kwargs: "# report"
    evals_pkg.timestamp_slug = lambda _value: "20260101T000000Z"
    evals_pkg.write_eval_report_csv = lambda *_args, **_kwargs: None

    agent_runtimes_pkg = types.ModuleType("agent_runtimes")
    agent_runtimes_evals_pkg = types.ModuleType("agent_runtimes.evals")
    agent_runtimes_client_pkg = types.ModuleType("agent_runtimes.client")

    client_pkg = types.ModuleType("datalayer_core.client")
    client_mod = types.ModuleType("datalayer_core.client.client")

    class _StubClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    client_mod.DatalayerClient = _StubClient
    client_pkg.DatalayerClient = _StubClient
    agent_runtimes_client_pkg.AgentClient = _StubClient

    agents_mod = types.ModuleType("datalayer_core.agents")
    agents_mod.create_cloud_agent_runtime = lambda *args, **kwargs: types.SimpleNamespace(
        runtime_name="pod-default", ingress="https://ingress"
    )
    agents_mod.teardown_agent_execution_resources = lambda *_args, **_kwargs: {
        "cloud_runtime_terminated": True
    }

    runtimes_pkg = types.ModuleType("datalayer_core.runtimes")
    runtimes_agent_mod = types.ModuleType("datalayer_core.runtimes.agent_runtime")
    runtimes_agent_mod.create_cloud_agent_runtime = agents_mod.create_cloud_agent_runtime
    runtimes_agent_mod.teardown_agent_execution_resources = (
        agents_mod.teardown_agent_execution_resources
    )

    utils_pkg = types.ModuleType("datalayer_core.utils")
    urls_mod = types.ModuleType("datalayer_core.utils.urls")

    class _StubUrls:
        @staticmethod
        def from_environment(**kwargs):
            return types.SimpleNamespace(**kwargs)

    urls_mod.DatalayerURLs = _StubUrls

    module_map = {
        "datalayer_core": datalayer_core,
        "datalayer_core.cli": cli_mod,
        "datalayer_core.cli.commands": commands_mod,
        "datalayer_core.cli.commands.agents": agents_cmd_mod,
        "agent_runtimes": agent_runtimes_pkg,
        "agent_runtimes.evals": agent_runtimes_evals_pkg,
        "agent_runtimes.evals.remote": evals_pkg,
        "agent_runtimes.client": agent_runtimes_client_pkg,
        "datalayer_core.client": client_pkg,
        "datalayer_core.client.client": client_mod,
        "datalayer_core.agents": agents_mod,
        "datalayer_core.runtimes": runtimes_pkg,
        "datalayer_core.runtimes.agent_runtime": runtimes_agent_mod,
        "datalayer_core.utils": utils_pkg,
        "datalayer_core.utils.urls": urls_mod,
    }
    for name, module in module_map.items():
        monkeypatch.setitem(sys.modules, name, module)

    module_path = (
        Path(__file__).resolve().parents[1] / "src" / "datalayer_evals_action.py"
    )
    spec = importlib.util.spec_from_file_location("datalayer_evals_action", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_parse_csv_dedupes_and_strips(action_module):
    assert action_module.parse_csv("a, b,a,, c ") == ["a", "b", "c"]


def test_resolve_evalset_id_from_spec_uses_optional_billing_entity_uid(action_module, tmp_path):
    spec_file = tmp_path / "evalset.json"
    spec_file.write_text(
        json.dumps(
            {
                "name": "Evalset A",
                "description": "demo",
                "cases": [{"name": "case-1"}],
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    class FakeClient:
        def evals_create_eval_from_spec(self, **kwargs):
            captured.update(kwargs)
            return {"evalset": {"id": "evalset-123"}}

    evalset_id = action_module._resolve_evalset_id(
        FakeClient(),
        explicit_evalset_id="",
        spec_file=str(spec_file),
        billing_entity_uid="",
        account_uid="",
    )

    assert evalset_id == "evalset-123"
    assert captured["billing_entity_uid"] is None
    assert captured["account_uid"] is None


def test_report_is_partial_detects_missing_experiments_or_runs(action_module):
    assert action_module._report_is_partial({"experiments": []}) is True
    assert (
        action_module._report_is_partial(
            {
                "experiments": [
                    {"id": "exp-1", "runs": []},
                ]
            }
        )
        is True
    )
    assert (
        action_module._report_is_partial(
            {
                "experiments": [
                    {"id": "exp-1", "runs": [{"id": "run-1"}]},
                ]
            }
        )
        is False
    )


def test_main_prepare_spec_mode_writes_lane_specific_spec(action_module, monkeypatch, tmp_path):
    spec_file = tmp_path / "simple.evalset.json"
    spec_file.write_text(json.dumps({"name": "simple", "cases": []}), encoding="utf-8")

    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("INPUT_MODE", "prepare-spec")
    monkeypatch.setenv("INPUT_EVALSET_SPEC_FILE", str(spec_file))
    monkeypatch.setenv("INPUT_RUN_ENVIRONMENT", "sdk-proxy")
    monkeypatch.setenv("INPUT_PREPARED_SPEC_OUTPUT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(action_module, "append_step_summary", lambda _text: None)

    exit_code = action_module.main()

    assert exit_code == 0
    output = output_file.read_text(encoding="utf-8")
    assert "prepared_spec_path=" in output

    prepared_path = None
    for line in output.splitlines():
        if line.startswith("prepared_spec_path="):
            prepared_path = Path(line.split("=", 1)[1])
            break
    assert prepared_path is not None
    payload = json.loads(prepared_path.read_text(encoding="utf-8"))
    assert payload["run_environment"] == "sdk-proxy"


def test_main_execute_runs_mode_requires_agentspec_ids(action_module, monkeypatch):
    monkeypatch.setenv("INPUT_MODE", "execute-runs")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_SPEC_FILE", "spec.json")
    monkeypatch.setenv("INPUT_AGENT_SPEC_IDS", "")

    exit_code = action_module.main()

    assert exit_code == 2


def test_parse_request_timeout_seconds_handles_bad_and_negative_values(action_module):
    assert action_module.parse_request_timeout_seconds("") == 180
    assert action_module.parse_request_timeout_seconds("abc") == 180
    assert action_module.parse_request_timeout_seconds("90") == 90
    assert action_module.parse_request_timeout_seconds("-5") == 1
    assert action_module.parse_request_timeout_seconds("0") == 1


def test_main_execute_runs_mode_forwards_request_timeout_seconds(
    action_module, monkeypatch, tmp_path
):
    spec_file = tmp_path / "spec.evalset.json"
    spec_file.write_text(json.dumps({"name": "spec", "cases": []}), encoding="utf-8")

    captured = {}

    def fake_execute_evalset_spec(_client, **kwargs):
        captured.update(kwargs)
        return {"evalset_id": "evalset-executed"}

    monkeypatch.setattr(action_module, "execute_evalset_spec", fake_execute_evalset_spec)
    monkeypatch.setattr(action_module, "append_step_summary", lambda _text: None)
    monkeypatch.setattr(action_module, "append_github_output", lambda _key, _value: None)

    monkeypatch.setenv("INPUT_MODE", "execute-runs")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_SPEC_FILE", str(spec_file))
    monkeypatch.setenv("INPUT_AGENT_SPEC_IDS", "a,b")
    monkeypatch.setenv("INPUT_REQUEST_TIMEOUT_SECONDS", "45")

    exit_code = action_module.main()

    assert exit_code == 0
    assert captured["request_timeout_seconds"] == 45


# --- The contract the action keeps (BENCHMARK.md, B0-05) -------------------


def _manifest_outputs() -> list[str]:
    """The output names `action.yml` declares, as the code spells them."""
    text = (Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8")
    block = text.split("\noutputs:\n", 1)[1].split("\nruns:\n", 1)[0]
    names = []
    for line in block.splitlines():
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            names.append(line.strip()[:-1].replace("-", "_"))
    return names


ACTION_OUTPUTS = [
    "live_report_url",
    "comparison_url",
    "secondary_comparison_url",
    "decision_comment",
    "decision_count",
    "decision_posted",
    "gate_status",
    "prepared_spec_path",
    "spec_path",
    "report_file",
    "evalset_id",
    "executed_evalset_id",
    "csv_file",
    "log_file",
    "timestamped_report_file",
    "timestamped_csv_file",
    "secondary_report_file",
    "secondary_csv_file",
    "secondary_log_file",
    "secondary_timestamped_report_file",
    "secondary_timestamped_csv_file",
    "comparison_summary_file",
    "failed_run_count",
    "primary_failed_run_count",
    "secondary_failed_run_count",
]


def _one_experiment_report() -> dict:
    """A report with something in it; an empty one fails the action on purpose."""
    return {
        "generated_at": "2026-01-01T00:00:00Z",
        "evalset_id": "evalset-1",
        "experiments": [{"id": "experiment-1", "runs": [{"id": "run-1", "status": "completed"}]}],
    }


def test_the_manifest_declares_exactly_these_outputs():
    assert _manifest_outputs() == ACTION_OUTPUTS


def test_run_report_writes_its_outputs(action_module, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    outputs = tmp_path / "outputs.txt"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("INPUT_MODE", "run-report")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_ID", "evalset-1")
    monkeypatch.setenv("INPUT_RUN_LIMIT", "1")
    monkeypatch.setattr(action_module, "build_eval_report", lambda *a, **k: _one_experiment_report())

    assert action_module.main() == 0

    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
    # Every run-report output, and nothing the other modes own.
    assert set(written) == set(ACTION_OUTPUTS) - {
        "prepared_spec_path",
        "spec_path",
        # post-decision's, which run-report has nothing to say about.
        "decision_comment",
        "decision_count",
        "decision_posted",
    }
    assert written["evalset_id"] == "evalset-1"
    assert written["failed_run_count"] == "0"
    assert written["live_report_url"] == "https://datalayer.app/benchmarks/evalset-1"
    assert written["gate_status"] == "skipped"  # no gate asked for
    # The first thing the summary says is where to read it (B6-01).
    assert summary.read_text(encoding="utf-8").splitlines()[2].startswith("**Live report:** https://datalayer.app/benchmarks/evalset-1")
    assert Path(written["report_file"]).read_text(encoding="utf-8").strip() == "# report"
    assert "Datalayer Evals Report" in summary.read_text(encoding="utf-8")


def test_a_single_run_is_a_valid_limit(action_module, monkeypatch, tmp_path):
    # The CLI and the action used to disagree: one refused fewer than two
    # runs, the other accepted one. Both accept one now.
    seen = {}

    def build(client, evalset_id, run_limit=None, **kwargs):
        seen["run_limit"] = run_limit
        return _one_experiment_report()

    monkeypatch.setattr(action_module, "build_eval_report", build)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INPUT_MODE", "run-report")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_ID", "evalset-1")
    monkeypatch.setenv("INPUT_RUN_LIMIT", "1")
    assert action_module.main() == 0
    assert seen["run_limit"] == 1


def test_execute_runs_names_the_local_agent_the_way_the_runner_does(action_module, monkeypatch):
    captured = {}

    def fake_execute(client, **kwargs):
        captured.update(kwargs)
        return {"evalset_id": "evalset-executed"}

    monkeypatch.setattr(action_module, "execute_evalset_spec", fake_execute)
    monkeypatch.setattr(action_module, "load_evalset_spec", lambda _path: {"name": "spec", "cases": []})

    executed = action_module._execute_eval_runs(
        client=object(),
        evalset_spec_file="spec.json",
        agent_spec_ids=["agent-a"],
        run_limit_raw="3",
        run_environment="sdk",
        agent_environment_name="ai-agents-env",
        execution_target="local",
        auto_start_local_agent_runtime=True,
        local_agent_base_url="http://127.0.0.1:8765",
        local_agent_name="my-agent",
        billing_entity_uid="",
        account_uid="",
        request_timeout_seconds=180,
    )
    assert executed["evalset_id"] == "evalset-executed"
    assert captured["agent_name"] == "my-agent"
    assert "local_agent_name" not in captured
    # Unset inputs are left to the runner's defaults rather than sent as None.
    assert "billing_entity_uid" not in captured
    assert captured["run_limit"] == 3
    assert captured["execution_target"] == "local"


def test_execute_runs_hands_the_concurrency_and_the_budget_to_the_launch(action_module, monkeypatch):
    """A cloud execution is a launch (B2-15): the runner gets the pool size
    and the credits cap the workflow inputs name."""
    seen = {}

    def fake_execute(_client, **kwargs):
        seen.update(kwargs)
        return {"evalset_id": "evalset-1"}

    monkeypatch.setattr(action_module, "execute_evalset_spec", fake_execute)
    monkeypatch.setattr(action_module, "make_client", lambda **kwargs: object())
    monkeypatch.setattr(action_module, "load_evalset_spec", lambda path: {"name": "x", "cases": [{"name": "c"}]})
    monkeypatch.setenv("INPUT_MODE", "execute-runs")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_SPEC_FILE", "spec.json")
    monkeypatch.setenv("INPUT_AGENT_SPEC_IDS", "jupyter-data-analyst")
    monkeypatch.setenv("INPUT_CONCURRENCY", "8")
    monkeypatch.setenv("INPUT_BUDGET", "12.5")
    assert action_module.main() == 0
    assert seen["concurrency"] == 8 and seen["credits_limit"] == 12.5 and seen["execution_target"] == "cloud"
    monkeypatch.setenv("INPUT_BUDGET", "lots")
    assert action_module.main() == 2


def _run_report(action_module, monkeypatch, tmp_path, report, **env):
    """One run-report pass: its exit code, the outputs it wrote, its summary."""
    outputs = tmp_path / "outputs.txt"
    summary = tmp_path / "summary.md"
    for path in (outputs, summary):
        if path.exists():
            path.unlink()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("INPUT_MODE", "run-report")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_ID", "evalset-1")
    for key in ("INPUT_PASS_RATE_THRESHOLD", "INPUT_MAX_REGRESSION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(action_module, "build_eval_report", lambda *a, **k: report)
    code = action_module.main()
    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
    return code, written, summary.read_text(encoding="utf-8")


def _scored_report(latest: float, drift: float) -> dict:
    return {
        "generated_at": "2026-01-01T00:00:00Z",
        "evalset_id": "evalset-1",
        "experiments": [
            {
                "id": "experiment-1",
                "name": "candidate",
                "latest_pass_rate": latest,
                "baseline_pass_rate": latest - drift,
                "drift_delta": drift,
                "runs": [{"id": "run-1", "status": "completed"}],
            }
        ],
    }


def test_the_quality_gate_is_computed_from_the_scores(action_module, monkeypatch, tmp_path):
    """B6-05: a fixture below the threshold yields `failed`, fails the step,
    and changes no other output; above it, `passed`; no gate, `skipped`."""
    without = _run_report(action_module, monkeypatch, tmp_path, _scored_report(0.7, -0.1))
    assert without[0] == 0 and without[1]["gate_status"] == "skipped"

    below = _run_report(action_module, monkeypatch, tmp_path, _scored_report(0.7, -0.1), INPUT_PASS_RATE_THRESHOLD="0.8")
    assert below[0] == 1 and below[1]["gate_status"] == "failed"
    assert {k: v for k, v in below[1].items() if k != "gate_status"} == {
        k: v for k, v in without[1].items() if k != "gate_status"
    }
    assert "Quality gate: **failed**" in below[2] and "70.0% is below the threshold 80.0%" in below[2]

    above = _run_report(action_module, monkeypatch, tmp_path, _scored_report(0.9, -0.01), INPUT_PASS_RATE_THRESHOLD="0.8", INPUT_MAX_REGRESSION="0.05")
    assert above[0] == 0 and above[1]["gate_status"] == "passed"

    regressed = _run_report(action_module, monkeypatch, tmp_path, _scored_report(0.9, -0.1), INPUT_MAX_REGRESSION="0.05")
    assert regressed[0] == 1 and regressed[1]["gate_status"] == "failed"
    assert "exceeds the allowed regression" in regressed[2]

    # A score that is not there is not a pass.
    unscored = _run_report(action_module, monkeypatch, tmp_path, _one_experiment_report(), INPUT_PASS_RATE_THRESHOLD="0.5")
    assert unscored[0] == 1 and "no scored run" in unscored[2]

    # A gate that is not a fraction is refused before anything runs.
    monkeypatch.setenv("INPUT_PASS_RATE_THRESHOLD", "80")
    assert action_module.main() == 2


def test_the_git_context_is_read_from_what_actions_sets(action_module, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 12}}), encoding="utf-8")
    context = action_module.git_context(
        {
            "GITHUB_SHA": "0123456789abcdef",
            "GITHUB_REF": "refs/pull/12/merge",
            "GITHUB_REPOSITORY": "datalayer/data-analysis",
            "GITHUB_RUN_ID": "42",
            "GITHUB_EVENT_PATH": str(event),
        }
    )
    assert context == {
        "sha": "0123456789abcdef",
        "ref": "refs/pull/12/merge",
        "repository": "datalayer/data-analysis",
        "run_id": "42",
        "pr_number": "12",
        "url": "https://github.com/datalayer/data-analysis/actions/runs/42",
    }
    # No event payload: the pull request comes from the ref, when it is one.
    assert action_module.git_context({"GITHUB_REF": "refs/pull/7/head"})["pr_number"] == "7"
    assert action_module.git_context({"GITHUB_REF": "refs/heads/main"})["pr_number"] == ""
    # Outside Actions everything is empty, and the runner drops empty values.
    assert all(value == "" for value in action_module.git_context({}).values())


def test_execute_runs_hands_the_git_context_and_names_the_live_report(action_module, monkeypatch, tmp_path):
    seen = {}

    def fake_execute(_client, **kwargs):
        seen.update(kwargs)
        return {"evalset_id": "evalset-1", "launch_ids": ["launch-9"], "view_url": "https://datalayer.app/runs/launch-9"}

    outputs = tmp_path / "outputs.txt"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_SHA", "abc")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "datalayer/x")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setattr(action_module, "execute_evalset_spec", fake_execute)
    monkeypatch.setattr(action_module, "make_client", lambda **kwargs: object())
    monkeypatch.setattr(action_module, "load_evalset_spec", lambda path: {"name": "x", "cases": [{"name": "c"}]})
    monkeypatch.setenv("INPUT_MODE", "execute-runs")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_SPEC_FILE", "spec.json")
    monkeypatch.setenv("INPUT_AGENT_SPEC_IDS", "jupyter-data-analyst")
    assert action_module.main() == 0
    assert seen["git"]["sha"] == "abc" and seen["git"]["repository"] == "datalayer/x" and seen["git"]["run_id"] == "7"
    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
    assert written["live_report_url"] == "https://datalayer.app/runs/launch-9"
    text = summary.read_text(encoding="utf-8")
    assert text.splitlines()[2] == "**Live report:** https://datalayer.app/runs/launch-9"
    assert "- Launches: launch-9" in text


def _two_experiment_report() -> dict:
    """A benchmark two agentspecs ran: what B6-03's link is for."""
    return {
        "generated_at": "2026-01-01T00:00:00Z",
        "evalset_id": "evalset-1",
        "experiments": [
            {"id": "experiment-1", "runs": [{"id": "run-1", "status": "completed"}]},
            {"id": "experiment-2", "runs": [{"id": "run-2", "status": "completed"}]},
        ],
    }


def test_run_report_links_the_comparison_when_more_than_one_subject_ran(
    action_module, monkeypatch, tmp_path
):
    """B6-03: one evalset with several agentspec experiments links to the
    cross-agentspec comparison."""
    monkeypatch.chdir(tmp_path)
    outputs = tmp_path / "outputs.txt"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("INPUT_MODE", "run-report")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_ID", "evalset-1")
    monkeypatch.setenv("INPUT_RUN_LIMIT", "1")
    monkeypatch.setattr(action_module, "build_eval_report", lambda *a, **k: _two_experiment_report())

    assert action_module.main() == 0

    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
    assert written["comparison_url"] == "https://datalayer.app/benchmarks/evalset-1/compare"
    assert written["secondary_comparison_url"] == ""
    assert "**Comparison:** https://datalayer.app/benchmarks/evalset-1/compare" in summary.read_text(
        encoding="utf-8"
    )


def test_one_subject_has_nothing_to_compare(action_module, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    outputs = tmp_path / "outputs.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("INPUT_MODE", "run-report")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_ID", "evalset-1")
    monkeypatch.setenv("INPUT_RUN_LIMIT", "1")
    monkeypatch.setattr(action_module, "build_eval_report", lambda *a, **k: _one_experiment_report())

    assert action_module.main() == 0

    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
    assert written["comparison_url"] == "", "one subject is a result, not a comparison"


def test_execute_runs_links_the_launches_it_submitted(action_module, monkeypatch, tmp_path):
    """B6-03: the link names this execution's launches, not whatever is newest
    by the time somebody opens it."""
    monkeypatch.chdir(tmp_path)
    outputs = tmp_path / "outputs.txt"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("INPUT_MODE", "execute-runs")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_EVALSET_SPEC_FILE", "spec.json")
    monkeypatch.setenv("INPUT_AGENT_SPEC_IDS", "agent-a,agent-b")
    monkeypatch.setenv("INPUT_EXECUTION_TARGET", "cloud")
    monkeypatch.setattr(action_module, "make_client", lambda **kwargs: object())
    monkeypatch.setattr(
        action_module,
        "_execute_eval_runs",
        lambda **kwargs: {
            "evalset_id": "evalset-9",
            "experiment_ids": ["experiment-a", "experiment-b"],
            "launch_ids": ["launch-1", "launch-2"],
        },
    )

    assert action_module.main() == 0

    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
    assert (
        written["comparison_url"]
        == "https://datalayer.app/benchmarks/evalset-9/compare?launches=launch-1,launch-2"
    )
    assert "**Comparison:**" in summary.read_text(encoding="utf-8")


def test_the_comparison_summary_says_where_to_read_each_side(action_module, tmp_path):
    path = tmp_path / "comparison.md"

    action_module._write_comparison_summary(
        path=path,
        primary_label="evalset-1",
        secondary_label="evalset-2",
        primary_report=_two_experiment_report(),
        secondary_report=_two_experiment_report(),
        primary_comparison_url="https://datalayer.app/benchmarks/evalset-1/compare",
        secondary_comparison_url="https://datalayer.app/benchmarks/evalset-2/compare",
    )

    written = path.read_text(encoding="utf-8")
    assert "Compare in Datalayer:" in written
    assert "- Primary: https://datalayer.app/benchmarks/evalset-1/compare" in written
    assert "- Secondary: https://datalayer.app/benchmarks/evalset-2/compare" in written


DECIDED = [
    {
        "outcome": "accepted_regression",
        "kind": "evaluator_issue",
        "note": "The joins task is a known flake.",
        "decided_at": "2026-09-12T10:00:00Z",
    },
    {"outcome": "rejected", "kind": "data_issue", "note": "", "decided_at": "2026-09-12T11:00:00Z"},
]


def test_the_decision_comment_says_what_was_decided_and_where_to_read_it(action_module):
    """B6-04: the decision and the link, and nothing else — a pull request is a
    public place in most repositories, and the numbers are behind an account."""
    body = action_module.decision_comment(
        decisions=DECIDED,
        report_url="https://datalayer.app/benchmarks/evalset-1/report",
        benchmark="evalset-1",
    )

    assert body == "\n".join(
        [
            "## Datalayer benchmark review",
            "",
            "**evalset-1**",
            "",
            "- **accepted regression** — evaluator issue: The joins task is a known flake. _(2026-09-12)_",
            "- **rejected** — data issue _(2026-09-12)_",
            "",
            "[Read the report](https://datalayer.app/benchmarks/evalset-1/report)",
            "",
            "<!-- datalayer-evals-decision -->",
        ]
    )
    assert "pass rate" not in body and "%" not in body, "the scores stay in the report"


def test_nothing_decided_is_nothing_to_say(action_module):
    assert action_module.decision_comment(decisions=[], report_url="https://x/report") == ""


def test_post_decision_leaves_the_comment_and_updates_it_next_time(action_module, monkeypatch, tmp_path):
    import httpx

    posted: list[tuple[str, str, dict]] = []
    existing: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "evals/decisions" in str(request.url):
            return httpx.Response(200, json={"success": True, "total": 2, "decisions": DECIDED})
        if request.method == "GET":
            return httpx.Response(200, json=existing)
        body = json.loads(request.content)
        posted.append((request.method, str(request.url), dict(request.headers)))
        return httpx.Response(201, json={"id": 42, "body": body["body"]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    class PatchedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", PatchedClient)
    monkeypatch.chdir(tmp_path)
    outputs = tmp_path / "outputs.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("INPUT_MODE", "post-decision")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_AI_AGENTS_URL", "https://agents.example")
    monkeypatch.setenv("INPUT_EVALSET_ID", "evalset-1")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("INPUT_REPOSITORY", "datalayer/osp")
    monkeypatch.setenv("INPUT_PR_NUMBER", "7")

    assert action_module.main() == 0

    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert written["decision_count"] == "2"
    assert written["decision_posted"] == "posted"
    method, url, headers = posted[-1]
    assert method == "POST"
    assert url == "https://api.github.com/repos/datalayer/osp/issues/7/comments"
    assert headers["authorization"] == "Bearer gh-token"

    # A second run finds its own comment and updates it, so a pull request ends
    # with one decision comment that is current rather than a row of them.
    existing.append({"id": 42, "body": f"old {action_module.DECISION_MARKER}"})
    posted.clear()
    assert action_module.main() == 0
    method, url, _ = posted[-1]
    assert method == "PATCH"
    assert url == "https://api.github.com/repos/datalayer/osp/issues/comments/42"


def test_post_decision_without_a_token_still_hands_over_the_comment(action_module, monkeypatch, tmp_path):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert "evals/decisions" in str(request.url), "nothing but the decisions is asked for"
        return httpx.Response(200, json={"success": True, "decisions": DECIDED})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    class PatchedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", PatchedClient)
    monkeypatch.chdir(tmp_path)
    outputs = tmp_path / "outputs.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("INPUT_MODE", "post-decision")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_AI_AGENTS_URL", "https://agents.example")
    monkeypatch.setenv("INPUT_EVALSET_ID", "evalset-1")
    monkeypatch.delenv("INPUT_GITHUB_TOKEN", raising=False)

    assert action_module.main() == 0

    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert written["decision_posted"] == ""
    assert "Datalayer benchmark review" in written["decision_comment"]


def test_post_decision_needs_something_to_ask_about(action_module, monkeypatch):
    monkeypatch.setenv("INPUT_MODE", "post-decision")
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.delenv("INPUT_EVALSET_ID", raising=False)
    monkeypatch.delenv("INPUT_LAUNCH_ID", raising=False)

    assert action_module.main() == 2
