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

World-map terrain tiles are not sprites: they are 1,761 plain 20×18 bitmaps packed into `TERRAIN.LBX` entry 0, decoded as `Draw_Picture()` and `MainScr_Maps.c` read them. They are listed as archive `TERRAIN_TILES`, one folder per tile.

**Checked on the real v1.31 files** (2026-10-29, AI Workstation, 34 s):
- 6,522 graphics decoded: 4,761 sprites from 77 archives plus the 1,761 terrain tiles. None failed and none had warnings.
- Spot-checked by eye: wizard portraits (own palette), unit figures with banner colours, buttons, the main-screen frame, spell-screen panels, and the terrain tiles.
- 503 entries are not graphics: sounds, music, data tables.
- Ten `HLPENTRY.LBX` data entries start with bytes that look like a sprite header. They are rejected because their frame tables point outside the entry.

`out/` holds the original copyrighted art and is git-ignored. Keep it that way.

### Step 2: image → description and prompt

For each exported sprite, a local vision model writes what it shows and a prompt that should make an image model draw it again.

```sh
llama-server -m <vision-model>.gguf --mmproj <its-mmproj>.gguf -ngl 99 --port 8080     # on the AI Workstation
python3 -m assetforge.describe out/originals --archive SPELLS --limit 20               # pilot: 20 spell icons
python3 -m assetforge.describe out/originals --skip-bucket terrain                     # everything but world terrain
python3 -m assetforge.describe out/originals                                           # everything
```

- Works with any OpenAI-compatible server: llama.cpp's `llama-server`, Ollama or LM Studio. Use `--endpoint` and `--model` to point elsewhere.
- Small sprites are enlarged with hard pixel edges before they're sent (at least 384 px on the long side). Transparent areas are shown as magenta, and the model is told so.
- The model also gets the archive's name and description for the entry.
- Each sprite gets a `describe.json` with these fields: subject, category, description, animation, style, prompt, negative prompt, tags.
- Each `describe.json` also records where it came from: model, endpoint, prompt version, seconds taken.
- If the export folder has a `hints.csv` (archive, entry, hint), the model is told what each sprite is. `python3 -m assetforge.remom_hints ~/STU/devel/ReMoM out/originals` writes one from ReMoM's game data. It covers:
  - all 198 unit icons;
  - the combat figures: 8 per unit, one per facing, in `Combat_Figure_Load()` order;
  - the 44 summoned-creature pictures in MONSTER.LBX.

  FIGURE15 and FIGURE16 aren't loaded by any code path and have no hints.
- Everything is also appended to `descriptions.jsonl`.
- Re-running skips sprites already described, so an interrupted run carries on. Use `--redo` to describe them again.
- The end-of-run line gives the average seconds per sprite: the first number for the budget.


### Step 3: generate replacements

Draws a replacement for each described sprite with SDXL on the local GPU (optionally with a style LoRA), then makes it game-ready:
1. Removes the flat magenta background the model is told to draw on.
2. Crops to the subject and fits it to 8× the sprite's size, sitting on the bottom edge.
3. Maps every pixel to a palette index, then turns each 8×8 block into one pixel of its most common colour, and redraws a dark outline.

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
| `--no-outline` | outline on | Don't redraw the sprite's edge in the darkest allowed colour. |
| `--colours sprite` / `game` | `sprite` | `sprite`: only the palette indices the original uses (falls back to `game` when the original uses fewer than 8). Keeps each replacement in its original's colours and its banner-colour indices available. `game`: indices 1–223. 0 is transparent; 224 and up overlap remap and colour cycling. |
| `--style FILE` | built-in | Style text added to every prompt. The step 4 style guide goes here. |
| `--seed N` | 1 | Each sprite's seed is derived from N and the sprite, so a re-run reproduces it. |
| `--archive`, `--limit`, `--redo` | | As in step 2. Already-generated sprites are skipped: the first image is kept. |

Only frame 0 of an animation is generated for now.

**Checked so far:**
- Background removal, fitting and palette mapping are tested on synthetic images.
- The SDXL wrapper ran under diffusers 0.42 against a tiny, randomly initialised SDXL pipeline on CPU, with and without a LoRA: right size, same seed gives the same image, the LoRA changes the output.
- Run for real in pilot 1, below.

### Buckets and the LoRA training set

```sh
python3 -m assetforge.buckets out/originals                   # add the bucket column to an existing catalog.csv
python3 -m assetforge.dataset out/originals out/dataset       # build the training set (about a minute, CPU only)
python3 -m assetforge.dataset out/originals out/dataset --bucket figure --frames all
```

**Buckets** group graphics that are drawn, trained and generated the same way. New exports get a `bucket` column in `catalog.csv` automatically. The rules (`assetforge/buckets.py`) go by archive. Two size rules come first: anything about full-screen size is a `screen`, and DIPLOMAC pictures 64 px or bigger are portraits.

