# sqlrecon：DuckDB SQL 改写结果对账

血缘图解释字段 *来自哪里*，但评审一段改写后的查询时，真正要回答的是：
**新旧两条 SQL 实际跑出来的结果是否保持了约定的结果？** `sqlrecon` 直接对
DuckDB 库文件做这件事——它实际执行两条查询、按多重集合比较结果，而不是分析
SQL 文字或只对比列结构。

- 只开 **只读连接**，每项检查的两条查询在 **同一读取事务** 里执行，看到同一
  数据快照；
- 比较 **忽略返回顺序、保留重复行数**；
- 业务键可分组、也可不分组（整表当一组）；键不是唯一键，同键可有多行，
  **NULL 键正常成组**；
- 组内用 **一对一最大匹配**（Hopcroft–Karp），一行不能复用抵多行；多个非键
  列必须由同一对行同时满足；
- 整数 / DECIMAL 全程精确十进制，**绝不先转 float**；特殊浮点按语义比较；
- 名称、类型、语句限制、执行错误与真实数据差异 **分开报告**，单项失败不影响
  其他检查。

## 1. 清单格式

清单是一个 JSON 文件，根对象含非空数组 `checks`。每个检查项：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 检查项名称，清单内唯一 |
| `old_sql` / `new_sql` | 二选一 | 内联 SQL 字符串 |
| `old_sql_file` / `new_sql_file` | 二选一 | 相对清单目录的 `.sql` 文件 |
| `keys` | 否 | 业务键列名数组；省略或空数组 = 不分组，整表当一组 |
| `tolerance` | 否 | 数值列容差数组，每项 `{"column", "abs", "rel"}` |
| `description` | 否 | 人读说明，原样进入报告 |

规则约束（加载期即拒绝，错误码 `check_manifest_invalid`）：

- `abs`、`rel` 必须是 **有限、非负** 的 JSON 数值（不接受 NaN/Inf/字符串/负数）；
- 键列始终精确比较，**不能** 声明容差；同一列最多一条容差；键列/容差列不许重名；
- 键列、容差列是否真实存在于查询输出，要等执行后核对，找不到报
  `schema_mismatch`（原因 `unknown_named_column`）。

容差判定式（仅作用于非键数值列，键列永远精确）：

```
|x - y| <= abs + rel * max(|x|, |y|)
```

边界包含：差值恰好等于阈值判相等。整数与 DECIMAL 用 `Decimal` 精确十进制
计算该式；FLOAT/DOUBLE 用 IEEE 浮点。

### 清单示例

```json
{
  "checks": [
    {
      "name": "hourly_rewrite",
      "keys": ["sensor_id", "hour_utc"],
      "old_sql_file": "queries/10_hourly_nested.sql",
      "new_sql_file": "queries/10_hourly_join.sql",
      "tolerance": [{"column": "avg_temperature", "abs": 1e-9, "rel": 1e-12}]
    },
    {
      "name": "whole_table_no_keys",
      "old_sql": "SELECT sensor_id, temperature FROM sensor_readings",
      "new_sql": "SELECT sensor_id, temperature FROM sensor_readings ORDER BY 1 DESC"
    }
  ]
}
```

## 2. 运行与三类结论

```bash
python3 -I src/sqlrecon/cli.py run \
  --db demo/recon/recon.duckdb \
  --manifest demo/recon/checks.json \
  --output report.json
```

报告顶层 `overall_status` 与进程退出码：

| overall_status | 退出码 | 含义 |
| --- | --- | --- |
| `equal` | 0 | 全部检查的结果多重集合一致 |
| `difference` | 1 | 所有检查都成功执行，但至少一项存在 **真实数据差异** |
| `error` | 2 | 至少一项 **检查无法执行或无法比较**（清单/SQL 限制/列结构/执行失败）；可能同时有数据差异 |
| — | 3 | 命令级用法或 IO 错误（清单/库文件打不开） |

每个检查项独立给出 `status`（`equal` / `difference` / `error`）。一项报错后
其余项照常执行；查询报错、返回空结果、前一项的输出都不会被当成通过。

### 检查错误的错误码（与数据差异分开）

