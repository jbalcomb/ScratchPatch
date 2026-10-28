# Asset Forge: plan

AI-generated asset packs for ReMoM (Master of Magic, reassembled). Nothing is built yet; this is the plan and the list of open questions.

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

## Questions only you can answer

1. **What is the pack for?**
   - A free replacement art set so ReMoM runs without the original data files.
   - A visual mod or upscale of the original look.
   - Art for a new game in a similar style.

   This decides the rest: resolution, whether the original art may be used as references, and whether the result can be shared.
2. **Legal.** The original art is copyrighted. Using it as reference images to make a *distributable* replacement is a grey area. Also check the image model's terms for commercial use and redistribution.
3. **Resolution.** Native 320×200, or hi-res assets for the upscale path? (The upscale BRA hasn't decided this yet.)
4. **Hosted or local image model,** and what GPU is on the AI Workstation?
5. **Budget** per asset and per pack.
6. **How good is good enough,** and who signs off? A human review gallery with approve/reject/retry is probably required at first.

## Build order

Each step is small, finishes, and is useful on its own:

1. **LBX → PNG exporter** (in ReMoM, per the upscale PRD). Gives you the reference set.
2. **Describe:** image → description + prompt (one vision-LLM call). The smallest real piece.
3. **Generate + post-process:** prompt → image → resized, palette-quantized PNG. Start with one static category, such as spell icons or unit figures.
4. **Style guide:** batch of references → style guide text.
5. **Refine loop:** prompt → image → judge → revised prompt, with round and cost caps and provenance records.
6. **Pack runner:** manifest in, approved images plus a review gallery out.
7. **PNG → LBX writer:** see the pack in the game.
8. Later: animations, seamless terrain, banner-colour remap ranges.

Prototype here in ScratchPatch. Move the format tools (steps 1 and 7) into ReMoM `tools/`, because the upscale project needs them too.
