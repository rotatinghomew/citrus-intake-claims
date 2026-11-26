"""凭据登记处：按来源保存各方交换材料，保留更正链与全部历史事实。

存储只负责登记与检索，不做业务裁决；业务规则在 weights/receiving/... 中。
所有追加式记录（磅房、仓班、争议、裁决、账单）只增不改，更正以新记录表达。
"""
from __future__ import annotations

from typing import Optional

from .errors import AlreadyFinalized, NotFound, ValidationError
from .models import (
    Adjustment,
    Claim,
    Contract,
    Dispatch,
    Ruling,
    Statement,
    WarehouseEvent,
    Weighing,
)


class Store:
    def __init__(self) -> None:
        self.dispatches: dict[str, Dispatch] = {}
        self.contracts: dict[str, Contract] = {}
        self._weighings: list[Weighing] = []
        self._events: list[WarehouseEvent] = []
        self._claims: dict[str, Claim] = {}
        self._rulings: dict[str, Ruling] = {}
        self._statements: dict[str, Statement] = {}
        self._adjustments: list[Adjustment] = []
        # 领用禁令：(segment, cell) -> 原因
        self.issue_bans: dict[tuple[str, str], str] = {}
        self._statement_seq = 0
        self._adjustment_seq = 0
        self._ruling_seq = 0

    # ---- 舱单与合同 ----
    def add_dispatch(self, dispatch: Dispatch) -> None:
        if dispatch.segment in self.dispatches:
            raise ValidationError(f"运输段 {dispatch.segment} 舱单已登记")
        cells = [c.cell for c in dispatch.compartments]
        if len(set(cells)) != len(cells):
            raise ValidationError("舱位编号必须唯一")
        if len(set(dispatch.stops)) != len(dispatch.stops):
            raise ValidationError("停靠厂点不得重复")
        self.dispatches[dispatch.segment] = dispatch

    def require_dispatch(self, segment: str) -> Dispatch:
        try:
            return self.dispatches[segment]
        except KeyError:
            raise NotFound(f"运输段 {segment} 不存在") from None

    def add_contract(self, segment: str, contract: Contract) -> None:
        self.contracts[segment] = contract

    def require_contract(self, segment: str) -> Contract:
        try:
            return self.contracts[segment]
        except KeyError:
            raise NotFound(f"运输段 {segment} 合同未登记") from None

    # ---- 磅房 ----
    def add_weighing(self, row: Weighing) -> None:
        if any(w.id == row.id for w in self._weighings):
            raise ValidationError(f"磅房记录编号 {row.id} 重复")
        if row.corrects is not None and not any(
            w.id == row.corrects and w.segment == row.segment for w in self._weighings
        ):
            raise ValidationError(f"更正记录 {row.corrects} 找不到原记录")
        if row.unit not in ("kg", "t"):
            raise ValidationError("重量单位只能是 kg 或 t")
        self._weighings.append(row)

    def weighings(self, segment: str) -> tuple[Weighing, ...]:
        return tuple(w for w in self._weighings if w.segment == segment)

    def effective_weighings(self, segment: str) -> tuple[Weighing, ...]:
        """应用更正链后的有效磅房记录（被更正的原记录剔除，保留历史）。"""
        corrected = {
            w.corrects for w in self._weighings
            if w.segment == segment and w.corrects is not None
        }
        return tuple(
            w for w in self._weighings
            if w.segment == segment and w.id not in corrected
        )

    # ---- 仓班 ----
    def add_event(self, event: WarehouseEvent) -> None:
        if any(e.id == event.id for e in self._events):
            raise ValidationError(f"仓班记录编号 {event.id} 重复")
        self._events.append(event)

    def events(self, segment: str | None = None) -> tuple[WarehouseEvent, ...]:
        return tuple(self._events)

    def events_for(self, segment: str, cell: Optional[str] = None) -> tuple[WarehouseEvent, ...]:
        cells = {c.cell for c in self.require_dispatch(segment).compartments}
        out = [
            e for e in self._events
            if e.cell in cells and (cell is None or e.cell == cell)
        ]
        return tuple(sorted(out, key=lambda e: e.at))

    def unload_site(self, segment: str, cell: str) -> Optional[str]:
        """舱位的实际卸货厂点（同一舱只允许一处实际卸完）。"""
        for e in self.events_for(segment, cell):
            if e.kind == "unload":
                return e.site
        return None

    # ---- 争议与裁决 ----
    def add_claim(self, claim: Claim) -> None:
        if claim.id in self._claims:
            raise ValidationError(f"争议编号 {claim.id} 重复")
        self._claims[claim.id] = claim

    def claims(self, segment: Optional[str] = None) -> tuple[Claim, ...]:
        out = self._claims.values()
        if segment is not None:
            out = (c for c in out if c.segment == segment)
        return tuple(sorted(out, key=lambda c: c.id))

    def require_claim(self, claim_id: str) -> Claim:
        try:
            return self._claims[claim_id]
        except KeyError:
            raise NotFound(f"争议 {claim_id} 不存在") from None

    def finalize_claim(self, claim_id: str, ruling_id: str) -> None:
        claim = self.require_claim(claim_id)
        if claim.status != "open":
            raise AlreadyFinalized(f"争议 {claim_id} 已定稿")
        self._claims[claim_id] = Claim(
            claim.id, claim.segment, claim.scope, claim.by, claim.reason,
            status="finalized", ruling_id=ruling_id,
        )

    def open_claims(self, segment: str, scope: Optional[str] = None) -> tuple[Claim, ...]:
        out = [
            c for c in self._claims.values()
            if c.segment == segment and c.status == "open"
            and (scope is None or c.scope == scope or c.scope == "vehicle")
        ]
        return tuple(sorted(out, key=lambda c: c.id))

    def add_ruling(self, ruling: Ruling) -> None:
        if ruling.id in self._rulings:
            raise ValidationError(f"裁决编号 {ruling.id} 重复")
        self._rulings[ruling.id] = ruling

    def ruling(self, ruling_id: str) -> Ruling:
        try:
            return self._rulings[ruling_id]
        except KeyError:
            raise NotFound(f"裁决 {ruling_id} 不存在") from None

    def rulings_for(self, segment: str, scope: str) -> tuple[Ruling, ...]:
        return tuple(
            r for r in self._rulings.values()
            if r.segment == segment and r.scope == scope
        )

    def next_ruling_id(self) -> str:
        self._ruling_seq += 1
        return f"R{self._ruling_seq:03d}"

    # ---- 对账单与调整单 ----
    def next_statement_id(self) -> str:
        self._statement_seq += 1
        return f"S{self._statement_seq:03d}"

    def add_statement(self, statement: Statement) -> None:
        self._statements[statement.id] = statement

    def sign_statement(self, statement_id: str) -> Statement:
        st = self.require_statement(statement_id)
        if st.signed:
            raise AlreadyFinalized(f"对账单 {statement_id} 已签认")
        signed = Statement(
            st.id, st.party, st.kind, st.segment, st.lines, st.total,
            st.currency, signed=True,
        )
        self._statements[statement_id] = signed
        return signed

    def require_statement(self, statement_id: str) -> Statement:
        try:
            return self._statements[statement_id]
        except KeyError:
            raise NotFound(f"对账单 {statement_id} 不存在") from None

    def statements_for(self, party: str, segment: Optional[str] = None) -> tuple[Statement, ...]:
        return tuple(
            s for s in self._statements.values()
            if s.party == party and (segment is None or s.segment == segment)
        )

    def next_adjustment_id(self) -> str:
        self._adjustment_seq += 1
        return f"A{self._adjustment_seq:03d}"

    def add_adjustment(self, adjustment: Adjustment) -> None:
        self._adjustments.append(adjustment)

    def adjustments_for(self, party: str, segment: Optional[str] = None) -> tuple[Adjustment, ...]:
        return tuple(
            a for a in self._adjustments
            if a.party == party and (segment is None or a.segment == segment)
        )

    # ---- 领用禁令（运行状态，由裁决/重算驱动） ----
    def ban_issue(self, segment: str, cell: str, reason: str) -> None:
        self.issue_bans[(segment, cell)] = reason

    def ban_reason(self, segment: str, cell: str) -> Optional[str]:
        return self.issue_bans.get((segment, cell))
