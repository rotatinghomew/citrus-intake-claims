"""争议定稿、分摊、领用禁令与账单更正的完整生命周期测试。"""
import unittest
from decimal import Decimal as D

from handover.errors import (
    AlreadyFinalized,
    ContradictoryEvidence,
    PermissionDenied,
    RuleViolation,
)
from handover.models import (
    CELL_WEIGHT_APPORTIONED,
    CELL_WEIGHT_CELL_SCALE,
    TOTAL_CONSISTENT,
    TOTAL_CONTRADICTION,
)
from handover.service import HandoverService

SEGMENT = "T-127"
AT0 = "2026-12-03T07:00:00+08:00"


def build_service(with_claims=True):
    """与 inputs 同构的内存场景，便于在时间轴上追加证据。"""
    svc = HandoverService()
    svc.register_dispatch({
        "segment": SEGMENT, "carrier": "YS2", "stops": ["FAC-A", "FAC-B"],
        "compartments": [
            {"cell": "C1", "seal": "D781", "declared_kg": 2400,
             "shares": [["COOP-F", "F391", 3], ["COOP-J", "J204", 1]]},
            {"cell": "C2", "seal": "D782", "declared_kg": 1600,
             "shares": [["COOP-F", "F399", 1]]},
            {"cell": "C3", "seal": "D783", "declared_kg": 1000,
             "shares": [["COOP-G", "G102", 1]]},
        ],
    })
    contract = {
        "goods": {"COOP-F": "0.62", "COOP-J": "0.74", "COOP-G": "0.69"},
        "currency": "CNY", "unverified_use_cap_kg": 1500,
        "weight_tolerance_kg": 100,
        "carrier_first_unload_fee": "160.00",
        "carrier_other_stop_fee": "55.00", "arbiter": "XU-40",
    }
    if with_claims:
        contract["claims"] = [
            {"id": "W1", "scope": "vehicle", "by": "FAC-A"},
            {"id": "W2", "scope": "C3", "by": "FAC-B", "reason": "seal"},
        ]
    svc.register_contract(SEGMENT, contract)

    svc.submit_weighing("ORIGIN", {
        "id": "S101", "source": "ORIGIN", "segment": SEGMENT,
        "scope": "vehicle", "unit": "t", "gross": "9.16", "tare": "4.16", "at": AT0,
    })
    svc.submit_weighing("site/FAC-A", {
        "id": "A204", "source": "FAC-A", "segment": SEGMENT,
        "scope": "vehicle", "unit": "kg", "gross": "8980", "tare": "4160", "at": AT0,
    })
    svc.submit_weighing("site/FAC-A", {
        "id": "A205", "source": "FAC-A", "segment": SEGMENT,
        "scope": "C1", "unit": "kg", "gross": "2345", "tare": "45", "at": AT0,
    })
    # 更正：整车净重由 4820 改为 4450，触发超容差称差
    svc.submit_weighing("site/FAC-A", {
        "id": "A244", "corrects": "A204", "source": "FAC-A", "segment": SEGMENT,
        "scope": "vehicle", "unit": "kg", "gross": "8610", "tare": "4160",
        "at": AT0, "sent_at": "2026-12-03T14:20:00+08:00",
    })

    svc.submit_unload("site/FAC-A", {
        "id": "R1", "segment": SEGMENT, "site": "FAC-A", "cell": "C1",
        "kind": "unload", "seal": "D781", "kg": 2300, "at": AT0,
    })
    svc.submit_unload("site/FAC-A", {
        "id": "R2", "segment": SEGMENT, "site": "FAC-A", "cell": "C2",
        "kind": "unload", "seal": "D782", "kg": 1620, "at": AT0,
    })
    svc.submit_issue("site/FAC-A", {
        "id": "U2", "segment": SEGMENT, "site": "FAC-A", "cell": "C2",
        "kind": "issue", "kg": 1480, "use": "run-28", "at": AT0,
    })
    svc.submit_unload("site/FAC-B", {
        "id": "R3", "segment": SEGMENT, "site": "FAC-B", "cell": "C3",
        "kind": "unload", "seal": "D788", "kg": 900, "at": AT0,
    })
    return svc