| Bucket | Trigger word | Sprites | Archives |
|---|---|---|---|
| figure | momfigure | 1,849 | FIGURES1–9, FIGURE10–16, CMBMAGIC |
| unit_icon | momuniticon | 198 | UNITS1, UNITS2 |
| portrait | momportrait | 167 | PORTRAIT, WIZARDS, MOODWIZ, LILWIZ, large DIPLOMAC |
| terrain | momterrain | 1,832 | TERRAIN_TILES, MAPBACK |
| combat_terrain | momcombatterrain | 755 | COMBAT, CMB*, DESERT, TUNDRA, CHRIVER |
| building | mombuilding | 350 | CITYSCAP, CMBTCITY, CITYWALL, CMBTWALL, WALLRISE |
| item | momitem | 152 | ITEMS, ITEMISC |
| icon | momicon | 362 | SPECIAL, SPECIAL2, COMPIX, UNITVIEW, RESOURCE |
| art | momart | 333 | MONSTER, SPECFX, SPELLOSE, CONQUEST, CMBTFX, SPLMASTR, EVENTS, WIN, LOSE, WIZLAB |
| screen | momscreen | 52 | 320×200 and similar, any archive |
| ui | momui | 472 | everything else |

**Training set** (`assetforge.dataset`), in the folder layout kohya_ss and OneTrainer read (`img/<repeats>_<token>/`, an image and a `.txt` caption per sample):
- **Crisp enlargement.** Each original is enlarged by the biggest whole number that fits the SDXL training size closest in shape, with nearest neighbour, so pixels stay square.
- **Key colour.** Transparent parts are filled with a flat key colour, and the picture is centred on a canvas of that colour at exactly an SDXL size, so the trainer doesn't resize it. Each bucket gets the key farthest from every colour its originals use (`buckets.json` records it). The aim is a LoRA that always draws that flat colour, so generation can key it out exactly. SDXL base ignores "plain magenta background" in a prompt.
- **Captions** start with the bucket's trigger word. The text comes from `describe.json` if the sprite has been described, otherwise from `hints.csv`, otherwise from the bucket's noun. Transparent pictures end with "isolated on a plain flat <key> background".
- **Duplicates are dropped.** Pictures under 6 px are skipped. Smaller buckets are repeated up to about `--balance` (500) images per epoch.
- **First run:** 5,829 images (555 duplicates skipped), 77 MB, 47 s. Keys: magenta for figures, unit icons, terrain and items; green for portraits, buildings, art, screens and UI; blue for icons.
- **Captions are mostly generic so far.** Only the 20 piloted icons have descriptions. Hints name 165 more unit icons, 1,448 combat figures and 40 monster pictures. The rest say only what the bucket is. A describe pass over everything (about 6 h of GPU) should come before training, or the LoRA learns style but can't tie it to subjects.

### Tests

```sh
python3 -m unittest discover -s tests -t .                                  # synthetic sprites and archives
REMOM_DIR=/c/STU/devel/ReMoM python3 -m unittest tests.test_engine_parity     # also: Python decoder vs the engine's C code
```

The parity test pulls the three decode functions out of ReMoM's `MoX/src/FLIC_Draw.c`, compiles them, and checks that they and the Python port produce the same pixels on 300 random sprites. It needs a C compiler. CI runs it against ReMoM `main`.

## Pilot 1 (2026-10-30): 20 hero unit icons

`UNITS1.LBX` entries 0–19, 18×16 each. Qwen3-VL 8B (4-bit) described them, then SDXL base 1.0 drew them (30 steps, `--colours sprite`, outline on). The vision container (`assetforge-vision`, port 11436) and SDXL ran one at a time, with the coder container stopped.

**Speed:**
- Describe: 3.8 s per sprite.
- Generate: 11.4 s per sprite.
- One first-version pass over all 4,761 sprites would take about 5 h (describe) + 15 h (generate) of GPU time. Terrain is excluded: it needs the procedural approach.

**What the pilot showed, and the fixes made:**
- **Archive names mislead the vision model.** "STATFIG1" became "statue of a wizard". `assetforge.remom_hints` now writes `hints.csv` from ReMoM's unit and race tables (all 198 unit icons named), and describe tells the model to trust it. Prompt v2 also stops it describing the magenta background.
- **SDXL reads only 77 tokens.** The magenta-background instruction at the end was being cut off. The prompt is now trimmed to fit with the model's tokenizer, dropping description phrases rather than the background or style.
- **Smooth downscaling to 18×16 turns to mush.** Now the image is mapped to the palette at 8× size, each 8×8 block becomes its most common colour, and a dark outline is redrawn. This gives flat colours and hard edges.

**Still open:**
- **SDXL base ignores "plain magenta background"** and draws muted, graded backgrounds, sometimes with frames or several figures. Background removal works only when the backdrop happens to be plain. Plan: train the LoRA on originals on a flat key colour (see the training set above), so the colour key works again. Keep a background-removal model as a fallback.
- **The originals share one template:** a small figure on a brown mount, under the green banner. Generated icons are free-standing figures. Style and composition are what the LoRA trained on the originals is for.
- **Banner colour:** the green patch is the owner's banner, but the vision model calls it a "green helmet". Banner colours need handling per unit.

