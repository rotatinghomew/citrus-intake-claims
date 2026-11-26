"""基于 inputs/ 合成交换材料的端到端测试。"""
import unittest
from decimal import Decimal as D
from pathlib import Path

from handover.loader import load_dir
from handover.models import (
    CELL_WEIGHT_CELL_SCALE,
    CELL_WEIGHT_QUARANTINED,
    HELD,
    BILLABLE,
    TOTAL_CONTESTED,
)
from handover.errors import RuleViolation

ROOT = Path(__file__).resolve().parents[1]


class SampleHandover(unittest.TestCase):
    def setUp(self):
        self.service = load_dir(ROOT / "inputs")
        self.segment = "T-127"

    def test_更正链使共同总重按4450进入称差争议(self):
        wr = self.service.weight_resolution(self.segment)
        self.assertEqual(wr.total_status, TOTAL_CONTESTED)
        self.assertIsNone(wr.common_total_kg)
        self.assertEqual(wr.vehicle_claim_ids, ("W1",))
        # A204 被 A244 更正后不再有效
        ids = {w.id for w in self.service.store.effective_weighings(self.segment)}
        self.assertNotIn("A204", ids)
        self.assertIn("A244", ids)

    def test_单舱独立实称优先保留(self):
        wr = self.service.weight_resolution(self.segment)
        c1 = wr.cell("C1")
        self.assertEqual(c1.status, CELL_WEIGHT_CELL_SCALE)
        self.assertEqual(c1.kg, D("2300"))
        self.assertEqual(c1.claim_ids, ())  # 不被整车争议牵连

    def test_无实称舱在总重未决时暂缓且申报重量不当结算(self):
        wr = self.service.weight_resolution(self.segment)
        c2 = wr.cell("C2")
        self.assertEqual(c2.status, "contested")
        self.assertIsNone(c2.kg)
        self.assertEqual(c2.claim_ids, ("W1",))

    def test_封签不符即使称重对上也只能隔离(self):
        overview = {r.cell: r for r in self.service.receiving(self.segment)}
        self.assertEqual(overview["C3"].zone, "quarantine")
        self.assertTrue(overview["C3"].issue_blocked)
        wr = self.service.weight_resolution(self.segment)
        self.assertEqual(wr.cell("C3").status, CELL_WEIGHT_QUARANTINED)

    def test_封签一致的舱进入待核实区且按双上限领料(self):
        overview = {r.cell: r for r in self.service.receiving(self.segment)}
        c2 = overview["C2"]
        self.assertEqual(c2.zone, "pending_verify")
        # 合同上限 1500 与到账 1620 取小
        self.assertEqual(c2.provisional_cap_kg, D("1500"))
        self.assertEqual(c2.issued_kg, D("1480"))
        ok, _ = self.service.can_issue_now(self.segment, "C2", D("20"))
        self.assertTrue(ok)
        ok, msg = self.service.can_issue_now(self.segment, "C2", D("21"))
        self.assertFalse(ok)
        self.assertIn("超出可领余量", msg)

    def test_隔离舱与未到厂舱不能领料(self):
        ok, msg = self.service.can_issue_now(self.segment, "C3", D("1"))
        self.assertFalse(ok)
        self.assertIn("隔离", msg)

    def test_同舱不能被下一厂重复接收(self):
        with self.assertRaises(RuleViolation):
            self.service.submit_unload("site/FAC-B", {
                "id": "R9", "segment": self.segment, "site": "FAC-B",
                "cell": "C1", "seal": "D781", "kg": 1,
                "at": "2026-12-03T10:00:00+08:00",
            })

    def test_非本厂仓班不能确认卸货(self):
        from handover.errors import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.submit_unload("site/FAC-B", {
                "id": "R8", "segment": self.segment, "site": "FAC-A",
                "cell": "C2", "seal": "D782", "kg": 1,
                "at": "2026-12-03T10:00:00+08:00",
            })

    def test_原料款仅独立实称舱可出账且按份额计价(self):
        from handover.billing import material_lines
        rows = {(ln.cell, ln.coop): ln for ln in material_lines(
            self.service.store, self.segment)}
        self.assertEqual(rows[("C1", "COOP-F")].status, BILLABLE)
        self.assertEqual(rows[("C1", "COOP-F")].weight_kg, D("1725.00"))
        self.assertEqual(rows[("C1", "COOP-J")].weight_kg, D("575.00"))
        self.assertEqual(rows[("C2", "COOP-F")].status, HELD)
        self.assertEqual(rows[("C3", "COOP-G")].status, HELD)

        st = self.service.build_statement(self.segment, "COOP-F", "goods")
        # 1069.50（C1/F391 一行；C2 暂缓不进账）
        self.assertEqual(st.total, D("1069.50"))
        self.assertEqual(len(st.lines), 1)
        signed = self.service.sign_statement(st.id)
        self.assertTrue(signed.signed)

    def test_合作社无账可出时拒绝建账(self):
        with self.assertRaises(RuleViolation):
            self.service.build_statement(self.segment, "COOP-G", "goods")

    def test_既有签认运费账保持原明细且不会重付(self):
        st = self.service.store.require_statement("P1")
        self.assertTrue(st.signed)
        self.assertEqual(st.total, D("160.00"))
        from handover.billing import freight_items
        items = freight_items(self.service.store, self.segment)
        # 首卸已在 P1 签认付款 → 不再作为新账项目；只剩暂缓的其他停靠
        self.assertEqual([i.item for i in items], ["other_stop"])
        self.assertTrue(all(i.status == HELD for i in items))

    def test_财务能区分已承诺与待裁结(self):
        pos = self.service.finance_position(self.segment)
        self.assertEqual(pos["committed"]["freight"], D("160.00"))
        pending_cells = {ln.cell for ln in pos["pending_goods"]}
        self.assertEqual(pending_cells, {"C2", "C3"})
        self.assertEqual({i.site for i in pos["pending_freight"]}, {"FAC-B"})

    def test_权限视图各自裁剪(self):
        coop = self.service.view_for("coop/COOP-F", self.segment)
        self.assertEqual({s["cell"] for s in coop["shares"]}, {"C1", "C2"})
        self.assertNotIn("COOP-J", str(coop["material"]))

        carrier = self.service.view_for("carrier/YS2", self.segment)
        self.assertIn("freight", carrier)

        gate = self.service.view_for("gate/FAC-B", self.segment)
        self.assertTrue(any(c["cell"] == "C3" for c in gate["cells"]))

        site_a = self.service.view_for("site/FAC-A", self.segment)
        self.assertTrue(all(e.site == "FAC-A" for e in site_a["events"]))

        coordinator = self.service.view_for("coordinator", self.segment)
        self.assertEqual(coordinator["carrier"], "YS2")

        denied = self.service.view_for("coop/COOP-X", self.segment)
        self.assertIn("error", denied)
        denied_arb = self.service.view_for("arbiter/NOPE", self.segment)
        self.assertIn("error", denied_arb)

    def test_协调员视图串起收货领用争议与账单(self):
        view = self.service.view_for("coordinator", self.segment)
        self.assertEqual({c.id for c in view["claims"]}, {"W1", "W2"})
        issues = [(e.cell, e.kg) for e in view["issues"]]
        self.assertEqual(issues, [("C2", D("1480"))])


if __name__ == "__main__":
    unittest.main()
