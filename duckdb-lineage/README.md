# DuckDB SQL 列级血缘抽取器 + 视图组发布工具 + 传感器历史补算链 + 结果对账 + 关系数据裁取

本仓库包含五个相互独立的工具：

* **lineage**：面向分析数据集 *变更审查* 的列级血缘工具（下文）。
* **viewctl**：把一组视图定义作为一个版本发布到 DuckDB 库文件的本地
  迁移工具（预览试装 / 事务应用 / 版本核对），见
  [`docs/viewctl.md`](docs/viewctl.md) 与 `demo/sensor/run_demo.py`。
* **sensorpipe**：传感器历史读数的本地补算链——读数先入库，校准晚补录或
  修改时在同一事务里重算受影响读数结论与小时指标，见
  [`docs/sensor_history.md`](docs/sensor_history.md) 与
  `demo/sensor_history/run_demo.py`。
* **sqlrecon**：针对 DuckDB 文件的 SQL 改写 **结果对账** 工具——按命名检查
  清单实际执行新旧两条只读查询，按多重集合比较结果（忽略顺序、保留重复行、
  业务键分组、一对一最大匹配、精确数值与容差），见
  [`docs/sqlrecon.md`](docs/sqlrecon.md) 与 `demo/recon/run_demo.py`。
* **relslice**：关系数据 **裁取** 工具——按 JSON 配方（基础表/身份列/
  子表->父表引用/起始身份）从少量起始记录出发，只沿子表到父表方向补入
  必需关联记录，产出可交给同事直接查询的 DuckDB 小库与追查说明，见
  [`docs/relslice.md`](docs/relslice.md) 与 `demo/relslice/run_demo.py`。

---

