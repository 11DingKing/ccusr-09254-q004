# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 跨地区每日工时上限

跨地区实训同时受学校培养规则（总学时要求）与所在地每日工时上限约束：

- **地区规则版本**：`POST /api/region-rules` 创建草稿，`POST /api/region-rules/{rule_id}/publish` 发布，`POST /api/region-rules/{rule_id}/retire` 退役。每个版本带有生效窗口 `[effective_from, effective_to)`；同一地区同一天若被多个已发布版本覆盖，版本号最大者生效。`POST /api/regions/{region}/rollback` 以历史版本的上限与窗口生成一个新的已发布版本，实现规则回滚。
- **活动地点证据**：`POST /api/plans/{plan_version}/locations` 记录活动自某时刻起所在的地区与 IANA 时区（可按学员细分）；移动活动在位置变更点自动分段，无证据时回落到本校（`home` 地区、方案时区）。
- **预览**：`GET /api/plans/{plan_version}/preview` 按各位置自己的时区把已确认签到切分当地自然日，合并重叠活动后识别超出每日上限的具体部分，并标注每段采用的规则版本。
- **人工例外**：`POST /api/plans/{plan_version}/exceptions` 创建的例外必须带范围（学员、地区，可选活动与当地日）和期限（`valid_from`/`valid_until`），只有在期限与范围内的超限才被豁免；`POST .../exceptions/{exception_id}/revoke` 撤销。
- **复核**：`GET /api/plans/{plan_version}/review` 列出超限发现及其豁免状态，`POST .../review` 记录复核结论（approved/dismissed）。
- **统计**：`GET /api/plans/{plan_version}/statistics` 按地区与整体汇总评估时长、超限、豁免与未结发现。
- **冻结快照**：`POST /api/plans/{plan_version}/freezes/{freeze_id}` 生成的快照包含 `regional` 部分，逐段记录当时采用的规则版本、上限与超限/豁免结果；之后的规则变更、回滚或例外调整都不会改写已冻结的解释。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；地区合规矩阵额外覆盖夏令时（回拨与跳过）、跨境移动分段、重叠活动合并计时、人工例外的范围与期限，以及规则回滚后冻结快照的稳定性；运行过程中不需要单独的数据库或网络服务。
