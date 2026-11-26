"""进厂交接服务门面：按业务方权限接收凭据并驱动交接、裁决与出账。"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .billing import (
    build_statement,
    post_freight_adjustment,
    post_weight_adjustments,
    sign_statement,
)
from .disputes import DisputeService
from .errors import ContradictoryEvidence, PermissionDenied, RuleViolation
from .models import (
    Compartment,
    Contract,
    Dispatch,
    Share,
    WarehouseEvent,
    Weighing,
    parse_dt,
)
from .receiving import (
    can_issue,
    quarantined_cells,
    receiving_overview,
    reconcile_issue_bans,
    register_issue,
    validate_unload_order,
)
from .store import Store
from .views import role_view
from .weights import TOTAL_CONTRADICTION, resolve_weights

VEHICLE = "vehicle"


class HandoverService:
    """一个服务实例承载一个或多个运输段的全部交接状态。"""

    def __init__(self, store: Optional[Store] = None) -> None:
        self.store = store or Store()
        self.disputes = DisputeService(self.store)

    # ---------- 舱单与合同（装车方/合同管理方登记） ----------

    def register_dispatch(self, payload: dict) -> Dispatch:
        compartments = tuple(
            Compartment(
                cell=row["cell"], seal=row["seal"],
                declared_kg=Decimal(str(row["declared_kg"])),
                shares=tuple(
                    Share(coop=s[0], batch=s[1], parts=int(s[2]))
                    for s in row["shares"]
                ),
            )
            for row in payload["compartments"]
        )
        dispatch = Dispatch(
            segment=payload["segment"], carrier=payload["carrier"],
            stops=tuple(payload["stops"]), compartments=compartments,
        )
        self.store.add_dispatch(dispatch)
        return dispatch

    def register_contract(self, segment: str, payload: dict) -> Contract:
        contract = Contract(
            goods_price={k: Decimal(str(v)) for k, v in payload["goods"].items()},
            currency=payload.get("currency", "CNY"),
            unverified_use_cap_kg=Decimal(str(payload["unverified_use_cap_kg"])),
            weight_tolerance_kg=Decimal(str(payload["weight_tolerance_kg"])),
            carrier_first_unload_fee=Decimal(str(payload["carrier_first_unload_fee"])),
            carrier_other_stop_fee=Decimal(str(payload["carrier_other_stop_fee"])),
            arbiter=payload["arbiter"],
            claims=(),
        )
        self.store.add_contract(segment, contract)
        # 合同里带的初始争议（如磅房报文落地前已登记的争议）
        for row in payload.get("claims", []):
            self.store.add_claim(self._claim_from(segment, row))
        return contract

    def _claim_from(self, segment: str, row: dict):
        from .models import Claim
        return Claim(
            id=row["id"], segment=segment, scope=row["scope"], by=row["by"],
            reason=row.get("reason", "weight"),
        )

    def import_legacy_signoff(self, segment: str, payload: dict) -> None:
        """登记既有已付款对账单（初始账单仅供对接既有明细，保持原明细）。"""
        from .models import FreightItem, Statement
        dispatch = self.store.require_dispatch(segment)
        contract = self.store.require_contract(segment)
        amount = Decimal(str(payload["amount"]))
        # 按已认定项目找到对应有效首卸站点，便于后续运费去重
        site = None
        if payload["item"] == "first_unload":
            for comp in dispatch.compartments:
                site = self.store.unload_site(segment, comp.cell)
                if site is not None:
                    break
        line = FreightItem(
            segment=segment, site=site, item=payload["item"],
            amount=amount, status="settled",
        )
        statement = Statement(
            id=payload["id"], party=dispatch.carrier, kind="freight",
            segment=segment, lines=(line,), total=amount,
            currency=contract.currency, signed=True,
        )
        self.store.add_statement(statement)

    # ---------- 磅房回传 ----------

    def submit_weighing(self, actor: str, payload: dict) -> Weighing:
        """装车方（ORIGIN）或到厂厂点提交磅房记录，允许补传与更正。"""
        segment = payload["segment"]
        dispatch = self.store.require_dispatch(segment)
        source = payload["source"]
        if source != "ORIGIN" and source not in dispatch.stops:
            raise PermissionDenied(f"{source} 无权回传运输段 {segment} 的磅房数据")
        if source == "ORIGIN":
            # 装车方来源只接受装车系统或装车门卫身份
            if actor not in ("ORIGIN",) and not actor.startswith(("origin/", "loader/")):
                raise PermissionDenied(f"{actor} 不能代装车方提交磅房记录")
        elif actor not in (source, f"site/{source}"):
            raise PermissionDenied(f"{actor} 不能代厂点 {source} 提交磅房记录")

        row = Weighing(
            id=payload["id"], source=source, segment=segment,
            scope=payload["scope"], unit=payload["unit"],
            gross=Decimal(str(payload["gross"])),
            tare=Decimal(str(payload["tare"])),
            at=parse_dt(payload["at"]),
            sent_at=parse_dt(payload["sent_at"]) if payload.get("sent_at") else None,
            corrects=payload.get("corrects"),
        )
        if row.scope != VEHICLE and row.scope not in {c.cell for c in dispatch.compartments}:
            raise RuleViolation(f"称重范围 {row.scope} 不在舱单内")
        self.store.add_weighing(row)
        # 补传证据可能解释已发生的交接，也可能触发矛盾重裁
        quarantined = quarantined_cells(self.store, segment)
        resolution = resolve_weights(self.store, segment, quarantined)
        if resolution.total_status == TOTAL_CONTRADICTION:
            raise ContradictoryEvidence(resolution.note)
        reconcile_issue_bans(self.store, segment)
        return row

    # ---------- 仓班现场 ----------

    def submit_unload(self, actor: str, payload: dict) -> WarehouseEvent:
        """只有负责相应厂点的仓班能确认卸货。"""
        segment = payload["segment"]
        dispatch = self.store.require_dispatch(segment)
        site = payload["site"]
        if actor not in (site, f"site/{site}"):
            raise PermissionDenied(f"{actor} 不是厂点 {site} 的仓班，不能确认卸货")
        if site not in dispatch.stops:
            raise PermissionDenied(f"厂点 {site} 不在运输段停靠序列内")
        cell = payload["cell"]
        validate_unload_order(self.store, segment, site, cell)

        event = WarehouseEvent(
            id=payload["id"], site=site, cell=cell, kind="unload",
            seal=payload.get("seal"), kg=Decimal(str(payload["kg"])),
            at=parse_dt(payload["at"]),
        )
        self.store.add_event(event)
        reconcile_issue_bans(self.store, segment)
        return event

    def submit_issue(self, actor: str, payload: dict) -> WarehouseEvent:
        segment = payload["segment"]
        site = payload["site"]
        if actor not in (site, f"site/{site}"):
            raise PermissionDenied(f"{actor} 不是厂点 {site} 的仓班，不能登记领用")
        event = WarehouseEvent(
            id=payload["id"], site=site, cell=payload["cell"], kind="issue",
            kg=Decimal(str(payload["kg"])), use=payload.get("use"),
            at=parse_dt(payload["at"]),
        )
        register_issue(self.store, segment, event)
        return event

    # ---------- 查询 ----------

    def weight_resolution(self, segment: str):
        quarantined = quarantined_cells(self.store, segment)
        return resolve_weights(self.store, segment, quarantined)

    def receiving(self, segment: str):
        return receiving_overview(self.store, segment)

    def can_issue_now(self, segment: str, cell: str, kg: Decimal):
        return can_issue(self.store, segment, cell, Decimal(str(kg)))

    def open_claim(self, segment: str, actor: str, scope: str,
                   reason: str = "weight"):
        claim = self.disputes.open_claim(segment, actor, scope, reason)
        reconcile_issue_bans(self.store, segment)
        return claim

    def decide_claim(self, segment: str, claim_id: str, actor: str,
                     disposition: str, at: str, **kwargs):
        ruling = self.disputes.decide(
            segment, claim_id, actor, disposition, parse_dt(at), **kwargs
        )
        reason = f"争议 {claim_id} 定稿处置 {disposition}"
        post_weight_adjustments(self.store, segment, ruling.id, reason)
        post_freight_adjustment(self.store, segment, ruling.id, reason)
        return ruling

    # ---------- 出账 ----------

    def build_statement(self, segment: str, party: str, kind: str):
        return build_statement(self.store, segment, party, kind)

    def sign_statement(self, statement_id: str):
        return sign_statement(self.store, statement_id)

    def finance_position(self, segment: str):
        from .billing import finance_position
        return finance_position(self.store, segment)

    # ---------- 权限视图 ----------

    def view_for(self, actor: str, segment: str) -> dict:
        return role_view(self.store, actor, segment)
