# Asset Forge

AI-generated asset packs for ReMoM (Master of Magic, reassembled): a free replacement for the original art, in the spirit of Freedoom. Below: what is built, the plan, and the decisions made so far.

## Built so far

### Step 1: LBX → PNG exporter

Turns every sprite in the game's `.LBX` archives into PNGs, a catalogue and a gallery page. Needs Python 3.10+, Pillow and numpy.

```sh
cd asset-forge
python3 -m pip install -r requirements.txt
python3 -m assetforge.export /c/STU/devel/ReMoM/assets out/originals      # folder with the .LBX files and FONTS.LBX
```

Then open `out/originals/index.html`. Per sprite, in `out/originals/<ARCHIVE>/<entry>_<name>/`:

| File | What it is |
|---|---|
| `frame_NN.png` | The frame as the player sees it: RGBA, game palette 0 (or `--palette N`), plus the sprite's own palette if it has one. Pixels the sprite doesn't draw are transparent. |
| `frame_NN_index.png` | The raw palette indices: grey level = index; alpha 255 = pixel, 128 = remap (shading) pixel, 0 = nothing drawn. Keeps the information that banner colours and shading depend on. |
| `sheet.png` | All frames side by side. |

`catalog.csv` has one row per archive entry: name and description from the LBX name table, size, frames, loop frame, delta or full frames, remap flag, own palette, warnings. The descriptions are the start of the pack manifest.

The decoder (`assetforge/flic.py`) is a port of the engine's `FLIC_Draw_Frame()`, `FLIC_Remap_Draw_Frame()` and `Draw_Picture_To_Bitmap()`. Delta-encoded animations are composited the way the engine does it, so every exported frame is complete.

Remap pixels (shadows and glass effects) recolour whatever is underneath in the game. The RGBA preview approximates them with the remap colour and its percentage from `FONTS.LBX`.

**Not yet checked against real game files.** The original LBX files are copyrighted and not in either repo, so the tests use synthetic sprites. Check:

1. Run the exporter on your v1.31 files.
2. Compare a few sprites with the game: a unit, a terrain tile, a wizard portrait, the main menu background.
3. Look in `catalog.csv` for `undecodable` rows and for anything in `warnings`.

### Step 2: image → description and prompt

For each exported sprite, a local vision model writes what it shows and a prompt that should make an image model draw it again.

```sh
llama-server -m <vision-model>.gguf --mmproj <its-mmproj>.gguf -ngl 99 --port 8080     # on the AI Workstation
python3 -m assetforge.describe out/originals --archive SPELLS --limit 20               # pilot: 20 spell icons
python3 -m assetforge.describe out/originals                                           # everything
```

- Works with any OpenAI-compatible server: llama.cpp's `llama-server`, Ollama or LM Studio. Use `--endpoint` and `--model` to point elsewhere.
- Small sprites are enlarged with hard pixel edges before they're sent (at least 384 px on the long side). Transparent areas are shown as magenta, and the model is told so.
- The model also gets the archive's name and description for the entry.
- Each sprite gets a `describe.json` with these fields: subject, category, description, animation, style, prompt, negative prompt, tags.
- Each `describe.json` also records where it came from: model, endpoint, prompt version, seconds taken.
- Everything is also appended to `descriptions.jsonl`.
- Re-running skips sprites already described, so an interrupted run carries on. Use `--redo` to describe them again.
- The end-of-run line gives the average seconds per sprite: the first number for the budget.

**Not yet run against a real model:** the tests use a fake server. Model quality on tiny pixel art is the thing the pilot has to show.

### Step 3: generate replacements

Draws a replacement for each described sprite with SDXL on the local GPU (optionally with a style LoRA), then makes it game-ready:
1. Removes the flat magenta background the model is told to draw on.
2. Crops to the subject and scales it to the sprite's size, sitting on the bottom edge.
3. Maps every pixel to a palette index.

```sh
python3 -m pip install torch --index-url https://download.pytorch.org/whl/cu128
python3 -m pip install -r requirements-gpu.txt
python3 -m assetforge.generate out/originals out/pack --model sd_xl_base_1.0.safetensors --archive SPELLS --limit 20
```

Open `out/pack/index.html`: each card shows the original, the replacement, and the model's raw image.

Per sprite, in `out/pack/<ARCHIVE>/<entry>_<name>/`:
- `raw.png`: the model's image.
- `frame_00.png` and `frame_00_index.png`: the replacement sprite, in the same two forms the exporter writes.
- `generate.json`: prompt, negative prompt, seed, model, LoRA, steps, timing, and which model wrote the description.

