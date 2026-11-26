"""新车/分舱接收、补传证据、单位换算与权限边界测试。"""
import unittest
from decimal import Decimal as D

from handover.errors import PermissionDenied, ValidationError
from handover.models import TOTAL_CONSISTENT
from handover.service import HandoverService

AT = "2026-12-05T08:00:00+08:00"


def new_service(segment="T-200"):
    svc = HandoverService()
    svc.register_dispatch({
        "segment": segment, "carrier": "YS8",
        "stops": ["FAC-A", "FAC-B"],
        "compartments": [
            {"cell": "K1", "seal": "S1", "declared_kg": 1000,
             "shares": [["COOP-F", "B1", 1]]},
            {"cell": "K2", "seal": "S2", "declared_kg": 1000,
             "shares": [["COOP-G", "B2", 1]]},
        ],
    })
    svc.register_contract(segment, {
        "goods": {"COOP-F": "0.50", "COOP-G": "0.60"},
        "unverified_use_cap_kg": 800, "weight_tolerance_kg": 50,
        "carrier_first_unload_fee": "100.00",
        "carrier_other_stop_fee": "40.00", "arbiter": "ARB-1",
    })
    return svc


class IngestionAndPermissions(unittest.TestCase):
    def test_系统接受新车新运输段并与既有段隔离(self):
        first = new_service("T-200")
        # 同一存储承载第二段新车，互不串数据
        second = HandoverService(first.store)
        second.register_dispatch({
            "segment": "T-300", "carrier": "YS9", "stops": ["FAC-A"],
            "compartments": [
                {"cell": "M1", "seal": "X1", "declared_kg": 500,
                 "shares": [["COOP-F", "B9", 1]]},
            ],
        })
        second.register_contract("T-300", {
            "goods": {"COOP-F": "0.50"}, "unverified_use_cap_kg": 100,
            "weight_tolerance_kg": 10, "carrier_first_unload_fee": "10.00",
            "carrier_other_stop_fee": "0", "arbiter": "ARB-1",
        })
        self.assertEqual(set(first.store.dispatches), {"T-200", "T-300"})
        self.assertEqual(
            {c.cell for c in first.store.require_dispatch("T-200").compartments},
            {"K1", "K2"},
        )
        self.assertEqual(
            {c.cell for c in first.store.require_dispatch("T-300").compartments},
            {"M1"},
        )

    def test_吨与千克单位换算后净重一致(self):
        svc = new_service()
        svc.submit_weighing("ORIGIN", {
            "id": "W1", "source": "ORIGIN", "segment": "T-200",
            "scope": "vehicle", "unit": "t", "gross": "3.00", "tare": "1.00",
            "at": AT,
        })
        svc.submit_weighing("site/FAC-A", {
            "id": "W2", "source": "FAC-A", "segment": "T-200",
            "scope": "vehicle", "unit": "kg", "gross": "3020", "tare": "1000",
            "at": AT,
        })
        wr = svc.weight_resolution("T-200")
        self.assertEqual(wr.total_status, TOTAL_CONSISTENT)
        self.assertEqual(wr.common_total_kg, D("2020"))

    def test_只有对应厂点能传本厂磅房与卸货(self):
        svc = new_service()
        with self.assertRaises(PermissionDenied):
            svc.submit_weighing("site/FAC-B", {
                "id": "W9", "source": "FAC-A", "segment": "T-200",
                "scope": "vehicle", "unit": "kg", "gross": "1", "tare": "0",
                "at": AT,
            })
        with self.assertRaises(PermissionDenied):
            svc.submit_unload("site/FAC-B", {
                "id": "E1", "segment": "T-200", "site": "FAC-A",
                "cell": "K1", "kind": "unload", "seal": "S1",
                "kg": 900, "at": AT,
            })

    def test_外部无关方不能提交凭据(self):
        svc = new_service()
        with self.assertRaises(PermissionDenied):
            svc.submit_weighing("coop/COOP-F", {
                "id": "W9", "source": "FAC-A", "segment": "T-200",
                "scope": "vehicle", "unit": "kg", "gross": "1", "tare": "0",
                "at": AT,
            })
        with self.assertRaises(PermissionDenied):
            svc.open_claim("T-200", "COOP-F", "vehicle")

    def test_补传的单舱实称可解释已发生交接并改变可领状态(self):
        svc = new_service()
        svc.submit_weighing("ORIGIN", {
            "id": "W1", "source": "ORIGIN", "segment": "T-200",
            "scope": "vehicle", "unit": "kg", "gross": "3000", "tare": "1000",
            "at": AT,
        })
        svc.submit_weighing("site/FAC-A", {
            "id": "W2", "source": "FAC-A", "segment": "T-200",
            "scope": "vehicle", "unit": "kg", "gross": "3000", "tare": "1000",
            "at": AT,
        })
        svc.submit_unload("site/FAC-A", {
            "id": "E1", "segment": "T-200", "site": "FAC-A", "cell": "K1",
            "kind": "unload", "seal": "S1", "kg": 950, "at": AT,
        })
        # 先领 800（上限）
        svc.submit_issue("site/FAC-A", {
            "id": "U1", "segment": "T-200", "site": "FAC-A", "cell": "K1",
            "kind": "issue", "kg": 800, "use": "run-1", "at": AT,
        })
        # 补传单舱实称只有 700 → 触发超领禁令
        svc.submit_weighing("site/FAC-A", {
            "id": "W3", "source": "FAC-A", "segment": "T-200",
            "scope": "K1", "unit": "kg", "gross": "700", "tare": "0",
            "at": AT, "sent_at": "2026-12-05T12:00:00+08:00",
        })
        self.assertIsNotNone(svc.store.ban_reason("T-200", "K1"))
        ok, msg = svc.can_issue_now("T-200", "K1", D("1"))
        self.assertFalse(ok)
        self.assertIn("领用禁令", msg)
        # 历史领用保留
        self.assertEqual(
            sum(e.kg for e in svc.store.events_for("T-200", "K1")
                if e.kind == "issue"),
            D("800"),
        )

    def test_更正必须指向存在的原记录(self):
        svc = new_service()
        with self.assertRaises(ValidationError):
            svc.submit_weighing("site/FAC-A", {
                "id": "W9", "corrects": "NOPE", "source": "FAC-A",
                "segment": "T-200", "scope": "vehicle", "unit": "kg",
                "gross": "1", "tare": "0", "at": AT,
            })

    def test_称重范围超出舱单被拒绝(self):
        from handover.errors import RuleViolation
        svc = new_service()
        with self.assertRaises(RuleViolation):
            svc.submit_weighing("site/FAC-A", {
                "id": "W9", "source": "FAC-A", "segment": "T-200",
                "scope": "K9", "unit": "kg", "gross": "1", "tare": "0", "at": AT,
            })

    def test_过磅证据不足时共同总重待裁(self):
        svc = new_service()
        # 只有装车方整车磅，缺到厂方
        svc.submit_weighing("ORIGIN", {
            "id": "W1", "source": "ORIGIN", "segment": "T-200",
            "scope": "vehicle", "unit": "kg", "gross": "3000", "tare": "1000",
            "at": AT,
        })
        wr = svc.weight_resolution("T-200")
        self.assertEqual(wr.total_status, "contested")
        self.assertIn("证据不足", wr.note)


if __name__ == "__main__":
    unittest.main()
