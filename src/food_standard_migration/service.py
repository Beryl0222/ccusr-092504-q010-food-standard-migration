"""标准迁移与批次处置领域服务。

事件溯源：所有状态变化先落成领域事件（经交换契约校验后追加到事件日志），
再由投影更新内存状态。服务中断后用同一日志重放即可恢复，未完成的取样、
审批、通知动作在恢复后继续执行。

规则选取：过程要求按生产时点、产品限量与检测方法按上市时点，三类分别判断。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .contracts import validate_event
from .models import (
    EVIDENCE_TYPES,
    ActionInterrupted,
    ActionKind,
    ActionRecord,
    ActionState,
    ApprovalError,
    AssociationStatus,
    BatchState,
    BatchStatus,
    ClauseKind,
    ClauseState,
    ContractError,
    DomainError,
    EvidenceItem,
    LotState,
    NotFoundError,
    ProductState,
    RecallState,
    ReportState,
    ReportStatus,
    StateError,
)
from .store import EventLog

Clock = Callable[[], datetime]
ActionHandler = Callable[["StandardMigrationService", ActionRecord, datetime], dict[str, Any]]

#: 严重度排序，用于合并多条条款的判定结果
_SEVERITY = {"pass": 0, "insufficient": 1, "fail": 2}


def load_default_schema() -> dict[str, Any]:
    """加载仓库内的领域事件契约。"""
    path = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_dt(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"无法解析时间: {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return parsed


def _iso(value: datetime) -> str:
    return value.isoformat()


def _fingerprint(content: Mapping[str, Any]) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ranges_overlap(
    a_min: int | None, a_max: int | None, b_min: int | None, b_max: int | None
) -> bool:
    low = max((v for v in (a_min, b_min) if v is not None), default=None)
    high = min((v for v in (a_max, b_max) if v is not None), default=None)
    return low is None or high is None or low <= high


class StandardMigrationService:
    """标准迁移与批次处置的完整领域服务。"""

    def __init__(
        self,
        schema: Mapping[str, Any] | None = None,
        *,
        log_path: str | Path | None = None,
        clock: Clock | None = None,
        action_handlers: Mapping[str, ActionHandler] | None = None,
    ) -> None:
        self._schema = schema if schema is not None else load_default_schema()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._log = EventLog(log_path)
        self._handlers: dict[str, ActionHandler] = dict(action_handlers or {})
        self._seq = 0
        self._versions: dict[tuple[str, str], int] = {}
        self._decision_seq = 0

        self.clauses: dict[str, ClauseState] = {}
        self.products: dict[str, ProductState] = {}
        self.lots: dict[str, LotState] = {}
        self.batches: dict[str, BatchState] = {}
        self.reports: dict[str, ReportState] = {}
        self.recalls: dict[str, RecallState] = {}
        self.actions: dict[str, ActionRecord] = {}
        self.associations: dict[tuple[str, str], AssociationStatus] = {}
        self.association_log: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []

        for event in self._log:
            self._replay(event)

    # ------------------------------------------------------------------
    # 事件基础：发射、校验、投影、重放
    # ------------------------------------------------------------------

    def _now(self, at: datetime | str | None) -> datetime:
        return _parse_dt(at) if at is not None else self._clock()

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        *,
        summary: str,
        at: datetime,
        **payload: Any,
    ) -> dict[str, Any]:
        key = (aggregate_type, aggregate_id)
        version = self._versions.get(key, 0) + 1
        event = {
            "event_id": f"evt-{self._seq + 1:08d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": _iso(at),
            "version": version,
            "summary": summary,
            **payload,
        }
        issues = validate_event(event, self._schema)
        if issues:
            raise ContractError(issues)
        self._seq += 1
        self._versions[key] = version
        self._log.append(event)
        self._apply(event)
        return event

    def _replay(self, event: dict[str, Any]) -> None:
        self._seq += 1
        key = (event["aggregate_type"], event["aggregate_id"])
        self._versions[key] = max(self._versions.get(key, 0), event["version"])
        self._apply(event)

    def _apply(self, event: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{event['event_type'].lower()}", None)
        if handler is not None:
            handler(event)

    # ------------------------------------------------------------------
    # 标准条款登记：发布、生效、替代、适用产品
    # ------------------------------------------------------------------

    def register_clause(
        self,
        clause_id: str,
        standard_code: str,
        clause_no: str,
        kind: str | ClauseKind,
        published_at: datetime | str,
        effective_at: datetime | str,
        *,
        supersedes: str | None = None,
        categories: Iterable[str] = (),
        min_age_months: int | None = None,
        max_age_months: int | None = None,
        nutrients: Iterable[str] = (),
        parameters: Mapping[str, Any] | None = None,
        actor: str,
        at: datetime | str | None = None,
    ) -> ClauseState:
        """登记标准条款；如替代旧条款则同时作废旧版，并为匹配产品生成待确认关联。"""
        at_dt = self._now(at)
        kind = ClauseKind(kind)
        published = _parse_dt(published_at)
        effective = _parse_dt(effective_at)
        if effective < published:
            raise DomainError("生效时间不得早于发布时间")
        if clause_id in self.clauses:
            raise StateError(f"条款已存在: {clause_id}")
        if supersedes is not None:
            old = self.clauses.get(supersedes)
            if old is None:
                raise NotFoundError(f"被替代的条款不存在: {supersedes}")
            if old.kind != kind:
                raise DomainError("替代条款与被替代条款类别必须一致")
            if old.superseded_by is not None:
                raise StateError(f"条款 {supersedes} 已被 {old.superseded_by} 替代")

        self._emit(
            "STANDARD_REGISTERED",
            "standard_revision",
            clause_id,
            summary=f"登记标准条款 {standard_code} {clause_no}（{kind.value}）",
            at=at_dt,
            clause_id=clause_id,
            standard_code=standard_code,
            clause_no=clause_no,
            kind=kind.value,
            published_at=_iso(published),
            effective_at=_iso(effective),
            supersedes=supersedes,
            categories=sorted(categories),
            min_age_months=min_age_months,
            max_age_months=max_age_months,
            nutrients=sorted(nutrients),
            parameters=dict(parameters or {}),
            actor=actor,
        )
        if supersedes is not None:
            self._emit(
                "STANDARD_SUPERSEDED",
                "standard_revision",
                supersedes,
                summary=f"条款 {supersedes} 被 {clause_id} 替代",
                at=at_dt,
                clause_id=supersedes,
                replaced_by=clause_id,
            )
        clause = self.clauses[clause_id]
        for product in self.products.values():
            if self._matches(clause, product):
                self._suggest_association(clause, product, at_dt)
        return clause

    @staticmethod
    def _matches(clause: ClauseState, product: ProductState) -> bool:
        """儿童适用年龄或营养素上限变化只影响匹配产品。"""
        if clause.categories and product.category not in clause.categories:
            return False
        if clause.min_age_months is not None or clause.max_age_months is not None:
            if not _ranges_overlap(
                clause.min_age_months, clause.max_age_months,
                product.min_age_months, product.max_age_months,
            ):
                return False
        if clause.nutrients and not (set(clause.nutrients) & set(product.nutrients)):
            return False
        return True

    def _suggest_association(self, clause: ClauseState, product: ProductState, at: datetime) -> None:
        key = (clause.clause_id, product.product_id)
        if self.associations.get(key) == AssociationStatus.CONFIRMED:
            return
        self._emit(
            "ASSOCIATION_SUGGESTED",
            "product",
            product.product_id,
            summary=f"条款 {clause.clause_id} 自动关联到产品 {product.product_id}，待合规确认",
            at=at,
            clause_id=clause.clause_id,
            product_id=product.product_id,
        )

    def confirm_association(
        self, clause_id: str, product_id: str, *, actor: str, at: datetime | str | None = None
    ) -> None:
        """合规人员确认自动关联；确认后该条款才参与评判，并重审该产品全部批次。"""
        at_dt = self._now(at)
        if clause_id not in self.clauses:
            raise NotFoundError(f"条款不存在: {clause_id}")
        if product_id not in self.products:
            raise NotFoundError(f"产品不存在: {product_id}")
        if self.associations.get((clause_id, product_id)) == AssociationStatus.CONFIRMED:
            return
        self._emit(
            "ASSOCIATION_CONFIRMED",
            "product",
            product_id,
            summary=f"合规确认条款 {clause_id} 适用于产品 {product_id}",
            at=at_dt,
            clause_id=clause_id,
            product_id=product_id,
            actor=actor,
        )
        for batch in self.batches.values():
            if batch.product_id == product_id:
                self.evaluate_batch(batch.batch_id, actor="system:association-confirmed", at=at_dt)

    def reject_association(
        self, clause_id: str, product_id: str, *, actor: str, at: datetime | str | None = None
    ) -> None:
        at_dt = self._now(at)
        key = (clause_id, product_id)
        if self.associations.get(key) != AssociationStatus.SUGGESTED:
            raise StateError("只有待确认的关联可以驳回")
        self._emit(
            "ASSOCIATION_REJECTED",
            "product",
            product_id,
            summary=f"合规驳回条款 {clause_id} 与产品 {product_id} 的关联",
            at=at_dt,
            clause_id=clause_id,
            product_id=product_id,
            actor=actor,
        )

    # ------------------------------------------------------------------
    # 产品、原料批次、产品批次登记
    # ------------------------------------------------------------------

    def register_product(
        self,
        product_id: str,
        name: str,
        category: str,
        *,
        min_age_months: int | None = None,
        max_age_months: int | None = None,
        nutrients: Iterable[str] = (),
        actor: str,
        at: datetime | str | None = None,
    ) -> ProductState:
        at_dt = self._now(at)
        if product_id in self.products:
            raise StateError(f"产品已存在: {product_id}")
        self._emit(
            "PRODUCT_REGISTERED",
            "product",
            product_id,
            summary=f"登记产品 {name}（{category}）",
            at=at_dt,
            product_id=product_id,
            name=name,
            category=category,
            min_age_months=min_age_months,
            max_age_months=max_age_months,
            nutrients=sorted(nutrients),
            actor=actor,
        )
        product = self.products[product_id]
        for clause in self.clauses.values():
            if self._matches(clause, product):
                self._suggest_association(clause, product, at_dt)
        return product

    def receive_lot(
        self,
        lot_id: str,
        supplier_id: str,
        material_code: str,
        *,
        actor: str,
        at: datetime | str | None = None,
    ) -> LotState:
        at_dt = self._now(at)
        if lot_id in self.lots:
            raise StateError(f"原料批次已存在: {lot_id}")
        self._emit(
            "LOT_RECEIVED",
            "material_lot",
            lot_id,
            summary=f"接收供应商 {supplier_id} 原料 {material_code} 批次 {lot_id}",
            at=at_dt,
            lot_id=lot_id,
            supplier_id=supplier_id,
            material_code=material_code,
            actor=actor,
        )
        return self.lots[lot_id]

    def fail_lot(
        self, lot_id: str, reason: str, *, actor: str, at: datetime | str | None = None
    ) -> list[str]:
        """原料批次不合格：只沿物料谱系影响相关库存。

        在库批次转待放行；已出库批次保留当时决定、追加处置记录；其余批次不受影响。
        """
        at_dt = self._now(at)
        lot = self.lots.get(lot_id)
        if lot is None:
            raise NotFoundError(f"原料批次不存在: {lot_id}")
        if lot.failed:
            raise StateError(f"原料批次已判定不合格: {lot_id}")
        self._emit(
            "LOT_FAILED",
            "material_lot",
            lot_id,
            summary=f"原料批次 {lot_id} 判定不合格：{reason}",
            at=at_dt,
            lot_id=lot_id,
            reason=reason,
            actor=actor,
        )
        affected: list[str] = []
        for batch in self.batches.values():
            if lot_id not in batch.material_lots:
                continue
            affected.append(batch.batch_id)
            if batch.status in (BatchStatus.RECALLED, BatchStatus.FROZEN, BatchStatus.REJECTED):
                continue
            if batch.status == BatchStatus.SHIPPED:
                self._record_disposition(
                    batch, BatchStatus.SHIPPED,
                    f"原料批次 {lot_id} 不合格，追加处置：评估召回", actor, at_dt,
                )
            else:
                self._record_disposition(
                    batch, BatchStatus.HOLD,
                    f"原料批次 {lot_id} 不合格，转待放行", actor, at_dt,
                )
        return affected

    def register_batch(
        self,
        batch_id: str,
        product_id: str,
        produced_at: datetime | str,
        market_at: datetime | str,
        formula_version: str,
        *,
        actor: str,
        at: datetime | str | None = None,
    ) -> BatchState:
        """登记产品批次并安排取样动作，随后做首次审核。"""
        at_dt = self._now(at)
        if product_id not in self.products:
            raise NotFoundError(f"产品不存在: {product_id}")
        if batch_id in self.batches:
            raise StateError(f"批次已存在: {batch_id}")
        produced = _parse_dt(produced_at)
        market = _parse_dt(market_at)
        if market < produced:
            raise DomainError("上市时点不得早于生产时点")
        self._emit(
            "BATCH_REGISTERED",
            "product_batch",
            batch_id,
            summary=f"登记产品批次 {batch_id}（产品 {product_id}，配方 {formula_version}）",
            at=at_dt,
            batch_id=batch_id,
            product_id=product_id,
            produced_at=_iso(produced),
            market_at=_iso(market),
            formula_version=formula_version,
            actor=actor,
        )
        self._schedule_action(ActionKind.SAMPLING, batch_id, {"batch_id": batch_id}, at_dt)
        self.evaluate_batch(batch_id, actor="system:initial-review", at=at_dt)
        return self.batches[batch_id]

    # ------------------------------------------------------------------
    # 证据关联与批次评判
    # ------------------------------------------------------------------

    def link_evidence(
        self,
        batch_id: str,
        evidence_type: str,
        evidence_id: str,
        data: Mapping[str, Any],
        *,
        actor: str,
        at: datetime | str | None = None,
    ) -> None:
        """关联配方版本、原料、过程记录、包装、标签稿、抽样计划、实验室方法及结果。

        新证据到达后自动重审，证据充分时解除待放行状态。
        """
        at_dt = self._now(at)
        batch = self._batch(batch_id)
        if evidence_type not in EVIDENCE_TYPES:
            raise DomainError(f"未登记的证据类型: {evidence_type}")
        if any(item.evidence_id == evidence_id for item in batch.evidence):
            raise StateError(f"证据已关联: {evidence_id}")
        if evidence_type == "material_lot":
            lot_id = str(data.get("lot_id", ""))
            if lot_id not in self.lots:
                raise NotFoundError(f"原料批次不存在: {lot_id}")
        self._emit(
            "EVIDENCE_LINKED",
            "product_batch",
            batch_id,
            summary=f"批次 {batch_id} 关联证据 {evidence_type}/{evidence_id}",
            at=at_dt,
            batch_id=batch_id,
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            data=dict(data),
            actor=actor,
        )
        if self.batches[batch_id].status != BatchStatus.FROZEN:
            self.evaluate_batch(batch_id, actor="system:evidence-linked", at=at_dt)

    def evaluate_batch(
        self, batch_id: str, *, actor: str, at: datetime | str | None = None
    ) -> dict[str, Any]:
        """按生产与上市时点选择规则，过程要求、产品限量、检测方法分别判断。"""
        at_dt = self._now(at)
        batch = self._batch(batch_id)
        if batch.status == BatchStatus.FROZEN:
            return {"batch_id": batch_id, "overall": "frozen", "kinds": {}, "status": batch.status.value}
        product = self.products[batch.product_id]

        kinds: dict[str, dict[str, Any]] = {}
        for kind in ClauseKind:
            ref_time = batch.produced_at if kind == ClauseKind.PROCESS else batch.market_at
            clauses = self._active_clauses(product.product_id, kind, ref_time)
            if not clauses:
                kinds[kind.value] = {"status": "pass", "clauses": [], "findings": ["无已确认的适用条款"]}
                continue
            status, findings = "pass", []
            for clause in clauses:
                clause_status, clause_findings = self._judge(kind, batch, product, clause)
                findings.extend(clause_findings)
                if _SEVERITY[clause_status] > _SEVERITY[status]:
                    status = clause_status
            kinds[kind.value] = {
                "status": status,
                "clauses": [c.clause_id for c in clauses],
                "findings": findings,
            }

        overall = "pass"
        for outcome in kinds.values():
            if _SEVERITY[outcome["status"]] > _SEVERITY[overall]:
                overall = outcome["status"]

        self._emit(
            "BATCH_REVIEWED",
            "product_batch",
            batch_id,
            summary=f"批次 {batch_id} 审核结果 {overall}",
            at=at_dt,
            batch_id=batch_id,
            overall=overall,
            kinds=kinds,
            actor=actor,
        )
        self._apply_review_disposition(batch, overall, actor, at_dt)
        return {
            "batch_id": batch_id,
            "overall": overall,
            "kinds": kinds,
            "status": self.batches[batch_id].status.value,
        }

    def _apply_review_disposition(
        self, batch: BatchState, overall: str, actor: str, at: datetime
    ) -> None:
        status = batch.status
        if status in (BatchStatus.RECALLED, BatchStatus.REJECTED):
            return  # 召回中与已判定不合格的批次不自动改态，保留处置记录
        if overall == "pass":
            if status in (BatchStatus.PENDING, BatchStatus.HOLD):
                self._record_disposition(batch, BatchStatus.RELEASED, "审核通过，放行", actor, at)
        elif overall == "insufficient":
            if status in (BatchStatus.PENDING, BatchStatus.RELEASED):
                self._record_disposition(batch, BatchStatus.HOLD, "证据不足，转待放行", actor, at)
            elif status == BatchStatus.SHIPPED:
                self._record_disposition(
                    batch, BatchStatus.SHIPPED, "新标下证据不足，追加处置：补充取证", actor, at
                )
        else:  # fail
            if status == BatchStatus.SHIPPED:
                self._record_disposition(
                    batch, BatchStatus.SHIPPED, "审核不通过，追加处置：评估召回", actor, at
                )
            else:
                self._record_disposition(batch, BatchStatus.REJECTED, "审核不通过", actor, at)

    def _active_clauses(
        self, product_id: str, kind: ClauseKind, ref_time: datetime
    ) -> list[ClauseState]:
        """已确认关联且在参照时点有效的条款；被已生效新版替代的旧版不参与。"""
        candidates = [
            clause
            for clause in self.clauses.values()
            if clause.kind == kind
            and clause.effective_at <= ref_time
            and self.associations.get((clause.clause_id, product_id)) == AssociationStatus.CONFIRMED
        ]

        def ancestry(clause: ClauseState) -> set[str]:
            """沿 supersedes 链向上收集全部旧版条款 id。"""
            seen: set[str] = set()
            current = clause
            while current.supersedes and current.supersedes not in seen:
                seen.add(current.supersedes)
                current = self.clauses.get(current.supersedes, current)
            return seen

        return [
            clause
            for clause in candidates
            if not any(
                clause.clause_id in ancestry(other)
                for other in candidates
                if other.clause_id != clause.clause_id
            )
        ]

    def _judge(
        self, kind: ClauseKind, batch: BatchState, product: ProductState, clause: ClauseState
    ) -> tuple[str, list[str]]:
        if kind == ClauseKind.PROCESS:
            return self._judge_process(batch, clause)
        if kind == ClauseKind.LIMIT:
            return self._judge_limit(batch, product, clause)
        return self._judge_method(batch, clause)

    @staticmethod
    def _judge_process(batch: BatchState, clause: ClauseState) -> tuple[str, list[str]]:
        params = clause.parameters
        status, findings = "pass", []
        records: dict[str, list[EvidenceItem]] = {}
        for item in batch.evidence_of("process_record"):
            records.setdefault(str(item.data.get("record_type", "")), []).append(item)

        def insufficient(message: str) -> None:
            nonlocal status
            findings.append(message)
            status = "insufficient" if status == "pass" else status

        def fail(message: str) -> None:
            nonlocal status
            findings.append(message)
            status = "fail"

        for required in params.get("required_records", []):
            if required not in records:
                insufficient(f"缺少过程记录: {required}")
        for key, record_type, label in (
            ("temperature_c", "temperature", "温度"),
            ("humidity_pct", "humidity", "湿度"),
        ):
            bounds = params.get(key)
            if not bounds:
                continue
            items = records.get(record_type, [])
            if not items:
                insufficient(f"缺少{label}记录")
                continue
            for item in items:
                value = item.data.get("value")
                if value is None:
                    insufficient(f"{label}记录 {item.evidence_id} 缺少数值")
                elif value < bounds["min"] or value > bounds["max"]:
                    fail(f"{label} {value} 超出条款 {clause.clause_id} 范围 [{bounds['min']}, {bounds['max']}]")
        allowed = params.get("packaging_materials")
        if allowed:
            packs = batch.evidence_of("packaging_material")
            if not packs:
                insufficient("缺少包装接触材料记录")
            elif not all(item.data.get("material") in allowed for item in packs):
                fail(f"包装接触材料不在条款 {clause.clause_id} 允许清单内")
        return status, findings

    @staticmethod
    def _judge_limit(
        batch: BatchState, product: ProductState, clause: ClauseState
    ) -> tuple[str, list[str]]:
        params = clause.parameters
        status, findings = "pass", []
        results = batch.evidence_of("lab_result")

        def insufficient(message: str) -> None:
            nonlocal status
            findings.append(message)
            status = "insufficient" if status == "pass" else status

        def fail(message: str) -> None:
            nonlocal status
            findings.append(message)
            status = "fail"

        def check_cap(analyte: str, cap: float) -> None:
            matches = [r for r in results if r.data.get("analyte") == analyte]
            if not matches:
                insufficient(f"缺少 {analyte} 检验结果")
                return
            value = matches[-1].data.get("value")
            if value is None:
                insufficient(f"{analyte} 检验结果缺少数值")
            elif value > cap:
                fail(f"{analyte}={value} 超过条款 {clause.clause_id} 限量 {cap}")

        for analyte, cap in params.get("analyte_limits", {}).items():
            check_cap(analyte, cap)
        for nutrient, cap in params.get("nutrient_caps", {}).items():
            if nutrient in product.nutrients:  # 营养素上限只影响含该营养素的产品
                check_cap(nutrient, cap)
        required_claims = params.get("required_label_claims", [])
        if required_claims:
            drafts = batch.evidence_of("label_draft")
            if not drafts:
                insufficient("缺少标签稿")
            else:
                claims = set(drafts[-1].data.get("claims", []))
                missing = [c for c in required_claims if c not in claims]
                if missing:
                    fail(f"标签稿缺少条款 {clause.clause_id} 必备标示: {', '.join(missing)}")
        return status, findings

    @staticmethod
    def _judge_method(batch: BatchState, clause: ClauseState) -> tuple[str, list[str]]:
        params = clause.parameters
        status, findings = "pass", []
        accepted = {params.get("method_code")} | set(params.get("also_accept", []))
        accepted.discard(None)
        results = batch.evidence_of("lab_result")
        for analyte in params.get("analytes", []):
            matches = [r for r in results if r.data.get("analyte") == analyte]
            if not matches:
                findings.append(f"{analyte} 无检验结果")
                status = "insufficient"
                continue
            used = matches[-1].data.get("method_code")
            if used not in accepted:
                findings.append(
                    f"{analyte} 使用方法 {used}，条款 {clause.clause_id} 要求 {sorted(accepted)}，旧方法结果不足以证明"
                )
                status = "insufficient"
        return status, findings

    # ------------------------------------------------------------------
    # 出库与处置
    # ------------------------------------------------------------------

    def ship_batch(
        self, batch_id: str, *, actor: str, at: datetime | str | None = None
    ) -> None:
        at_dt = self._now(at)
        batch = self._batch(batch_id)
        if batch.status != BatchStatus.RELEASED:
            raise StateError("只有已放行批次可以出库")
        self._emit(
            "BATCH_SHIPPED",
            "product_batch",
            batch_id,
            summary=f"批次 {batch_id} 出库",
            at=at_dt,
            batch_id=batch_id,
            actor=actor,
        )
        self._record_disposition(batch, BatchStatus.SHIPPED, "批次出库", actor, at_dt)

    def _record_disposition(
        self, batch: BatchState, to_status: BatchStatus, reason: str, actor: str, at: datetime
    ) -> None:
        """追加一条处置记录；已出库批次的历史决定保留，新决定只追加。"""
        decision_id = f"dec-{self._decision_seq + 1:04d}"
        self._emit(
            "DISPOSITION_CHANGED",
            "compliance_decision",
            decision_id,
            summary=f"批次 {batch.batch_id} 处置 {batch.status.value} -> {to_status.value}：{reason}",
            at=at,
            decision_id=decision_id,
            batch_id=batch.batch_id,
            from_status=batch.status.value,
            to_status=to_status.value,
            reason=reason,
            actor=actor,
        )

    # ------------------------------------------------------------------
    # 检验报告：幂等、冲突冻结
    # ------------------------------------------------------------------

    def submit_report(
        self,
        report_no: str,
        content: Mapping[str, Any],
        *,
        actor: str,
        at: datetime | str | None = None,
    ) -> ReportStatus:
        """接收检验报告。

        相同编号相同内容重送：幂等丢弃，不重复放行；
        相同编号不同内容：冻结其全部引用批次。
        content 形如 {"results": [{"batch_id", "analyte", "value", "method_code"}, ...]}。
        """
        at_dt = self._now(at)
        fingerprint = _fingerprint(content)
        results = [dict(item) for item in content.get("results", [])]
        batch_ids = sorted({str(item["batch_id"]) for item in results if "batch_id" in item})
        for batch_id in batch_ids:
            self._batch(batch_id)

        existing = self.reports.get(report_no)
        if existing is not None and existing.fingerprint == fingerprint:
            self._emit(
                "REPORT_DUPLICATE",
                "lab_report",
                report_no,
                summary=f"报告 {report_no} 重送，内容一致，幂等丢弃",
                at=at_dt,
                report_no=report_no,
                fingerprint=fingerprint,
            )
            return ReportStatus.DUPLICATE
        if existing is not None:
            self._emit(
                "REPORT_CONFLICTED",
                "lab_report",
                report_no,
                summary=f"报告 {report_no} 编号相同而内容不同，冻结引用批次",
                at=at_dt,
                report_no=report_no,
                old_fingerprint=existing.fingerprint,
                new_fingerprint=fingerprint,
                batch_ids=batch_ids,
            )
            for batch_id in sorted(self.reports[report_no].referenced_batch_ids):
                batch = self.batches[batch_id]
                if batch.status in (BatchStatus.FROZEN, BatchStatus.RECALLED):
                    continue
                self._emit(
                    "BATCH_FROZEN",
                    "product_batch",
                    batch_id,
                    summary=f"批次 {batch_id} 因报告 {report_no} 冲突被冻结",
                    at=at_dt,
                    batch_id=batch_id,
                    report_no=report_no,
                )
                self._record_disposition(
                    batch, BatchStatus.FROZEN, f"报告 {report_no} 编号冲突，冻结", actor, at_dt
                )
            return ReportStatus.CONFLICTED

        self._emit(
            "REPORT_ACCEPTED",
            "lab_report",
            report_no,
            summary=f"接受检验报告 {report_no}",
            at=at_dt,
            report_no=report_no,
            fingerprint=fingerprint,
            batch_ids=batch_ids,
            results=results,
            actor=actor,
        )
        for index, item in enumerate(results):
            batch_id = str(item["batch_id"])
            self._emit(
                "EVIDENCE_LINKED",
                "product_batch",
                batch_id,
                summary=f"批次 {batch_id} 关联报告 {report_no} 检验结果",
                at=at_dt,
                batch_id=batch_id,
                evidence_type="lab_result",
                evidence_id=f"{report_no}:{batch_id}:{index}",
                data={**item, "report_no": report_no},
                actor=f"report:{report_no}",
            )
        for batch_id in batch_ids:
            if self.batches[batch_id].status != BatchStatus.FROZEN:
                self.evaluate_batch(batch_id, actor=f"report:{report_no}", at=at_dt)
        return ReportStatus.ACCEPTED

    def unfreeze_batch(
        self, batch_id: str, *, rationale: str, actor: str, at: datetime | str | None = None
    ) -> None:
        """合规人员解除冻结，批次回到待放行并重审。"""
        at_dt = self._now(at)
        batch = self._batch(batch_id)
        if batch.status != BatchStatus.FROZEN:
            raise StateError("只有冻结批次可以解冻")
        self._emit(
            "BATCH_UNFROZEN",
            "product_batch",
            batch_id,
            summary=f"批次 {batch_id} 解冻：{rationale}",
            at=at_dt,
            batch_id=batch_id,
            rationale=rationale,
            actor=actor,
        )
        self._record_disposition(batch, BatchStatus.HOLD, f"解冻：{rationale}", actor, at_dt)
        self.evaluate_batch(batch_id, actor="system:unfrozen", at=at_dt)

    # ------------------------------------------------------------------
    # 召回与范围调整
    # ------------------------------------------------------------------

    def declare_recall(
        self,
        reason: str,
        batch_ids: Iterable[str],
        *,
        actor: str,
        at: datetime | str | None = None,
    ) -> str:
        """宣布召回并安排通知动作；各批次处置历史保留。"""
        at_dt = self._now(at)
        scope = sorted(set(batch_ids))
        if not scope:
            raise DomainError("召回范围不能为空")
        for batch_id in scope:
            self._batch(batch_id)
        recall_id = f"rec-{len(self.recalls) + 1:04d}"
        self._emit(
            "RECALL_DECLARED",
            "recall",
            recall_id,
            summary=f"宣布召回 {recall_id}：{reason}",
            at=at_dt,
            recall_id=recall_id,
            reason=reason,
            batch_ids=scope,
            actor=actor,
        )
        for batch_id in scope:
            batch = self.batches[batch_id]
            if batch.status == BatchStatus.RECALLED:
                continue
            self._record_disposition(batch, BatchStatus.RECALLED, f"召回 {recall_id}：{reason}", actor, at_dt)
        self._schedule_action(
            ActionKind.NOTIFICATION, recall_id,
            {"recall_id": recall_id, "channel": "regulator_and_customers"}, at_dt,
        )
        return recall_id

    def request_recall_narrowing(
        self,
        recall_id: str,
        keep_batch_ids: Iterable[str],
        *,
        requested_by: str,
        at: datetime | str | None = None,
    ) -> str:
        """申请缩小召回范围，生成待批准的审批动作。"""
        at_dt = self._now(at)
        recall = self._recall(recall_id)
        if recall.status != "active":
            raise StateError("召回已关闭")
        keep = sorted(set(keep_batch_ids))
        if not set(keep) <= recall.scope:
            raise DomainError("缩小后的范围必须是当前范围的子集")
        removed = sorted(recall.scope - set(keep))
        if not removed:
            raise DomainError("范围未发生变化")
        action_id = self._schedule_action(
            ActionKind.APPROVAL, recall_id,
            {"recall_id": recall_id, "keep": keep, "removed": removed, "requested_by": requested_by},
            at_dt,
        )
        self._emit(
            "RECALL_NARROWING_REQUESTED",
            "recall",
            recall_id,
            summary=f"召回 {recall_id} 申请缩小范围，移除 {removed}",
            at=at_dt,
            recall_id=recall_id,
            action_id=action_id,
            keep=keep,
            removed=removed,
            requested_by=requested_by,
        )
        return action_id

    def approve_action(
        self, action_id: str, *, approver: str, at: datetime | str | None = None
    ) -> None:
        """批准审批动作；召回缩小范围必须由独立于申请人的人员批准。"""
        at_dt = self._now(at)
        action = self._action(action_id)
        if action.kind != ActionKind.APPROVAL:
            raise StateError("该动作不是审批动作")
        if action.state != ActionState.PENDING:
            raise StateError("审批动作已处理")
        requested_by = action.payload["requested_by"]
        if approver == requested_by:
            raise ApprovalError("召回缩小范围需要独立批准，批准人不能是申请人")
        recall_id = action.payload["recall_id"]
        removed = list(action.payload["removed"])
        self._emit(
            "RECALL_NARROWED",
            "recall",
            recall_id,
            summary=f"召回 {recall_id} 范围缩小，移除 {removed}",
            at=at_dt,
            recall_id=recall_id,
            action_id=action_id,
            removed_batch_ids=removed,
            keep=list(action.payload["keep"]),
            approved_by=approver,
        )
        for batch_id in removed:
            batch = self.batches[batch_id]
            restore = batch.recall_restore or BatchStatus.HOLD
            self._record_disposition(
                batch, restore, f"召回 {recall_id} 范围缩小，恢复处置", approver, at_dt
            )
        self._emit(
            "ACTION_COMPLETED",
            "workflow_action",
            action_id,
            summary=f"审批动作 {action_id} 已批准",
            at=at_dt,
            action_id=action_id,
            result={"outcome": "approved", "approved_by": approver},
        )

    def reject_action(
        self, action_id: str, *, approver: str, at: datetime | str | None = None
    ) -> None:
        at_dt = self._now(at)
        action = self._action(action_id)
        if action.kind != ActionKind.APPROVAL:
            raise StateError("该动作不是审批动作")
        if action.state != ActionState.PENDING:
            raise StateError("审批动作已处理")
        if approver == action.payload["requested_by"]:
            raise ApprovalError("审批人不能是申请人")
        recall_id = action.payload["recall_id"]
        self._emit(
            "RECALL_NARROWING_REJECTED",
            "recall",
            recall_id,
            summary=f"召回 {recall_id} 缩小范围申请被驳回",
            at=at_dt,
            recall_id=recall_id,
            action_id=action_id,
            rejected_by=approver,
        )
        self._emit(
            "ACTION_COMPLETED",
            "workflow_action",
            action_id,
            summary=f"审批动作 {action_id} 已驳回",
            at=at_dt,
            action_id=action_id,
            result={"outcome": "rejected", "rejected_by": approver},
        )

    # ------------------------------------------------------------------
    # 工作流动作：调度、执行、中断恢复
    # ------------------------------------------------------------------

    def _schedule_action(
        self, kind: ActionKind, subject_id: str, payload: Mapping[str, Any], at: datetime
    ) -> str:
        action_id = f"act-{len(self.actions) + 1:04d}"
        self._emit(
            "ACTION_SCHEDULED",
            "workflow_action",
            action_id,
            summary=f"调度{kind.value}动作 {action_id}",
            at=at,
            action_id=action_id,
            kind=kind.value,
            subject_id=subject_id,
            payload=dict(payload),
        )
        return action_id

    def resume(self, *, at: datetime | str | None = None) -> list[str]:
        """继续未完成动作：取样与通知自动重试，审批动作等待人工处理。"""
        at_dt = self._now(at)
        completed: list[str] = []
        for action in sorted(self.actions.values(), key=lambda item: item.action_id):
            if action.kind == ActionKind.APPROVAL:
                continue
            if action.state not in (ActionState.PENDING, ActionState.IN_PROGRESS):
                continue
            self._execute_action(action, at_dt)
            completed.append(action.action_id)
        return completed

    def _execute_action(self, action: ActionRecord, at: datetime) -> None:
        self._emit(
            "ACTION_STARTED",
            "workflow_action",
            action.action_id,
            summary=f"开始执行动作 {action.action_id}",
            at=at,
            action_id=action.action_id,
        )
        handler = self._handlers.get(action.kind.value) or self._default_handler(action.kind)
        try:
            result = handler(self, action, at)
        except ActionInterrupted:
            raise  # 中断：保持 IN_PROGRESS，恢复后继续
        except Exception as exc:  # noqa: BLE001 - 失败落事件，其余动作继续
            self._emit(
                "ACTION_FAILED",
                "workflow_action",
                action.action_id,
                summary=f"动作 {action.action_id} 失败：{exc}",
                at=at,
                action_id=action.action_id,
                error=str(exc),
            )
            return
        self._emit(
            "ACTION_COMPLETED",
            "workflow_action",
            action.action_id,
            summary=f"动作 {action.action_id} 完成",
            at=at,
            action_id=action.action_id,
            result=result,
        )

    def _default_handler(self, kind: ActionKind) -> ActionHandler:
        if kind == ActionKind.SAMPLING:
            return StandardMigrationService._handle_sampling
        if kind == ActionKind.NOTIFICATION:
            return StandardMigrationService._handle_notification
        raise DomainError(f"没有 {kind.value} 的默认处理器")

    @staticmethod
    def _handle_sampling(
        service: "StandardMigrationService", action: ActionRecord, at: datetime
    ) -> dict[str, Any]:
        batch_id = action.payload["batch_id"]
        evidence_id = f"sample-{action.action_id}"
        batch = service.batches[batch_id]
        if any(item.evidence_id == evidence_id for item in batch.evidence):
            return {"evidence_id": evidence_id, "deduplicated": True}
        service.link_evidence(
            batch_id, "sample_record", evidence_id,
            {"sampled": True, "plan": "default"},
            actor="system:sampling", at=at,
        )
        return {"evidence_id": evidence_id}

    @staticmethod
    def _handle_notification(
        service: "StandardMigrationService", action: ActionRecord, at: datetime
    ) -> dict[str, Any]:
        return {
            "notification_id": f"ntf-{action.action_id}",
            "recall_id": action.payload.get("recall_id"),
            "channel": action.payload.get("channel", "compliance"),
        }

    # ------------------------------------------------------------------
    # 监管追溯
    # ------------------------------------------------------------------

    def trace_batch(self, batch_id: str) -> dict[str, Any]:
        """从批次追到当时有效条款、过程证据、检验方法和每次处置。"""
        batch = self._batch(batch_id)
        product = self.products[batch.product_id]
        applicable: dict[str, list[dict[str, Any]]] = {}
        for kind in ClauseKind:
            ref_time = batch.produced_at if kind == ClauseKind.PROCESS else batch.market_at
            applicable[kind.value] = [
                self._clause_view(clause)
                for clause in self._active_clauses(product.product_id, kind, ref_time)
            ]
        evidence: dict[str, list[dict[str, Any]]] = {}
        for item in batch.evidence:
            evidence.setdefault(item.evidence_type, []).append(
                {
                    "evidence_id": item.evidence_id,
                    "data": item.data,
                    "linked_at": _iso(item.linked_at),
                    "actor": item.actor,
                }
            )
        recalls = []
        for recall in self.recalls.values():
            if batch_id in recall.scope or any(
                batch_id in narrowing["removed"] for narrowing in recall.narrowings
            ):
                recalls.append(
                    {
                        "recall_id": recall.recall_id,
                        "status": recall.status,
                        "in_scope": batch_id in recall.scope,
                        "narrowings": [
                            n for n in recall.narrowings if batch_id in n["removed"]
                        ],
                    }
                )
        return {
            "batch_id": batch_id,
            "product_id": product.product_id,
            "status": batch.status.value,
            "produced_at": _iso(batch.produced_at),
            "market_at": _iso(batch.market_at),
            "formula_version": batch.formula_version,
            "applicable_clauses": applicable,
            "material_lots": [
                {
                    "lot_id": lot.lot_id,
                    "supplier_id": lot.supplier_id,
                    "material_code": lot.material_code,
                    "failed": lot.failed,
                }
                for lot in (self.lots[lot_id] for lot_id in batch.material_lots)
            ],
            "evidence": evidence,
            "reviews": list(batch.reviews),
            "decisions": list(batch.decisions),
            "recalls": recalls,
            "reports": [
                {"report_no": r.report_no, "status": r.status.value}
                for r in self.reports.values()
                if batch_id in r.referenced_batch_ids
            ],
        }

    def trace_product(self, product_id: str) -> dict[str, Any]:
        """从产品追到当时有效条款、过程证据、检验方法和每次范围调整。"""
        if product_id not in self.products:
            raise NotFoundError(f"产品不存在: {product_id}")
        product = self.products[product_id]
        batch_ids = [b.batch_id for b in self.batches.values() if b.product_id == product_id]
        scope_adjustments: list[dict[str, Any]] = []
        for recall in self.recalls.values():
            if not (recall.scope & set(batch_ids)) and not any(
                set(n["removed"]) & set(batch_ids) for n in recall.narrowings
            ):
                continue
            for narrowing in recall.narrowings:
                scope_adjustments.append({"recall_id": recall.recall_id, **narrowing})
        return {
            "product_id": product_id,
            "name": product.name,
            "category": product.category,
            "confirmed_clauses": [
                self._clause_view(self.clauses[clause_id])
                for (clause_id, pid), status in sorted(self.associations.items())
                if pid == product_id and status == AssociationStatus.CONFIRMED
            ],
            "association_history": [
                entry for entry in self.association_log if entry["product_id"] == product_id
            ],
            "batches": [self.trace_batch(batch_id) for batch_id in sorted(batch_ids)],
            "scope_adjustments": scope_adjustments,
        }

    @staticmethod
    def _clause_view(clause: ClauseState) -> dict[str, Any]:
        return {
            "clause_id": clause.clause_id,
            "standard_code": clause.standard_code,
            "clause_no": clause.clause_no,
            "kind": clause.kind.value,
            "published_at": _iso(clause.published_at),
            "effective_at": _iso(clause.effective_at),
            "supersedes": clause.supersedes,
            "superseded_by": clause.superseded_by,
            "parameters": clause.parameters,
        }

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def _batch(self, batch_id: str) -> BatchState:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"批次不存在: {batch_id}")
        return batch

    def _recall(self, recall_id: str) -> RecallState:
        recall = self.recalls.get(recall_id)
        if recall is None:
            raise NotFoundError(f"召回不存在: {recall_id}")
        return recall

    def _action(self, action_id: str) -> ActionRecord:
        action = self.actions.get(action_id)
        if action is None:
            raise NotFoundError(f"动作不存在: {action_id}")
        return action

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._log.events

    # ------------------------------------------------------------------
    # 投影：由事件重建状态
    # ------------------------------------------------------------------

    def _on_standard_registered(self, event: dict[str, Any]) -> None:
        self.clauses[event["clause_id"]] = ClauseState(
            clause_id=event["clause_id"],
            standard_code=event["standard_code"],
            clause_no=event["clause_no"],
            kind=ClauseKind(event["kind"]),
            published_at=_parse_dt(event["published_at"]),
            effective_at=_parse_dt(event["effective_at"]),
            supersedes=event.get("supersedes"),
            categories=frozenset(event.get("categories", [])),
            min_age_months=event.get("min_age_months"),
            max_age_months=event.get("max_age_months"),
            nutrients=frozenset(event.get("nutrients", [])),
            parameters=dict(event.get("parameters", {})),
        )

    def _on_standard_superseded(self, event: dict[str, Any]) -> None:
        self.clauses[event["clause_id"]].superseded_by = event["replaced_by"]

    def _on_product_registered(self, event: dict[str, Any]) -> None:
        self.products[event["product_id"]] = ProductState(
            product_id=event["product_id"],
            name=event["name"],
            category=event["category"],
            min_age_months=event.get("min_age_months"),
            max_age_months=event.get("max_age_months"),
            nutrients=frozenset(event.get("nutrients", [])),
        )

    def _record_association(self, event: dict[str, Any], status: AssociationStatus) -> None:
        key = (event["clause_id"], event["product_id"])
        self.associations[key] = status
        self.association_log.append(
            {
                "clause_id": event["clause_id"],
                "product_id": event["product_id"],
                "status": status.value,
                "actor": event.get("actor", "system"),
                "at": event["occurred_at"],
            }
        )

    def _on_association_suggested(self, event: dict[str, Any]) -> None:
        self._record_association(event, AssociationStatus.SUGGESTED)

    def _on_association_confirmed(self, event: dict[str, Any]) -> None:
        self._record_association(event, AssociationStatus.CONFIRMED)

    def _on_association_rejected(self, event: dict[str, Any]) -> None:
        self._record_association(event, AssociationStatus.REJECTED)

    def _on_lot_received(self, event: dict[str, Any]) -> None:
        self.lots[event["lot_id"]] = LotState(
            lot_id=event["lot_id"],
            supplier_id=event["supplier_id"],
            material_code=event["material_code"],
            received_at=_parse_dt(event["occurred_at"]),
        )

    def _on_lot_failed(self, event: dict[str, Any]) -> None:
        lot = self.lots[event["lot_id"]]
        lot.failed = True
        lot.fail_reason = event["reason"]

    def _on_batch_registered(self, event: dict[str, Any]) -> None:
        self.batches[event["batch_id"]] = BatchState(
            batch_id=event["batch_id"],
            product_id=event["product_id"],
            produced_at=_parse_dt(event["produced_at"]),
            market_at=_parse_dt(event["market_at"]),
            formula_version=event["formula_version"],
        )

    def _on_evidence_linked(self, event: dict[str, Any]) -> None:
        batch = self.batches[event["batch_id"]]
        batch.evidence.append(
            EvidenceItem(
                evidence_id=event["evidence_id"],
                evidence_type=event["evidence_type"],
                data=dict(event["data"]),
                linked_at=_parse_dt(event["occurred_at"]),
                actor=event.get("actor", "system"),
            )
        )
        if event["evidence_type"] == "material_lot":
            lot_id = str(event["data"]["lot_id"])
            if lot_id not in batch.material_lots:
                batch.material_lots.append(lot_id)

    def _on_batch_reviewed(self, event: dict[str, Any]) -> None:
        self.batches[event["batch_id"]].reviews.append(
            {
                "overall": event["overall"],
                "kinds": event["kinds"],
                "actor": event.get("actor", "system"),
                "at": event["occurred_at"],
            }
        )

    def _on_disposition_changed(self, event: dict[str, Any]) -> None:
        self._decision_seq += 1
        batch = self.batches[event["batch_id"]]
        to_status = BatchStatus(event["to_status"])
        if to_status == BatchStatus.RECALLED and batch.status != BatchStatus.RECALLED:
            batch.recall_restore = batch.status
        batch.decisions.append(
            {
                "decision_id": event["decision_id"],
                "from_status": event["from_status"],
                "to_status": event["to_status"],
                "reason": event["reason"],
                "actor": event.get("actor", "system"),
                "at": event["occurred_at"],
            }
        )
        batch.status = to_status

    def _on_batch_shipped(self, event: dict[str, Any]) -> None:
        self.batches[event["batch_id"]].shipped_at = _parse_dt(event["occurred_at"])

    def _on_batch_frozen(self, event: dict[str, Any]) -> None:
        pass  # 状态变化由伴随的 DISPOSITION_CHANGED 投影

    def _on_batch_unfrozen(self, event: dict[str, Any]) -> None:
        pass

    def _on_report_accepted(self, event: dict[str, Any]) -> None:
        self.reports[event["report_no"]] = ReportState(
            report_no=event["report_no"],
            fingerprint=event["fingerprint"],
            batch_ids=list(event["batch_ids"]),
            status=ReportStatus.ACCEPTED,
            results=[dict(item) for item in event["results"]],
            referenced_batch_ids=set(event["batch_ids"]),
        )

    def _on_report_duplicate(self, event: dict[str, Any]) -> None:
        pass  # 幂等丢弃，不改变批次状态

    def _on_report_conflicted(self, event: dict[str, Any]) -> None:
        report = self.reports[event["report_no"]]
        report.status = ReportStatus.CONFLICTED
        report.referenced_batch_ids |= set(event["batch_ids"])

    def _on_recall_declared(self, event: dict[str, Any]) -> None:
        self.recalls[event["recall_id"]] = RecallState(
            recall_id=event["recall_id"],
            reason=event["reason"],
            scope=set(event["batch_ids"]),
            created_by=event["actor"],
        )

    def _on_recall_narrowing_requested(self, event: dict[str, Any]) -> None:
        pass  # 范围在批准时才变化

    def _on_recall_narrowed(self, event: dict[str, Any]) -> None:
        recall = self.recalls[event["recall_id"]]
        recall.scope = set(event["keep"])
        recall.narrowings.append(
            {
                "removed": list(event["removed_batch_ids"]),
                "keep": list(event["keep"]),
                "requested_by": self.actions[event["action_id"]].payload["requested_by"],
                "approved_by": event["approved_by"],
                "at": event["occurred_at"],
            }
        )

    def _on_recall_narrowing_rejected(self, event: dict[str, Any]) -> None:
        pass  # 范围不变，事件本身留痕

    def _on_action_scheduled(self, event: dict[str, Any]) -> None:
        self.actions[event["action_id"]] = ActionRecord(
            action_id=event["action_id"],
            kind=ActionKind(event["kind"]),
            subject_id=event["subject_id"],
            payload=dict(event["payload"]),
        )

    def _on_action_started(self, event: dict[str, Any]) -> None:
        action = self.actions[event["action_id"]]
        action.state = ActionState.IN_PROGRESS
        action.attempts += 1

    def _on_action_completed(self, event: dict[str, Any]) -> None:
        action = self.actions[event["action_id"]]
        action.state = ActionState.DONE
        action.result = dict(event.get("result", {}))
        if action.kind == ActionKind.NOTIFICATION:
            self.notifications.append(action.result)

    def _on_action_failed(self, event: dict[str, Any]) -> None:
        action = self.actions[event["action_id"]]
        action.state = ActionState.FAILED
        action.error = event["error"]
