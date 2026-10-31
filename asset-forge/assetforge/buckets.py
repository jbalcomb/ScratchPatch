"""Sort the game's graphics into buckets: groups that are drawn, trained and generated the same way (combat figures,
unit icons, world terrain, ...). Each bucket has a trigger word for training captions and a plain-English noun.

    python3 -m assetforge.buckets out/originals      # add/refresh the bucket column of catalog.csv, print counts

The rules go by archive, with two size rules first: anything near full-screen size is a screen, and the diplomacy
archive mixes wizard portraits with small interface pieces.
"""

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Bucket:
    name: str
    token: str      # trigger word that starts every training caption of this bucket
    noun: str       # what one item is, for captions that have nothing better


BUCKETS = {b.name: b for b in [
    Bucket("figure", "momfigure", "combat figure of a fantasy unit"),
    Bucket("unit_icon", "momuniticon", "small unit icon"),
    Bucket("portrait", "momportrait", "portrait of a wizard"),
    Bucket("terrain", "momterrain", "top-down world map terrain tile"),
    Bucket("combat_terrain", "momcombatterrain", "isometric battlefield terrain tile"),
    Bucket("building", "mombuilding", "isometric city building"),
    Bucket("item", "momitem", "magic item icon"),
    Bucket("icon", "momicon", "small interface icon"),
    Bucket("art", "momart", "fantasy illustration"),
    Bucket("screen", "momscreen", "full-screen background picture"),
    Bucket("ui", "momui", "game interface element"),
]}

ARCHIVES = {
    "figure": "CMBMAGIC FIGURES1 FIGURES2 FIGURES3 FIGURES4 FIGURES5 FIGURES6 FIGURES7 FIGURES8 FIGURES9 "
              "FIGURE10 FIGURE11 FIGURE12 FIGURE13 FIGURE14 FIGURE15 FIGURE16",
    "unit_icon": "UNITS1 UNITS2",
    "portrait": "PORTRAIT WIZARDS MOODWIZ LILWIZ",
    "terrain": "TERRAIN_TILES MAPBACK",
    "combat_terrain": "COMBAT CMBGRASS CMBGRASC CMBDESRT CMBDESRC CMBMOUNT CMBMOUNC CMBTUNDR CMBTUNDC DESERT TUNDRA "
                      "CHRIVER",
    "building": "CITYSCAP CMBTCITY CITYWALL CMBTWALL WALLRISE",
    "item": "ITEMS ITEMISC",
    "icon": "SPECIAL SPECIAL2 COMPIX UNITVIEW RESOURCE",
    "art": "MONSTER SPECFX SPELLOSE CONQUEST CMBTFX SPLMASTR EVENTS WIN LOSE WIZLAB",
}
ARCHIVE_BUCKET = {a: b for b, names in ARCHIVES.items() for a in names.split()}
SCREEN_SIZE = (200, 150)        # at least this wide and tall: a screen or a full-screen panel
PORTRAIT_SIZE = 64              # DIPLOMAC pictures at least this big are wizard portraits


def bucket_of(archive, width, height):
    """Bucket name for a graphic, from its archive (without .LBX) and size in pixels."""
    archive = archive.upper()
    bucket = ARCHIVE_BUCKET.get(archive, "ui")
    if width >= SCREEN_SIZE[0] and height >= SCREEN_SIZE[1] and bucket != "art":
        return "screen"
    if archive == "DIPLOMAC" and min(width, height) >= PORTRAIT_SIZE:
        return "portrait"
    return bucket


def add_bucket_column(rows):
    """Set row['bucket'] on catalog rows (sprites only; other entries get '')."""
    for row in rows:
        row["bucket"] = bucket_of(row["archive"], int(row["width"]), int(row["height"])) if row["kind"] == "sprite" else ""
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir", type=Path, help="output folder of assetforge.export")
    args = ap.parse_args(argv)
    path = args.export_dir / "catalog.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames)
        rows = add_bucket_column(list(reader))
    if "bucket" not in columns:
        columns.insert(columns.index("kind") + 1, "bucket")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, columns)
        w.writeheader()
        w.writerows(rows)
    counts = Counter(r["bucket"] for r in rows if r["bucket"])
    for name in BUCKETS:
        print(f"{name:15} {counts[name]:5}")
    print(f"{sum(counts.values())} sprites -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
