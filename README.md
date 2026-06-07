[![Datalayer](https://assets.datalayer.tech/datalayer-25.svg)](https://datalayer.io)

[![Become a Sponsor](https://img.shields.io/static/v1?label=Become%20a%20Sponsor&message=%E2%9D%A4&logo=GitHub&style=flat&color=1ABC9C)](https://github.com/sponsors/datalayer)

# ☰ 👉 Datalayer GitHub Actions

This repository contains reusable GitHub Actions for Datalayer workflows.

## datalayer-evals

The datalayer-evals action runs Datalayer eval reports in CI and produces report artifacts.

It uses the `datalayer-core` Python API directly (the `DatalayerClient` and the
core eval-report helpers) rather than shelling out to the CLI, so the generated
reports contain the full structured failure diagnostics (per-run failure causes,
stages, types and detail excerpts). Failures are also aggregated into the GitHub
step summary and exposed as action outputs.

It supports two execution modes:

- Primary report mode (single evalset).
- Comparison mode (primary + secondary evalsets) with a generated summary markdown.

Evalsets can be provided as IDs, or created on the fly from spec files.

Primary report mode produces, for each report:

- `<output-markdown>` and a matching `.csv` (when export-csv is true)
- a `<output-markdown>.log` artifact containing the full structured report JSON
  (including every run-level failure cause)
- timestamped files `report-<timestamp>.md` and `report-<timestamp>.csv`

The action is implemented in Python and can be consumed from other repositories.

### Inputs

- evalset-id: required, target evalset UID
- evalset-spec-file: optional, path to primary evalset spec JSON; action creates evalset and reports it
- secondary-evalset-id: optional, secondary evalset UID
- secondary-evalset-spec-file: optional, path to secondary evalset spec JSON
- token: required, Datalayer API token
- ai-agents-url: optional, override API URL
- account-uid: optional, account/org context
- run-limit: optional, default 50
- output-markdown: optional, default evals-report.md
- secondary-output-markdown: optional, output file for secondary report
- comparison-summary-output: optional, output file for secondary-vs-primary summary
- export-csv: optional, default true
- iam-url: optional, IAM URL override used when creating the optional agent runtime
- runtimes-url: optional, Runtimes URL override used when creating the optional agent runtime
- agentspec-id: optional, create an agent runtime before reporting using this spec id (default example-simple)
- agentspec: optional, URL or local file path to YAML/JSON agent spec; mutually exclusive with agentspec-id
- agent-environment-name: optional, default ai-agents-env
- agent-given-name: optional runtime name for the created agent runtime
- agent-time-reservation: optional runtime reservation in minutes, default 10
- billable-account-uid: optional billable account UID used when creating the optional agent runtime

If billable-account-uid is not provided, the action also checks the environment
for DATALAYER_BILLABLE_ACCOUNT_UID (for example from a repository secret).

When the action creates a runtime via agentspec-id or agentspec, it
automatically tears the runtime down after report generation (including
early-exit paths).

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
- agent-runtime-pod-name: pod name of runtime optionally created through the core client
- agent-runtime-ingress: ingress URL of that optional runtime
- failed-run-count: total number of failed runs across primary and secondary reports
- primary-failed-run-count: number of failed runs in the primary report
- secondary-failed-run-count: number of failed runs in the secondary report

### Use From Another Repository

Example workflow step (single evalset):

uses: datalayer/github-actions@v1
with:
	evalset-id: 01KXXXXXXXXXXXX
	token: ${{ secrets.DATALAYER_API_KEY }}
	run-limit: "50"
	output-markdown: artifacts/evals-report.md
	export-csv: "true"

Example workflow step with runtime bootstrap from spec id before report:

uses: datalayer/github-actions@v1
with:
	evalset-id: 01KXXXXXXXXXXXX
	token: ${{ secrets.DATALAYER_API_KEY }}
	agentspec-id: example-simple
	agent-environment-name: ai-agents-env
	agent-time-reservation: "10"
	billable-account-uid: ${{ secrets.DATALAYER_BILLABLE_ACCOUNT_UID }}
	output-markdown: artifacts/evals-report.md
	export-csv: "true"

Example workflow step (two spec files, one comparison run):

uses: datalayer/github-actions@v1
with:
	evalset-spec-file: .github/evals/no-codemode.evalset.json
	secondary-evalset-spec-file: .github/evals/codemode.evalset.json
	token: ${{ secrets.DATALAYER_API_KEY }}
	output-markdown: artifacts/no-codemode-report.md
	secondary-output-markdown: artifacts/codemode-report.md
	comparison-summary-output: artifacts/comparison-summary.md
	export-csv: "true"

Upload artifacts in the consumer workflow:

uses: actions/upload-artifact@v4
with:
	name: evals-report
	path: |
		artifacts/evals-report.md
		artifacts/evals-report.csv
		artifacts/evals-report.md.log

For two-spec comparison mode, also upload:

		artifacts/no-codemode-report.md
		artifacts/no-codemode-report.csv
		artifacts/no-codemode-report.md.log
		artifacts/codemode-report.md
		artifacts/codemode-report.csv
		artifacts/codemode-report.md.log
		artifacts/comparison-summary.md

### Publish New Versions

1. Commit and push changes to main.
2. Tag a version.
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
