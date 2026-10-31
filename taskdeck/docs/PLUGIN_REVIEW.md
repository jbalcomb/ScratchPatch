# Plugin review

Reviewing the job plugins for shared code is an ongoing process, not a one-time clean-up. It has an
automated part (the analyzer, run in CI) and a human part (deciding what to extract, recorded below).

## The analyzer

```bash
PYTHONPATH=src python -m taskdeck.review                # Markdown report to stdout
PYTHONPATH=src python -m taskdeck.review --json --out reports/review.json
PYTHONPATH=src python -m taskdeck.review --strict       # exit 1 on duplicate code / hygiene / load errors
```

What the report contains:

| Section | How it's found | Typical action |
|---|---|---|
| **Duplicate code** | Every top-level function in every job module (and in `taskdeck.shared`) is parsed, docstrings are dropped and local names are replaced with placeholders, then pairs from *different* plugins are compared with a token diff. Renamed copies score 100%. | `extract`: move to `taskdeck.shared`. `reuse`: a shared helper already exists, so call it. |
| **Repeated step sequences** | Contiguous runs of step keys that two or more pipelines share (only the longest runs are reported). | When a run appears in 3+ jobs, add a composite shared step. |
| **Pipeline shapes** | Each pipeline's stage sequence, grouped. | When most jobs share a shape, promote it to a pipeline template (see ARCHITECTURE roadmap). |
| **Shared-step adoption** | Fraction of each pipeline's steps that come from `taskdeck.shared`. | Low adoption is a prompt to look at that job first. |
| **Plugin hygiene** | Missing description/tags, no PRESENT step, required fields without VALIDATE, stages out of order, duplicate field names, `build_pipeline` errors. | Fix in the plugin. |

Tune the duplicate detector with `--threshold` (default 0.85) and `--min-tokens` (default 40, which skips
trivial functions).

## When it runs

* **Every push and PR**: CI runs the report, attaches it to the job summary and as an artifact, and fails the
  build on duplicate code, hygiene issues or load errors (`--strict`). Repeated step sequences and shapes
  are advisory and never fail the build.
* **Weekly** (Monday cron): the same job, so drift shows up even when nobody touches `jobs/`.
* **Before adding a job**: run it locally. `tests/test_plugins.py` also enforces hygiene for every built-in job.

## Review checklist (per new or changed job)

- [ ] Does any step or helper do what an existing `taskdeck.shared` function does? Use the shared one.
- [ ] Is anything here likely to be needed by a second job? Write it in `shared` from the start.
- [ ] Are the steps tagged with the right stage and in canonical order?
- [ ] Does the job's error text tell the user what to fix?
- [ ] Are there tags and quick actions that make the job easy to find in the palette?
- [ ] Does the report's duplicate section stay empty?

## Extraction rules

1. **Rule of two for helpers, three for pipeline structure.** Duplicate functions get extracted at the
   second copy. Composite steps and pipeline templates wait for a third user, so the abstraction is shaped
   by real cases.
2. Shared steps are factories decorated with `@shared_step(stage)`, so the review can track them.
3. Extraction is behaviour-preserving: move the code, point the jobs at it, and keep the job tests passing.
4. Record the decision in the log below, including decisions *not* to extract.

## Review log

| Date | Finding | Decision |
|---|---|---|
| 2026-10-18 | Initial set of 6 jobs. Input validation (`require`, `in_range`), text normalization and result presentation were written once in `taskdeck.shared.steps`; word splitting is in `taskdeck.shared.text` (used by `text_stats` and `case_convert`). | Baseline. Duplicate code: none. |
| 2026-10-18 | Step run `require → normalize_text` in 2 jobs (`case_convert`, `text_stats`); `require → in_range` in 2 jobs (`json_format`, `tip_split`). | Below the rule-of-three bar; revisit when a third text job lands (candidate: `shared.text_input`). |
| 2026-10-18 | Shapes: 3 × `validate → prepare → execute → present`, 3 × `validate → execute → present`. | The second is the first with PREPARE optional. That is the candidate template; wait for more jobs before building it. |
| 2026-10-18 | Added `daily_log`, the first job with network I/O, settings and a commit step. Its HTTP and Google auth code went straight into `taskdeck.shared.http` / `taskdeck.shared.google`, since other jobs will need them. Its pipeline uses only 20% shared steps; its local steps are job-specific (model call, calculations). The review now scans every module of a multi-file job and every `taskdeck.shared` module. | No duplicates. Watch for a second model-calling job: "call model → parse JSON" is the next shared-step candidate. |
| 2026-10-19 | Added `daily_log_reconcile`, a second job over the same sheet and settings. Shared settings became a core feature (`JobPlugin.settings_from`) rather than a copy; confirm-before-overwrite became core too (`NeedsConfirmation`, shown by the host as a button). Sheet reads/writes/backups live in `taskdeck.shared.google`. The hygiene rule "declares no input fields" now allows jobs that only need settings. | No duplicates. If a second sheet-backed job appears, the "one row per key, no gaps" logic in `daily_log/reconcile.py` is the next extraction candidate (`taskdeck.shared.sheet_table`). |
| 2026-10-25 | Credentials vault: secret settings moved from the JSON store into `taskdeck.core.secrets.Vault`, routed by the host. Jobs only declare `secret=True`. | Core, not shared: every job's secrets go through it without code in the job. |
| 2026-10-26 | Added `extract_archive`, the first job with its own screen (`make_screen`) and work that outlives its screen. Background work became core (`taskdeck.core.tasks.TaskManager`, shown on Home); phone storage helpers went to `taskdeck.shared.files`, the file browser to `taskdeck.ui.file_browser`, the 7-Zip engine to `taskdeck.shared.archive`. The engine-check test job was removed. | No duplicates. The job keeps fields + a pipeline too, so it runs headless in tests. |
| 2026-10-26 | Google Drive destination for `extract_archive`. Sign-in (`taskdeck.shared.google_oauth`, device flow), the Drive client (`taskdeck.shared.gdrive`, resumable uploads) and connected accounts (`taskdeck.ui.accounts`, stored as credentials) are shared, not job code: Daily Log or Dropbox can use the same pieces. `http.send` added for status-level access (308 resumes). | No duplicates. |
| 2026-10-31 | Added the six Receipts jobs (`taskdeck.jobs.receipts`), over one HTTP API with one shared setting. Multipart uploads went into `taskdeck.shared.http` (`multipart`, `post_multipart`, raw `data` for `request`). The API client, readiness checks and a `fetch` step stay in `receipts/api.py`. Runs `service_configured → fetch` and `service_configured → read_photos` repeat inside the package only. | No duplicates. `fetch` (GET a path into `ctx`) moves to `taskdeck.shared.steps` if a job outside Receipts needs it. |
