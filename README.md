[![Datalayer](https://assets.datalayer.tech/datalayer-25.svg)](https://datalayer.io)

[![Become a Sponsor](https://img.shields.io/static/v1?label=Become%20a%20Sponsor&message=%E2%9D%A4&logo=GitHub&style=flat&color=1ABC9C)](https://github.com/sponsors/datalayer)

# ☰ 🎬 Datalayer GitHub Actions

This repository contains reusable GitHub Actions for Datalayer workflows.

## datalayer-evals

The datalayer-evals action runs Datalayer evals in CI and produces report artifacts.

It uses the `agent-runtimes` `AgentClient` and the `agent-runtimes`
eval-report helpers directly rather than shelling out to the CLI, so the generated
reports contain the full structured failure diagnostics (per-run failure causes,
stages, types and detail excerpts). Failures are also aggregated into the GitHub
step summary and exposed as action outputs.

It supports two execution modes:

- Primary report mode (single evalset).
- Comparison mode (primary + secondary evalsets) with a generated summary markdown.

Real run execution is runner-first and uses `agent_runtimes.evals.remote.execute_evalset_spec`
with one runtime per agentspec id in `mode=execute-runs`.

Evalsets can be provided as IDs, or created on the fly from spec files.

Primary report mode produces, for each report:

- `<output-markdown>` and a matching `.csv` (when export-csv is true)
- a `<output-markdown>.log` artifact containing the full structured report JSON
  (including every run-level failure cause)
- timestamped files `report-<timestamp>.md` and `report-<timestamp>.csv`

The action is implemented in Python and can be consumed from other repositories.

### Per-Case Scores

Every generated report includes a **Per-Case Outcomes** section, rendered by the
`agent-runtimes` report helpers from each run's `metrics.case_results`. For every
case it shows the pass rate across the fetched runs and an **Avg Score** in the
`[0, 1]` range (the mean of that case's per-run `score`). When multiple
agentspecs are present (for example `codemode` vs `nocodemode`), a per-case
pass-rate-by-agentspec table is added so you can see which cases regress under
which variant.

Canonical score semantics, case-vs-report evaluator guidance, and interpretation
rules (agent-backed vs synthetic behavior) are documented in the UI docs:
[Evals](https://datalayer.ai/docs/evals).

If a run does not store `case_results`, the Per-Case Outcomes section notes that
no per-case results were recorded and only aggregate pass-rate metrics are shown.

### Secrets

Authentication and billing context are supplied through repository secrets so
they never appear in workflow files or logs:

| Secret | Maps to input | Required | Purpose |
| :-- | :-- | :-- | :-- |
| `DATALAYER_API_KEY` | `api-key` | ✅ Required | Authenticates every call the action makes to Datalayer. |
| `DATALAYER_BILLABLE_ACCOUNT_UID` | `billable-account-uid` | Optional | Billable account context used for eval operations. |

If the selected agentspec model provider needs credentials, define those as
GitHub secrets too and expose them as environment variables on the action step.

Amazon Bedrock example (for Bedrock-hosted models referenced by agentspecs):

| Secret | Required for Bedrock agentspecs | Purpose |
| :-- | :-- | :-- |
| `AWS_ACCESS_KEY_ID` | ✅ Yes | AWS access key used by the Bedrock client. |
| `AWS_SECRET_ACCESS_KEY` | ✅ Yes | AWS secret key used by the Bedrock client. |
| `AWS_DEFAULT_REGION` | ✅ Yes | Region hosting the Bedrock model endpoint. |

Reference them in the consumer workflow:

```yaml
- name: Run eval report (Bedrock-backed agentspec)
	uses: datalayer/github-actions@v1
	env:
		AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
		AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
		AWS_DEFAULT_REGION: ${{ secrets.AWS_DEFAULT_REGION }}
	with:
		api-key: ${{ secrets.DATALAYER_API_KEY }}
		billable-account-uid: ${{ secrets.DATALAYER_BILLABLE_ACCOUNT_UID }}
		evalset-id: 01KXXXXXXXXXXXX
```

To make the billable account optional at dispatch time while still defaulting to
the secret, use the `||` fallback:

```yaml
  billable-account-uid: ${{ inputs.billable_account_uid || secrets.DATALAYER_BILLABLE_ACCOUNT_UID }}
```

### Report Upload

When `upload-report-artifacts` is `true` (the default), the action uploads the
generated **markdown** and **CSV** reports (plus the structured `.log` JSON and
any timestamped/secondary/comparison files) as a build artifact in a final step
— no extra upload step is required in the consumer workflow.

### Inputs

- evalset-id: required, target evalset UID
- evalset-spec-file: optional, path to primary evalset spec JSON; action creates evalset and reports it
- secondary-evalset-id: optional, secondary evalset UID
- secondary-evalset-spec-file: optional, path to secondary evalset spec JSON
- api-key: required, Datalayer API key
- ai-agents-url: optional, override API URL
- billable-account-uid: optional, billable account UID context for eval operations

When `billable-account-uid` is omitted (or empty), the action does not force a
billing override and calls run in the default account context for the API key.
- run-limit: optional, default 50
- output-markdown: optional, default evals-report.md
- secondary-output-markdown: optional, output file for secondary report
- comparison-summary-output: optional, output file for secondary-vs-primary summary
- export-csv: optional, default true
- upload-report-artifacts: optional, default true; uploads generated markdown/csv/log artifacts in a final step
- report-artifact-name: optional, default datalayer-evals-reports
- iam-url: optional, IAM URL override used by mode=execute-runs
- runtimes-url: optional, Runtimes URL override used by mode=execute-runs
- agentspec-ids: optional, comma-separated list of spec ids for mode=execute-runs
- agent-environment-name: optional, default ai-agents-env; used by mode=execute-runs
- execution-target: optional, default cloud; one of cloud or local for mode=execute-runs
- auto-start-local-agent-runtime: optional, default false; when local, auto-start a local agent-runtimes server if none is reachable
- local-agent-base-url: optional, default http://127.0.0.1:8765; local runtime base URL when execution-target=local
- local-agent-name: optional, default default; local agent id when execution-target=local

At least one of evalset-id or evalset-spec-file must be provided.

### Outputs

- report-file: markdown report file path
- csv-file: CSV report file path (empty when export-csv=false)
- log-file: full structured report JSON log file path (captures all failure causes)
- timestamped_report_file: timestamped markdown path
- timestamped_csv_file: timestamped CSV path
- secondary-report-file: secondary markdown report path
- secondary-csv-file: secondary CSV report path
- secondary-log-file: secondary structured report JSON log
- secondary-timestamped-report-file: secondary timestamped markdown
- secondary-timestamped-csv-file: secondary timestamped CSV
- comparison-summary-file: generated comparison summary markdown
- failed-run-count: total number of failed runs across primary and secondary reports
- primary-failed-run-count: number of failed runs in the primary report
- secondary-failed-run-count: number of failed runs in the secondary report

### Use From Another Repository

Example workflow step (single evalset):

```yaml
uses: datalayer/github-actions@v1
with:
	evalset-id: 01KXXXXXXXXXXXX
	api-key: ${{ secrets.DATALAYER_API_KEY }}
	run-limit: "50"
	output-markdown: artifacts/evals-report.md
	export-csv: "true"
```

Example workflow step with execute-runs (runner-backed):

```yaml
uses: datalayer/github-actions@v1
with:
	mode: execute-runs
	evalset-id: 01KXXXXXXXXXXXX
	api-key: ${{ secrets.DATALAYER_API_KEY }}
	evalset-spec-file: .github/evals/spec.evalset.json
	agentspec-ids: example-evals,example-evals-nocodemode
	agent-environment-name: ai-agents-env
	execution-target: cloud
	billable-account-uid: ${{ secrets.DATALAYER_BILLABLE_ACCOUNT_UID }}
	output-markdown: artifacts/evals-report.md
	export-csv: "true"
```

Example workflow step with execute-runs on local agent-runtimes:

```yaml
uses: datalayer/github-actions@v1
with:
	mode: execute-runs
	evalset-id: 01KXXXXXXXXXXXX
	api-key: ${{ secrets.DATALAYER_API_KEY }}
	evalset-spec-file: .github/evals/spec.evalset.json
	agentspec-ids: example-evals
	execution-target: local
	auto-start-local-agent-runtime: "true"
	local-agent-base-url: http://127.0.0.1:8765
	local-agent-name: default
	output-markdown: artifacts/evals-local-report.md
	export-csv: "true"
```

The action now includes a final upload step by default (`upload-report-artifacts=true`) that publishes markdown/csv/log artifacts.

To disable built-in upload and manage upload yourself:

```yaml
uses: datalayer/github-actions@v1
with:
	evalset-id: 01KXXXXXXXXXXXX
	api-key: ${{ secrets.DATALAYER_API_KEY }}
	upload-report-artifacts: "false"
```

Example workflow step (two spec files, one comparison run):

```yaml
uses: datalayer/github-actions@v1
with:
	evalset-spec-file: .github/evals/no-codemode.evalset.json
	secondary-evalset-spec-file: .github/evals/codemode.evalset.json
	api-key: ${{ secrets.DATALAYER_API_KEY }}
	output-markdown: artifacts/no-codemode-report.md
	secondary-output-markdown: artifacts/codemode-report.md
	comparison-summary-output: artifacts/comparison-summary.md
	export-csv: "true"
```

Upload artifacts in the consumer workflow:

```yaml
uses: actions/upload-artifact@v4
with:
	name: evals-report
	path: |
		artifacts/evals-report.md
		artifacts/evals-report.csv
		artifacts/evals-report.md.log
```

For two-spec comparison mode, also upload:

```
		artifacts/no-codemode-report.md
		artifacts/no-codemode-report.csv
		artifacts/no-codemode-report.md.log
		artifacts/codemode-report.md
		artifacts/codemode-report.csv
		artifacts/codemode-report.md.log
		artifacts/comparison-summary.md
```

### Publish New Versions

1. Commit and push changes to main.
2. Tag a version.```yaml

3. Push the tag.

Commands:

git tag -a v1.0.0 -m "datalayer-evals v1.0.0"
git push origin v1.0.0

Recommended tag strategy:

- Maintain a moving major tag for stable consumers.
- Example:
	- v1.0.0 immutable release tag
	- v1 moving major tag

Move major tag:

git tag -f v1 v1.0.0
git push -f origin v1

Consumers should reference v1 for stable updates, or pin an immutable tag for strict reproducibility.
