"""Write hints.csv: what each exported graphic is, taken from ReMoM's game data, for assetforge.describe.

    python3 -m assetforge.remom_hints ~/STU/devel/ReMoM out/originals

Archive entry names are 8-character abbreviations ("STATFIG1") that can mislead a vision model, and small sprites are
hard to read. The game data says what they are. Currently covered: unit icons, UNITS1.LBX entry i = unit type i and
UNITS2.LBX entry i = unit type 120 + i (Load_Unit_StatFigs() in MoM/src/LOADER.c), named from _unit_type_table in
MoM/src/UNITTYPE.c and _race_type_names in MoM/src/RACETYPE.c.

hints.csv columns: archive, entry, hint. It can also be written or extended by hand.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

HEROES = 35                  # unit types 0-34 are heroes (UNITTYPE.c)
UNITS2_FIRST = 120
NUM_UNIT_TYPES = 198
REALMS = {"Arcane", "Nature", "Sorcery", "Chaos", "Life", "Death"}


def c_strings(source, prefix):
    """{number: text} from lines like: char unit_type_name_042[] = "Spearmen";"""
    return {int(n): s for n, s in re.findall(rf'^char {prefix}(\d+)\[\] = "(.*)";', source, re.M)}


def unit_types(remom):
    """[(name, race)] for every unit type, in unit type order."""
    unittype = (remom / "MoM/src/UNITTYPE.c").read_text(encoding="latin-1")
    races = c_strings((remom / "MoM/src/RACETYPE.c").read_text(encoding="latin-1"), "race_type_name_")
    names = c_strings(unittype, "unit_type_name_")
    table = unittype[unittype.index("_unit_type_table[NUM_UNIT_TYPES]"):]
    out = []
    for name_idx, values in re.findall(r"^\s*(?:/\*.*?\*/\s*)?\{&_unit_type_names\[(\d+)\],([^}]*)\}", table, re.M):
        race = int(values.split(",")[10])          # columns: name, mel, rng, rt, amm, hit, def, res, mov, cos, upk, rac
        out.append((names[int(name_idx)], races.get(race, "")))
    if len(out) != NUM_UNIT_TYPES:
        raise ValueError(f"found {len(out)} unit types in UNITTYPE.c, expected {NUM_UNIT_TYPES}: the table's layout changed")
    return out


def unit_hint(unit_type, name, race):
    if unit_type < HEROES:
        return f"hero unit: {name}" + (f" ({race})" if race else "")
    if race in REALMS:
        return f"{name}, a summoned {race.lower()} magic creature"
    return f"{race} {name}".strip() + " unit"


def hints(remom):
    rows = []
    for t, (name, race) in enumerate(unit_types(remom)):
        archive, entry = ("UNITS1", t) if t < UNITS2_FIRST else ("UNITS2", t - UNITS2_FIRST)
        rows.append({"archive": archive, "entry": entry, "hint": f"icon of a {unit_hint(t, name, race)}"})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("remom", type=Path, help="ReMoM source checkout")
    ap.add_argument("export_dir", type=Path, help="output folder of assetforge.export")
    args = ap.parse_args(argv)
    rows = hints(args.remom)
    with open(args.export_dir / "hints.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["archive", "entry", "hint"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} hints -> {args.export_dir / 'hints.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
