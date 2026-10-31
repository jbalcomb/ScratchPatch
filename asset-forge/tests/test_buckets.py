import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from assetforge import buckets
from assetforge.buckets import bucket_of


class BucketTest(unittest.TestCase):

    def test_by_archive(self):
        self.assertEqual(bucket_of("FIGURES3", 28, 30), "figure")
        self.assertEqual(bucket_of("figure12", 28, 30), "figure")
        self.assertEqual(bucket_of("UNITS2", 18, 16), "unit_icon")
        self.assertEqual(bucket_of("TERRAIN_TILES", 20, 18), "terrain")
        self.assertEqual(bucket_of("CMBTUNDC", 30, 16), "combat_terrain")
        self.assertEqual(bucket_of("ITEMS", 17, 17), "item")
        self.assertEqual(bucket_of("NOSUCH", 10, 10), "ui")

    def test_size_rules(self):
        self.assertEqual(bucket_of("RELOAD", 320, 200), "screen")
        self.assertEqual(bucket_of("SPLMASTR", 320, 200), "art")       # big art stays art
        self.assertEqual(bucket_of("DIPLOMAC", 107, 109), "portrait")
        self.assertEqual(bucket_of("DIPLOMAC", 22, 9), "ui")

    def test_every_bucket_has_a_unique_token(self):
        tokens = [b.token for b in buckets.BUCKETS.values()]
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertTrue(set(buckets.ARCHIVES) <= set(buckets.BUCKETS))

    def test_adds_column_to_existing_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.csv"
            path.write_text("archive,entry,name,kind,width,height\nUNITS1,0,A,sprite,18,16\nUNITS1,1,B,other,,\n")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(buckets.main([tmp]), 0)
                self.assertEqual(buckets.main([tmp]), 0)                # again: column not added twice
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ["archive", "entry", "name", "kind", "bucket", "width", "height"])
                self.assertEqual([r["bucket"] for r in reader], ["unit_icon", ""])