| Option | Default | What it does |
|---|---|---|
| `--model` | (required) | An SDXL `.safetensors` checkpoint, or a diffusers model folder or ID. The first load of a single-file checkpoint downloads its config from Hugging Face. |
| `--lora`, `--lora-scale` | none, 1.0 | A style LoRA. |
| `--scale N` | 1 | Replacement size is the original size × N. At 1 it fits today's ReMoM; bigger needs the ReMoM changes. |
| `--colours sprite` / `game` | `sprite` | `sprite`: only the palette indices the original uses (falls back to `game` when the original uses fewer than 8). Keeps each replacement in its original's colours and its banner-colour indices available. `game`: indices 1–223. 0 is transparent; 224 and up overlap remap and colour cycling. |
| `--style FILE` | built-in | Style text added to every prompt. The step 4 style guide goes here. |
| `--seed N` | 1 | Each sprite's seed is derived from N and the sprite, so a re-run reproduces it. |
| `--archive`, `--limit`, `--redo` | | As in step 2. Already-generated sprites are skipped: the first image is kept. |

Only frame 0 of an animation is generated for now.

**Checked so far:**
- Background removal, fitting and palette mapping are tested on synthetic images.
- The SDXL wrapper ran under diffusers 0.42 against a tiny, randomly initialised SDXL pipeline on CPU, with and without a LoRA: right size, same seed gives the same image, the LoRA changes the output.
- No real checkpoint has been run, so image quality is unknown until the pilot.

### Tests

```sh
python3 -m unittest discover -s tests -t .                                  # synthetic sprites and archives
REMOM_DIR=/c/STU/devel/ReMoM python3 -m unittest tests.test_engine_parity     # also: Python decoder vs the engine's C code
```

The parity test pulls the three decode functions out of ReMoM's `MoX/src/FLIC_Draw.c`, compiles them, and checks that they and the Python port produce the same pixels on 300 random sprites. It needs a C compiler. CI runs it against ReMoM `main`.

## AI Workstation (RTX 5060 Ti, 16 GB)

- **RTX 50-series cards need recent builds.** You need a current NVIDIA driver, a CUDA 12.8 or newer build of llama.cpp, and PyTorch built for CUDA 12.8 or newer (`cu128`, see `requirements-gpu.txt`). Older builds fail with errors about `sm_120`.
- **Vision model (steps 2 and 5):** 16 GB holds a 7B-class vision model at 8-bit, or a larger one at 4-bit. Try a couple on the 20-sprite pilot and keep the one whose prompts work best.
- **Image model:** SDXL-based models run comfortably in 16 GB. Their licence (OpenRAIL++) allows redistributing output, which a Freedoom-style pack needs. Some newer, larger models also fit, but several have non-commercial licences: check the licence before switching.
- **LoRA training:** an SDXL LoRA trains comfortably in 16 GB, for example with kohya_ss or OneTrainer.
- **Sharing the GPU:** an SDXL model in half precision plus a 4-bit 7B vision model fit together. The refine loop (step 5) can then keep both loaded. If memory runs out, run them one at a time.

## Decisions so far

- **Purpose:** a complete replacement for the copyrighted art, Freedoom-style. Later, the same tools make art for a new game from another asset pack.
- **Original art as reference and training data:** accepted (grey area, knowingly).
- **Image model:** local, on the AI Workstation, so a style LoRA can be trained.
- **Style:** a LoRA plus a written style guide.
- **Size:** not limited to the original dimensions or aspect ratio. ReMoM must be changed to load and draw the new assets (a separate ReMoM work item).
- **Sign-off:** at first, accept the first image generated for every asset until the pack is complete. Later: thumbs up/down inside ReMoM, then a web service for browsing, ratings, A/B tests, and user-submitted versions that are ranked alongside.

## What it should do

1. **Text to image.** A description goes in, an image comes out.
2. **Image to prompt.** An image goes in; out come a description and a prompt meant to make another model reproduce it.
3. **Batch to style guide.** A set of images goes in; out comes a written style guide that another model can follow to match them.
4. **Refine loop.** Two models go back and forth: one writes the prompt, the image model draws, a vision model compares the result with a set of reference images and says what is wrong, and the prompt is revised. Repeat until it passes or the budget runs out.
5. **Whole pack.** Given a list of every asset the pack needs, run the loop for each one on its own and produce the full pack.

All five are the same loop with different inputs, so build the loop once.

```
pack manifest ──► for each asset:
                    prompt writer (LLM) ──► image model ──► post-process ──► judge (vision LLM + metrics)
                          ▲                                                        │
                          └──────────── critique, until pass or budget ◄───────────┘
                                                                                   ▼
                                                              approved/ + provenance record
```

## Things the plan depends on

### 1. LLMs don't draw. You need a separate image model.

Claude (and llama.cpp text models) can *describe*, *write prompts*, and *judge* images, but cannot produce them. The image side needs one of:

- a hosted image API (OpenAI, Google Gemini image models, and others), paid per image, no GPU needed; or
- a local diffusion model (Stable Diffusion / Flux family via ComfyUI or diffusers). Free per image, needs a GPU with enough VRAM, and is the only route to training a style LoRA.

Check current options and prices when starting: this market changes month to month.

### 2. Prompts alone will not keep the style consistent.

Wording drifts. What keeps a pack consistent, from strongest to weakest:

1. **Deterministic post-processing** (strongest and cheapest): downscale to the exact target size, quantize to the game's 256-colour palette, force the transparent index, clean up stray pixels. Every image then shares the same palette and pixel grid no matter what the model did.
2. **Reference-image conditioning:** pass sample images to the model along with the prompt (image-to-image, IP-Adapter, or an API's reference-image input).
3. **A style LoRA** trained on a few dozen approved images (local models only).
4. **A fixed style guide** prepended to every prompt (feature 3 writes it).
5. Fixed seeds and model versions, recorded with every image.

### 3. Master of Magic art has hard technical constraints.

From the ReMoM sources (`tools/asset_catalog`, `doc/@AI_Plans/PRD-Graphics-Upscale.md`, `doc/MoX-Palette*.md`):

- **320×200, 8-bit palette-indexed.** Sprites are FLIC-encoded inside 103 `.LBX` archives: about 4,800 sprites. Image models output full-colour RGB at 1024px or so, so everything must be shrunk and quantized.
- **Palette index 0 is transparent**, and about 413 sprites carry their own palette instead of the shared one.
- **Runtime recolouring.** Banner (player) colours, spell tints and shading are palette swaps (remap tables). A generated unit must put its banner colour in the right palette indices or it won't recolour. A plain RGB pipeline loses this.
- **Animations** (multi-frame FLIC) must stay registered frame to frame. Image models are bad at this. Leave animated sprites until static ones work.
- **Terrain tiles must tile seamlessly.** Needs its own check.

### 4. Two tools are missing in ReMoM, and they come first.

- **LBX → PNG exporter.** No tool on the project renders a sprite to a viewable image yet (`lbx_extract` dumps opaque `.BIN`). Without it there is no reference set to compare against. This is Phase 1 of the existing upscale PRD; it serves both projects.
- **PNG → LBX writer** (FLIC encoder). Without it, generated art can't be seen in the running game. Until then, review in a gallery page.

### 5. The asset list already exists.

`python -m tools.asset_catalog` writes `tools/fields/assets.fwv`: one row per sprite with archive, entry, name, width, height, frames, palette and remap flags. That is the skeleton of the pack manifest. Each row needs a short description of what it depicts and a category (unit, terrain, portrait, UI, spell icon) added.

### 6. The judge needs more than an LLM opinion.

Vision LLMs are inconsistent judges and drift toward approving. Combine them with numeric checks:

- exact size, palette and transparency checks (pass/fail, free);
- embedding similarity to the reference set (CLIP/DINO), to catch style drift;
- a vision-LLM critique that returns specific changes (for the next prompt) plus a score, in a fixed JSON format;
- a hard cap on rounds and spend per asset, with failures queued for a human instead of retried forever.

### 7. Record where every image came from.

For each image: prompt, model and version, seed, reference images used, round number, judge scores, and cost. You need this to reproduce an image, to re-run a pack on a newer model, and for the licensing question below.

## Still open

1. **The AI Workstation:** RTX 5060 Ti, 16 GB. Still to confirm: which OS.
2. **Image model licence.** For a Freedoom-style pack you need to be free to redistribute the output. Model licences differ: some are permissive, and some popular ones restrict commercial use. Pick a model whose licence allows redistribution, and record the model name and version with every image.
3. **The pack's own licence:** for example BSD like Freedoom, or CC BY-SA. It has to be compatible with the model's terms.
4. **Budget.** Running locally, the cost is GPU time, not money. Run a pilot of about 20 assets in one category and measure time per image, time per describe or judge call, and how many images were usable. Then set per-asset caps.

## Build order

Each step is small, finishes, and is useful on its own:

1. **LBX → PNG exporter.** Gives you the reference set. *Built, see above; needs a check on real files.*
2. **Describe:** image → description + prompt (one vision-LLM call). *Built, see above; needs a pilot on a real model.*
3. **Generate + post-process:** prompt → image → resized, palette-quantized PNG. *Built, see above; needs a pilot on a real checkpoint.*
4. **Style guide:** batch of references → style guide text.
5. **Refine loop:** prompt → image → judge → revised prompt, with round and cost caps and provenance records.
6. **Pack runner:** manifest in, approved images plus a review gallery out.
7. **PNG → LBX writer:** see the pack in the game.
8. Later: animations, seamless terrain, banner-colour remap ranges.

Prototype here in ScratchPatch. Move the format tools (steps 1 and 7) into ReMoM `tools/`, because the upscale project needs them too.