class DisputeLifecycle(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_仲裁按4450定稿后按申报重量分摊且不超共同总重(self):
        ruling = self.svc.decide_claim(
            SEGMENT, "W1", "XU-40", "correct",
            "2026-12-04T10:00:00+08:00", weight_kg=D("4450"),
        )
        self.assertEqual(ruling.decided_by, "XU-40")
        wr = self.svc.weight_resolution(SEGMENT)
        self.assertEqual(wr.total_status, TOTAL_CONSISTENT)
        self.assertEqual(wr.common_total_kg, D("4450"))
        c2, c3 = wr.cell("C2"), wr.cell("C3")
        self.assertEqual(c2.status, CELL_WEIGHT_APPORTIONED)
        # 隔离舱 C3 先按申报比例 1000/2600 剥离，C2 独占剩余可分摊重量
        quarantined_share = D("4450") * D("1000") / D("2600")
        expected_c2 = D("4450") - D("2300") - quarantined_share
        self.assertEqual(c2.kg, expected_c2)
        self.assertLessEqual(c2.kg + D("2300"), D("4450"))
        # C3 维持隔离，不形成结算重量
        self.assertEqual(c3.status, "quarantined")
        self.assertIsNone(c3.kg)

    def test_非约定仲裁员无权裁决(self):
        with self.assertRaises(PermissionDenied):
            self.svc.decide_claim(
                SEGMENT, "W1", "XU-99", "correct",
                "2026-12-04T10:00:00+08:00", weight_kg=D("4450"),
            )

    def test_双方认可同样可以定稿(self):
        ruling = self.svc.decide_claim(
            SEGMENT, "W1", f"mutual:FAC-A:YS2", "uphold",
            "2026-12-04T10:00:00+08:00",
        )
        self.assertEqual(ruling.decided_by, "mutual")
        self.assertEqual(self.svc.weight_resolution(SEGMENT).total_status,
                         TOTAL_CONSISTENT)

    def test_互认方身份不符时拒绝(self):
        with self.assertRaises(PermissionDenied):
            self.svc.decide_claim(
                SEGMENT, "W1", "mutual:FAC-B:YS2", "uphold",
                "2026-12-04T10:00:00+08:00",
            )

    def test_定稿后超领舱下禁令且历史领用保留(self):
        self.svc.decide_claim(
            SEGMENT, "W1", "XU-40", "correct",
            "2026-12-04T10:00:00+08:00", weight_kg=D("4450"),
        )
        ban = self.svc.store.ban_reason(SEGMENT, "C2")
        self.assertIsNotNone(ban)
        self.assertIn("超领", ban)
        ok, msg = self.svc.can_issue_now(SEGMENT, "C2", D("1"))
        self.assertFalse(ok)
        self.assertIn("领用禁令", msg)
        # 历史领用去向仍在
        issues = [e for e in self.svc.store.events_for(SEGMENT, "C2")
                  if e.kind == "issue"]
        self.assertEqual(issues[0].use, "run-28")
        self.assertEqual(issues[0].kg, D("1480"))

    def test_定稿后原料出账与已签认账单的冲抵调整(self):
        # 定稿前先把可独立出账的 C1 签认
        st_f = self.svc.build_statement(SEGMENT, "COOP-F", "goods")
        self.assertEqual(st_f.total, D("1069.50"))
        self.svc.sign_statement(st_f.id)
        st_j = self.svc.build_statement(SEGMENT, "COOP-J", "goods")
        self.assertEqual(st_j.total, D("425.50"))
        self.svc.sign_statement(st_j.id)

        self.svc.decide_claim(
            SEGMENT, "W1", "XU-40", "correct",
            "2026-12-04T10:00:00+08:00", weight_kg=D("4450"),
        )

        # 原账明细冻结不变
        self.assertEqual(self.svc.store.require_statement(st_f.id).total,
                         D("1069.50"))
        # C1 重量未变 → 无调整；C2 是新增可出账项（此前未签认），直接进新账
        self.assertEqual(
            self.svc.store.adjustments_for("COOP-F", SEGMENT), ())
        new_f = self.svc.build_statement(SEGMENT, "COOP-F", "goods")
        self.assertEqual({ln.cell for ln in new_f.lines}, {"C2"})
        quarantined_share = D("4450") * D("1000") / D("2600")
        c2_weight = D("4450") - D("2300") - quarantined_share
        self.assertEqual(new_f.total, (c2_weight * D("0.62")).quantize(D("0.01")))

    def test_重量下调产生冲抵调整且不得重付原账(self):
        # 无登记争议；C1 有独立实称，可先签认全额，再补开单舱争议并裁决下调
        svc = build_service(with_claims=False)
        st = svc.build_statement(SEGMENT, "COOP-F", "goods")
        self.assertEqual(st.total, D("1069.50"))
        svc.sign_statement(st.id)

        svc.open_claim(SEGMENT, "FAC-A", "C1", "weight")
        claim = next(c for c in svc.store.claims(SEGMENT) if c.scope == "C1")
        svc.decide_claim(
            SEGMENT, claim.id, "XU-40", "correct",
            "2026-12-04T11:00:00+08:00", weight_kg=D("2250"),
        )
        adjustments = svc.store.adjustments_for("COOP-F", SEGMENT)
        self.assertEqual(len(adjustments), 1)
        adj = adjustments[0]
        self.assertEqual(adj.direction, "offset")
        # 原 1725kg → 1687.5kg，差额 37.5kg × 0.62 = 23.25
        self.assertEqual(adj.amount, D("23.25"))
        # 原账保持
        self.assertEqual(svc.store.require_statement(st.id).total, D("1069.50"))

    def test_封签争议放行后恢复结算与停靠运费(self):
        # 既有首卸运费已签认付款（与样例 P1 同构）
        self.svc.import_legacy_signoff(SEGMENT, {
            "id": "P1", "scope": "C1", "item": "first_unload",
            "amount": "160.00", "paid": True,
        })
        # 先了结整车称差
        self.svc.decide_claim(
            SEGMENT, "W1", "XU-40", "correct",
            "2026-12-04T10:00:00+08:00", weight_kg=D("4450"),
        )
        # C3 仍隔离，COOP-G 无账可出
        with self.assertRaises(RuleViolation):
            self.svc.build_statement(SEGMENT, "COOP-G", "goods")

        self.svc.decide_claim(
            SEGMENT, "W2", "XU-40", "release",
            "2026-12-04T15:00:00+08:00",
        )
        overview = {r.cell: r for r in self.svc.receiving(SEGMENT)}
        self.assertEqual(overview["C3"].zone, "pending_verify")
        st_g = self.svc.build_statement(SEGMENT, "COOP-G", "goods")
        self.assertEqual(len(st_g.lines), 1)
        self.assertGreater(st_g.total, D("0"))

        from handover.billing import freight_items
        items = {(i.item, i.site): i for i in freight_items(
            self.svc.store, SEGMENT)}
        self.assertEqual(items[("other_stop", "FAC-B")].status, "billable")
        freight = self.svc.build_statement(SEGMENT, "YS2", "freight")
        # P1 已付首卸不重出；本账只含可出账的 other_stop 55
        self.assertEqual(freight.total, D("55.00"))
        self.assertEqual({ln.item for ln in freight.lines}, {"other_stop"})

    def test_封签争议拒收则该舱不构成有效停靠(self):
        self.svc.decide_claim(
            SEGMENT, "W1", "XU-40", "correct",
            "2026-12-04T10:00:00+08:00", weight_kg=D("4450"),
        )
        self.svc.decide_claim(
            SEGMENT, "W2", "XU-40", "reject",
            "2026-12-04T15:00:00+08:00",
        )
        from handover.billing import freight_items
        sites = [i.site for i in freight_items(self.svc.store, SEGMENT)]
        self.assertNotIn("FAC-B", sites)

    def test_封签争议uphold维持隔离且不能领料(self):
        self.svc.decide_claim(
            SEGMENT, "W1", "XU-40", "uphold",
            "2026-12-04T10:00:00+08:00",
        )
        self.svc.decide_claim(
            SEGMENT, "W2", "XU-40", "uphold",
            "2026-12-04T15:00:00+08:00",
        )
        overview = {r.cell: r for r in self.svc.receiving(SEGMENT)}
        self.assertEqual(overview["C3"].zone, "quarantine")
        ok, msg = self.svc.can_issue_now(SEGMENT, "C3", D("1"))
        self.assertFalse(ok)
        self.assertIn("隔离", msg)
        with self.assertRaises(RuleViolation):
            self.svc.build_statement(SEGMENT, "COOP-G", "goods")

    def test_争议不能重复定稿(self):
        from handover.errors import AlreadyFinalized, RuleViolation
        self.svc.decide_claim(
            SEGMENT, "W1", "XU-40", "uphold",
            "2026-12-04T10:00:00+08:00",
        )
        # 存储层在状态机上拒绝重复定稿
        with self.assertRaises((AlreadyFinalized, RuleViolation)):
            self.svc.decide_claim(
                SEGMENT, "W1", "XU-40", "uphold",
                "2026-12-04T11:00:00+08:00",
            )

    def test_接收地拒收后已签运费通过冲抵调整不重付(self):
        # with_claims=False：无登记争议，运费可直接出账签认
        svc = build_service(with_claims=False)
        freight = svc.build_statement(SEGMENT, "YS2", "freight")
        self.assertEqual(freight.total, D("215.00"))
        svc.sign_statement(freight.id)

        # 事后对 C3 提封签争议并拒收：FAC-B 不再是有效停靠
        claim = svc.open_claim(SEGMENT, "FAC-B", "C3", "seal")
        svc.decide_claim(
            SEGMENT, claim.id, "XU-40", "reject",
            "2026-12-04T16:00:00+08:00",
        )
        adjustments = svc.store.adjustments_for("YS2", SEGMENT)
        # other_stop 55 元被冲抵；首卸仍有效
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0].direction, "offset")
        self.assertEqual(adjustments[0].amount, D("55.00"))
        # 原账 215 不动，付款方不会被要求重付
        self.assertEqual(svc.store.require_statement(freight.id).total, D("215.00"))


