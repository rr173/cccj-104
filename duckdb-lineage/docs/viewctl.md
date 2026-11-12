# viewctl：DuckDB 视图组版本化发布工具

把一组视图定义作为 **一个版本** 落到指定的 DuckDB 库文件里：预览先给出
差异计划并在事务里试装验证，应用把全部视图调整与版本登记放进同一事务。
本工具与血缘抽取器（`src/lineage/`）相互独立，血缘命令不受影响。

```bash
# 查看库内登记状态（版本号 / 管理集 / 发布历史）
python3 -I src/viewctl/cli.py status  --db sensor.duckdb

# 预览：候选 vs 已登记版本的差异计划 + 事务内试装（不改动库）
python3 -I src/viewctl/cli.py preview --db sensor.duckdb --assets demo/sensor/assets_v1

# 应用：核对预览令牌后，在同一事务里完成全部调整与版本登记
python3 -I src/viewctl/cli.py apply   --db sensor.duckdb --assets demo/sensor/assets_v1 \
  --expect-db-version 0 \
  --expect-source-digest sha256:<预览输出的指纹>
```

退出码：`0`=成功；`2`=领域错误（校验 / 绑定 / 过期等，见下文错误码）；
`3`=用法或 IO 错误。所有输出均为 JSON。

## 1. 资产目录与清单格式

```
assets/
├── manifest.json       # 清单：本工具负责维护的视图名 -> 查询文件
└── queries/*.sql       # 每个视图恰好一条 SELECT
```

```json
{
  "schema": "main",
  "views": {
    "sensor_clean": "queries/10_sensor_clean.sql",
    "sensor_hourly": "queries/20_sensor_hourly.sql",
    "sensor_alerts": "queries/30_sensor_alerts.sql"
  }
}
```

* `schema`（可选，默认 `main`）：视图落库的 schema；视图名也可写成
  `analytics.sensor_hourly` 单独指定。
* `views`（必填）：视图名 -> 查询文件（相对清单所在目录，必须位于资产
  目录内）。允许为 `{}`，表示本版本撤下全部已登记视图。
* 视图名大小写不敏感，规范化为小写 `schema.name` 作为身份；清单内
  （含 schema 限定后）重名即 `duplicate_name`。
* `_viewctl` 是注册表保留 schema，不能作为视图落点。
* 每个查询文件必须是 UTF-8 的 **单条 SELECT**（可含 CTE / UNION ALL），
  不允许 DDL / DML，否则 `query_error`。
* **清单顺序不代表执行顺序。** 执行顺序由视图间引用关系拓扑决定
  （被引用的一层先建）；CTE 名与表别名是查询内部局部名，不会被误认为
  数据库对象，也不会据此制造循环。

## 2. 管理模型：注册表就在库文件里

工具在目标库的 `_viewctl` schema 里维护注册表（首次成功应用时创建）：

| 表 | 内容 |
| --- | --- |
| `_viewctl.meta` | `version`：单调递增的发布版本号（可直接 SQL 查询观察） |
| `_viewctl.objects` | 当前由本工具管理的视图及其定义文本、内容哈希 |
| `_viewctl.history` | 每次成功发布的审计记录（版本、时间、操作摘要） |

* **管理集合 = 注册表内容。** 发布时碰巧存在于库里的其它视图/表一律是
  外部对象：不收编、不覆盖，也绝不因候选清单缺少某名字而删除它们。
* 撤下集合只来自注册表（上一版本管理的、候选清单不再声明的名字）。
* 数据库重开后管理集照样可识别（注册表持久化在库文件里），
  `status` 与 `preview` 都以此为据。
* 候选视图可以引用库里已有的基础表和其它视图；这些外部对象不会被
  本批迁移修改。候选名字若撞上非本工具管理的对象，报
  `name_collision` 并拒绝。

## 3. 预览输出什么

`preview` 先做静态检查，再在事务里 **试装** 候选并让 DuckDB 绑定每个
最终视图，最后无论成败一律 `ROLLBACK`——预览不在库内留下任何变更
（包括不创建注册表）。输出示例：

