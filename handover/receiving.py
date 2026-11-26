"""到厂交接：封签核验、交接地点顺序、待核实区与临时领用。

关键口径：

- 同一运输段按实物舱次选择交接地点：上一处实际卸完的舱不能被下一厂接收。
- 封签一致且仓班确认卸货的原料进入待核实区；封签不符即使称重对上也只能隔离。
- 临时领用上限按舱计算，取合同约定上限与本厂实际到账数量的较小值。
- 新核实重量低于已领用量时，登记明确差异并禁止后续领用；实际领用去向保留，
  绝不抹掉历史领用让两张表平衡。
- 只有负责相应厂点的仓班能确认卸货或提出称差。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .errors import RuleViolation
from .models import (
    CELL_WEIGHT_CONTESTED,
    CELL_WEIGHT_QUARANTINED,
    KIND_ISSUE,
    KIND_UNLOAD,
    SEAL_MISMATCH,
    ZONE_PENDING,
    ZONE_QUARANTINE,
    CellReceiving,
    WarehouseEvent,
)
from .store import Store
from .weights import TOTAL_CONTRADICTION, resolve_weights


@dataclass(frozen=True)
class SealCheck:
    cell: str
    expected: str
    scanned: Optional[str]
    matched: bool


def seal_checks(store: Store, segment: str) -> dict[str, SealCheck]:
    """按卸货扫描与舱单封签逐舱核验（未到厂的舱无扫描记录）。"""
    dispatch = store.require_dispatch(segment)
    out: dict[str, SealCheck] = {}
    for comp in dispatch.compartments:
        unload = next(
            (e for e in store.events_for(segment, comp.cell) if e.kind == KIND_UNLOAD),
            None,
        )
        scanned = unload.seal if unload else None
        out[comp.cell] = SealCheck(
            cell=comp.cell, expected=comp.seal, scanned=scanned,
            matched=scanned is not None and scanned == comp.seal,
        )
    return out


def quarantined_cells(store: Store, segment: str) -> frozenset[str]:
    """封签不符（含封签争议未决）的舱一律隔离；已定稿放行的恢复正常。"""
    checks = seal_checks(store, segment)
    quarantined = {
        cell for cell, chk in checks.items()
        if chk.scanned is not None and not chk.matched
    }
    # 未定稿的封签争议同样保持隔离
    for claim in store.open_claims(segment):
        if claim.reason == "seal" and claim.scope != "vehicle":
            quarantined.add(claim.scope)
    # 已定稿封签争议：release 解除隔离；reject/uphold 维持隔离
    for claim in store.claims(segment):
        if claim.reason != "seal" or claim.status != "finalized":
            continue
        ruling = store.ruling(claim.ruling_id)
        if ruling.disposition == "release":
            quarantined.discard(claim.scope)
        else:
            quarantined.add(claim.scope)
    return frozenset(quarantined)


def validate_unload_order(store: Store, segment: str, site: str, cell: str) -> None:
    """交接地点顺序校验：上一处实际卸完的舱不能被下一厂接收。"""
    dispatch = store.require_dispatch(segment)
    if site not in dispatch.stops:
        raise RuleViolation(f"厂点 {site} 不在运输段 {segment} 的停靠序列内")
    site_order = {s: i for i, s in enumerate(dispatch.stops)}
    cell_site = store.unload_site(segment, cell)
    if cell_site is not None:
        raise RuleViolation(f"舱 {cell} 已在 {cell_site} 实际卸完，不能重复接收")
    # 该舱若已被更靠前的厂点接收过会由上一条拦截；此处再拦截跨厂重收
    prior_cells_at_site = [
        e.cell for e in store.events_for(segment)
        if e.kind == KIND_UNLOAD and site_order[e.site] > site_order[site]
    ]
    if cell in prior_cells_at_site:
        raise RuleViolation(f"舱 {cell} 已被后续厂点接收，{site} 不能再接收")


def _issued_kg(store: Store, segment: str, cell: str) -> Decimal:
    return sum(
        (e.kg or Decimal("0")) for e in store.events_for(segment, cell)
        if e.kind == KIND_ISSUE
    ) or Decimal("0")


def _unload_kg(store: Store, segment: str, cell: str) -> Optional[Decimal]:
    unload = next(
        (e for e in store.events_for(segment, cell) if e.kind == KIND_UNLOAD), None
    )
    return unload.kg if unload else None


def receiving_overview(store: Store, segment: str):
    """逐舱交接状态，供仓班判断此刻能否领料。"""
    dispatch = store.require_dispatch(segment)
    contract = store.require_contract(segment)
    checks = seal_checks(store, segment)
    quarantined = quarantined_cells(store, segment)
    resolution = resolve_weights(store, segment, quarantined)

    out: list[CellReceiving] = []
    for comp in dispatch.compartments:
        cell = comp.cell
        chk = checks[cell]
        unload_kg = _unload_kg(store, segment, cell)
        issued = _issued_kg(store, segment, cell)
        site = store.unload_site(segment, cell)
        cw = resolution.cell(cell)
        held = cw.claim_ids if cw else ()

        if chk.scanned is None:
            zone = None
            seal_status = "not_arrived"
        elif cell in quarantined:
            # 扫描不符且未被定稿放行：隔离
            zone = ZONE_QUARANTINE
            seal_status = SEAL_MISMATCH
        elif not chk.matched:
            # 扫描曾不符，但封签争议已定稿放行：恢复待核实区并保留事实标记
            zone = ZONE_PENDING
            seal_status = "released_after_mismatch"
        else:
            zone = ZONE_PENDING
            seal_status = "match"

        # 临时领用上限：合同上限与本厂实际到账数量取小；隔离舱不得领用
        cap = Decimal("0")
        if zone == ZONE_PENDING and unload_kg is not None:
            cap = min(contract.unverified_use_cap_kg, unload_kg)

        ban = store.ban_reason(segment, cell)
        issue_blocked = zone != ZONE_PENDING or ban is not None or issued >= cap
        reason = ban
        if zone == ZONE_QUARANTINE:
            reason = reason or "封签不符已隔离，禁止领用"
        elif zone is None:
            reason = reason or "尚未到厂确认卸货"
        elif ban is None and issued >= cap and cap > 0:
            reason = f"已领 {issued}kg 达到临时领用上限 {cap}kg"

        out.append(CellReceiving(
            cell=cell, site=site, seal_status=seal_status, zone=zone,
            unload_kg=unload_kg, issued_kg=issued,
            provisional_cap_kg=cap, issue_blocked=issue_blocked,
            block_reason=reason, weight_status=cw.status if cw else "unknown",
            held_claim_ids=held,
        ))
    return tuple(out)


def can_issue(store: Store, segment: str, cell: str, kg: Decimal) -> tuple[bool, str]:
    """仓班领料前的实时判定。"""
    overview = {row.cell: row for row in receiving_overview(store, segment)}
    if cell not in overview:
        return False, f"舱 {cell} 不属于运输段 {segment}"
    quarantined = quarantined_cells(store, segment)
    resolution = resolve_weights(store, segment, quarantined)
    if resolution.total_status == TOTAL_CONTRADICTION:
        return False, "重量凭据矛盾待重裁，不得凭矛盾重量新增领用"
    row = overview[cell]
    if row.zone == ZONE_QUARANTINE:
        return False, "封签不符隔离舱，不能领用"
    if row.zone is None:
        return False, "该舱尚未确认卸货入待核实区"
    if store.ban_reason(segment, cell):
        return False, f"领用禁令：{store.ban_reason(segment, cell)}"
    remaining = row.provisional_cap_kg - row.issued_kg
    if kg > remaining:
        return False, (
            f"申请 {kg}kg 超出可领余量 {remaining}kg"
            f"（上限 {row.provisional_cap_kg}kg，已领 {row.issued_kg}kg）"
        )
    return True, f"可领 {kg}kg，领后累计 {row.issued_kg + kg}kg"


def reconcile_issue_bans(store: Store, segment: str) -> list[str]:
    """重量证据变化后重算：新核实重量低于已领用即下禁令，保留领用去向。

    返回本次刷新出的禁令说明列表（已存在的不重复登记）。
    """
    quarantined = quarantined_cells(store, segment)
    resolution = resolve_weights(store, segment, quarantined)
    refreshed: list[str] = []
    for cw in resolution.cells:
        if cw.status in (CELL_WEIGHT_CONTESTED, CELL_WEIGHT_QUARANTINED):
            continue
        if cw.kg is None:
            continue
        issued = _issued_kg(store, segment, cw.cell)
        if issued > cw.kg:
            diff = issued - cw.kg
            reason = (
                f"已核实重量 {cw.kg}kg 低于实际领用 {issued}kg，"
                f"超领 {diff}kg；停止后续领用，历史领用去向保留待裁"
            )
            if store.ban_reason(segment, cw.cell) != reason:
                store.ban_issue(segment, cw.cell, reason)
                refreshed.append(f"{cw.cell}: {reason}")
    return refreshed


def register_issue(store: Store, segment: str, event: WarehouseEvent) -> None:
    """领出登记：先过实时判定，再保留现场事实。"""
    ok, reason = can_issue(store, segment, event.cell, event.kg)
    if not ok:
        raise RuleViolation(reason)
    store.add_event(event)
