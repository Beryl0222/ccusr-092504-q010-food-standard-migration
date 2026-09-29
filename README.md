# 食品新标迁移与批次处置

本项目提供食品新标迁移与批次处置的领域服务与事件交换约定。多项新标准（污染物控制、儿童营养补充品、牛乳蛋白标签、香料指标、生物毒素检测等）同时到达时，系统登记标准条款的发布、生效、替代与适用产品，关联批次证据，按生产与上市时点分别评判，并支持批次处置、召回范围调整与监管追溯。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/food_standard_migration/contracts.py`：不依赖第三方包的基础校验器。
- `src/food_standard_migration/models.py`：领域状态模型与枚举。
- `src/food_standard_migration/store.py`：追加式事件日志（JSONL 持久化、重放恢复）。
- `src/food_standard_migration/service.py`：标准迁移与批次处置领域服务。
- `tests/test_contracts.py`：契约边界检查。
- `tests/test_service.py`：领域服务行为检查。

## 领域行为

**标准条款登记**（`register_clause`）：记录发布时间、生效时间、替代关系与适用范围（产品类别、儿童适用年龄、营养素）。登记新版时旧版自动作废；系统按适用范围为匹配产品生成关联建议——儿童适用年龄或营养素上限变化只影响匹配产品。**任何自动关联都必须由合规人员确认**（`confirm_association`）后才参与评判。

**证据关联**（`link_evidence`）：批次可关联配方版本、供应商原料批次、加工温湿度记录、包装接触材料、标签稿、抽样计划、实验室方法及结果。新证据到达后自动重审，证据充分时解除待放行状态。

**按时点选规、三类分别判断**（`evaluate_batch`）：过程要求按**生产时点**、产品限量与检测方法按**上市时点**选取当时有效的条款版本，三类独立给出通过/证据不足/不通过。旧检验方法合格并不能证明满足新标签或过程控制要求——方法条款更新后，旧方法结果记为证据不足。

**批次处置**：审核通过放行；证据不足转待放行；不通过判不合格。已出库批次保留当时决定，新发现只追加处置记录（`DISPOSITION_CHANGED`），历史决定不可改写。

**检验报告**（`submit_report`）：相同编号相同内容重送幂等丢弃，不重复放行；相同编号而内容不同则冻结其全部引用批次，由合规人员核实后解冻重审（`unfreeze_batch`）。

**物料谱系**（`fail_lot`）：原料批次不合格只沿谱系影响相关库存——在库批次转待放行，已出库批次追加处置，谱系之外批次不受影响。

**召回与范围调整**：`declare_recall` 宣布召回并调度通知动作；`request_recall_narrowing` 申请缩小范围，生成审批动作，**必须由独立于申请人的批准人批准**（`approve_action`）后范围才变化，被移除批次恢复召回前状态。每次范围调整均留痕。

**中断恢复**（`resume`）：取样、审批、通知动作以事件形式持久化。服务在动作途中中断后，用同一事件日志重建服务并调用 `resume()`：取样与通知自动重试（处理器幂等，不产生重复副作用），审批动作恢复为待人工处理状态。

**监管追溯**（`trace_product` / `trace_batch`）：从产品追到各批次当时有效条款（含替代链）、过程证据、检验方法与报告、每次处置决定和每次召回范围调整。

## 条款参数约定

- 过程要求 `process`：`temperature_c`/`humidity_pct`（`min`/`max`）、`required_records`、`packaging_materials`（允许清单）。
- 产品限量 `limit`：`analyte_limits`（污染物上限）、`nutrient_caps`（营养素上限，只影响含该营养素的产品）、`required_label_claims`（标签必备标示）。
- 检测方法 `method`：`method_code`、`analytes`、`also_accept`（可接受的等效方法）。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
