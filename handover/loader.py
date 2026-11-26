"""把《磅房交换格式》的 JSON 交换材料装载进交接服务。

每个文件代表一个独立业务方的记录，装载时按来源做权限校验，
与真实的分方提交路径一致；下载来的交换材料视为不可信数据，
调用方应把输入目录与代码目录隔离，并用 python3 -I 运行。
"""
from __future__ import annotations

import json
from pathlib import Path

from .service import HandoverService


def load_dir(path: str | Path) -> HandoverService:
    root = Path(path)
    service = HandoverService()

    dispatch = json.loads((root / "dispatch.json").read_text(encoding="utf-8"))
    segment = dispatch["segment"]
    service.register_dispatch(dispatch)

    contracts = json.loads((root / "contracts.json").read_text(encoding="utf-8"))
    service.register_contract(segment, contracts)

    for row in json.loads((root / "weighing.json").read_text(encoding="utf-8")):
        actor = "ORIGIN" if row["source"] == "ORIGIN" else f"site/{row['source']}"
        service.submit_weighing(actor, row)

    for row in json.loads((root / "warehouse.json").read_text(encoding="utf-8")):
        row = {"segment": segment, **row}
        if row["kind"] == "unload":
            service.submit_unload(f"site/{row['site']}", row)
        else:
            service.submit_issue(f"site/{row['site']}", row)

    # 既有签认账单在卸货事实之后登记，才能关联到有效首卸站点
    if contracts.get("carrier_legacy_signoff"):
        service.import_legacy_signoff(segment, contracts["carrier_legacy_signoff"])

    return service
