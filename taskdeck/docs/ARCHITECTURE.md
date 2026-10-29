# Architecture

## Layers

```
ui/      (Flet)  ──uses──▶  core/  ◀──uses──  jobs/*  ──uses──▶  shared/
                              ▲                                    │
                              └──────────────uses──────────────────┘
review/  reads jobs/ + shared/ source and core metadata; never imported by the app
```

* **core** has no UI dependency, so the plugin contract, pipeline runner, palette ranking and favorites can
  be unit-tested quickly and could sit behind a different front end later.
* **jobs** depend only on `core` and `shared`, never on `ui`. A job describes *what* it needs (fields) and
  *what it does* (pipeline); the UI decides how that looks. This keeps navigation consistent: every job
  looks and behaves the same.
* **shared** is where code goes once more than one job needs it.

## Plugin contract (`core/plugin.py`)

```python
class UnitConvert(JobPlugin):
    id = "unit_convert"                     # stable id: used for pins, history and palette ids
    title = "Unit Converter"
    description = "..."                     # shown in lists, searched by the palette
    category = "Everyday"                   # groups the All jobs screen
    icon = "SWAP_HORIZ"                     # flet.Icons name
    tags = ("km", "miles", ...)             # extra palette keywords
    fields = (Field.number("value", "Value", required=True), Field.choice(...), ...)
    quick_actions = (QuickAction("Convert: km to mi", {"from_unit": "km", "to_unit": "mi"}),)
    accounts = ()                           # accounts it signs in with, e.g. ((GOOGLE_ACCOUNT, "drive"),):
                                            # unlocked before Run (docs/CREDENTIALS.md)

    def build_pipeline(self) -> Pipeline: ...

PLUGIN = UnitConvert()
```

Discovery (`PluginRegistry.discover`) imports every sub-package of `taskdeck.jobs` and every
`taskdeck.jobs` entry point, so external packages can ship jobs too. A plugin that fails to import is listed
under *Failed to load* in All jobs and does not crash the app.

Field kinds: `text`, `multiline`, `number`, `choice`, `toggle`. `Field.coerce` turns raw UI values into
Python values and raises `StepError` with a message the user can act on.

`quick_actions` are presets: each one becomes its own palette entry that opens the job with inputs
pre-filled.

Jobs that copy files somewhere set `nature` to `JobNature.ONE_TIME_COPY` (a fixed snapshot, e.g. an
archive: mismatching files there are broken leftovers, uploaded fresh) or `JobNature.ONGOING_SYNC` (files
that keep changing: older versions are updated in place). See [JOB_KINDS.md](JOB_KINDS.md).

## Pipeline (`core/pipeline.py`)

A `Pipeline` is an ordered list of `Step`s that share a `Context`:

* `ctx["name"]` reads a value from the intermediate data first, then from the inputs. `ctx["key"] = v`
  stores intermediate data.
* A step raises `StepError("message")` to stop with a user-facing error. Any other exception is caught
  and reported with the step's name, so a buggy plugin cannot crash the app.
* A step can set `ctx.result = Result(text=..., rows=[...])`, which the UI renders. Usually a shared
  `present_*` step does this.
* `Pipeline.run` reports progress per step, supports cancellation and returns a `RunReport` with
  per-step timings. The UI shows this in the Pipeline panel.

Every step declares one of four **stages**:

| Stage | Purpose | Shared examples |
|---|---|---|
| `VALIDATE` | check inputs | `require`, `in_range` |
| `PREPARE` | normalize or derive inputs | `normalize_text` |
| `EXECUTE` | the job's actual work | (job-specific) |
| `PRESENT` | shape output for display | `present_rows`, `present_text` |

Today stages are labels. They let the review compare pipelines by *shape* (for example
`validate → prepare → execute → present`) and check that steps run in canonical order. They are the hook
for the "bigger, more defined pipeline": when the review shows that most jobs share a shape, promote it to a
template in `core` (see the roadmap below).

Shared steps are built by factories decorated with `@shared_step(stage)`, which gives every step they
produce a stable `shared.<name>` key. The review uses that key to measure how widely each shared step is used.

## Command palette (`core/commands.py`, `ui/palette.py`)

* The app registers one command per job, one per quick action, plus navigation, pin/unpin, run/close
  current job, theme and history actions.
* Commands can be context-sensitive (`when=`), e.g. *Unpin current job* appears only while a pinned job is open.
* Ranking: an in-order fuzzy subsequence match on the title, with bonuses for consecutive characters, word
  starts and prefix matches. If the title doesn't match, every query word must appear in the subtitle or
  keywords (scored lower). Use counts from the palette and from job runs add a small boost, so the palette
  adapts to how you work.
* Prefixes: `>` actions only, `@` jobs and presets only.

## Persistence (`core/storage.py`, `core/favorites.py`)

A single JSON file (`state.json`) holds pins (in order), run history (capped at 100, inputs restricted to
JSON scalars), palette usage counts and settings. Writes are atomic (temp file + `os.replace`).

## Roadmap for the pipeline

1. **Now**: free-form pipelines with stage labels, shared steps and the review to measure overlap.
2. **When a shape dominates**: add `core.templates.StandardPipeline(validate=[...], prepare=[...],
   execute=..., present=...)` that enforces stage order and fills in default steps (for example `require`
   for every required field). Migrate jobs one at a time; the review's *shape* section tracks progress.
3. **When step runs repeat**: add composite shared steps (e.g. `text_input(name)` = `require` +
   `normalize_text`) once a run appears in three or more jobs.
4. **Later**: async/long-running steps (network, files), with the same progress and cancellation model.
