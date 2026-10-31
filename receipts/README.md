# Receipts

Phone photos of receipts and products, read by local models on the AI Workstation, turned into structured
records and cataloged. The full design is in [PRD.md](PRD.md).

**Status: Phase 1, the parsing bake-off.** Nothing past it is built yet. The bake-off decides which engine
the pipeline will use, so it comes first.

## The bake-off

`bakeoff.py` runs each candidate engine on 25 to 30 real, hand-labeled receipts and scores the results. It
needs Python 3.10+ and the standard library; Pillow is optional but recommended, because without it phone
photos go to vision models unrotated and full size.

| Engine (`engines.json`) | How it reads a receipt |
|---|---|
| `qwen3-vl-8b` | All photos of the receipt in one request to a vision model; JSON back |
| `paddleocr-vl+llm` | PaddleOCR-VL transcribes each photo (`OCR:` prompt); a text LLM structures it |
| `paddleocr+llm` | PaddleOCR text with per-line confidence; a text LLM structures it |
| `doctr+llm` | docTR, the same way |
| `tesseract+llm` | Tesseract, the same way (baseline) |

These candidates came from web roundups and haven't been tried on these receipts; the bake-off decides.
Every model is reached through an OpenAI-compatible endpoint (llama-server, or llama-swap in front of it,
which keeps one model on the shared GPU at a time). The `model` names in `engines.json` must match your
llama-swap config. Edit the file to add, drop or retune candidates.

### 1. Build the test set

```bash
cd ~/ScratchPatch/receipts
python bakeoff.py add ~/Downloads/IMG_0412.jpg --tags thermal,faded
python bakeoff.py add IMG_0413.jpg IMG_0414.jpg IMG_0415.jpg --tags long   # one receipt, top to bottom
```

Each receipt gets a folder under `~/receipts/bakeoff/set/` (`r001`, `r002`, …; or set `RECEIPTS_BAKEOFF_HOME`), with its
photos numbered in order. Receipts with more than one photo are tagged `multi-photo` automatically. Include
crumpled, faded, thermal-paper and long receipts. Photos must be JPEG, PNG or WebP, so export HEIC as JPEG
first. They stay out of the repo.

### 2. Label it

Write `label.json` in each receipt folder:

```json
{
  "store": ["Giant Eagle", "GIANT EAGLE #4"],
  "date": "2026-10-12",
  "tax": "0.42",
  "total": "7.19",
  "items": [
    {"name": "BANANAS", "quantity": 1, "price": "1.29"},
    {"name": "MILK 2% GAL", "price": "3.99"},
    {"name": "BONUS SAVINGS", "price": "-1.00"}
  ]
}
```

- `store`: one name, or a list of accepted spellings. Matching ignores case and punctuation.
- `price`: the amount printed on that line (the line total). Discounts and coupons are items with a
  negative price, and deposits and bag fees are items too, so that items + tax = total.
- Leave out payment, change and loyalty lines.

To save typing, run one engine first and start from its output: `python bakeoff.py label qwen3-vl-8b`
writes `label.draft.json` next to each unlabeled receipt. Correct the draft against the photos, then
rename it to `label.json`. Then run:

```bash
python bakeoff.py check      # unparseable values, implausible dates, items + tax != total
```

### 3. Run and score

```bash
python bakeoff.py run qwen3-vl-8b          # one engine
python bakeoff.py run --all                # every engine in engines.json, one after another
python bakeoff.py score                    # writes ~/receipts/bakeoff/results/report.md
```

The model server comes from `RECEIPTS_LLM_URL`, falling back to `LLM_URL`. The text model for the
OCR + LLM engines comes from `RECEIPTS_TEXT_MODEL`, falling back to `LLM_MODEL`, and the key from
`LLM_API_KEY`. These are the same `~/.config/secrets.env` names that budget uses.

Each engine's output is saved per receipt in `results/<engine>/<receipt>.json`, holding the raw reply,
the parsed JSON, the seconds taken and any OCR lines with confidence (the `engine_runs` table of the PRD).
Re-running skips receipts that are already done; use `--force` to redo them, or `--receipt r007` for one.
OCR + LLM engines first OCR every receipt and then structure every receipt, so the GPU swaps models once
per run rather than once per receipt. Because the first request also loads the model, compare engines on
median seconds.

Scoring follows the PRD. Total, date, store and tax must match exactly. A line item counts as found when
its name is at least 60% similar (`--name-threshold`) and its price is within a cent. The report has:

- **Per engine:** field accuracy, line-item precision / recall / F1, receipts fully right, and seconds.
  It also shows how often the engine's own items + tax = total check passes, and how often the receipt
  is fully right when that check passes, which tells you whether the check can be trusted for auto-accept.
- **By condition:** total accuracy for each tag (thermal, faded, multi-photo, …).
- **Engine pairs:** how often two engines agree on total and date, and whether they are right when they
  agree. This answers whether to run two engines permanently and use their disagreement for confidence.
- **Per receipt:** what each engine got right or wrong.

### With Docker

```bash
cd ~/ScratchPatch/receipts
docker compose build                                 # Tesseract built in; see Dockerfile for the others
docker compose run --rm bakeoff run --all
docker compose run --rm bakeoff score
```

### Installing the OCR back ends (outside Docker)

- Tesseract: `sudo apt install tesseract-ocr`
- docTR: `pip install "python-doctr[torch]"`
- PaddleOCR: `pip install paddleocr paddlepaddle-gpu` (the `paddlepaddle` CPU build also works). Both
  PaddleOCR 2.x and 3.x APIs are handled, but neither has been run here yet.
- PaddleOCR-VL needs a server that speaks the OpenAI chat API, behind llama-swap like the others. Whether
  llama.cpp serves it, or it needs vLLM, still has to be checked on the workstation.

## Tests

```bash
cd receipts && pytest -q
```

The tests use a fake llama-server, so they need no GPU, models or OCR installs.

## Done when

An engine, or a pair of engines, is chosen. Record the choice and the report's numbers in PRD.md (open
questions) before Phase 2 starts.