面向分析数据集 **变更审查** 的列级血缘工具。Python 编写，SQL 语法树交给
[SQLGlot](https://github.com/tobymao/sqlglot)（DuckDB 方言）解析，输出确定性
JSON 血缘图，并把每个输出字段的依赖按 **变化原因** 严格区分为三类：

* **value_sources**：参与字段 *值计算* 的基础列（算术、聚合参数、投影列）；
* **control_sources**：只影响 *哪些行能匹配/被选中/被分组/被排序* 的来源
  （WHERE / HAVING / JOIN ON / QUALIFY / GROUP BY、CASE 条件、窗口
  PARTITION BY/ORDER BY、EXISTS/IN 的匹配/比较列）；
* **existence_sources**：只影响 *行集/子结果是否为空* 的来源，即
  `row_set:catalog.schema.table` 标记（EXISTS 依赖表是否还有行；标量子查询
  在输入集合为空时让结果变成 NULL）。

基础列采用 `catalog.schema.table.column` 稳定身份；`COUNT(*)` 等无列聚合记为
行集标记，不伪造全列依赖。三类原因不靠并桶来让边数"看起来完整"：只出现在
`EXISTS` 投影里的列是占位，不进入任何桶。

## 目录结构

```
duckdb-lineage/
├── catalog/catalog.json          # 表结构目录（catalog/schema/table/column）
├── sql/                          # 20 个可执行样例（01~18）
├── expected/expected_lineage.json# 手工标注的期望依赖集合
├── repro/                        # 缺陷复现 + 修复后断言（DuckDB 因果验证）
├── src/lineage/                  # 抽取器源码
│   ├── analyzer.py               # 主编排：解析/前置检查/作用域/基数语义/UNION
│   ├── scope.py                  # 作用域链、CTE 遮蔽、相关绑定、三类子查询语义
│   ├── expressions.py            # 表达式 value/control/existence 分桶、CASE/窗口/EXISTS
│   ├── positions.py              # token 行列定位
│   ├── catalog.py / model.py / graph.py / errors.py / cli.py
├── src/viewctl/                  # 视图组版本化发布工具（与抽取器独立）
│   ├── manifest.py               # 资产清单加载、源指纹
│   ├── refs.py                   # 对象引用提取（CTE/别名感知）、拓扑排序
│   ├── registry.py               # 库内注册表（版本号/管理集/历史）
│   ├── planner.py / core.py / cli.py / errors.py
├── src/sensorpipe/               # 传感器历史补算链（与上面两者独立）
│   ├── inputs.py                 # JSONL 逐行解析与字段校验
│   ├── db.py                     # 表结构（原始读数/校准/明细/小时/影响范围）
│   ├── core.py                   # 事务化导入/校准回补/小时重算/影响登记
│   └── cli.py                    # init/ingest/calibrate/detail/hourly/runs/rebuild/status
├── src/sqlrecon/                 # SQL 改写结果对账（与上面三者独立）
│   ├── guard.py                  # 单条只读查询静态护栏（写/多语句/外部文件/随机表达式）
│   ├── manifest.py               # 命名检查清单加载与规则校验
│   ├── runner.py                 # 只读连接、同一读取事务执行、列结构核对
│   ├── compare.py                # 分组、精确/容差标量、Hopcroft-Karp 一对一最大匹配
│   ├── encoding.py               # 不丢精度的自描述值编码（大整数/DECIMAL/NaN/Inf）
│   └── engine.py / cli.py / errors.py
├── src/relslice/                 # 关系数据裁取（与上面四者独立）
│   ├── recipe.py                 # 配方加载与结构校验（表/身份列/引用/起点，带位置报错）
│   ├── source.py                 # 只读源库：结构核对、身份唯一性/空值校验、按身份取行
│   ├── closure.py                # 纯集合闭包算法（不依赖 duckdb）：单向追踪、环收敛、预算
│   ├── writer.py                 # 目标库临时文件写入 + 硬链接原子发布（拒绝覆盖）
│   └── engine.py / cli.py / errors.py
├── demo/relslice/                # 裁取演示：建库/配方/独立集合算法核对/全场景矩阵
├── demo/sensor/                  # 传感器三层视图两版发布演示（独立预期核对）
├── demo/sensor_history/          # 历史补算演示（含真实 SIGKILL 恢复检查）
├── demo/recon/                   # 结果对账演示（建库/26 项清单/查询对/一键演示）
├── demo/relslice/                # 关系裁取演示（建库/配方/独立集合算法核对）
├── tools/sensor_oracle.py        # 不复用线上逻辑的独立全量对照计算
├── tools/exhaustive_recon.py     # 独立穷举配对核对（不调用线上最大匹配函数）
├── scripts/                      # DuckDB 执行、期望核对、pytest、一键验证
├── tests/test_lineage.py         # 32 个血缘单元测试
├── tests/test_viewctl.py         # 34 个发布工具回归测试（实际开库核对）
├── tests/test_sensorpipe.py      # 17 个补算链回归测试（实际开库核对）
├── tests/test_sqlrecon.py        # 78 个对账回归测试（实际开库 + 穷举/确定性核对）
├── tests/test_relslice.py        # 34 个裁取回归测试（纯集合 + 实际开库核对）
├── docs/dialect_and_errors.md    # 方言范围、三类依赖语义与错误定位
├── docs/viewctl.md               # 清单格式、预览内容、版本核对与失败处理
├── docs/sensor_history.md        # 输入格式、拒绝处理、缺校准补救、事务保证
├── docs/sqlrecon.md              # 对账规则格式、差异读法、三类运行结论
├── docs/relslice.md              # 裁取配方、闭包语义、预算、追查说明与失败处理
└── docs/root_cause.md            # 本次缺陷的根因说明
```

## 安装

```bash
python3 -m pip install -r requirements.txt
```

## 使用

所有命令均可在隔离模式 `python3 -I` 下运行（CLI 会自行补回 user site）。

```bash
# 1) 完整 JSON 血缘图（节点/边，边带表达式路径）
python3 -I src/lineage/cli.py graph \
  --catalog catalog/catalog.json --sql sql/06_case_window.sql

# 2) 字段级原始结构（每字段 value/control/existence 三类来源 + paths）
python3 -I src/lineage/cli.py graph --raw \
  --catalog catalog/catalog.json --sql sql/09_filter_only_column.sql

# 3) 字段依赖查询
python3 -I src/lineage/cli.py deps \
  --catalog catalog/catalog.json --sql sql/09_filter_only_column.sql \
  --field customer_name

# 4) 基础列（或整表/行集）变动的下游影响查询
python3 -I src/lineage/cli.py impact \
  --catalog catalog/catalog.json --sql sql/06_case_window.sql \
  --ref shop.main.customers.city

# 5) 批量分析整个目录
python3 -I src/lineage/cli.py batch \
  --catalog catalog/catalog.json --sql-dir sql

# 6) 视图组版本化发布（viewctl，独立于血缘命令）
python3 -I src/viewctl/cli.py preview --db /path/sensor.duckdb \
  --assets demo/sensor/assets_v1
python3 -I demo/sensor/run_demo.py   # 端到端演示（建库/播种/预览/发布/核对）

# 7) 传感器历史读数补算（sensorpipe，独立于上面两个工具）
python3 -I src/sensorpipe/cli.py init --db /tmp/sensor.duckdb
python3 -I src/sensorpipe/cli.py ingest --db /tmp/sensor.duckdb \
  --file demo/sensor_history/input/readings.jsonl
python3 -I src/sensorpipe/cli.py calibrate --db /tmp/sensor.duckdb \
  --file demo/sensor_history/input/calibrations_step1.jsonl
python3 -I src/sensorpipe/cli.py detail  --db /tmp/sensor.duckdb
python3 -I src/sensorpipe/cli.py hourly  --db /tmp/sensor.duckdb
python3 -I demo/sensor_history/run_demo.py        # 端到端演示（含崩溃恢复）
python3 -I tools/sensor_oracle.py --db DB --check # 独立全量对照

# 8) SQL 改写结果对账（sqlrecon，独立于上面三个工具）
python3 -I demo/recon/build_db.py                 # 构建演示库
python3 -I src/sqlrecon/cli.py run \
  --db demo/recon/recon.duckdb \
  --manifest demo/recon/checks.json               # 退出码 0/1/2 = 相同/差异/检查错误
python3 -I demo/recon/run_demo.py                 # 端到端演示（含穷举交叉核对）

# 9) 关系数据裁取（relslice，独立于上面四个工具）
python3 -I demo/relslice/build_db.py              # 构建演示业务库
python3 -I src/relslice/cli.py extract \
  --source demo/relslice/business.duckdb \
  --recipe demo/relslice/recipe.json \
  --target /tmp/extract.duckdb --budget 500       # 退出码 0/1/2 = 成功/裁取失败/命令错误
python3 -I demo/relslice/run_demo.py              # 端到端演示（独立集合算法逐表核对）
```

### 输出片段（图格式）

```json
{
  "from": "shop.main.customers.city",
  "to": "<top_query>.running_amount",
  "type": "control",
  "paths": [["projection", "[2]", "window", "partition_by", "[0]", "column", "city"]]
}
```

`paths` 记录来源经过的表达式位置，审查时可还原"这个基础列是在哪一层、哪个
子句影响到该字段"。

## 验证方式（两步 + 自动化）

```bash
bash scripts/run_all.sh
```

1. **DuckDB 先执行**：`scripts/run_duckdb_validation.py` 依据 catalog 建内存库、
   插入确定性数据，真实执行全部受支持样例（递归 CTE/动态 SQL/歧义列单独说明）。
2. **核对手工标注**：`scripts/check_expected.py` 把抽取结果与
   `expected/expected_lineage.json` 的人工依赖集合逐项断言。
3. `scripts/run_pytest.py` 跑全部单元测试（血缘 + viewctl + sensorpipe）；
   并对每个 SQL 重复运行比对 MD5，证明 JSON 输出字节级确定。
4. 两个传感器演示：`demo/sensor/run_demo.py`（视图发布）与
   `demo/sensor_history/run_demo.py`（历史补算 + 真实中断恢复）。

## 样例覆盖矩阵

| 样例 | 验证点 |
| --- | --- |
| 01_cte_shadow | CTE 遮蔽同名基础表，内层过滤穿透为 control |
| 02_two_layer_alias | 两层别名重定基，禁止 `d.oid` 拼接假边 |
| 03_star | 裸 `*` 与 `c.*` 星号展开（15 列） |
| 04_correlated_exists | 相关 EXISTS，外层绑定穿透，匹配列进 control、行集进 existence |
| 05_union_all | 分支列名不同的 UNION ALL 按位置合并 |
| 06_case_window | CASE 条件/分支分桶、窗口 PARTITION/ORDER、COUNT(*) 行集 |
| 07_ambiguous | 未限定同名列歧义诊断（候选 + 行列号） |
| 08a/08b/08c | 递归 CTE / 动态 SQL / 解析失败 → 明确 unsupported |
| 09_filter_only | **仅筛选不参与值计算** 的列，证明 value/control 可分 |
| 10_having_derived | HAVING 引用派生列，递归到基础身份 |
| 11_named_window | 命名窗口 WINDOW 子句 + 跨 catalog.schema 限定表名 |
| 12_filtered_count | 筛选后的 CTE 被外层 COUNT(*) 消费：不引用内层列，status 仍为 control |
| 13_join_name_and_scalar_role | 连接只取客户姓名仍保留内层筛选；标量子查询 amount=value/status=control 不串桶 |
| 14_exists_row_set | 只选常量的 EXISTS：删空基础表的影响经行集标记追到 existence |
| 15_exists_projection_placeholder | EXISTS 投影列只是占位，三桶皆无；匹配=control、行集=existence |
| 16_not_exists | NOT EXISTS 与 EXISTS 对称：匹配=control、行集=existence |
| 17_scalar_constant_with_predicate | 标量常量 + 匹配谓词：删最后匹配行 1→NULL，行集=existence |
| 18_empty_input_aggregate | 空输入无分组 COUNT 恒一行，内层筛选不扩散到外层行选择 |

### 缺陷复现（可直接运行）

```bash
# 1) 行存在性 / 派生筛选上带
python3 -I repro/repro_row_existence.py
# 2) EXISTS 投影占位、标量 NULL、空输入聚合（三类原因分开）
python3 -I repro/repro_exists_scalar_aggregate.py
```

`repro_exists_scalar_aggregate.py` 用 DuckDB 建小数据，逐次删除/恢复匹配记录，
并单独修改只出现在 `EXISTS` 投影里的 `status`/`amount`，记录真实结果是否改变；
再断言工具的依赖与 impact 类型与之一致，并回归派生筛选上带与非法 SQL 拒绝。
简明根因见 [`docs/root_cause.md`](docs/root_cause.md)，完整语义规则见
[`docs/dialect_and_errors.md`](docs/dialect_and_errors.md) 第 3 节。

方言边界、unsupported 清单与错误定位原理见
[`docs/dialect_and_errors.md`](docs/dialect_and_errors.md)。
