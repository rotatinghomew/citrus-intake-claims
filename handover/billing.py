"""分方出账：合作社原料款与承运人运费分别处理。

- 原料按有效收货重量和供货份额计价；装车申报份额不是各厂点结算重量。
- 运费按有效首卸（一处）与其余有效停靠（每处一项）分别认定。
- 同车不同争议范围独立出账，只有争议涉及的款项暂缓。
- 已签认对账单明细冻结；后续重量裁决或接收地更正另列补收/冲抵调整单，
  付款方不得因更正重付原账。
- 重量份额保留小数，价款在签认汇总后按人民币分四舍五入。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .errors import RuleViolation, ValidationError
from .models import (
    BILLABLE,
    HELD,
    Adjustment,
    CellWeight,
    Contract,
    Dispatch,
    FreightItem,
    MaterialLine,
    Statement,
    money,
)
from .receiving import quarantined_cells
from .store import Store
from .weights import TOTAL_CONTRADICTION, resolve_weights

VEHICLE = "vehicle"
REASON_SEAL = "seal"


# ---------- 暂缓判定 ----------

def _line_status(claim_ids: tuple[str, ...]) -> str:
    return HELD if claim_ids else BILLABLE


# ---------- 原料款 ----------

def material_lines(store: Store, segment: str) -> tuple[MaterialLine, ...]:
    """按当前有效重量生成原料款明细（待裁舱暂缓，不臆造金额）。

    单舱独立有效实称优先保留、可独立出账；只有受争议直接影响的舱暂缓。
    """
    dispatch: Dispatch = store.require_dispatch(segment)
    contract: Contract = store.require_contract(segment)
    quarantined = quarantined_cells(store, segment)
    resolution = resolve_weights(store, segment, quarantined)

    lines: list[MaterialLine] = []
    for comp in dispatch.compartments:
        cw: Optional[CellWeight] = resolution.cell(comp.cell)
        claim_ids = tuple(cw.claim_ids) if cw else tuple(
            c.id for c in store.open_claims(segment) if c.scope in (VEHICLE, comp.cell)
        )
        for share in comp.shares:
            if comp.cell in quarantined or cw is None or cw.kg is None:
                # 隔离/待裁舱：汇总直接影响该舱的未决争议（含封签争议）
                held_ids = tuple(
                    cl.id for cl in store.open_claims(segment)
                    if cl.scope in (VEHICLE, comp.cell)
                )
                lines.append(MaterialLine(
                    segment=segment, cell=comp.cell, coop=share.coop,
                    batch=share.batch, weight_kg=Decimal("0"),
                    price_per_kg=contract.goods_price[share.coop],
                    amount=Decimal("0.00"), status=HELD,
                    claim_ids=held_ids,
                    basis=cw.basis if cw else None,
                ))
                continue
            weight = cw.kg * comp.ratio(share.coop)
            amount = weight * contract.goods_price[share.coop]
            lines.append(MaterialLine(
                segment=segment, cell=comp.cell, coop=share.coop,
                batch=share.batch, weight_kg=weight,
                price_per_kg=contract.goods_price[share.coop],
                # 明细保留小数，分位四舍五入只在对账单汇总时做；
                # 这里保留未舍入金额以便调整单精确重算
                amount=amount, status=_line_status(claim_ids),
                claim_ids=claim_ids, basis=cw.status,
            ))
    return tuple(lines)


# ---------- 运费 ----------

def _effective_stops(store: Store, dispatch: Dispatch) -> list[str]:
    """有效停靠：按舱次顺序，有实际卸完且未被拒收的厂点，每处只计一次。"""
    ordered: list[str] = []
    for comp in dispatch.compartments:
        site = store.unload_site(dispatch.segment, comp.cell)
        if site is None:
            continue
        # 封签争议未决或最终拒收的舱不构成有效卸货
        rejected = any(
            store.ruling(c.ruling_id).disposition == "reject"
            for c in store.claims(dispatch.segment)
            if c.scope == comp.cell and c.status == "finalized"
            and c.reason == REASON_SEAL
        )
        if rejected:
            continue
        if site not in ordered:
            ordered.append(site)
    # 按舱单停靠顺序排（同一处多舱仍按一个停靠计）
    order = {s: i for i, s in enumerate(dispatch.stops)}
    return sorted(ordered, key=lambda s: order.get(s, 99))


def freight_items(store: Store, segment: str) -> tuple[FreightItem, ...]:
    """当前可开新账的运费项（已签认项保持原账，不重复出账）。"""
    settled_keys = {
        (ln.item, ln.site)
        for st in store._statements.values()
        if st.segment == segment and st.kind == "freight" and st.signed
        for ln in st.lines
    }
    return tuple(
        item for item in _all_freight_items(store, segment)
        if (item.item, item.site) not in settled_keys
    )


def _all_freight_items(store: Store, segment: str) -> tuple[FreightItem, ...]:
    """按当前证据认定的全部运费项（含已签认项目，供更正调整比对）。"""
    dispatch: Dispatch = store.require_dispatch(segment)
    contract: Contract = store.require_contract(segment)
    stops = _effective_stops(store, dispatch)
    quarantined = quarantined_cells(store, segment)
    resolution = resolve_weights(store, segment, quarantined)
    items: list[FreightItem] = []
    for idx, site in enumerate(stops):
        if idx == 0:
            item, fee = "first_unload", contract.carrier_first_unload_fee
        else:
            item, fee = "other_stop", contract.carrier_other_stop_fee
        # 该停靠涉及的未决争议：落在该站点任一舱的争议；
        # 整车称差只有在该站存在无独立实称/隔离舱时才暂缓本停靠运费，
        # 单舱独立有效实称的首卸不受整车争议牵连。
        site_cells = {
            c.cell for c in dispatch.compartments
            if store.unload_site(segment, c.cell) == site
        }
        claim_ids: list[str] = []
        for cl in store.open_claims(segment):
            if cl.scope in site_cells:
                claim_ids.append(cl.id)
            elif cl.scope == VEHICLE and any(
                (cw := resolution.cell(cell)) is None
                or cw.status != "cell_scale"
                for cell in site_cells
            ):
                claim_ids.append(cl.id)
        items.append(FreightItem(
            segment=segment, site=site, item=item, amount=fee,
            status=_line_status(tuple(claim_ids)), claim_ids=tuple(claim_ids),
        ))
    return tuple(items)


# ---------- 对账单 ----------

def build_statement(store: Store, segment: str, party: str, kind: str) -> Statement:
    """为某合作社或承运人生成只含当前可出账项的对账单。"""
    if kind not in ("goods", "freight"):
        raise ValidationError("账单类型只能是 goods 或 freight")
    quarantined = quarantined_cells(store, segment)
    resolution = resolve_weights(store, segment, quarantined)
    if resolution.total_status == TOTAL_CONTRADICTION:
        raise RuleViolation(
            "重量凭据矛盾待重裁，不能凭矛盾重量新增领用或结清相关原料款"
        )
    if kind == "freight":
        dispatch = store.require_dispatch(segment)
        if party != dispatch.carrier:
            raise ValidationError(f"{party} 不是该段承运人 {dispatch.carrier}")
        items = [i for i in freight_items(store, segment) if i.status == BILLABLE]
        lines = tuple(items)
        total = money(sum((i.amount for i in items), Decimal("0")))
    else:
        # 已签认账单中的明细行不再重开；其重量更正只通过调整单处理
        signed_cells = {
            (ln.cell, ln.coop)
            for st in store._statements.values()
            if st.segment == segment and st.kind == "goods" and st.signed
            for ln in st.lines
        }
        lines = tuple(
            ln for ln in material_lines(store, segment)
            if ln.coop == party and ln.status == BILLABLE
            and (ln.cell, ln.coop) not in signed_cells
        )
        if not lines:
            raise RuleViolation(f"{party} 当前没有可出账的原料款（相关款项暂缓或无收货）")
        total = money(sum((ln.amount for ln in lines), Decimal("0")))

    contract = store.require_contract(segment)
    statement = Statement(
        id=store.next_statement_id(), party=party, kind=kind, segment=segment,
        lines=lines, total=total, currency=contract.currency,
    )
    store.add_statement(statement)
    return statement


def sign_statement(store: Store, statement_id: str) -> Statement:
    """财务签认：明细从此冻结。"""
    return store.sign_statement(statement_id)


# ---------- 更正调整 ----------

def _material_amount_now(store: Store, segment: str, cell: str, coop: str) -> Decimal:
    for ln in material_lines(store, segment):
        if ln.cell == cell and ln.coop == coop:
            return ln.amount
    return Decimal("0")


def post_weight_adjustments(store: Store, segment: str, ruling_id: str,
                            reason_text: str) -> list[Adjustment]:
    """重量/接收地定稿后，对已签认原料账逐行生成补收或冲抵。

    原账明细不动；新重量对应金额高于原账为补收（receivable），
    低于原账为冲抵（offset）。无差额不生成。
    """
    dispatch: Dispatch = store.require_dispatch(segment)
    out: list[Adjustment] = []
    signed_goods = [
        s for s in store._statements.values()
        if s.segment == segment and s.kind == "goods" and s.signed
    ]
    for statement in signed_goods:
        for line in statement.lines:
            current = _material_amount_now(store, segment, line.cell, line.coop)
            delta = money(current - line.amount)
            if delta == 0:
                continue
            adj = Adjustment(
                id=store.next_adjustment_id(), segment=segment,
                party=statement.party, statement_id=statement.id,
                direction="receivable" if delta > 0 else "offset",
                amount=abs(delta), reason=reason_text, ruling_id=ruling_id,
            )
            store.add_adjustment(adj)
            out.append(adj)
    return out


def post_freight_adjustment(store: Store, segment: str, ruling_id: str,
                            reason_text: str) -> list[Adjustment]:
    """接收地更正导致停靠认定变化时，对已签认运费账作冲抵/补收。

    同一项目类型（首卸/其他停靠）在当前有效停靠中仍有同额认定的，视为继续
    有效、不作调整；认定消失或金额变化的部分才生成补收/冲抵。
    """
    out: list[Adjustment] = []
    current_items = _all_freight_items(store, segment)
    signed = [
        s for s in store._statements.values()
        if s.segment == segment and s.kind == "freight" and s.signed
    ]
    for statement in signed:
        for line in statement.lines:
            same_kind = [i for i in current_items if i.item == line.item]
            now = same_kind[0].amount if same_kind else Decimal("0")
            delta = money(now - line.amount)
            if delta == 0:
                continue
            adj = Adjustment(
                id=store.next_adjustment_id(), segment=segment,
                party=statement.party, statement_id=statement.id,
                direction="receivable" if delta > 0 else "offset",
                amount=abs(delta), reason=reason_text, ruling_id=ruling_id,
            )
            store.add_adjustment(adj)
            out.append(adj)
    return out


def finance_position(store: Store, segment: str) -> dict:
    """财务在生成新账前辨认：已承诺（签认）金额与仍待裁结部分。"""
    contract = store.require_contract(segment)
    signed = [
        s for s in store._statements.values()
        if s.segment == segment and s.signed
    ]
    committed = {
        "goods": money(sum((s.total for s in signed if s.kind == "goods"), Decimal("0"))),
        "freight": money(sum((s.total for s in signed if s.kind == "freight"), Decimal("0"))),
        "currency": contract.currency,
    }
    pending_goods = [ln for ln in material_lines(store, segment) if ln.status == HELD]
    pending_freight = [i for i in freight_items(store, segment) if i.status == HELD]
    return {
        "segment": segment,
        "committed": committed,
        "signed_statements": tuple(s.id for s in signed),
        "pending_goods": tuple(pending_goods),
        "pending_freight": tuple(pending_freight),
        "open_claims": store.claims(segment),
        "adjustments": tuple(
            a for a in store._adjustments if a.segment == segment
        ),
    }
