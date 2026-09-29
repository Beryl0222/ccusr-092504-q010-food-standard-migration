"""标准迁移与批次处置的领域模型。

状态对象由事件投影重建，命令侧只通过事件改变状态，保证中断后可重放恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ClauseKind(str, Enum):
    """标准条款类别，三类分别判断。"""

    PROCESS = "process"  # 过程要求
    LIMIT = "limit"  # 产品限量
    METHOD = "method"  # 检测方法


class BatchStatus(str, Enum):
    PENDING = "pending"  # 待审核
    HOLD = "hold"  # 待放行（证据不足或待处置）
    RELEASED = "released"  # 已放行
    REJECTED = "rejected"  # 判定不合格
    SHIPPED = "shipped"  # 已出库
    RECALLED = "recalled"  # 召回中
    FROZEN = "frozen"  # 冻结（报告编号冲突）


class AssociationStatus(str, Enum):
    SUGGESTED = "suggested"  # 系统自动建议，待合规确认
    CONFIRMED = "confirmed"  # 合规人员已确认
    REJECTED = "rejected"  # 合规人员已驳回


class ActionKind(str, Enum):
    SAMPLING = "sampling"  # 取样
    APPROVAL = "approval"  # 审批
    NOTIFICATION = "notification"  # 通知


class ActionState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class ReportStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"  # 编号与内容均相同，幂等丢弃
    CONFLICTED = "conflicted"  # 编号相同而内容不同，冻结引用批次


#: 可关联到批次的证据类型
EVIDENCE_TYPES = frozenset(
    {
        "formula_version",  # 配方版本
        "material_lot",  # 供应商原料批次
        "process_record",  # 加工温湿度等过程记录
        "packaging_material",  # 包装接触材料
        "label_draft",  # 标签稿
        "sampling_plan",  # 抽样计划
        "lab_method",  # 实验室方法
        "lab_result",  # 实验室结果
        "sample_record",  # 取样执行记录
    }
)


class DomainError(Exception):
    """领域规则冲突。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


class ApprovalError(DomainError):
    """审批独立性或状态不满足。"""


class ContractError(DomainError):
    """事件未通过交换契约校验。"""

    def __init__(self, issues: list[Any]) -> None:
        self.issues = issues
        detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
        super().__init__(f"事件契约校验失败: {detail}")


class ActionInterrupted(Exception):
    """动作执行途中中断（如进程崩溃）。

    不产生 FAILED 事件，动作保持 IN_PROGRESS，恢复后继续执行。
    处理器应当幂等，重试不会产生重复副作用。
    """


@dataclass
class ClauseState:
    clause_id: str
    standard_code: str
    clause_no: str
    kind: ClauseKind
    published_at: datetime
    effective_at: datetime
    supersedes: str | None
    categories: frozenset[str]
    min_age_months: int | None
    max_age_months: int | None
    nutrients: frozenset[str]
    parameters: dict[str, Any]
    superseded_by: str | None = None


@dataclass
class ProductState:
    product_id: str
    name: str
    category: str
    min_age_months: int | None
    max_age_months: int | None
    nutrients: frozenset[str]


@dataclass
class LotState:
    lot_id: str
    supplier_id: str
    material_code: str
    received_at: datetime
    failed: bool = False
    fail_reason: str = ""


@dataclass
class EvidenceItem:
    evidence_id: str
    evidence_type: str
    data: dict[str, Any]
    linked_at: datetime
    actor: str


@dataclass
class BatchState:
    batch_id: str
    product_id: str
    produced_at: datetime
    market_at: datetime
    formula_version: str
    status: BatchStatus = BatchStatus.PENDING
    shipped_at: datetime | None = None
    material_lots: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    recall_restore: BatchStatus | None = None  # 召回前状态，缩小范围时恢复

    def evidence_of(self, evidence_type: str) -> list[EvidenceItem]:
        return [item for item in self.evidence if item.evidence_type == evidence_type]


@dataclass
class ReportState:
    report_no: str
    fingerprint: str
    batch_ids: list[str]
    status: ReportStatus
    results: list[dict[str, Any]]
    referenced_batch_ids: set[str] = field(default_factory=set)  # 所有版本的引用并集


@dataclass
class RecallState:
    recall_id: str
    reason: str
    scope: set[str]
    created_by: str
    status: str = "active"
    narrowings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ActionRecord:
    action_id: str
    kind: ActionKind
    subject_id: str
    payload: dict[str, Any]
    state: ActionState = ActionState.PENDING
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str = ""
