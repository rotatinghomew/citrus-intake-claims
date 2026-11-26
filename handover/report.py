"""命令行报告：查看某运输段此刻的交接、重量、争议与出账状态。

用法：
    python3 -m handover.report inputs
    python3 -m handover.report inputs --actor coordinator
    python3 -m handover.report inputs --actor site/FAC-A
"""
from __future__ import annotations

import argparse
import sys

from .loader import load_dir


def _print_weight(view: dict) -> None:
    wr = view["weight_resolution"]
    print(f"[重量] 共同总重状态={wr.total_status} 共同总重={wr.common_total_kg}kg")
    if wr.note:
        print(f"       {wr.note}")
    for cw in wr.cells:
        kg = "待裁" if cw.kg is None else f"{cw.kg}kg"
        claims = f" 争议={','.join(cw.claim_ids)}" if cw.claim_ids else ""
        print(f"  舱 {cw.cell}: {cw.status:<12} {kg}{claims}")


def _print_receiving(view: dict) -> None:
    print("[交接/领用]")
    for row in view["receiving"]:
        site = row.site or "未到厂"
        print(
            f"  舱 {row.cell} @{site} 封签={row.seal_status} 区域={row.zone or '-'} "
            f"到账={row.unload_kg} 已领={row.issued_kg}/{row.provisional_cap_kg} "
            f"可领料={not row.issue_blocked}"
        )
        if row.block_reason:
            print(f"      └ {row.block_reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="进厂交接状态报告")
    parser.add_argument("input_dir", help="交换 JSON 所在目录")
    parser.add_argument("--segment", help="运输段编号（默认取舱单中的唯一段）")
    parser.add_argument("--actor", default="coordinator",
                        help="角色视图，如 coordinator、site/FAC-A、coop/COOP-F")
    args = parser.parse_args(argv)

    service = load_dir(args.input_dir)
    segment = args.segment or next(iter(service.store.dispatches))
    view = service.view_for(args.actor, segment)
    if "error" in view:
        print(view["error"], file=sys.stderr)
        return 2

    print(f"运输段 {segment} 角色视图={args.actor}")
    if view["role"] == "coordinator":
        _print_weight(view)
        _print_receiving(view)
        print("[待裁议题]")
        for claim in view["claims"]:
            print(f"  {claim.id} scope={claim.scope} reason={claim.reason} "
                  f"by={claim.by} status={claim.status}")
        print("[原料款]")
        for ln in view["material"]:
            amt = f"{ln.amount:.4f}" if ln.status == "billable" else "暂缓"
            print(f"  {ln.coop} {ln.cell}/{ln.batch} 重量={ln.weight_kg} "
                  f"状态={ln.status} 金额={amt}")
        print("[运费]")
        for it in view["freight"]:
            print(f"  {it.item} @{it.site} {it.amount} 状态={it.status}")
        print("[对账单/调整]")
        for st in view["statements"]:
            print(f"  账单 {st.id} {st.party} {st.kind} 合计={st.total} 签认={st.signed}")
        for adj in view["adjustments"]:
            print(f"  调整 {adj.id} {adj.party} {adj.direction} {adj.amount} "
                  f"依据={adj.ruling_id}")
    else:
        for key, value in view.items():
            if key == "role":
                continue
            print(f"[{key}] {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
