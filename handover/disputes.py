"""称差与封签争议：登记、双方认可处置、仲裁员裁决与定稿。

- 称差超合同容差或过磅证据不足时，厂方和承运方可争议某舱或整段。
- 封签争议与称差可同时存在；只有双方认可的处置或约定仲裁员的裁决能定稿。
- 只有争议涉及的款项暂缓；定稿后按裁决恢复出账或触发更正。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .errors import PermissionDenied, RuleViolation, ValidationError
from .models import (
    Claim,
    Contract,
    Dispatch,
    Ruling,
)
from .receiving import reconcile_issue_bans, seal_checks
from .store import Store

VEHICLE = "vehicle"
REASON_SEAL = "seal"
REASON_WEIGHT = "weight"


class DisputeService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _validate_scope(self, segment: str, scope: str) -> None:
        dispatch = self.store.require_dispatch(segment)
        if scope != VEHICLE and scope not in {c.cell for c in dispatch.compartments}:
            raise ValidationError(f"争议范围 {scope} 既不是整车也不是该段舱位")

    def open_claim(self, segment: str, actor: str, scope: str,
                   reason: str = REASON_WEIGHT) -> Claim:
        """厂方（仅本厂点相关）或承运方登记争议。"""
        dispatch = self.store.require_dispatch(segment)
        self._validate_scope(segment, scope)
        if reason not in (REASON_WEIGHT, REASON_SEAL):
            raise ValidationError("争议原因只能是 weight 或 seal")

        is_carrier = actor == dispatch.carrier
        is_site = actor in dispatch.stops
        if not (is_carrier or is_site):
            raise PermissionDenied(f"{actor} 无权对运输段 {segment} 提出争议")

        if reason == REASON_SEAL:
            if scope == VEHICLE:
                raise ValidationError("封签争议必须针对具体舱位")
            if is_carrier:
                # 承运方可就封签事实提出争议
                pass
            else:
                check = seal_checks(self.store, segment)[scope]
                if check.scanned is None:
                    raise RuleViolation(f"舱 {scope} 尚无到厂封签扫描，不能提封签争议")

        # 编号在运输段内唯一，避开合同预置编号（如 W1）
        existing = {c.id for c in self.store.claims(segment)}
        seq = len(existing) + 1
        claim_id = f"C{seq:03d}"
        while claim_id in existing:
            seq += 1
            claim_id = f"C{seq:03d}"
        claim = Claim(
            id=claim_id,
            segment=segment, scope=scope, by=actor, reason=reason,
        )
        self.store.add_claim(claim)
        return claim

    def decide(self, segment: str, claim_id: str, actor: str, disposition: str,
               at: datetime, weight_kg=None, site: Optional[str] = None,
               note: Optional[str] = None) -> Ruling:
        """提交定稿处置。

        actor 为 "mutual:<厂点>:<承运方>" 形式表示双方认可；
        或为合同约定的仲裁员编号。
        """
        dispatch: Dispatch = self.store.require_dispatch(segment)
        contract: Contract = self.store.require_contract(segment)
        claim = self.store.require_claim(claim_id)
        if claim.segment != segment:
            raise ValidationError("争议不属于该运输段")
        if claim.status != "open":
            raise RuleViolation(f"争议 {claim_id} 已定稿，不能再裁决")
        if disposition not in ("uphold", "correct", "release", "reject"):
            raise ValidationError("处置只能是 uphold/correct/release/reject")

        decided_by: str
        if actor.startswith("mutual:"):
            parts = actor.split(":")
            if len(parts) != 3:
                raise ValidationError("双方认可写法为 mutual:厂点:承运方")
            _, site_party, carrier_party = parts
            if carrier_party != dispatch.carrier:
                raise PermissionDenied("双方认可必须由承运方与争议厂点共同作出")
            # 厂方一方须是实际发起该争议的厂点（厂方发起时）
            if claim.by in dispatch.stops and site_party != claim.by:
                raise PermissionDenied(
                    f"厂点 {site_party} 不是争议 {claim_id} 的发起厂点 {claim.by}"
                )
            if claim.by == dispatch.carrier and site_party not in dispatch.stops:
                raise PermissionDenied("双方认可必须包含一个停靠厂点")
            decided_by = "mutual"
        else:
            if actor != contract.arbiter:
                raise PermissionDenied(
                    f"{actor} 不是合同约定仲裁员 {contract.arbiter}，无权裁决"
                )
            decided_by = actor

        if disposition == "correct" and weight_kg is None and site is None:
            raise ValidationError("correct 处置必须给出裁定重量或接收地更正")
        if claim.reason == REASON_SEAL and disposition not in ("release", "reject", "uphold"):
            raise ValidationError("封签争议只能作 release/reject/uphold 处置")

        ruling = Ruling(
            id=self.store.next_ruling_id(), claim_id=claim_id, segment=segment,
            scope=claim.scope, decided_by=decided_by, disposition=disposition,
            at=at, weight_kg=weight_kg, site=site, note=note,
        )
        self.store.add_ruling(ruling)
        self.store.finalize_claim(claim_id, ruling.id)
        # 定稿可能改变可核实重量，立即刷新领用禁令
        reconcile_issue_bans(self.store, segment)
        return ruling

    @staticmethod
    def claims_affecting(store: Store, segment: str, cell: str,
                         only_open: bool = True) -> tuple[Claim, ...]:
        """影响某舱的全部争议：本舱争议 + 整车争议。"""
        claims = store.open_claims(segment) if only_open else store.claims(segment)
        return tuple(c for c in claims if c.scope in (VEHICLE, cell))
