"""按角色裁剪的权限视图：各方只能看见自己有权的证据与金额。

角色前缀：
  gate/<厂点>      门卫：本点舱位、封签扫描、交接区状态
  site/<厂点>      仓班：本厂点交接、领用上限、本点称重与争议
  coop/<合作社>    合作社：本社批号、份额、本社原料款
  carrier/<承运方> 承运方：运输段、运费、整车与本舱称重、争议
  finance          财务：账单、调整、暂缓与已承诺金额（不看封签原图等现场凭据）
  coordinator      协调员：把收货、领用、待裁议题和账单联系到同一运输段
  arbiter/<编号>   仲裁员：争议相关的全部证据
"""
from __future__ import annotations

from .billing import (
    finance_position,
    freight_items,
    material_lines,
)
from .models import KIND_ISSUE
from .receiving import quarantined_cells, receiving_overview, seal_checks
from .store import Store
from .weights import resolve_weights


class Actor:
    def __init__(self, raw: str):
        parts = raw.split("/", 1)
        self.kind = parts[0]
        self.ref = parts[1] if len(parts) > 1 else None

    def __repr__(self) -> str:
        return f"Actor({self.kind!r}, {self.ref!r})"


def _require_segment(store: Store, segment: str):
    return store.require_dispatch(segment)


def role_view(store: Store, actor_raw: str, segment: str) -> dict:
    actor = Actor(actor_raw)
    dispatch = _require_segment(store, segment)
    kind, ref = actor.kind, actor.ref

    if kind == "gate":
        if ref not in dispatch.stops:
            return {"error": f"{ref} 不负责该运输段的厂点"}
        checks = seal_checks(store, segment)
        overview = {r.cell: r for r in receiving_overview(store, segment)}
        cells = []
        for comp in dispatch.compartments:
            site = store.unload_site(segment, comp.cell)
            if site == ref or site is None:
                cells.append({
                    "cell": comp.cell, "seal_expected": comp.seal,
                    "seal_scan": checks[comp.cell].scanned,
                    "zone": overview[comp.cell].zone,
                })
        return {"role": "gate", "site": ref, "segment": segment, "cells": cells}

    if kind == "site":
        if ref not in dispatch.stops:
            return {"error": f"{ref} 不负责该运输段的厂点"}
        events = [
            e for e in store.events_for(segment)
            if e.site == ref
        ]
        overview = [
            r for r in receiving_overview(store, segment)
            if r.site == ref or (r.site is None and ref == dispatch.stops[0])
        ]
        weights = [
            w for w in store.effective_weighings(segment)
            if w.source == ref or (w.scope != "vehicle" and store.unload_site(segment, w.scope) == ref)
        ]
        return {
            "role": "site", "site": ref, "segment": segment,
            "receiving": overview,
            "events": events,
            "weighings": weights,
            "claims": [c for c in store.claims(segment)
                       if c.by == ref or c.scope == "vehicle"],
        }

    if kind == "coop":
        own_shares = []
        for comp in dispatch.compartments:
            for sh in comp.shares:
                if sh.coop == ref:
                    own_shares.append({
                        "cell": comp.cell, "batch": sh.batch,
                        "parts": sh.parts, "ratio": str(comp.ratio(sh.coop)),
                        "declared_kg": str(comp.declared_kg),
                    })
        if not own_shares:
            return {"error": f"{ref} 在该运输段没有供货份额"}
        return {
            "role": "coop", "coop": ref, "segment": segment,
            "shares": own_shares,
            "material": [ln for ln in material_lines(store, segment)
                         if ln.coop == ref],
            "statements": store.statements_for(ref, segment),
            "adjustments": store.adjustments_for(ref, segment),
            "claims": store.claims(segment),
        }

    if kind == "carrier":
        if ref != dispatch.carrier:
            return {"error": f"{ref} 不是该运输段承运人 {dispatch.carrier}"}
        return {
            "role": "carrier", "carrier": ref, "segment": segment,
            "stops": dispatch.stops,
            "weighings": store.effective_weighings(segment),
            "freight": freight_items(store, segment),
            "statements": store.statements_for(ref, segment),
            "adjustments": store.adjustments_for(ref, segment),
            "claims": store.claims(segment),
        }

    if kind == "finance":
        return {"role": "finance", **finance_position(store, segment)}

    if kind == "coordinator":
        overview = receiving_overview(store, segment)
        quarantined = quarantined_cells(store, segment)
        resolution = resolve_weights(store, segment, quarantined)
        return {
            "role": "coordinator", "segment": segment,
            "carrier": dispatch.carrier, "stops": dispatch.stops,
            "weight_resolution": resolution,
            "receiving": overview,
            "issues": [e for e in store.events_for(segment) if e.kind == KIND_ISSUE],
            "claims": store.claims(segment),
            "rulings": [store.ruling(c.ruling_id)
                        for c in store.claims(segment) if c.ruling_id],
            "material": material_lines(store, segment),
            "freight": freight_items(store, segment),
            "statements": [s for s in store._statements.values()
                           if s.segment == segment],
            "adjustments": [a for a in store._adjustments
                            if a.segment == segment],
            "issue_bans": [
                {"cell": cell, "reason": reason}
                for (seg, cell), reason in store.issue_bans.items()
                if seg == segment
            ],
        }

    if kind == "arbiter":
        contract = store.require_contract(segment)
        if ref != contract.arbiter:
            return {"error": f"{ref} 不是约定仲裁员 {contract.arbiter}"}
        quarantined = quarantined_cells(store, segment)
        return {
            "role": "arbiter", "arbiter": ref, "segment": segment,
            "claims": store.claims(segment),
            "rulings": [store.ruling(c.ruling_id)
                        for c in store.claims(segment) if c.ruling_id],
            "weighings": store.effective_weighings(segment),
            "events": store.events_for(segment),
            "seal_checks": seal_checks(store, segment),
            "weight_resolution": resolve_weights(store, segment, quarantined),
        }

    return {"error": f"未知角色 {kind}"}