| code | 触发 |
| --- | --- |
| `check_manifest_invalid` | 清单结构/取值不合法 |
| `sql_rejected` | 静态护栏拒绝（多语句、写操作、外部文件、非确定性表达式、非 SELECT 根等） |
| `schema_mismatch` | 列数/列名/顺序/DuckDB 逻辑类型不符、重名列、找不到的键列或规则列、容差配在非数值列 |
| `sql_execution_error` | 通过静态检查但 DuckDB 执行失败（缺表/缺列/类型错等） |
| `database_open_error` | 库文件缺失或无法只读打开 |

## 3. 查询安全约束

只放行 **库内表和视图上的单条只读查询**，支持 WITH 与集合查询
（UNION/INTERSECT/EXCEPT）。判定基于完整 AST，**不只看开头是不是 SELECT**：

- 拒绝多语句；根节点必须是 SELECT / UNION / INTERSECT / EXCEPT；
- 拒绝任何 DML/DDL/事务/连接管理/`PRAGMA`/`SET`/`CALL`/`COPY`/`SHOW` 等节点，
  **包括藏在 WITH 的 CTE 体内的 `UPDATE/DELETE/INSERT ... RETURNING`**；
- 拒绝读外部文件的表函数（`read_csv*`/`read_json*`/`parquet_scan`/`glob`/
  `*_scan` 等）与字符串字面量关系（`FROM 'x.parquet'`）；
- 拒绝每次运行会变的表达式：`random`/`rand`、`uuid`、`now`、`current_date`/
  `current_time`/`current_timestamp`、`localtime`/`localtimestamp`、
  `transaction_timestamp` 等；
- 只读连接是第二道闸门：即使出现未覆盖的写路径，DuckDB 也会在执行期拒绝。

## 4. 比较语义与差异怎么读

### 4.1 分组与多重集合

- 给了 `keys` 就按键值成组，组内比较；不给键整张结果是一个组。
- NULL 键在分组语义里彼此相等，NULL 键的行自成一组，正常参与配对。
- 行不去重：完全相同的两行各占一个匹配节点，重复行数被完整保留。

### 4.2 为什么需要一对一最大匹配

重复键加容差时，逐行贪心挑"最近值"会误判。演示数据里同键两行：

```
左 [2.04, 1.95]   右 [2.00, 2.10]   容差 abs = 0.10
```

- 贪心：2.04 先挑走最近的 2.00（差 0.04），1.95 唯一能配的 2.00 已被占用，
  只剩 2.10（差 0.15 > 0.10），于是误报不一致，只配成 1 对；
- 最大匹配：存在完整配对 `2.04→2.10`（0.06）、`1.95→2.00`（0.05），2 对全配上，
  判相同。

工具在每个组内构造二分图（边 = 两行在 **所有列同一对行上** 同时满足规则），
求一对一最大匹配；只有每行都找到对面不同的一行才判相同。因此：

- 一行不会被复用去抵掉多个重复行；
- 多个非键列由同一对行同时满足，不会各列独立排序后再比较。

### 4.3 标量规则

- 非键列默认按类型和值精确比较；键列始终精确，不套容差。
- 整数族（含 HUGEINT）与 DECIMAL 用 Python int / Decimal 精确比较与容差运算，
  不经过 float；很大的精确整数不会丢精度。
- NULL 只等于 NULL；NaN 只等于 NaN；+Inf / -Inf 分别只等于自身。
- 两边列名、列顺序、DuckDB 逻辑类型（含 DECIMAL 标度）必须完全相符；
  不做隐式类型转换，不会把数值转成字符串再说相同，也不偷偷丢列。

### 4.4 差异报告读法

配不满时，报告 **只列两边剩余行及其重复次数**（保留键与全部原始列值），
不会拼出一对"变化行"当作确定原因。一个差异组：

```json
{
  "key": {"sensor_id": {"t": "varchar", "v": "s1"}},
  "left_rows": 3, "right_rows": 4, "matched_pairs": 3,
  "left_unmatched": [],
  "right_unmatched": [
    {
      "row": {
        "sensor_id":   {"t": "varchar", "v": "s1"},
        "measured_at": {"t": "timestamp", "v": "2026-03-01T10:05:00"},
        "temperature": {"t": "float", "v": 21.5}
      },
      "count": 1
    }
  ]
}
```

