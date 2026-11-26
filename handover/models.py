"""领域模型：各方交换凭据与服务产物。

凭据类一律不可变（frozen），服务产物（对账单、调整单、裁决）在签认/定稿
前允许更新状态。金额与重量统一使用 Decimal，保留小数，只在人民币分位汇总。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

# ---- 状态枚举（字符串常量，便于序列化与测试断言） ----

SEAL_MATCH = "match"              # 到厂封签与舱单一致
SEAL_MISMATCH = "mismatch"        # 封签不符：只能隔离
ZONE_PENDING = "pending_verify"   # 待核实区
ZONE_QUARANTINE = "quarantine"    # 隔离区

# 单舱重量的可结算状态
CELL_WEIGHT_CELL_SCALE = "cell_scale"      # 单舱独立有效实称
CELL_WEIGHT_APPORTIONED = "apportioned"    # 无单舱实称，按申报重量分摊
CELL_WEIGHT_CONTESTED = "contested"        # 实称冲突或证据不足，待裁
CELL_WEIGHT_QUARANTINED = "quarantined"    # 封签隔离，重量不参与结算

# 共同总重状态
TOTAL_CONSISTENT = "consistent"            # 装车方与到厂方整车磅一致
TOTAL_CONTESTED = "contested"              # 超容差/证据不足，待裁
TOTAL_CONTRADICTION = "contradiction"      # 已核实舱重合计超过共同总重

# 裁决处置
RULING_UPHOLD = "uphold"        # 维持原凭据
RULING_CORRECT = "correct"      # 按裁决重量/接收地更正
RULING_RELEASE = "release"      # 封签争议放行（认可隔离舱恢复正常）
RULING_REJECT = "reject"        # 封签争议拒收

OPEN = "open"
HELD = "held"                  # 款项暂缓
BILLABLE = "billable"          # 可出账
SETTLED = "settled"            # 已签认结清

KIND_UNLOAD = "unload"
KIND_ISSUE = "issue"


def parse_dt(value: str) -> datetime:
    """解析带偏移量的 ISO 8601 时间。"""
    return datetime.fromisoformat(value)


def money(value: Decimal) -> Decimal:
    """人民币金额四舍五入到分（ROUND_HALF_UP）。"""
    return value.quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Share:
    """舱内供货份额：合作社、本方批号、比例份数。"""
    coop: str
    batch: str
    parts: int


@dataclass(frozen=True)
class Compartment:
    cell: str
    seal: str
    declared_kg: Decimal
    shares: tuple[Share, ...]

    def ratio(self, coop: str) -> Decimal:
        total = Decimal(sum(s.parts for s in self.shares))
        own = Decimal(sum(s.parts for s in self.shares if s.coop == coop))
        return own / total


@dataclass(frozen=True)
class Dispatch:
    """装车方舱单：一个运输段、停靠厂点顺序与分舱。"""
    segment: str
    carrier: str
    stops: tuple[str, ...]
    compartments: tuple[Compartment, ...]

    def cell(self, cell: str) -> Optional[Compartment]:
        return next((c for c in self.compartments if c.cell == cell), None)


@dataclass(frozen=True)
class Weighing:
    """磅房回传。净重 = 总重 - 皮重，按 unit 折算为 kg。

    有 corrects 时为更正记录：保留原编号，另带更正标识；
    sent_at 为发送时间，at 为实称时间（允许补传）。
    """
    id: str
    source: str
    segment: str
    scope: str                 # "vehicle" 或舱号
    unit: str                  # kg / t
    gross: Decimal
    tare: Decimal
    at: datetime
    sent_at: Optional[datetime] = None
    corrects: Optional[str] = None

    @property
    def net_kg(self) -> Decimal:
        net = self.gross - self.tare
        return net * Decimal("1000") if self.unit == "t" else net

    @property
    def is_correction(self) -> bool:
        return self.corrects is not None


@dataclass(frozen=True)
class WarehouseEvent:
    """仓班现场事实：卸货扫描或领出。只有对应厂点的仓班能登记。"""
    id: str
    site: str
    cell: str
    kind: str                  # unload / issue
    at: datetime
    seal: Optional[str] = None
    kg: Optional[Decimal] = None
    use: Optional[str] = None


@dataclass(frozen=True)
class Claim:
    """称差或封签争议，可针对整段（vehicle）或单舱。"""
    id: str
    segment: str
    scope: str                 # vehicle / 舱号
    by: str                    # 发起方（厂点或承运方）
    reason: str                # weight（默认）/ seal
    status: str = OPEN
    ruling_id: Optional[str] = None


@dataclass(frozen=True)
class Ruling:
    """定稿裁决：双方认可处置或约定仲裁员裁决。"""
    id: str
    claim_id: str
    segment: str
    scope: str
    decided_by: str            # "mutual" 或仲裁员编号
    disposition: str           # uphold / correct / release / reject
    at: datetime
    weight_kg: Optional[Decimal] = None      # disposition=correct 时的裁定重量
    site: Optional[str] = None               # 接收地更正
    note: Optional[str] = None


@dataclass(frozen=True)
class Contract:
    goods_price: dict[str, Decimal]
    currency: str
    unverified_use_cap_kg: Decimal
    weight_tolerance_kg: Decimal
    carrier_first_unload_fee: Decimal
    carrier_other_stop_fee: Decimal
    arbiter: str
    claims: tuple[Claim, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CellWeight:
    """单舱重量解析结果。"""
    cell: str
    status: str
    kg: Optional[Decimal] = None
    basis: Optional[str] = None        # 采用的磅房记录 id / "declared_apportion"
    claim_ids: tuple[str, ...] = ()
    note: Optional[str] = None


@dataclass(frozen=True)
class WeightResolution:
    """一个运输段在当前证据下的重量全景（可随补传证据重算）。"""
    segment: str
    total_status: str
    common_total_kg: Optional[Decimal]
    vehicle_claim_ids: tuple[str, ...]
    cells: tuple[CellWeight, ...]
    note: Optional[str] = None

    def cell(self, cell: str) -> Optional[CellWeight]:
        return next((c for c in self.cells if c.cell == cell), None)


@dataclass(frozen=True)
class CellReceiving:
    """单舱交接状态：仓班据此判断能否卸、卸到哪儿、能否领料。"""
    cell: str
    site: Optional[str]
    seal_status: str
    zone: Optional[str]
    unload_kg: Optional[Decimal]
    issued_kg: Decimal
    provisional_cap_kg: Decimal        # 当前临时领用上限（合同上限与到账数量取小）
    issue_blocked: bool
    block_reason: Optional[str]
    weight_status: str
    held_claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterialLine:
    """原料款明细行：某舱某合作社批号按有效收货重量与份额计价。"""
    segment: str
    cell: str
    coop: str
    batch: str
    weight_kg: Decimal
    price_per_kg: Decimal
    amount: Decimal
    status: str                        # billable / held / settled
    claim_ids: tuple[str, ...] = ()
    basis: Optional[str] = None


@dataclass(frozen=True)
class FreightItem:
    """运费认定项：有效首卸一处、其余有效停靠每处一项。"""
    segment: str
    site: Optional[str]
    item: str                          # first_unload / other_stop
    amount: Decimal
    status: str                        # billable / held / settled
    claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Statement:
    """对账单。签认后明细冻结，更正只能另开调整单。"""
    id: str
    party: str                         # 合作社或承运方
    kind: str                          # goods / freight
    segment: str
    lines: tuple
    total: Decimal
    currency: str
    signed: bool = False


@dataclass(frozen=True)
class Adjustment:
    """重量裁决或接收地更正产生的补收/冲抵，不改动已签认原账。"""
    id: str
    segment: str
    party: str
    statement_id: str
    direction: str                     # receivable 补收 / offset 冲抵
    amount: Decimal
    reason: str
    ruling_id: str
