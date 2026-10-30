"""Build step 3: draw a replacement for each described sprite with a local image model, then turn it into a
game-ready sprite (background removed, shrunk, mapped to the game palette).

    python3 -m assetforge.generate out/originals out/pack --model sd_xl_base_1.0.safetensors --archive SPELLS --limit 20
    python3 -m assetforge.generate out/originals out/pack --model ... --lora my_style.safetensors --scale 2

Reads describe.json (assetforge.describe) and palette.json (assetforge.export). Per sprite, in
OUT/<ARCHIVE>/<entry>_<name>/: raw.png (the model's image), frame_00.png (RGBA sprite), frame_00_index.png
(palette indices, same form as the exporter's) and generate.json (prompt, seed, model, timings). OUT/index.html
shows originals and replacements side by side.

The first image for each sprite is kept, so re-running skips sprites already generated (--redo to replace).
Only the first frame of an animation is generated for now.
"""

import argparse
import html
import json
import os
import sys
import time
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

from . import pixelize

STYLE = "pixel art game sprite, 1990s DOS fantasy strategy game, hand-drawn, crisp outlines, limited palette"
BACKGROUND = "isolated on a plain flat magenta background"
NEGATIVE = "photo, 3d render, blurry, text, watermark, signature, border, frame, scenery, ground, cast shadow, magenta in the subject"
SDXL_SIZES = [(1024, 1024), (1152, 896), (896, 1152), (1216, 832), (832, 1216), (1344, 768), (768, 1344),
              (1536, 640), (640, 1536)]
MIN_SPRITE_COLOURS = 8
SAFE_INDICES = set(range(1, 224))     # 0 is drawn as black/transparent, 224+ overlap colour cycling and remap


def model_size(width, height):
    """The SDXL training resolution closest in shape to the sprite."""
    aspect = width / height
    return min(SDXL_SIZES, key=lambda s: abs(np.log(s[0] / s[1] / aspect)))


def sprite_seed(base, archive, entry):
    return (base + zlib.crc32(f"{archive}/{entry}".encode())) % 2**31


def build_prompt(desc, style, count_tokens=None, limit=75):
    """Description prompt, then the background instruction, then the style. If count_tokens is given, drop phrases
    from the end of the description until it all fits (SDXL's text encoders read only 77 tokens, 2 of them markers)."""
    phrases = [p.strip() for p in desc["prompt"].replace("transparent background", "").split(",") if p.strip()]
    tail = [BACKGROUND, style]
    while True:
        prompt = ", ".join(phrases + [t for t in tail if t])
        if count_tokens is None or count_tokens(prompt) <= limit or len(phrases) <= 1:
            return prompt
        phrases.pop()


def allowed_colours(sprite_dir, mode):
    """Palette indices a replacement may use: those the original uses ('sprite') or the safe range ('game')."""
    if mode == "sprite":
        used = set()
        for f in sorted(sprite_dir.glob("frame_*_index.png")):
            with Image.open(f) as img:
                a = np.asarray(img)
            used |= set(np.unique(a[:, :, 0][a[:, :, 1] == 255]).tolist())
        used.discard(0)
        if len(used) >= MIN_SPRITE_COLOURS:
            return used
    return set(SAFE_INDICES)


class Diffusers:
    """SDXL (or any diffusers text-to-image pipeline) on the local GPU, with an optional LoRA."""

    def __init__(self, model, lora=None, lora_scale=1.0, steps=30, cfg=6.0, device=None):
        import torch
        from diffusers import AutoPipelineForText2Image, StableDiffusionXLPipeline
        from diffusers.utils import logging as diffusers_logging
        diffusers_logging.set_verbosity_error()
        self.torch = torch
        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        kw = {"torch_dtype": torch.float16 if device == "cuda" else torch.float32}
        if Path(model).suffix in (".safetensors", ".ckpt"):
            self.pipe = StableDiffusionXLPipeline.from_single_file(model, **kw)
        else:
            self.pipe = AutoPipelineForText2Image.from_pretrained(model, **kw)
        if lora:
            self.pipe.load_lora_weights(lora)
            self.pipe.fuse_lora(lora_scale=lora_scale)
        self.pipe.to(device)
        self.device, self.steps, self.cfg = device, steps, cfg
        self.info = {"model": str(model), "lora": str(lora) if lora else None, "lora_scale": lora_scale if lora else None,
                     "steps": steps, "cfg": cfg}

    def count_tokens(self, text):
        return len(self.pipe.tokenizer(text, add_special_tokens=False).input_ids)

    def __call__(self, prompt, negative, size, seed):
        gen = self.torch.Generator(self.device).manual_seed(seed)
        return self.pipe(prompt=prompt, negative_prompt=negative, width=size[0], height=size[1],
                         num_inference_steps=self.steps, guidance_scale=self.cfg, generator=gen).images[0]


BACKENDS = {"diffusers": Diffusers}


