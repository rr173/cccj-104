# sensorpipe：传感器历史读数补算链

本工具解决"探头换过、校准系数晚几天才补录"的问题：读数先按原样入库，
校准记录随后补录或修改时，**同一个事务**里重算受影响的读数结论与小时指标，
不需要重导历史读数，也不会把无关设备/时段的报表一起改写。

与 `lineage`（血缘）、`viewctl`（视图发布）相互独立，只依赖 DuckDB 与
Python 标准库（不依赖 pytz；库内时刻都是 UTC 墙上时间 `TIMESTAMP`）。

```
读数 JSONL ──ingest──┐
                     ├─ sensor.duckdb ── detail / hourly / runs 查询
校准 JSONL ─calibrate┘
```

## 1. 数据模型

| 表 | 内容 |
| --- | --- |
| `main.sensor_readings` | 原始读数。`reading_id` 主键；沿用演示列名 `sensor_id / measured_at / temperature / battery / status`，另存规范化内容指纹 `content_sha256`。`temperature` **始终是原始温度**，修正温度只存在于结论表。 |
| `main.sensor_calibrations` | 校准记录：`calibration_id / sensor_id / valid_from / valid_to / gain / offset_value / revoked`。生效区间为 **`[valid_from, valid_to)`**；同设备未撤销记录的区间互不相交，可首尾相接。撤销不删行，置 `revoked = TRUE` 并保留原区间，便于追溯和覆盖旧区间重算。 |
| `main.sensor_reading_results` | 每条读数一条可查结论：命中的 `calibration_id`、`temperature_corrected`、`included`、`reason` 与 `hour_utc`。`reason ∈ {included, fault, missing_calibration, out_of_range}`。 |
| `main.sensor_hourly` | 只由 `included` 样本构成的小时指标：`sensor_id, hour_utc, samples, avg_temperature, min_battery`。空组删除，不留旧行。 |
| `main.sensor_processing_runs` | 每次成功处理的影响范围摘要（重算/变化/删除计数 + `details_json`）；`submitted_at` 为 UTC 墙上时间。 |
| `main.sensor_run_readings` / `sensor_run_hours` | 本次被重算的每条读数、每个小时的 before/after（小时含 `created/updated/deleted/unchanged`）。 |

`revision` 列只在**该行业务内容实际变化**时 +1。它是"无关记录没有被改写"
的可观察证据：回归测试会比较未受影响设备/小时的 revision 是否保持不变。

### 处理结论规则

1. `status <> 'OK'` → `fault`（即使命中校准也记录修正值，便于追溯，但不进指标）；
2. 未命中任何生效校准 → `missing_calibration`；
3. 修正温度 = `temperature * gain + offset`（结果按 12 位小数舍入以抹掉二进制尾差），
   不在闭区间 `[-50, 80]` → `out_of_range`；
4. 其余 → `included`。

小时指标按 **UTC** 整点划分，`samples = COUNT(*)`、`avg_temperature = AVG(修正温度)`、
`min_battery = MIN(battery)`。只有 `included` 样本参与。

## 2. 输入格式

### 读数（JSON Lines）

```json
{"reading_id":"s1-r1","sensor_id":"s1","measured_at":"2026-03-01T09:50:00Z","temperature":20.0,"battery":91.0,"status":"OK"}
```

* 6 个字段必填，**不允许未知字段**（防止拼错字段被静默忽略）；
* `measured_at` 必须是**带时区**的 ISO-8601（`Z` 或 `+08:00`），解析后立即换算成 UTC；
* `temperature` / `battery` 必须是有限数值（拒绝 NaN/Infinity/布尔/字符串）；
* `status` 非空字符串，落库统一大写；当前语义只认 `OK` 为正常，其它都按故障处理；
* `reading_id` 全库唯一：同号同内容再次送入不增加样本（**内容比较前先按 UTC 归一化时刻**，
  所以 `2026-03-01T10:00:00Z` 与 `2026-03-01T18:00:00+08:00` 视为同一内容）；
  同号不同内容 → `reading_conflict`，**整批撤销**（包括同批里其它本来合法的新读数）。

### 校准（JSON Lines，支持一次提交多条）

新增/修改/恢复：

```json
{"calibration_id":"c1","sensor_id":"s1","valid_from":"2026-03-01T10:00:00Z","valid_to":"2026-03-01T11:00:00Z","gain":1.1,"offset":0.5}
```

撤销：

```json
{"calibration_id":"c1","revoke":true}
```

* `valid_to` 必须严格晚于 `valid_from`；
* `gain` / `offset` 必须是有限数值；
* 同设备生效区间相交 → `calibration_overlap`，**整批回滚**（首尾相接允许）；
* 同一批内对同一 `calibration_id` 多次写入以后出现者为准，因此
  **"先撤销、再重录"可以放在同一次操作里**；
* 原样重复提交一条已存在且内容相同的校准是幂等的：不重算、不提升
  `revision`，也不出现在影响范围里；
* 已有 `calibration_id` 不能改到另一台设备（`calibration_sensor_mismatch`），
  请撤销旧记录并新建；
* 撤销不存在的 id → `unknown_calibration`。

## 3. 命令行入口