```json
{
  "status": "ok",
  "db_version": 1,
  "source_digest": "sha256:…",
  "token": { "db_version": 1, "source_digest": "sha256:…" },
  "plan": {
    "create":  ["main.sensor_cleaned"],
    "replace": ["main.sensor_hourly"],
    "drop":    ["main.sensor_clean"],
    "keep":    ["main.sensor_alerts"],
    "order": [
      {"seq": 1, "action": "create",  "name": "main.sensor_cleaned"},
      {"seq": 2, "action": "replace", "name": "main.sensor_hourly"},
      {"seq": 3, "action": "drop",    "name": "main.sensor_clean"}
    ]
  },
  "checks": {
    "static": "passed",
    "trial_install": "passed (rolled back)",
    "bound_views": ["main.sensor_cleaned", "main.sensor_hourly", "main.sensor_alerts"]
  }
}
```

* `plan`：候选与库内已登记版本的差异——新建 / 替换（定义文本不同）/
  撤下 / 保留，以及拓扑执行顺序（先建被引用层，撤下放在最后）。
* `checks.bound_views`：试装后实际让 DuckDB 绑定过的视图（含引用了
  变更对象的存量外部视图）。列不存在、类型不兼容等问题在这一步暴露，
  错误载荷里的 `asset` 指出对应资产文件。
* `token`：应用所需的预览令牌（见下一节）。

## 4. 预览 -> 应用：版本核对（可观察，不靠进程内变量）

`apply` 必须携带最近一次 `preview` 输出的令牌，两种方式等价：

```bash
--expect-db-version N --expect-source-digest sha256:…
--token-file token.json        # 内容即预览输出的 token 对象
```

应用前核对两类变化，任一不符即拒绝（退出码 2），不改动任何内容：

* **`stale_source`**：预览后查询文件被改动。重新计算资产指纹与令牌
  比对（纯文件核对，不依赖任何记忆状态）。
* **`stale_database`**：预览后库内登记版本被推进（例如另一份工具实例
  先发布了更新）。版本号落库于 `_viewctl.meta`，事务内再次读取比对，
  可直接 `SELECT value FROM _viewctl.meta WHERE key='version'` 观察。

被拒绝后重新 `preview` 拿到新令牌即可正常发布。

## 5. 应用的事务保证

正式应用把 **全部视图调整 + 注册表更新 + 版本号推进** 放进同一事务：
任一步失败（DDL 错误、绑定失败、版本核对失败）整体 `ROLLBACK`，库回
到操作前的定义与登记内容。基础表数据本就不在操作范围内，不会被改变。
候选与登记内容完全一致时应用是空操作（`note=no_changes`，不推进版本）。

## 6. 失败后的处理方式

所有失败都以结构化错误载荷返回（`status=error` + `errors` 数组），
库内不留半新半旧状态。常见错误码：

| code | 含义与处理 |
| --- | --- |
| `manifest_error` / `duplicate_name` | 清单结构或命名问题：改清单 |
| `missing_resource` | 清单声明的查询文件缺失/不可读：补齐文件 |
| `query_error` | 查询文件不是单条 SELECT 或含 DDL/DML：改查询 |
| `cycle` | 候选视图循环引用（含自引用），`chain` 给出环：断开引用环 |
| `drop_blocked` | 撤下对象仍被保留视图或外部视图引用，`referenced_by[].chain` 给出依赖链（前者引用后者）：先调整引用方 |
| `name_collision` | 候选名撞上非本工具管理的对象：改名或人工移除对方 |
| `object_type_conflict` | 已登记对象在库内变成了表等其它类型：人工处理 |
| `bind_error` | 试装/发布时 DuckDB 绑定失败（列不存在、类型不兼容等），`asset` 指出资产：修查询后重新预览 |
| `ddl_error` | 其它 DDL 执行失败：查看 message，修正后重新预览 |
| `stale_source` / `stale_database` | 预览已过期：重新 `preview` 再 `apply` |
| `io_error` / `usage_error` | 库文件不存在、参数缺失等 |

注意：DuckDB 文件同一时刻只允许一个读写连接；若库被其它进程占用，
打开会失败并报 `io_error`。

## 7. 演示与回归

```bash
python3 -I demo/sensor/run_demo.py     # 传感器三层视图两版发布全流程
python3 -I scripts/run_pytest.py tests/test_viewctl.py -q
```

演示数据为传感器测量明细（`sensor_readings` 基础表），经
`sensor_clean`（清洗）→ `sensor_hourly`（小时汇总）→ `sensor_alerts`
（告警）逐层计算；两版发布（首装 → 上游更名 + 阈值调整 + 撤下旧对象）
前后查询到的业务数值都与脚本内硬编码的独立预期逐项比对。
