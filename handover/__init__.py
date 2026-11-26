"""果皮分舱进厂交接服务。

模块划分：

- models    不可变的交换凭据与服务产物（舱单、磅房、仓班、对账单等）
- store     按业务方来源登记凭据，保留更正链与全部历史事实
- weights   共同总重、单舱有效实称与申报重量分摊
- receiving 交接地点顺序、封签核验、待核实区、临时领用上限与领用禁令
- disputes  争议登记、双方认可处置与仲裁员裁决、定稿与暂缓
- billing   合作社原料款、承运人运费、签认冻结与更正补收/冲抵
- views     门卫、仓班、合作社、承运方、财务、协调员、仲裁员的权限视图
- service   统一门面，串起全部用例
"""
from .service import HandoverService

__all__ = ["HandoverService"]
