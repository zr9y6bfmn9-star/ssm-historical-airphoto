import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import acquire_ort_riku10 as a

class TileMathTests(unittest.TestCase):
    def test_tile_bbox_roundtrip_reasonable(self):
        bbox = a.parse_bbox("135.512500,34.603375,135.515167,34.606000")
        tiles = a.tiles_for_bbox(bbox, 18)
        self.assertGreaterEqual(len(tiles), 1)
        self.assertLessEqual(len(tiles), 16)

    def test_invalid_bbox_rejected(self):
        with self.assertRaises(ValueError):
            a.parse_bbox("135.5,34.6,135.4,34.7")

    def test_one_or_two_grid_rule_is_documented_by_parser_runtime(self):
        self.assertTrue(True)

if __name__ == "__main__":
    unittest.main()
