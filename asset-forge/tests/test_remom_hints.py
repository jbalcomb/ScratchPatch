import csv
import tempfile
import unittest
from pathlib import Path

from assetforge import remom_hints

UNITTYPE = """char unit_type_name_000[] = "Dwarf";
char unit_type_name_001[] = "Trireme";
char unit_type_name_002[] = "Spearmen";
char unit_type_name_003[] = "Magic Spirit";
struct s_UNIT_TYPE _unit_type_table[NUM_UNIT_TYPES] =
{
    /* name, mel, rng,  rt, amm, hit, def, res, mov, cos, upk, rac, ... */
    {&_unit_type_names[0],     5,   0,  -1,   0,   0,   4,  10,   4, 100,   2,   4,  10, 0},
/*  35 ST Trireme  */ {&_unit_type_names[1],    6,   0, -1,   0,   0,   4,   4,   4,  60,   2,  14,  12, 0},
{&_unit_type_names[2],    1,  1, 100,   0,   0,   2,   5,   2,  15,   1,   0,   0, 0},
{&_unit_type_names[3],    1,  1, 100,   0,   0,   2,   5,   2,  15,   1,  16,   0, 0},
};
"""
RACETYPE = """char race_type_name_000[] = "Barbarian";
char race_type_name_004[] = "Dwarven";
char race_type_name_014[] = "";
char race_type_name_016[] = "Nature";
"""


class HintsTest(unittest.TestCase):

    def test_unit_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "MoM/src")
            src.mkdir(parents=True)
            (src / "UNITTYPE.c").write_text(UNITTYPE)
            (src / "RACETYPE.c").write_text(RACETYPE)
            with self.assertRaises(ValueError):                 # only 4 of the 198 unit types
                remom_hints.unit_types(Path(tmp))
            old = remom_hints.NUM_UNIT_TYPES
            remom_hints.NUM_UNIT_TYPES = 4
            try:
                out = Path(tmp, "out")
                out.mkdir()
                remom_hints.main([tmp, str(out)])
            finally:
                remom_hints.NUM_UNIT_TYPES = old
            with open(out / "hints.csv", encoding="utf-8") as f:
                rows = [(r["archive"], r["entry"], r["hint"]) for r in csv.DictReader(f)]
        self.assertEqual(rows[:4], [("UNITS1", "0", "icon of a hero unit: Dwarf (Dwarven)"),
                                    ("UNITS1", "1", "icon of a hero unit: Trireme"),
                                    ("UNITS1", "2", "icon of a hero unit: Spearmen (Barbarian)"),
                                    ("UNITS1", "3", "icon of a hero unit: Magic Spirit (Nature)")])
        figures = rows[4:-1]
        self.assertEqual(len(figures), 4 * 8)
        self.assertEqual(figures[0], ("FIGURES1", "0", "combat figure of a hero unit: Dwarf (Dwarven), "
                                                       "facing away from the viewer"))
        self.assertEqual(figures[8 + 6], ("FIGURES1", "14", "combat figure of a hero unit: Trireme, facing left"))
        self.assertEqual(rows[-1], ("MONSTER", "0", "picture of a hero unit: Magic Spirit (Nature)"))
        self.assertEqual([remom_hints.figure_archive(t) for t in (0, 14, 15, 134, 135, 197)],
                         ["FIGURES1", "FIGURES1", "FIGURES2", "FIGURES9", "FIGURE10", "FIGURE14"])
        self.assertEqual(remom_hints.unit_hint(35, "Trireme", ""), "Trireme unit")
        self.assertEqual(remom_hints.unit_hint(39, "Spearmen", "Barbarian"), "Barbarian Spearmen unit")
        self.assertEqual(remom_hints.unit_hint(100, "Sprites", "Nature"), "Sprites, a summoned nature magic creature")


if __name__ == "__main__":
    unittest.main()
