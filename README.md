# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 跨地区每日工时上限

跨地区实训同时受学校培养规则与活动所在地的每日工时上限约束。在 `/api/plans/{plan_version}` 下提供以下接口：

- **规则版本**：`PUT /regions/{code}` 注册地区（IANA 时区）；`POST /cap-rules` 创建草稿（可 `PATCH` 上限），`POST /cap-rules/{id}/transition` 执行 `published → retired → restored` 生命周期。发布新版本自动在该时刻关闭同地区旧版本的生效窗口；退役后恢复（回滚）会为同一 `rule_id` 追加一段新的生效窗口，历史窗口原样保留，评估按活动发生时刻选窗。
- **活动地点证据**：打卡 payload 通过 `location`（静止）或 `track`（移动轨迹，变更点序列）携带地点，二者互斥；`PUT /evidence/{id}` 登记 GPS、边检等凭证，引擎校验学员、地区与观察时刻是否落在活动窗口内，不符合的证据标记为 `invalid`。
- **切分与超限识别**：活动依次在位置变更点、当地日历日边界（天然兼容夏令时春进/秋退）、规则生效边界、例外有效期边界上切分；按（地区, 当地日, 规则窗口）分桶，重叠活动取并集后与上限比较，超限秒数确定性地归因到最晚开始的活动。
- **预览**：`POST /regional/preview` 评估未落库的草稿打卡（可叠加已确认活动）。
- **人工例外复核**：`POST /exceptions` 必须指定学员范围（地区/活动可选收窄）和起止期限，`cap_override_seconds` 缺省表示完全豁免；申请处于 `requested` 不生效，经 `/review` 批准后适用，可 `/revoke` 撤销。
- **统计**：`GET /regional/overview`、`GET /regional/students/{id}`、`GET /regional/statistics` 给出逐桶切片解释与按地区聚合。
- **冻结快照**：`regional_compliance` 块随冻结固化地区目录、规则版本窗口目录、例外清单与每段采用的规则（`rule_id`/`window_id`/`rule_status`/`cap_seconds`）；规则随后回滚不影响已冻结快照。
