# DuckDB SQL 列级血缘抽取器

面向分析数据集 **变更审查** 的列级血缘工具。Python 编写，SQL 语法树交给
[SQLGlot](https://github.com/tobymao/sqlglot)（DuckDB 方言）解析，输出确定性
JSON 血缘图，并把每个输出字段的依赖严格区分为：

* **value_sources**：参与字段 *值计算* 的基础列；
* **control_sources**：只影响结果 *存在性/选择/分组/排序* 的来源
  （WHERE / HAVING / JOIN ON / QUALIFY、CASE 条件、窗口 PARTITION BY/ORDER BY、
  EXISTS/IN 子查询）。

基础列采用 `catalog.schema.table.column` 稳定身份；`COUNT(*)` 等无列聚合记为
`row_set:catalog.schema.table` 行集标记，不伪造全列依赖。

## 目录结构

```
duckdb-lineage/
├── catalog/catalog.json          # 表结构目录（catalog/schema/table/column）
├── sql/                          # 11 个可执行样例（01~11）
├── expected/expected_lineage.json# 手工标注的期望依赖集合
├── src/lineage/                  # 抽取器源码
│   ├── analyzer.py               # 主编排：解析/前置检查/作用域/UNION 合并
│   ├── scope.py                  # 作用域链、CTE 遮蔽、相关绑定、歧义诊断
│   ├── expressions.py            # 表达式 value/control 分桶、CASE/窗口/EXISTS
│   ├── positions.py              # token 行列定位
│   ├── catalog.py / model.py / graph.py / errors.py / cli.py
├── scripts/                      # DuckDB 执行、期望核对、pytest、一键验证
├── tests/test_lineage.py         # 17 个 pytest 单元测试
└── docs/dialect_and_errors.md    # 方言范围与错误定位说明
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

# 2) 字段级原始结构（每字段 value_sources/control_sources + paths）
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
3. `scripts/run_pytest.py` 跑 17 个单元测试；并对每个 SQL 重复运行比对 MD5，
   证明 JSON 输出字节级确定。

## 样例覆盖矩阵

| 样例 | 验证点 |
| --- | --- |
| 01_cte_shadow | CTE 遮蔽同名基础表，内层过滤穿透为 control |
| 02_two_layer_alias | 两层别名重定基，禁止 `d.oid` 拼接假边 |
| 03_star | 裸 `*` 与 `c.*` 星号展开（15 列） |
| 04_correlated_exists | 相关 EXISTS，外层绑定穿透，内部来源只进 control |
| 05_union_all | 分支列名不同的 UNION ALL 按位置合并 |
| 06_case_window | CASE 条件/分支分桶、窗口 PARTITION/ORDER、COUNT(*) 行集 |
| 07_ambiguous | 未限定同名列歧义诊断（候选 + 行列号） |
| 08a/08b/08c | 递归 CTE / 动态 SQL / 解析失败 → 明确 unsupported |
| 09_filter_only | **仅筛选不参与值计算** 的列，证明两类依赖可分 |
| 10_having_derived | HAVING 引用派生列，递归到基础身份 |
| 11_named_window | 命名窗口 WINDOW 子句 + 跨 catalog.schema 限定表名 |

方言边界、unsupported 清单与错误定位原理见
[`docs/dialect_and_errors.md`](docs/dialect_and_errors.md)。
