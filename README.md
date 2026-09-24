# 食品新标迁移与批次处置

本项目提供食品新标迁移与批次处置所需的领域事件交换约定与基础校验库。各接入方使用统一的聚合标识、事件版本和发生时间表达业务事实，避免跨系统交换时丢失来源顺序。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/food_standard_migration/contracts.py`：不依赖第三方包的基础校验器。
- `tests/test_contracts.py`：契约边界检查。

当前核心对象包括standard_revision、material_lot、product_batch、compliance_decision，事件类型包括STANDARD_REGISTERED、LOT_RECEIVED、PROCESS_EVIDENCE_CAPTURED、BATCH_REVIEWED、DISPOSITION_CHANGED。校验器负责交换层必填字段、类型、时间和版本检查，领域服务可在此约定上组合业务流程。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