- `left_rows`/`right_rows`：该组两边各有多少行；`matched_pairs`：配上的对数；
- `left_unmatched` / `right_unmatched`：剩余行按完整行值聚合，`count` 是该值
  剩余的重复次数；复制行表现为右侧剩余，删除行表现为左侧剩余。
- 无键检查的 `key` 为空对象 `{}`。

### 4.5 值编码：不丢精度、可恢复类型

普通 JSON 数字会把大整数/DECIMAL 截断成 float，也无法表示 NaN/Inf。报告里
每个标量编码成 `{"t": <类型族>, "v": <值表达>}`：

| 类型族 | 表达 | 例子 |
| --- | --- | --- |
| `integer` | JSON 整数（int 原样，不经过 float） | `{"t":"integer","v":123456789012345678901234}` |
| `decimal` | 保留全部位数的十进制字符串 | `{"t":"decimal","v":"123456789012345.123456"}` |
| `float` | 有限数为 JSON number；NaN/Inf 为字符串标记 | `{"t":"float","v":"NaN"}` / `"Infinity"` / `"-Infinity"` |
| `null` | `v` 为 null | `{"t":"null","v":null}` |
| `varchar`/`boolean`/`date`/`timestamp`/`blob`/`uuid`/... | 各自自描述表达 | blob 用 base64 |

同样的数据换任意行顺序再次运行，结论、匹配数量与差异展示保持稳定
（匹配前节点按行值规范排序，剩余行按值聚合排序）。

## 5. 从建库到对账的复现命令

```bash
# 依赖
python3 -m pip install -r requirements.txt

# 1) 构建演示库（可重复运行，会重建 demo/recon/recon.duckdb）
python3 -I demo/recon/build_db.py

# 2) 运行 26 项检查清单（含 equal/difference/error 三类，退出码预期为 2）
python3 -I src/sqlrecon/cli.py run \
  --db demo/recon/recon.duckdb \
  --manifest demo/recon/checks.json \
  --output demo/recon/last_report.json

# 3) 独立穷举配对交叉核对（不调用线上 Hopcroft-Karp 匹配函数）
python3 -I tools/exhaustive_recon.py \
  --db demo/recon/recon.duckdb \
  --manifest demo/recon/checks.json \
  --report demo/recon/last_report.json

# 4) 一键端到端演示（建库 + 断言全部预期 + 穷举核对 + 三类退出码）
python3 -I demo/recon/run_demo.py

# 5) 回归测试
python3 -I scripts/run_pytest.py tests/test_sqlrecon.py -q
```

## 6. 演示场景覆盖

| 检查 | 场景 | 预期 |
| --- | --- | --- |
| 01 | 等价嵌套相关子查询 vs JOIN 预聚改写 | equal |
| 02 | UNION ALL 分支顺序对调（顺序被忽略） | equal |
| 03 / 04 | 错误复制一行 / 错误删除一行 | difference（剩余行+次数） |
| 05 | 重复键+容差贪心陷阱：贪心失败、最大匹配完整配对 | equal（2 对） |
| 06 | 每列单独能配上、整行配不上的反例 | difference（0 对） |
| 07 | HUGEINT 大整数 + 高精度 DECIMAL 精确比较 | equal |
| 08 / 09 | NaN/±Inf/NULL 混合；多引入一个 NaN | equal / difference |
| 10 | NULL 业务键正常成组、重复行配对 | equal |
| 11 / 12 | 两边皆空；空 vs 凭空一行 | equal / difference |
| 13 / 14 / 15 | 容差恰好落阈值 / 超阈值 / 纯相对容差边界 | equal / difference / equal |
| 16 | 不指定键，整表当一组 | equal |
| 17 / 18 | 列签名不符 / 重名输出列 | error: schema_mismatch |
| 19 / 20 | 找不到的容差列 / 键列 | error: schema_mismatch |
| 21 | UPDATE 藏在 WITH CTE 体内 | error: sql_rejected |
| 22 / 23 / 24 | 外部文件表函数 / random() / 多语句 | error: sql_rejected |
| 25 / 26 | 查询执行报错后，下一项仍正常运行 | error 后接 equal |
