"""核对业务方交换材料的格式和来源引用。"""
from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(name):
    return json.loads((ROOT / "inputs" / (name + ".json")).read_text(encoding="utf-8"))


class Exchanges(unittest.TestCase):
    def test_称重范围与磅房更正(self):
        dispatch = source("dispatch")
        weights = source("weighing")
        compartments = {row["cell"]: row for row in dispatch["compartments"]}
        self.assertEqual(set(dispatch["stops"]), {"FAC-A", "FAC-B"})
        self.assertTrue(all(row["segment"] == dispatch["segment"] for row in weights))
        self.assertEqual({row["unit"] for row in weights}, {"kg", "t"})
        ids = {row["id"]: row for row in weights}
        revised = next(row for row in weights if "corrects" in row)
        original = ids[revised["corrects"]]
        self.assertEqual((revised["scope"], revised["at"]), (original["scope"], original["at"]))
        self.assertNotEqual(revised["gross"], original["gross"])
        warehouse = source("warehouse")
        self.assertTrue(all(row["cell"] in compartments for row in warehouse))
        self.assertTrue(any(row["kind"] == "issue" for row in warehouse))
        self.assertTrue(any(row.get("seal") and row["seal"] != compartments[row["cell"]]["seal"] for row in warehouse))
