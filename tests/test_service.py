from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from food_standard_migration import (
    ActionInterrupted,
    ActionState,
    ApprovalError,
    AssociationStatus,
    BatchStatus,
    DomainError,
    NotFoundError,
    ReportStatus,
    StandardMigrationService,
    validate_event,
)
from food_standard_migration.service import load_default_schema

CST = timezone(timedelta(hours=8))


def ts(month: int, day: int, hour: int = 0) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=CST)


def make_service(log_path: str | None = None, action_handlers=None) -> StandardMigrationService:
    return StandardMigrationService(
        load_default_schema(),
        log_path=log_path,
        clock=lambda: ts(9, 29, 9),
        action_handlers=action_handlers,
    )


def add_dairy_product(service: StandardMigrationService, product_id: str = "P-DAIRY") -> str:
    service.register_product(product_id, "灭菌乳", "dairy", actor="qa")
    return product_id


class ClauseRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        add_dairy_product(self.service)

    def test_clause_lifecycle_and_applicable_products(self) -> None:
        service = self.service
        service.register_clause(
            "C-PROC-1", "GB 12693", "4.1", "process", ts(1, 1), ts(2, 1),
            categories=["dairy"], parameters={"temperature_c": {"min": 0, "max": 10}},
            actor="registrar",
        )
        # 登记后自动关联适用产品，但仍是待确认状态
        self.assertEqual(
            AssociationStatus.SUGGESTED, service.associations[("C-PROC-1", "P-DAIRY")]
        )
        service.register_clause(
            "C-PROC-2", "GB 12693", "4.1", "process", ts(3, 1), ts(6, 1),
            supersedes="C-PROC-1", categories=["dairy"],
            parameters={"temperature_c": {"min": 0, "max": 4}},
            actor="registrar",
        )
        self.assertEqual("C-PROC-2", service.clauses["C-PROC-1"].superseded_by)
        self.assertEqual(
            AssociationStatus.SUGGESTED, service.associations[("C-PROC-2", "P-DAIRY")]
        )

    def test_clause_registration_validation(self) -> None:
        service = self.service
        with self.assertRaises(DomainError):
            service.register_clause(
                "C-BAD", "GB 1", "1", "process", ts(5, 1), ts(4, 1), actor="registrar"
            )
        with self.assertRaises(NotFoundError):
            service.register_clause(
                "C-ORPHAN", "GB 1", "1", "process", ts(1, 1), ts(2, 1),
                supersedes="C-MISSING", actor="registrar",
            )

    def test_child_age_and_nutrient_changes_only_match_products(self) -> None:
        service = make_service()
        service.register_product(
            "P-KID", "儿童成长奶粉", "supplement",
            min_age_months=36, max_age_months=60, nutrients=["vitamin_a"], actor="qa",
        )
        service.register_product(
            "P-ADULT", "成人奶粉", "supplement",
            min_age_months=216, nutrients=["vitamin_a"], actor="qa",
        )
        service.register_product(
            "P-KID-NOVA", "儿童米粉", "supplement",
            min_age_months=36, max_age_months=60, nutrients=["vitamin_c"], actor="qa",
        )
        service.register_clause(
            "C-VITA", "GB 14880", "2.1", "limit", ts(1, 1), ts(2, 1),
            min_age_months=36, max_age_months=72, nutrients=["vitamin_a"],
            parameters={"nutrient_caps": {"vitamin_a": 400}},
            actor="registrar",
        )
        # 儿童适用年龄与营养素上限变化只影响匹配产品
        self.assertEqual(
            AssociationStatus.SUGGESTED, service.associations[("C-VITA", "P-KID")]
        )
        self.assertNotIn(("C-VITA", "P-ADULT"), service.associations)
        self.assertNotIn(("C-VITA", "P-KID-NOVA"), service.associations)


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        add_dairy_product(self.service)

    def _confirm_limit_clause(self) -> None:
        self.service.register_clause(
            "C-LIMIT", "GB 2762", "1.1", "limit", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"analyte_limits": {"lead": 0.5}},
            actor="registrar",
        )
        self.service.confirm_association("C-LIMIT", "P-DAIRY", actor="compliance")

    def test_auto_association_requires_confirmation(self) -> None:
        service = self.service
        service.register_clause(
            "C-LIMIT", "GB 2762", "1.1", "limit", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"analyte_limits": {"lead": 0.5}},
            actor="registrar",
        )
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        # 未确认的自动关联不参与评判
        self.assertEqual(BatchStatus.RELEASED, service.batches["B-1"].status)
        service.confirm_association("C-LIMIT", "P-DAIRY", actor="compliance")
        # 确认后条款生效：缺少检验结果，转待放行
        self.assertEqual(BatchStatus.HOLD, service.batches["B-1"].status)

    def test_rules_selected_by_production_and_market_time(self) -> None:
        service = self.service
        service.register_clause(
            "C-P1", "GB 12693", "4.1", "process", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"temperature_c": {"min": 0, "max": 10}},
            actor="registrar",
        )
        service.register_clause(
            "C-P2", "GB 12693", "4.1", "process", ts(2, 1), ts(6, 1),
            supersedes="C-P1", categories=["dairy"],
            parameters={"temperature_c": {"min": 0, "max": 4}},
            actor="registrar",
        )
        service.register_clause(
            "C-L1", "GB 2762", "1.1", "limit", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"analyte_limits": {"lead": 0.5}},
            actor="registrar",
        )
        service.register_clause(
            "C-L2", "GB 2762", "1.1", "limit", ts(2, 1), ts(7, 1),
            supersedes="C-L1", categories=["dairy"],
            parameters={"analyte_limits": {"lead": 0.2}},
            actor="registrar",
        )
        for clause_id in ("C-P1", "C-P2", "C-L1", "C-L2"):
            service.confirm_association(clause_id, "P-DAIRY", actor="compliance")
        # 生产于 3 月（旧过程要求），上市于 8 月（新限量已生效）
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        service.link_evidence(
            "B-1", "process_record", "PR-1",
            {"record_type": "temperature", "value": 7}, actor="qa",
        )
        service.link_evidence(
            "B-1", "lab_result", "LR-1",
            {"analyte": "lead", "value": 0.3, "method_code": "GB-A"}, actor="lab",
        )
        outcome = service.evaluate_batch("B-1", actor="qa")
        # 过程要求按生产时点选旧版（7℃ 在 0-10 内），产品限量按上市时点选新版（0.3 > 0.2）
        self.assertEqual("pass", outcome["kinds"]["process"]["status"])
        self.assertEqual(["C-P1"], outcome["kinds"]["process"]["clauses"])
        self.assertEqual("fail", outcome["kinds"]["limit"]["status"])
        self.assertEqual(["C-L2"], outcome["kinds"]["limit"]["clauses"])
        self.assertEqual("fail", outcome["overall"])
        self.assertEqual(BatchStatus.REJECTED, service.batches["B-1"].status)

    def test_three_kinds_judged_independently(self) -> None:
        service = self.service
        service.register_clause(
            "C-P", "GB 12693", "4.1", "process", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"temperature_c": {"min": 0, "max": 4}},
            actor="registrar",
        )
        service.register_clause(
            "C-L", "GB 2762", "1.1", "limit", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"analyte_limits": {"lead": 0.5}},
            actor="registrar",
        )
        service.register_clause(
            "C-M", "GB 5009", "1", "method", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"method_code": "GB-LEAD", "analytes": ["lead"]},
            actor="registrar",
        )
        for clause_id in ("C-P", "C-L", "C-M"):
            service.confirm_association(clause_id, "P-DAIRY", actor="compliance")
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        service.link_evidence(
            "B-1", "process_record", "PR-1",
            {"record_type": "temperature", "value": 9}, actor="qa",
        )
        service.link_evidence(
            "B-1", "lab_result", "LR-1",
            {"analyte": "lead", "value": 0.1, "method_code": "GB-LEAD"}, actor="lab",
        )
        outcome = service.evaluate_batch("B-1", actor="qa")
        self.assertEqual("fail", outcome["kinds"]["process"]["status"])
        self.assertEqual("pass", outcome["kinds"]["limit"]["status"])
        self.assertEqual("pass", outcome["kinds"]["method"]["status"])
        self.assertEqual("fail", outcome["overall"])

    def test_new_evidence_lifts_hold(self) -> None:
        service = self.service
        self._confirm_limit_clause()
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        self.assertEqual(BatchStatus.HOLD, service.batches["B-1"].status)
        service.link_evidence(
            "B-1", "lab_result", "LR-1",
            {"analyte": "lead", "value": 0.1, "method_code": "GB-A"}, actor="lab",
        )
        self.assertEqual(BatchStatus.RELEASED, service.batches["B-1"].status)

    def test_old_method_result_is_insufficient(self) -> None:
        service = self.service
        service.register_clause(
            "C-L", "GB 2762", "1.1", "limit", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"analyte_limits": {"lead": 0.5}},
            actor="registrar",
        )
        service.register_clause(
            "C-M1", "GB 5009", "1", "method", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"method_code": "GB-A", "analytes": ["lead"]},
            actor="registrar",
        )
        service.register_clause(
            "C-M2", "GB 5009", "1", "method", ts(2, 1), ts(5, 1),
            supersedes="C-M1", categories=["dairy"],
            parameters={"method_code": "GB-B", "analytes": ["lead"]},
            actor="registrar",
        )
        for clause_id in ("C-L", "C-M1", "C-M2"):
            service.confirm_association(clause_id, "P-DAIRY", actor="compliance")
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        # 旧检验方法合格并不能证明满足新要求
        service.link_evidence(
            "B-1", "lab_result", "LR-1",
            {"analyte": "lead", "value": 0.1, "method_code": "GB-A"}, actor="lab",
        )
        self.assertEqual(BatchStatus.HOLD, service.batches["B-1"].status)
        outcome = service.evaluate_batch("B-1", actor="qa")
        self.assertEqual("insufficient", outcome["kinds"]["method"]["status"])
        # 按新方法重新检测后解除待放行
        service.link_evidence(
            "B-1", "lab_result", "LR-2",
            {"analyte": "lead", "value": 0.1, "method_code": "GB-B"}, actor="lab",
        )
        self.assertEqual(BatchStatus.RELEASED, service.batches["B-1"].status)


class DispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        add_dairy_product(self.service)
        self.service.register_clause(
            "C-LIMIT", "GB 2762", "1.1", "limit", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"analyte_limits": {"lead": 0.5}},
            actor="registrar",
        )
        self.service.confirm_association("C-LIMIT", "P-DAIRY", actor="compliance")

    def _released_batch(self, batch_id: str) -> None:
        self.service.register_batch(batch_id, "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        self.service.link_evidence(
            batch_id, "lab_result", f"LR-{batch_id}",
            {"analyte": "lead", "value": 0.1, "method_code": "GB-A"}, actor="lab",
        )

    def test_shipped_batch_keeps_decision_and_appends(self) -> None:
        service = self.service
        self._released_batch("B-1")
        service.ship_batch("B-1", actor="logistics", at=ts(8, 2))
        self.assertEqual(BatchStatus.SHIPPED, service.batches["B-1"].status)
        # 新限量在上市时点前生效：已出库批次保留当时决定，追加处置
        service.register_clause(
            "C-LIMIT-2", "GB 2762", "1.1", "limit", ts(2, 1), ts(7, 1),
            supersedes="C-LIMIT", categories=["dairy"],
            parameters={"analyte_limits": {"lead": 0.05}},
            actor="registrar",
        )
        service.confirm_association("C-LIMIT-2", "P-DAIRY", actor="compliance")
        batch = service.batches["B-1"]
        self.assertEqual(BatchStatus.SHIPPED, batch.status)
        trail = [(d["from_status"], d["to_status"]) for d in batch.decisions]
        self.assertIn(("hold", "released"), trail)
        self.assertIn(("released", "shipped"), trail)
        self.assertEqual(("shipped", "shipped"), trail[-1])
        self.assertIn("追加处置", batch.decisions[-1]["reason"])

    def test_duplicate_report_does_not_release_twice(self) -> None:
        service = self.service
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        content = {"results": [{"batch_id": "B-1", "analyte": "lead", "value": 0.1,
                                "method_code": "GB-A"}]}
        status = service.submit_report("RPT-1", content, actor="lab")
        self.assertEqual(ReportStatus.ACCEPTED, status)
        self.assertEqual(BatchStatus.RELEASED, service.batches["B-1"].status)
        # 新标准使证据不足，批次回到待放行
        service.register_clause(
            "C-HG", "GB 2762", "1.2", "limit", ts(2, 1), ts(7, 1),
            categories=["dairy"], parameters={"analyte_limits": {"mercury": 0.1}},
            actor="registrar",
        )
        service.confirm_association("C-HG", "P-DAIRY", actor="compliance")
        self.assertEqual(BatchStatus.HOLD, service.batches["B-1"].status)
        # 相同报告重送不得重复放行
        again = service.submit_report("RPT-1", content, actor="lab")
        self.assertEqual(ReportStatus.DUPLICATE, again)
        batch = service.batches["B-1"]
        self.assertEqual(BatchStatus.HOLD, batch.status)
        self.assertEqual(3, len(batch.decisions))
        self.assertTrue(
            any(e["event_type"] == "REPORT_DUPLICATE" for e in service.events)
        )

    def test_conflicting_report_freezes_referenced_batches(self) -> None:
        service = make_service()
        add_dairy_product(service)
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        service.register_batch("B-2", "P-DAIRY", ts(3, 2), ts(8, 2), "F-1", actor="qa")
        content_v1 = {"results": [{"batch_id": "B-1", "analyte": "lead", "value": 0.1}]}
        self.assertEqual(ReportStatus.ACCEPTED, service.submit_report("RPT-9", content_v1, actor="lab"))
        self.assertEqual(ReportStatus.DUPLICATE, service.submit_report("RPT-9", content_v1, actor="lab"))
        # 编号相同而内容不同：冻结两个版本引用的全部批次
        content_v2 = {"results": [
            {"batch_id": "B-1", "analyte": "lead", "value": 0.4},
            {"batch_id": "B-2", "analyte": "lead", "value": 0.2},
        ]}
        self.assertEqual(ReportStatus.CONFLICTED, service.submit_report("RPT-9", content_v2, actor="lab"))
        self.assertEqual(BatchStatus.FROZEN, service.batches["B-1"].status)
        self.assertEqual(BatchStatus.FROZEN, service.batches["B-2"].status)
        outcome = service.evaluate_batch("B-1", actor="qa")
        self.assertEqual("frozen", outcome["overall"])
        # 合规解冻后重审
        service.unfreeze_batch("B-1", rationale="报告重发经核实", actor="compliance")
        self.assertEqual(BatchStatus.RELEASED, service.batches["B-1"].status)
        self.assertEqual(BatchStatus.FROZEN, service.batches["B-2"].status)

    def test_failed_lot_affects_only_genealogy_inventory(self) -> None:
        service = make_service()
        add_dairy_product(service)
        service.receive_lot("LOT-A", "SUP-1", "milk_powder", actor="warehouse")
        service.receive_lot("LOT-B", "SUP-2", "lactose", actor="warehouse")
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        service.register_batch("B-2", "P-DAIRY", ts(3, 2), ts(8, 2), "F-1", actor="qa")
        service.register_batch("B-3", "P-DAIRY", ts(3, 3), ts(8, 3), "F-1", actor="qa")
        service.link_evidence("B-1", "material_lot", "ML-1", {"lot_id": "LOT-A"}, actor="qa")
        service.link_evidence("B-2", "material_lot", "ML-2", {"lot_id": "LOT-B"}, actor="qa")
        service.link_evidence("B-3", "material_lot", "ML-3", {"lot_id": "LOT-A"}, actor="qa")
        service.ship_batch("B-3", actor="logistics", at=ts(8, 4))

        affected = service.fail_lot("LOT-A", "黄曲霉毒素超标", actor="lab")
        self.assertEqual({"B-1", "B-3"}, set(affected))
        # 在库批次转待放行
        self.assertEqual(BatchStatus.HOLD, service.batches["B-1"].status)
        # 谱系之外的批次不受影响
        self.assertEqual(BatchStatus.RELEASED, service.batches["B-2"].status)
        self.assertEqual([], service.batches["B-2"].decisions[1:])
        # 已出库批次保留当时决定并追加处置
        b3 = service.batches["B-3"]
        self.assertEqual(BatchStatus.SHIPPED, b3.status)
        self.assertIn("追加处置", b3.decisions[-1]["reason"])


class RecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        add_dairy_product(self.service)
        for batch_id in ("B-1", "B-2"):
            self.service.register_batch(batch_id, "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")

    def test_recall_narrowing_requires_independent_approval(self) -> None:
        service = self.service
        recall_id = service.declare_recall("污染物超标", ["B-1", "B-2"], actor="alice")
        self.assertEqual(BatchStatus.RECALLED, service.batches["B-1"].status)
        self.assertEqual(BatchStatus.RECALLED, service.batches["B-2"].status)

        action_id = service.request_recall_narrowing(recall_id, ["B-1"], requested_by="alice")
        # 申请人不能批准自己的缩小范围申请
        with self.assertRaises(ApprovalError):
            service.approve_action(action_id, approver="alice")
        # 独立批准人批准后范围缩小，被移除批次恢复召回前状态
        service.approve_action(action_id, approver="bob")
        self.assertEqual({"B-1"}, service.recalls[recall_id].scope)
        self.assertEqual(BatchStatus.RECALLED, service.batches["B-1"].status)
        self.assertEqual(BatchStatus.RELEASED, service.batches["B-2"].status)
        narrowing = service.recalls[recall_id].narrowings[0]
        self.assertEqual("alice", narrowing["requested_by"])
        self.assertEqual("bob", narrowing["approved_by"])

    def test_recall_narrowing_rejection_keeps_scope(self) -> None:
        service = self.service
        recall_id = service.declare_recall("标签错误", ["B-1"], actor="alice")
        action_id = service.request_recall_narrowing(recall_id, [], requested_by="carol")
        service.reject_action(action_id, approver="dave")
        self.assertEqual({"B-1"}, service.recalls[recall_id].scope)
        self.assertEqual(BatchStatus.RECALLED, service.batches["B-1"].status)

    def test_narrowing_must_shrink_scope(self) -> None:
        service = self.service
        recall_id = service.declare_recall("标签错误", ["B-1"], actor="alice")
        with self.assertRaises(DomainError):
            service.request_recall_narrowing(recall_id, ["B-1", "B-9"], requested_by="carol")
        with self.assertRaises(DomainError):
            service.request_recall_narrowing(recall_id, ["B-1"], requested_by="carol")


