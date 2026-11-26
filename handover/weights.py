"""重量解析：共同有效总重、单舱有效实称、申报重量分摊。

规则（对应业务口径）：

1. 更正记录取代原记录，原记录保留但不再有效。
2. scope=vehicle 的整车磅以舱单所列全舱为共同范围；装车方与到厂方各有
   有效整车磅且净重差不超过合同容差时，共同总重成立，否则进入称差争议。
3. 单舱独立有效实称优先保留：同一舱多条实称在容差内互证，超容差即争议。
4. 无单舱实称的舱按舱单申报重量在“共同总重扣除已核实舱重”的余量内分摊，
   分摊重量不得超出共同有效总重。
5. 已核实舱重合计超过共同总重时整组凭据矛盾，必须重新裁定：不得削减有效
   实称凑总重，也不得凭矛盾重量新增领用或结清原料款。
6. 封签不符的隔离舱不参与结算重量，其实物份额仍在整车总重之内。
7. 已定稿争议按裁决重量处理（correct 更正、uphold 维持）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .models import (
    CELL_WEIGHT_APPORTIONED,
    CELL_WEIGHT_CELL_SCALE,
    CELL_WEIGHT_CONTESTED,
    CELL_WEIGHT_QUARANTINED,
    Claim,
    Ruling,
    TOTAL_CONSISTENT,
    TOTAL_CONTESTED,
    TOTAL_CONTRADICTION,
    CellWeight,
    WeightResolution,
)
from .store import Store

VEHICLE = "vehicle"
REASON_SEAL = "seal"


def _is_weight_claim(claim: Claim) -> bool:
    return claim.reason != REASON_SEAL


def _weight_rulings(store: Store, segment: str) -> dict[str, Ruling]:
    """取各 scope 最近一次已定稿的重量裁决（封签裁决不参与重量口径）。"""
    out: dict[str, Ruling] = {}
    for claim in store.claims(segment):
        if claim.status != "finalized" or not _is_weight_claim(claim):
            continue
        out[claim.scope] = store.ruling(claim.ruling_id)
    return out


def _open_weight_claims(store: Store, segment: str) -> tuple[Claim, ...]:
    return tuple(c for c in store.open_claims(segment) if _is_weight_claim(c))


def resolve_weights(store: Store, segment: str,
                    quarantined_cells: frozenset[str] = frozenset()) -> WeightResolution:
    """按当前全部有效证据解析运输段重量。

    quarantined_cells 由 receiving 层依据封签核验给出。
    """
    dispatch = store.require_dispatch(segment)
    contract = store.require_contract(segment)
    tolerance = contract.weight_tolerance_kg
    effective = store.effective_weighings(segment)
    rulings = _weight_rulings(store, segment)
    open_claims = _open_weight_claims(store, segment)
    vehicle_claims = tuple(c.id for c in open_claims if c.scope == VEHICLE)

    # ---- 共同总重 ----
    vehicle_rows = [w for w in effective if w.scope == VEHICLE]
    total_status: str
    common_total: Optional[Decimal]
    total_note: Optional[str] = None

    if VEHICLE in rulings and rulings[VEHICLE].disposition == "correct":
        common_total = rulings[VEHICLE].weight_kg
        total_status = TOTAL_CONSISTENT
    elif VEHICLE in rulings:  # uphold：以到厂方实称为准
        arrived = [w for w in vehicle_rows if w.source != "ORIGIN"]
        common_total = arrived[0].net_kg if arrived else vehicle_rows[0].net_kg
        total_status = TOTAL_CONSISTENT
    else:
        if len({w.source for w in vehicle_rows}) < 2:
            # 装车方或到厂方整车磅缺失：过磅证据不足
            total_status = TOTAL_CONTESTED
            common_total = None
            total_note = "整车过磅证据不足，需补齐装车方与到厂方整车磅"
        else:
            nets = sorted((w.net_kg for w in vehicle_rows))
            if nets[-1] - nets[0] > tolerance:
                total_status = TOTAL_CONTESTED
                common_total = None
                total_note = f"整车净重差 {nets[-1] - nets[0]}kg 超过容差 {tolerance}kg"
            else:
                # 共同有效总重取到厂方整车磅（例中厂方整车磅在首次卸车前）
                arrived = [w for w in vehicle_rows if w.source != "ORIGIN"]
                common_total = arrived[0].net_kg if arrived else nets[0]
                total_status = TOTAL_CONSISTENT

    # ---- 单舱实称 ----
    cell_rows: dict[str, list] = {}
    for w in effective:
        if w.scope != VEHICLE:
            cell_rows.setdefault(w.scope, []).append(w)

    cell_weights: list[CellWeight] = []
    verified_sum = Decimal("0")
    for comp in dispatch.compartments:
        cell = comp.cell

        if cell in quarantined_cells:
            seal_claim = tuple(
                c.id for c in open_claims
                if c.scope == cell and c.reason == REASON_SEAL
            )
            cell_weights.append(CellWeight(
                cell=cell, status=CELL_WEIGHT_QUARANTINED,
                kg=None, claim_ids=seal_claim,
                note="封签不符已隔离，重量不参与结算",
            ))
            continue

        if cell in rulings and rulings[cell].disposition == "correct":
            ruling = rulings[cell]
            kg = ruling.weight_kg
            cell_weights.append(CellWeight(
                cell=cell, status=CELL_WEIGHT_CELL_SCALE, kg=kg,
                basis=f"ruling:{ruling.id}", claim_ids=(),
            ))
            verified_sum += kg
            continue

        rows = cell_rows.get(cell, [])
        if rows:
            nets = sorted(r.net_kg for r in rows)
            cell_specific_open = tuple(
                c.id for c in open_claims if c.scope == cell and _is_weight_claim(c)
            )
            if cell_specific_open or (len(nets) > 1 and nets[-1] - nets[0] > tolerance):
                cell_weights.append(CellWeight(
                    cell=cell, status=CELL_WEIGHT_CONTESTED, kg=None,
                    basis=",".join(r.id for r in rows),
                    claim_ids=cell_specific_open,
                    note="单舱实称冲突，待裁定",
                ))
                continue
            # 容差内互证，取到厂方实称（无则取第一条）。
            # 单舱独立有效实称优先保留：不被整车争议拖入暂缓。
            chosen = next((r for r in rows if r.source != "ORIGIN"), rows[0])
            cell_weights.append(CellWeight(
                cell=cell, status=CELL_WEIGHT_CELL_SCALE, kg=chosen.net_kg,
                basis=chosen.id, claim_ids=(),
            ))
            verified_sum += chosen.net_kg
            continue

        # 无单舱实称：先占位，分摊在总重确定后统一计算；
        # 整车未决称差直接影响这组舱的可结算重量。
        vehicle_open = tuple(c.id for c in open_claims if c.scope == VEHICLE)
        cell_weights.append(CellWeight(
            cell=cell, status=CELL_WEIGHT_APPORTIONED, kg=None,
            basis="declared_apportion", claim_ids=vehicle_open,
        ))

    # ---- 矛盾检查：已核实舱重合计不得超过共同总重 ----
    if common_total is not None and verified_sum > common_total:
        return WeightResolution(
            segment=segment,
            total_status=TOTAL_CONTRADICTION,
            common_total_kg=common_total,
            vehicle_claim_ids=vehicle_claims,
            cells=tuple(cell_weights),
            note=(
                f"已核实舱重合计 {verified_sum}kg 超过共同总重 {common_total}kg，"
                "该组凭据须重新裁定；不得削减有效实称凑总重"
            ),
        )

    # ---- 申报重量分摊 ----
    if common_total is not None:
        # 仅非隔离、无独立实称的舱参与分摊；隔离舱不参与结算重量，
        # 但其实物仍在整车总重内，先按申报比例剥离隔离舱占用的重量。
        declared_map = {c.cell: c.declared_kg for c in dispatch.compartments}
        quarantined_declared = sum(
            declared_map[c.cell] for c in dispatch.compartments
            if c.cell in quarantined_cells
        )
        apportion_cells = [
            cw for cw in cell_weights
            if cw.status == CELL_WEIGHT_APPORTIONED
            and cw.cell not in quarantined_cells
        ]
        if apportion_cells:
            apportionable_declared = sum(
                declared_map[cw.cell] for cw in apportion_cells
            )
            full_declared = quarantined_declared + apportionable_declared
            # 隔离舱按申报比例占共同总重的份额（仅用于剥离，不形成结算重量）
            quarantined_share = (
                common_total * quarantined_declared / full_declared
                if full_declared else Decimal("0")
            )
            remainder = common_total - verified_sum - quarantined_share
            base = apportionable_declared
            running = Decimal("0")
            for idx, cw in enumerate(apportion_cells):
                if idx == len(apportion_cells) - 1:
                    # 末位吸收舍入，保证合计不超共同总重
                    kg = remainder - running
                else:
                    kg = (remainder * declared_map[cw.cell] / base)
                    running += kg
                pos = next(i for i, c in enumerate(cell_weights) if c.cell == cw.cell)
                cell_weights[pos] = CellWeight(
                    cell=cw.cell, status=CELL_WEIGHT_APPORTIONED, kg=kg,
                    basis=cw.basis,
                    # 共同总重一致（含已定稿）后整车称差不再暂缓分摊舱
                    claim_ids=() if total_status == TOTAL_CONSISTENT
                    else cw.claim_ids,
                )
        # 隔离的无实称舱保持隔离状态，不参与分摊
        cell_weights = [
            cw if not (cw.status == CELL_WEIGHT_APPORTIONED
                       and cw.cell in quarantined_cells)
            else CellWeight(
                cell=cw.cell, status=CELL_WEIGHT_QUARANTINED, kg=None,
                basis=cw.basis, claim_ids=cw.claim_ids,
                note="封签不符已隔离，重量不参与结算",
            )
            for cw in cell_weights
        ]
    else:
        # 共同总重未定：分摊舱没有可据以结算的重量
        cell_weights = [
            CellWeight(
                cell=cw.cell,
                status=CELL_WEIGHT_CONTESTED if cw.status == CELL_WEIGHT_APPORTIONED
                else cw.status,
                kg=cw.kg, basis=cw.basis, claim_ids=cw.claim_ids,
                note=("共同总重待裁，申报份额不能直接当作结算重量"
                      if cw.status == CELL_WEIGHT_APPORTIONED else cw.note),
            )
            for cw in cell_weights
        ]

    return WeightResolution(
        segment=segment,
        total_status=total_status,
        common_total_kg=common_total,
        vehicle_claim_ids=vehicle_claims,
        cells=tuple(cell_weights),
        note=total_note,
    )


def cell_settlement_weight(resolution: WeightResolution, cell: str) -> Optional[Decimal]:
    """该舱当前可据以结算/领用核算的重量；待裁或隔离时为 None。"""
    cw = resolution.cell(cell)
    if cw is None or cw.status in (CELL_WEIGHT_CONTESTED, CELL_WEIGHT_QUARANTINED):
        return None
    return cw.kg
