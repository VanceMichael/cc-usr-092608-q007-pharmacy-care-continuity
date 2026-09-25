# 药店健康管理与用药连续服务后端

支持连锁药店从一次性售药转向慢病与全周期健康管理：跨门店健康计划续接、
用药依从性随访、购药安全决策、批次召回闭环、应急保供、库存全程对账、
断网补录与待联系顾客协调。纯 Python 3.11 标准库实现，无外部依赖。

## 一、能力总览

| 需求 | 实现位置 |
| --- | --- |
| 顾客选择服务计划，分别授权健康记录/提醒/跨店续接 | `services/enrollment.py`、`models.Scope` |
| 负责药师资质、医嘱摘要、用药计划版本、测量观察、随访结论、转诊、不良反应 | `services/records.py`、`services/registry.py` |
| 普通店员只接触领取与配送，健康访问按角色+授权双重判定并留痕 | `services/access.py` |
| 跨店咨询先找当前有效计划与最近安全结论 | `PharmacyBackend.cross_store_consult` |
| 高风险调整由另一名有权限人员复核后才生效 | `RecordService.propose_change/review_change` |
| 终止服务后新健康访问立即停止，历史交易与安全处置可依法追溯 | `EnrollmentService.terminate` |
| 提前领取/疗程重叠/替代品牌/召回批次 → 允许/阻断/转人工并给出原因 | `services/dispense.py` |
| 预留→领取→退回→报损→调拨全程对账，应急保供不绕过批次风险 | `services/inventory.py` |
| 断网补录保留真实发生时间，晚到信息只影响之后的建议 | `services/timeline.py`、`timeutil.py` |
| 到期随访、批次召回、门店调拨协调，重启不丢待联系顾客 | `services/contacts.py`、`repository.py` 快照 |
| 顾客看当前方案与下一步；药师可溯源每次建议的授权/药品/观察/执业责任 | `services/advice.py` |

## 二、模块结构

```
src/
  models.py          枚举与实体（dataclass），含全部原因码与状态机
  errors.py          DomainError 及授权/资质/库存/批次风险子类
  timeutil.py        业务时间与入库时间解析；set_now 供断网恢复测试
  repository.py      内存集合 + JSON 原子快照（24 个集合统一持久化）
  backend.py         PharmacyBackend 门面，装配全部服务与跨店咨询入口
  services/
    registry.py      门店/人员/顾客/计划/药品/品规/批次/召回档案
    access.py        角色边界、分项授权、药师资质、访问日志
    enrollment.py    入组、逐项授权变更、终止（即停新访问、保留可追溯）
    records.py       医嘱、用药计划版本链、观察、随访/安全结论、转诊、ADR、双人复核
    inventory.py     批次台账、预留锁定、领取/退回/报损、调拨、召回红线
    dispense.py      购药规则引擎、店员最小知情、销售执行、药师原因展开
    contacts.py      待联系顾客统一队列（去重合并、扫描、召回处置）
    timeline.py      断网补录、晚到边界、顾客时间线
    advice.py        建议证据快照与溯源、顾客当前方案视图
```

## 三、关键规则

### 授权与角色

- 三项授权相互独立：`health_records`、`reminders`、`cross_store`，可逐项授予/撤回。
- 药师访问健康信息须同时满足：角色为执业药师、资质编号有效且未过期、顾客对应授权有效。
- 普通店员仅可办理领取/配送（`assert_fulfillment`），任何健康 scope 直接拒绝；
  运营人员两者皆不可。店员可**代报**不良反应（只写不读档案）。
- 每次授权判定（允许与拒绝）都写 `access_logs`，含原因。
- 终止服务写入终止时间并撤回全部授权：终止之后的新健康访问立即失败；
  既有交易、计划版本、随访、ADR 处置等记录不删除，按历史时间点仍可回放。

### 高风险调整双人复核

药师 `propose_change` 提出调整；若命中计划配置的高风险药品自动升级为 `high`。
高风险调整必须由**另一名** `safety_reviewer` 复核：通过才发布用药计划新版本，
驳回不产生新版本；提出人不能复核自己的调整。

### 购药决策（明确原因）

- `allow`：可由店员直接执行（预留即领，FEFO 先到期先出，自动跳过召回批次）。
- `block`：召回批次（最高优先，应急保供也不绕过）、严重提前领取、疗程重叠、库存不足。
- `manual`：替代品牌、轻度提前领取、跨店未授权、高风险调整待复核、
  无有效计划/身份无法确认。
- 应急保供仅把提前领取、疗程重叠两类临床阻断降级为转人工；召回始终阻断。
- 店员只看到 `clerk_message`（办理/停发/联系药师）；药师经再次授权校验后
  通过 `pharmacist_explanation` 查看完整依据。每次请求写 `attempts` 留痕。

### 库存与批次

- 实物库存由出入库流水带符号求和；锁定库存为有效预留；可售=实物−锁定。
- 召回批次可售恒为 0；预留、领取（二次核对）、调拨出库与到货上架四处拦截；
  在途批次若运输期间被召回，到货隔离不得入库。
- 断网补录已既成销售使用 `historical=True` 通道登记，允许账实相符，
  并对“召回生效后售出”打 `sold_after_recall_effective` 标志供追溯。

### 时间、补录与协调

- 每条业务记录成对保存 `occurred_at`（真实发生）与 `recorded_at`（入库）。
- “截至 T 可见”要求两者都不晚于 T：晚到信息不会改变既往建议，只影响之后的建议。
- 待联系任务按顾客合并多原因（随访到期、召回通知、ADR 回访、调拨到货、预留到期），
  随仓储快照持久化；重启后开放任务不丢失。撤回提醒授权或终止服务只取消提醒类任务，
  召回与 ADR 等安全义务任务保留。

## 四、开发命令

运行测试（44 个用例）：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src tests
```

## 五、最小用法

```python
from src.backend import PharmacyBackend
from src.models import Scope

b = PharmacyBackend("data/state.json")          # 自动从快照恢复
b.registry.register_store("S1", "一店")
b.registry.register_staff("P1", "甲药师", "pharmacist", "ZY-1001", "2030-12-31...")
b.registry.register_customer("C1", "顾客*明")
b.registry.register_plan("CHRONIC", "慢病陪伴计划", high_risk_drugs=("DRUG_WARF",))
b.enrollment.enroll(
    "C1", "CHRONIC", "S1",
    {Scope.HEALTH_RECORDS: True, Scope.REMINDERS: True, Scope.CROSS_STORE: True},
    responsible_pharmacist_id="P1",
)
# 跨店咨询：当前有效计划 + 最近安全结论
packet = b.cross_store_consult("C1", "P2", "S2")
# 购药：允许/阻断/转人工
decision = b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK")
if decision["decision"] == "allow":
    b.dispense.execute(decision, "CK")
b.save()                                        # 原子快照
```

示例世界（脱敏）与端到端场景见 `tests/world.py` 与 `tests/test_end_to_end.py`。