class RecoveryTests(unittest.TestCase):
    def test_interrupted_actions_resume_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = str(Path(tmp) / "events.jsonl")
            service = make_service(log_path)
            add_dairy_product(service)
            service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
            service.declare_recall("污染物超标", ["B-1"], actor="alice")
            # 服务在取样与通知执行前中断：不重放任何动作

            reopened = make_service(log_path)
            self.assertEqual(ActionState.PENDING, reopened.actions["act-0001"].state)
            completed = reopened.resume()
            self.assertEqual({"act-0001", "act-0002"}, set(completed))
            self.assertEqual(ActionState.DONE, reopened.actions["act-0001"].state)
            self.assertEqual(ActionState.DONE, reopened.actions["act-0002"].state)
            # 取样动作补上了取样记录，通知动作留下了通知
            evidence_ids = {item.evidence_id for item in reopened.batches["B-1"].evidence}
            self.assertIn("sample-act-0001", evidence_ids)
            self.assertEqual(1, len(reopened.notifications))

            # 再次恢复：动作已完成，不重复执行
            third = make_service(log_path)
            self.assertEqual([], third.resume())
            self.assertEqual(1, len(third.notifications))

            # 事件日志全部通过交换契约校验
            schema = load_default_schema()
            for line in Path(log_path).read_text(encoding="utf-8").splitlines():
                self.assertEqual([], validate_event(json.loads(line), schema))

    def test_crash_during_notification_continues_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = str(Path(tmp) / "events.jsonl")
            sent: list[str] = []
            crashed = {"done": False}

            def flaky_notification(service, action, at):
                if not crashed["done"]:
                    crashed["done"] = True
                    sent.append(action.action_id)  # 副作用已发生，随后崩溃
                    raise ActionInterrupted("模拟通知途中进程崩溃")
                return {"notification_id": f"ntf-{action.action_id}",
                        "recall_id": action.payload.get("recall_id"),
                        "channel": action.payload.get("channel")}

            handlers = {"notification": flaky_notification}
            service = make_service(log_path, action_handlers=handlers)
            add_dairy_product(service)
            service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
            service.declare_recall("污染物超标", ["B-1"], actor="alice")
            with self.assertRaises(ActionInterrupted):
                service.resume()
            self.assertEqual(ActionState.IN_PROGRESS, service.actions["act-0002"].state)

            reopened = make_service(log_path, action_handlers=handlers)
            reopened.resume()
            self.assertEqual(ActionState.DONE, reopened.actions["act-0002"].state)
            self.assertEqual(2, reopened.actions["act-0002"].attempts)
            # 幂等处理器保证副作用不重复
            self.assertEqual(["act-0002"], sent)
            self.assertEqual(1, len(reopened.notifications))

    def test_pending_approval_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = str(Path(tmp) / "events.jsonl")
            service = make_service(log_path)
            add_dairy_product(service)
            service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
            service.register_batch("B-2", "P-DAIRY", ts(3, 2), ts(8, 2), "F-1", actor="qa")
            recall_id = service.declare_recall("污染物超标", ["B-1", "B-2"], actor="alice")
            action_id = service.request_recall_narrowing(recall_id, ["B-1"], requested_by="alice")
            # 审批途中中断：恢复后自动动作被执行，审批动作仍在等待人工
            reopened = make_service(log_path)
            completed = reopened.resume()
            self.assertNotIn(action_id, completed)
            self.assertEqual(ActionState.PENDING, reopened.actions[action_id].state)
            reopened.approve_action(action_id, approver="bob")
            self.assertEqual({"B-1"}, reopened.recalls[recall_id].scope)


