"""Write hints.csv: what each exported graphic is, taken from ReMoM's game data, for assetforge.describe.

    python3 -m assetforge.remom_hints ~/STU/devel/ReMoM out/originals

Archive entry names are 8-character abbreviations ("STATFIG1") that can mislead a vision model, and small sprites are
hard to read. The game data says what they are. Units are named from _unit_type_table in MoM/src/UNITTYPE.c and
_race_type_names in MoM/src/RACETYPE.c. Covered:
- unit icons: UNITS1.LBX entry i = unit type i, UNITS2.LBX entry i = unit type 120 + i (Load_Unit_StatFigs(),
  MoM/src/LOADER.c);
- combat figures: unit type t is in FIGURES<t/15+1>.LBX (FIGURE10.LBX and up without the S), entries (t%15)*8 to
  (t%15)*8+7, one per facing in the order of e_COMBAT_ENTITY_MISSILE_DIRECTION (Combat_Figure_Load(), Combat.c);
- summoned creature pictures: MONSTER.LBX entry i = unit type of Magic Spirit + i (Spells137.c); entries 44-47 are
  other pictures.

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
FIGURES_PER_FILE = 15
FACINGS = ["facing away from the viewer", "facing away, toward the upper right", "facing right",
           "facing the viewer, toward the lower right", "facing the viewer", "facing the viewer, toward the lower left",
           "facing left", "facing away, toward the upper left"]
FIRST_SUMMON = "Magic Spirit"


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


def figure_archive(unit_type):
    n = unit_type // FIGURES_PER_FILE + 1
    return f"FIGURES{n}" if n < 10 else f"FIGURE{n}"


def hints(remom):
    types = unit_types(remom)
    rows = []
    for t, (name, race) in enumerate(types):
        archive, entry = ("UNITS1", t) if t < UNITS2_FIRST else ("UNITS2", t - UNITS2_FIRST)
        rows.append({"archive": archive, "entry": entry, "hint": f"icon of a {unit_hint(t, name, race)}"})
    for t, (name, race) in enumerate(types):
        for facing, words in enumerate(FACINGS):
            rows.append({"archive": figure_archive(t), "entry": t % FIGURES_PER_FILE * len(FACINGS) + facing,
                         "hint": f"combat figure of a {unit_hint(t, name, race)}, {words}"})
    first = [name for name, _ in types].index(FIRST_SUMMON)
    for t in range(first, len(types)):
        name, race = types[t]
        rows.append({"archive": "MONSTER", "entry": t - first, "hint": f"picture of a {unit_hint(t, name, race)}"})
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