## Background-removal test (2026-10-30)

Three rembg models (MIT licence) on pilot 1's 20 SDXL images, CPU only (onnxruntime), each run in its own process with RAM capped at 20 GB:

| Model | Per image | Peak RAM | Result |
|---|---|---|---|
| isnet-general-use | 0.2 s | 1.5 GB | Clean figures; also drops most of SDXL's frames and circles. **The one to use.** |
| isnet-anime | 0.2 s | 1.8 GB | Drops the whole figure in about 6 of 20. |
| birefnet-general | 4.9 s | 13.9 GB | Keeps frames, character sheets and crowds as subject. |

BiRefNet's 13.9 GB is a fixed working set (reached by image 3, then flat), not a leak. An earlier uncapped run of it filled 30 GB of RAM plus 8 GB of swap and took the desktop session down with it. The coder model's `llama-server` alone holds 7.7 GB of ordinary RAM.

**Rule from this:** every heavy job runs under a memory cap, so the job dies instead of the desktop:

```sh
systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 .venv/bin/python -m assetforge.<step> ...
```

## AI Workstation (RTX 5060 Ti, 16 GB, Linux)

- **RTX 50-series cards need recent builds.** You need a current NVIDIA driver, a CUDA 12.8 or newer build of llama.cpp, and PyTorch built for CUDA 12.8 or newer (`cu128`, see `requirements-gpu.txt`). Older builds fail with errors about `sm_120`.
- **Vision model (steps 2 and 5):** 16 GB holds a 7B-class vision model at 8-bit, or a larger one at 4-bit. Try a couple on the 20-sprite pilot and keep the one whose prompts work best.
- **Image model:** SDXL-based models run comfortably in 16 GB. Their licence (OpenRAIL++) allows redistributing output, which a Freedoom-style pack needs. Some newer, larger models also fit, but several have non-commercial licences: check the licence before switching.
- **LoRA training:** an SDXL LoRA trains comfortably in 16 GB, for example with kohya_ss or OneTrainer.
- **Sharing the GPU:** an SDXL model in half precision plus a 4-bit 7B vision model fit together. The refine loop (step 5) can then keep both loaded. If memory runs out, run them one at a time.
- **Set up on this machine:**
  - Python environment: `asset-forge/.venv` (PyTorch 2.11 for CUDA 12.8, diffusers 0.42).
  - SDXL base: `~/develai/stable-diffusion-webui/models/Stable-diffusion/sd_xl_base_1.0.safetensors`.
  - Qwen3-VL: `~/develai/llama.cpp/models/qwen3-vl/`.
  - Vision server: start it with `docker run -d --rm --name assetforge-vision --gpus all -p 127.0.0.1:11436:8080 -v ~/develai/llama.cpp/models/qwen3-vl:/models:ro ghcr.io/ggml-org/llama.cpp:server-cuda13 -m /models/Qwen3VL-8B-Instruct-Q4_K_M.gguf --mmproj /models/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf -ngl 99 -c 8192 --host 0.0.0.0 --port 8080`, then pass `--endpoint http://127.0.0.1:11436/v1` to describe.
  - The coder container (`develai-llama-cpp`) uses 12.8 GB: stop it before a run and `docker start` it afterwards.

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
- **Terrain tiles must tile seamlessly.** Most of the 1,761 tiles are coastline and river joining pieces: hundreds of variants of a few base textures. Generating them one at a time can't line up. Plan: generate the base textures (grass, ocean, forest, ...) and build the joining pieces from them with code, using the originals' shapes as masks.

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

1. **Image model licence.** For a Freedoom-style pack you need to be free to redistribute the output. Model licences differ: some are permissive, and some popular ones restrict commercial use. Pick a model whose licence allows redistribution, and record the model name and version with every image.
2. **The pack's own licence:** for example BSD like Freedoom, or CC BY-SA. It has to be compatible with the model's terms.
3. **Budget.** Pilot 1 measured 3.8 s per description and 11.4 s per image: about 20 GPU hours for a first-version pass over every sprite. Per-asset caps for the refine loop can be set once it exists.

## Build order

Each step is small, finishes, and is useful on its own:

1. **LBX → PNG exporter.** Gives you the reference set. *Built and checked on the real files.*
2. **Describe:** image → description + prompt (one vision-LLM call). *Built; piloted.*
3. **Generate + post-process:** prompt → image → resized, palette-quantized PNG. *Built; piloted. Next: describe everything, then train the LoRA on `assetforge.dataset` output.*
4. **Style guide:** batch of references → style guide text.
5. **Refine loop:** prompt → image → judge → revised prompt, with round and cost caps and provenance records.
6. **Pack runner:** manifest in, approved images plus a review gallery out.
7. **PNG → LBX writer:** see the pack in the game.
8. Later: animations, seamless terrain, banner-colour remap ranges.

Prototype here in ScratchPatch. Move the format tools (steps 1 and 7) into ReMoM `tools/`, because the upscale project needs them too.