class TraceTests(unittest.TestCase):
    def test_trace_product_end_to_end(self) -> None:
        service = make_service()
        add_dairy_product(service)
        service.register_clause(
            "C-P", "GB 12693", "4.1", "process", ts(1, 1), ts(1, 1),
            categories=["dairy"],
            parameters={"temperature_c": {"min": 0, "max": 10},
                        "required_records": ["temperature"],
                        "packaging_materials": ["PET"]},
            actor="registrar",
        )
        service.register_clause(
            "C-L", "GB 2762", "1.1", "limit", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"analyte_limits": {"lead": 0.5}},
            actor="registrar",
        )
        service.register_clause(
            "C-M", "GB 5009", "1", "method", ts(1, 1), ts(1, 1),
            categories=["dairy"], parameters={"method_code": "GB-LEAD", "analytes": ["lead"]},
            actor="registrar",
        )
        for clause_id in ("C-P", "C-L", "C-M"):
            service.confirm_association(clause_id, "P-DAIRY", actor="compliance")
        service.receive_lot("LOT-A", "SUP-1", "milk_powder", actor="warehouse")
        service.register_batch("B-1", "P-DAIRY", ts(3, 1), ts(8, 1), "F-1", actor="qa")
        service.link_evidence("B-1", "material_lot", "ML-1", {"lot_id": "LOT-A"}, actor="qa")
        service.link_evidence("B-1", "process_record", "PR-1",
                              {"record_type": "temperature", "value": 5}, actor="qa")
        service.link_evidence("B-1", "packaging_material", "PK-1", {"material": "PET"}, actor="qa")
        service.link_evidence("B-1", "lab_result", "LR-1",
                              {"analyte": "lead", "value": 0.1, "method_code": "GB-LEAD"},
                              actor="lab")
        service.ship_batch("B-1", actor="logistics", at=ts(8, 2))
        recall_id = service.declare_recall("留样复检异常", ["B-1"], actor="alice")
        action_id = service.request_recall_narrowing(recall_id, [], requested_by="carol")
        service.approve_action(action_id, approver="dave")

        trace = service.trace_product("P-DAIRY")
        self.assertEqual("灭菌乳", trace["name"])
        self.assertEqual(
            {"C-P", "C-L", "C-M"}, {c["clause_id"] for c in trace["confirmed_clauses"]}
        )
        # 关联历史：每条条款都有建议与确认记录
        suggested = [e for e in trace["association_history"] if e["status"] == "suggested"]
        confirmed = [e for e in trace["association_history"] if e["status"] == "confirmed"]
        self.assertEqual(3, len(suggested))
        self.assertEqual(3, len(confirmed))

        batch_trace = trace["batches"][0]
        # 当时有效条款
        self.assertEqual("C-P", batch_trace["applicable_clauses"]["process"][0]["clause_id"])
        self.assertEqual("C-L", batch_trace["applicable_clauses"]["limit"][0]["clause_id"])
        self.assertEqual("C-M", batch_trace["applicable_clauses"]["method"][0]["clause_id"])
        # 过程证据与检验方法
        self.assertEqual(5, batch_trace["evidence"]["process_record"][0]["data"]["value"])
        self.assertEqual(
            "GB-LEAD", batch_trace["evidence"]["lab_result"][0]["data"]["method_code"]
        )
        self.assertEqual("LOT-A", batch_trace["material_lots"][0]["lot_id"])
        # 处置轨迹完整：放行 -> 出库 -> 召回 -> 范围缩小恢复
        trail = [(d["from_status"], d["to_status"]) for d in batch_trace["decisions"]]
        self.assertEqual(
            [("pending", "hold"), ("hold", "released"), ("released", "shipped"),
             ("shipped", "recalled"), ("recalled", "shipped")],
            trail,
        )
        # 每次范围调整可追溯
        self.assertEqual(1, len(trace["scope_adjustments"]))
        adjustment = trace["scope_adjustments"][0]
        self.assertEqual(recall_id, adjustment["recall_id"])
        self.assertEqual(["B-1"], adjustment["removed"])
        self.assertEqual("dave", adjustment["approved_by"])
        self.assertEqual(BatchStatus.SHIPPED, service.batches["B-1"].status)


if __name__ == "__main__":
    unittest.main()
