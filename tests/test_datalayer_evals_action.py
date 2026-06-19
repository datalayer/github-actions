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

    evals_pkg = types.ModuleType("datalayer_core.evals")
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

    client_pkg = types.ModuleType("datalayer_core.client")
    client_mod = types.ModuleType("datalayer_core.client.client")

    class _StubClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    client_mod.DatalayerClient = _StubClient

    agents_mod = types.ModuleType("datalayer_core.agents")
    agents_mod.create_cloud_agent_runtime = lambda *args, **kwargs: types.SimpleNamespace(
        pod_name="pod-default", ingress="https://ingress"
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
        "datalayer_core.evals": evals_pkg,
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


def test_create_agent_runtime_billable_account_optional(action_module, monkeypatch):
    calls = {}

    def fake_create_cloud_agent_runtime(_client, **kwargs):
        calls["kwargs"] = kwargs
        return types.SimpleNamespace(pod_name="pod-1", ingress="https://ing-1")

    monkeypatch.setattr(action_module, "create_cloud_agent_runtime", fake_create_cloud_agent_runtime)

    client = object()

    pod_name, ingress = action_module._create_agent_runtime(
        client,
        environment_name="env",
        given_name="",
        time_reservation="10",
        agent_spec_id="example-simple",
        agent_spec="",
        billable_account_uid="",
    )

    assert pod_name == "pod-1"
    assert ingress == "https://ing-1"
    assert calls["kwargs"]["billable_account_uid"] is None

    action_module._create_agent_runtime(
        client,
        environment_name="env",
        given_name="",
        time_reservation="10",
        agent_spec_id="example-simple",
        agent_spec="",
        billable_account_uid="acct-123",
    )
    assert calls["kwargs"]["billable_account_uid"] == "acct-123"


def test_resolve_evalset_id_from_spec_uses_optional_account_uid(action_module, tmp_path):
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
        account_uid="",
    )

    assert evalset_id == "evalset-123"
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


def test_main_rejects_conflicting_agentspec_inputs(action_module, monkeypatch):
    monkeypatch.setenv("INPUT_API_KEY", "key")
    monkeypatch.setenv("INPUT_AGENT_SPEC_IDS", "a,b")
    monkeypatch.setenv("INPUT_AGENT_SPEC_ID", "a")
    monkeypatch.setenv("INPUT_AGENT_SPEC", "")

    # Avoid accidental file writes/summary writes from future logic changes.
    monkeypatch.setattr(action_module, "append_step_summary", lambda _text: None)

    exit_code = action_module.main()

    assert exit_code == 2