def write_gallery(src, out):
    items = []
    for gen_file in sorted(out.glob("*/*/generate.json")):
        g = json.loads(gen_file.read_text())
        folder = gen_file.parent.relative_to(out).as_posix()
        orig = Path(os.path.relpath(src / folder / "frame_00.png", out)).as_posix()
        items.append(f"""<figure><div class=pair><img src="{html.escape(orig)}">
<img src="{html.escape(folder)}/frame_00.png"><img class=raw src="{html.escape(folder)}/raw.png"></div>
<figcaption>{html.escape(g['archive'])} {g['entry']} {html.escape(g['name'])}: {html.escape(g['subject'])}
<br><small>{html.escape(g['prompt'])}</small></figcaption></figure>""")
    (out / "index.html").write_text("""<!doctype html><meta charset="utf-8"><title>Generated Sprites</title>
<style>
body{font:14px system-ui,sans-serif;margin:16px;background:#202020;color:#ddd}
.grid{display:flex;flex-wrap:wrap;gap:12px}
figure{margin:0;background:#2c2c2c;padding:6px;max-width:100%}
figcaption{font-size:12px;color:#aaa;max-width:420px}
.pair{display:flex;gap:8px;align-items:flex-end}
img{image-rendering:pixelated;max-width:100%;background:repeating-conic-gradient(#3a3a3a 0 25%,#303030 0 50%) 0 0/16px 16px}
img.raw{image-rendering:auto;width:128px}
</style>
<p>Each row: original, replacement, model output. Zoom: <select onchange="document.querySelectorAll('.pair img:not(.raw)').forEach(i=>i.style.width=i.naturalWidth*this.value+'px')">
<option>1<option>2<option selected>3<option>4</select></p>
<div class=grid>""" + "\n".join(items) + """</div>
<script>document.querySelectorAll('.pair img:not(.raw)').forEach(i=>i.onload=()=>i.style.width=i.naturalWidth*3+'px')</script>""",
                                  encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("originals", type=Path, help="output folder of assetforge.export, with describe.json files")
    ap.add_argument("out", type=Path, help="folder for the generated pack")
    ap.add_argument("--model", required=True, help="SDXL checkpoint (.safetensors) or a diffusers model folder/id")
    ap.add_argument("--lora", help="style LoRA (.safetensors)")
    ap.add_argument("--lora-scale", type=float, default=1.0)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--cfg", type=float, default=6.0, help="guidance scale")
    ap.add_argument("--seed", type=int, default=1, help="base seed; each sprite's seed is derived from it")
    ap.add_argument("--scale", type=int, default=1, help="replacement size = original size x this (default 1)")
    ap.add_argument("--colours", choices=["sprite", "game"], default="sprite",
                    help="palette indices allowed: those the original uses (default) or the safe game range")
    ap.add_argument("--no-outline", dest="outline", action="store_false",
                    help="don't redraw the sprite's edge in the darkest allowed colour (MoM sprites have dark outlines)")
    ap.add_argument("--style", help="text file with the style guide to add to every prompt")
    ap.add_argument("--archive", action="append", help="only this archive, e.g. SPELLS (repeatable)")
    ap.add_argument("--limit", type=int, help="generate at most this many sprites")
    ap.add_argument("--redo", action="store_true", help="generate again even if the sprite already has one")
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="diffusers")
    args = ap.parse_args(argv)

    style = args.style and Path(args.style).read_text(encoding="utf-8").strip() or STYLE
    game_palette = [tuple(c) for c in json.loads((args.originals / "palette.json").read_text())]
    descs = sorted(args.originals.glob("*/*/describe.json"))
    if args.archive:
        wanted = {a.upper().removesuffix(".LBX") for a in args.archive}
        descs = [d for d in descs if d.parent.parent.name in wanted]
    if not descs:
        print("nothing to generate: run assetforge.describe first", file=sys.stderr)
        return 1

    generator = None
    done = skipped = 0
    seconds = 0.0
    for desc_file in descs:
        if args.limit is not None and done >= args.limit:
            break
        desc = json.loads(desc_file.read_text(encoding="utf-8"))
        src_dir = desc_file.parent
        dst_dir = args.out / src_dir.relative_to(args.originals)
        if (dst_dir / "generate.json").exists() and not args.redo:
            skipped += 1
            continue
        if generator is None:
            print(f"loading {args.model} ...")
            generator = BACKENDS[args.backend](args.model, lora=args.lora, lora_scale=args.lora_scale,
                                               steps=args.steps, cfg=args.cfg)
        pal_file = src_dir / "palette.json"
        palette = [tuple(c) for c in json.loads(pal_file.read_text())] if pal_file.exists() else game_palette
        target = (desc["width"] * args.scale, desc["height"] * args.scale)
        size = model_size(*target)
        seed = sprite_seed(args.seed, desc["archive"], desc["entry"])
        prompt = build_prompt(desc, style, getattr(generator, "count_tokens", None))
        negative = ", ".join(p for p in (desc.get("negative_prompt", ""), NEGATIVE) if p)
        started = time.time()
        raw = generator(prompt, negative, size, seed)
        took = time.time() - started
        allowed = allowed_colours(src_dir, args.colours)
        edge = pixelize.darkest(palette, allowed) if args.outline else None
        sprite = pixelize.pixelize(raw, target, palette, allowed, outline_colour=edge)
        dst_dir.mkdir(parents=True, exist_ok=True)
        raw.save(dst_dir / "raw.png")
        sprite.rgba.save(dst_dir / "frame_00.png")
        pixelize.index_image(sprite).save(dst_dir / "frame_00_index.png")
        record = {"archive": desc["archive"], "entry": desc["entry"], "name": desc["name"], "subject": desc["subject"],
                  "width": target[0], "height": target[1], "frames_generated": 1, "frames_original": desc["frames"],
                  "prompt": prompt, "negative_prompt": negative, "seed": seed, "model_size": list(size),
                  "colours": args.colours, "allowed_indices": len(allowed), "outline": args.outline, "seconds": round(took, 1),
                  "when": time.strftime("%Y-%m-%dT%H:%M:%S"), **generator.info,
                  "described_by": desc.get("provenance", {}).get("model")}
        (dst_dir / "generate.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        done += 1
        seconds += took
        print(f"{desc['archive']}[{desc['entry']}] {desc['name']}: {desc['subject']} ({took:.1f}s)")
    if done or skipped:
        write_gallery(args.originals, args.out)
    average = f", {seconds / done:.1f}s each" if done else ""
    print(f"{done} generated{average}, {skipped} already done -> {args.out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