```bash
python3 -I src/sensorpipe/cli.py init      --db /path/sensor.duckdb
python3 -I src/sensorpipe/cli.py ingest    --db /path/sensor.duckdb --file readings.jsonl
python3 -I src/sensorpipe/cli.py calibrate --db /path/sensor.duckdb --file cals.jsonl
# 也可以用 --stdin 从标准输入读取
python3 -I src/sensorpipe/cli.py detail    --db /path/sensor.duckdb [--reading-id R] [--sensor-id S] [--reason X] [--status OK]
python3 -I src/sensorpipe/cli.py hourly    --db /path/sensor.duckdb [--sensor-id S]
python3 -I src/sensorpipe/cli.py runs      --db /path/sensor.duckdb [--limit N]
python3 -I src/sensorpipe/cli.py status    --db /path/sensor.duckdb
python3 -I src/sensorpipe/cli.py rebuild   --db /path/sensor.duckdb   # 恢复/对照用全量重算
```

退出码：`0` 成功；`2` 领域拒绝（校验/冲突/相交/未初始化）；`3` IO/用法错误。
所有失败都输出结构化 JSON，逐条说明 `code / message`（输入行错误还带 `line`、`field`）。

### 影响范围

`ingest` 与 `calibrate` 的返回（以及 `runs` 查询）包含：

* `affected_readings`：本次被重新计算结论的 `reading_id`；
* `affected_hours`：本次被重新计算的小时及 `change`（`created/updated/deleted/unchanged`）；
* `affected_windows`：本次触碰的设备时间窗口（**旧区间 ∪ 新区间**）；
* `readings_recomputed / readings_changed / hours_changed / hours_deleted` 计数。

修改校准只重算"该 `calibration_id` 改动前区间 ∪ 改动后区间"内的读数；
撤销记录保留原区间，因此撤销也能正确覆盖旧区间。其它设备、不相交时段不改写。

## 4. 拒绝的处理方式

| 场景 | 错误码 | 处理 |
| --- | --- | --- |
| 读数缺字段/未知字段/时刻无时区/数值非有限 | `input_error` | 逐行报出，整批不入库 |
| 同 `reading_id` 内容不同 | `reading_conflict` | 整批撤销；库内原读数不变 |
| 校准 `valid_to <= valid_from` | `input_error` | 整批拒绝 |
| 同设备生效区间相交 | `calibration_overlap` | 整批回滚 |
| 撤销不存在的校准 | `unknown_calibration` | 整批拒绝 |
| 改校准所属设备 | `calibration_sensor_mismatch` | 整批拒绝 |
| 库未初始化 / 结构不兼容 | `not_initialized` / `incompatible_schema` | 拒绝并提示 `init` 或迁移 |
| 库文件不存在/无法打开 | `io_error` | 退出码 3 |

**重要**：读数文件里只要有一行解析失败，整份文件都不导入（不做部分导入），
修正后重传即可。

## 5. 缺少校准时怎么补救

`detail` 会把这些读数标为 `missing_calibration`，小时报表里没有它们。
正确做法是**补录覆盖该时段的校准**，而不是重导读数：

```json
{"calibration_id":"s1-2026-03-01-morning","sensor_id":"s1",
 "valid_from":"2026-03-01T09:00:00Z","valid_to":"2026-03-01T11:00:00Z",
 "gain":1.1,"offset":0.5}
```

`calibrate` 会立刻重算该时段读数（原来 `missing_calibration` 的样本可能变成
`included`、进入小时指标），并更新受影响小时；不需要调用 `ingest`。

若同一时段已有校准但需要调整：直接用同一个 `calibration_id` 提交新系数/边界；
若完全作废：`{"calibration_id":"...","revoke":true}`。撤销后若该小时最后一个
样本退出，小时行会被删除；之后重录同 id 会重建该小时。

## 6. 事务与崩溃恢复

`ingest` / `calibrate` 的全部写入（原始读数、校准、明细结论、小时指标、
影响范围登记）都在**一个 DuckDB 事务**里；任何一步失败或进程被杀，
DuckDB 通过 WAL 回滚整个事务。因此重启后不会出现"校准已更新、报表还用旧值"
的中间状态。

回归里用 `demo/sensor_history/crash_child.py` 在事务内（校准与明细已写、
小时未写）真实 `SIGKILL` 进程，重开库逐层比对；再正常执行同一操作确认可继续。

`rebuild` 只在恢复/对照时使用：从原始读数 + 校准全量重算结论与小时，
并在 `sensor_processing_runs` 留一条 `rebuild_all` 记录。

## 7. 演示与回归

```bash
python3 -I demo/sensor_history/run_demo.py       # 可重跑的小数据演示
python3 -I tools/sensor_oracle.py --db DB --check  # 独立全量对照
bash scripts/run_all.sh                         # 全仓库一键验证
```

演示覆盖：先收读数再补历史校准、边界两侧命中相邻系数、同一瞬间两种时区表示、
缩短区间使样本退出并删除最后一个样本所在小时、撤销后重录恢复、重传与冲突、
重叠拒绝、事务中途真实崩溃恢复。预期均值均按小数据手算。

`tools/sensor_oracle.py` 是一个**不复用** `sensorpipe` 区间查询/回填/小时重算
代码的全量纯 Python 实现（线性扫描校准区间、重新聚合），逐条比对
`sensor_reading_results` 与 `sensor_hourly`；回归用它证明"算对受影响部分"，
并配合 revision 检查证明"无关记录没有被改写"。

## 8. 与既有工具的关系

* `demo/sensor/run_demo.py` 与 `viewctl` 仍可照常运行：它使用自己的
  `main.sensor_readings`（无 `reading_id`）。本链路要求 `reading_id` 主键，
  `init` 遇到旧结构会明确报 `incompatible_schema`，请使用独立库文件。
* 血缘工具（`lineage`）与视图发布（`viewctl`）的 CLI 和验证入口保持不变。