class ContradictionRules(unittest.TestCase):
    def _consistent_service(self):
        """共同总重成立（4900 vs 5000，差 100 不超容差）、无登记争议的场景。"""
        svc = build_service(with_claims=False)
        # A244 已更正 A204，再次更正须指向 A244
        svc.submit_weighing("site/FAC-A", {
            "id": "A245", "corrects": "A244", "source": "FAC-A",
            "segment": SEGMENT, "scope": "vehicle", "unit": "kg",
            "gross": "9060", "tare": "4160", "at": AT0,
        })
        wr = svc.weight_resolution(SEGMENT)
        self.assertEqual(wr.total_status, TOTAL_CONSISTENT)
        self.assertEqual(wr.common_total_kg, D("4900"))
        return svc

    def test_核实舱重合计超过共同总重必须重裁且不得新增领用(self):
        svc = self._consistent_service()
        # C3 封签不符已隔离，不参与实称合计；用 C2 独立实称构造冲突。
        # C1 实称 2300 + C2 实称 2700 = 5000 > 共同总重 4900，提交即触发矛盾重裁
        with self.assertRaises(ContradictoryEvidence):
            svc.submit_weighing("site/FAC-A", {
                "id": "B201", "source": "FAC-A", "segment": SEGMENT,
                "scope": "C2", "unit": "kg", "gross": "2700", "tare": "0", "at": AT0,
            })
        # 该组凭据处于矛盾状态：不能新增领用、不能结清原料款
        wr = svc.weight_resolution(SEGMENT)
        self.assertEqual(wr.total_status, TOTAL_CONTRADICTION)
        with self.assertRaises(RuleViolation):
            svc.submit_issue("site/FAC-A", {
                "id": "U9", "segment": SEGMENT, "site": "FAC-A", "cell": "C1",
                "kind": "issue", "kg": 10, "use": "run-x", "at": AT0,
            })
        with self.assertRaises(RuleViolation):
            svc.build_statement(SEGMENT, "COOP-F", "goods")

    def test_不削减有效实称凑总重(self):
        svc = self._consistent_service()
        try:
            svc.submit_weighing("site/FAC-A", {
                "id": "B201", "source": "FAC-A", "segment": SEGMENT,
                "scope": "C2", "unit": "kg", "gross": "2700", "tare": "0", "at": AT0,
            })
        except ContradictoryEvidence:
            pass
        wr = svc.weight_resolution(SEGMENT)
        # C1、C2 的有效实称原样保留，不被削减去凑总重
        self.assertEqual(wr.cell("C1").kg, D("2300"))
        self.assertEqual(wr.cell("C2").kg, D("2700"))
        self.assertEqual(wr.cell("C2").status, CELL_WEIGHT_CELL_SCALE)


if __name__ == "__main__":
    unittest.main()
