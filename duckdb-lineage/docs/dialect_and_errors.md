# 方言范围与错误定位

## 1. 受支持的语法范围

解析器固定使用 sqlglot 的 **DuckDB 方言**（`read="duckdb"`）。仅对下列语法承诺
列级血缘的正确性；超出范围一律返回结构化 `unsupported`，绝不输出伪装完整的血缘。

| 类别 | 支持的构造 | 血缘处理要点 |
| --- | --- | --- |
| 查询主体 | `SELECT`、CTE（`WITH`）、嵌套子查询、派生表 | 每个 SELECT 独立作用域，沿父链解析相关引用 |
| 关系 | 表别名、`FROM`、显式 `JOIN ... ON`（含 LEFT/RIGHT/INNER/FULL） | 别名遮蔽；JOIN ON 全部归 `control_sources` |
| 集合运算 | `UNION ALL` | 按位置合并分支，输出列名取左分支 |
| 表达式 | 算术/比较/逻辑/函数、`CASE WHEN` | CASE 条件→control，THEN/ELSE→value |
| 窗口 | 窗口函数、内联 `OVER`、命名 `WINDOW` 子句 | 参数→value；PARTITION BY/ORDER BY→control |
| 聚合 | SUM/AVG/MIN/MAX/COUNT 等 | `COUNT(*)`/无参排名函数→行集标记 |
| 子查询谓词 | 相关/非相关 `EXISTS`、`NOT EXISTS`、`IN (subquery)`、标量子查询 | EXISTS/IN 内部来源整体归 control |
| 过滤 | `WHERE`、`HAVING`、`QUALIFY`、`GROUP BY` | 关系层谓词整体归 control，分发到全部输出字段 |
| 投影 | 裸 `*`、限定 `t.*`、列别名 | 按目录/派生绑定逐列展开 |
| 名字解析 | CTE 遮蔽基础表、相关子查询外层绑定、限定/未限定列、投影别名 | 未限定列多候选→歧义诊断 |

### 基础身份约定

* 基础列稳定身份：`catalog.schema.table.column`（来自 `catalog/catalog.json`）。
* 行集标记：`row_set:catalog.schema.table`，用于 `COUNT(*)`、`ROW_NUMBER()` 等
  不引用具体列、只依赖"关系行是否存在/有多少行"的表达式。
* 派生表/CTE 的输出列在被外层引用时做 **重定基（rebase）**：把内层来源路径前置
  外层访问路径，最终仍落到基础身份，不会产生 `derived_alias.col` 这类拼接假边。

## 2. 明确拒绝（unsupported）的语法

| 构造 | 拒绝原因 |
| --- | --- |
| 递归 CTE（`WITH RECURSIVE`） | 递归固定点无法静态展开为有限列级血缘 |
| 动态 SQL（`EXECUTE IMMEDIATE` / `PREPARE`） | 真实语句文本运行时才确定，静态看不到 AST |
| 解析失败的 SQL | 无法保证 AST 正确，返回 `parse_error` 与行列号 |
| DML/DDL（INSERT/UPDATE/CREATE 等） | 不在 SELECT 只读血缘方言范围 |
| `LATERAL`、`UNNEST`/表函数（`read_csv_auto` 等） | 逐行派生或运行时取值，列绑定无法静态确定 |
| `UNION`（去重）、`INTERSECT`、`EXCEPT` | 去重/交集改变行存在性语义，当前仅建模 UNION ALL |
| `PIVOT` | 输出列依赖运行时数据取值 |
| 多语句脚本 | 一次只分析单条查询 |

> 注意："DuckDB 能执行"不等于"能静态抽血缘"。递归 CTE 在 DuckDB 合法，但本工具
> 仍按上表拒绝，以保证任何 `status=ok` 的结果都是完整可信的。

## 3. 两类依赖的语义边界

* `value_sources`：参与输出字段 **值计算** 的来源。修改这些基础列会改变字段取值。
* `control_sources`：只影响结果 **存在性/选择/分组/排序** 的来源，包括
  `WHERE`、`HAVING`、`QUALIFY`、`JOIN ON`、CASE 条件、窗口 `PARTITION BY`/`ORDER BY`、
  `EXISTS`/`IN` 子查询内部。修改这些列会改变"哪些行出现/如何分框"，但不改变
  单行内字段的计算公式。

同一基础列可以同时出现在两个桶（例如 HAVING 引用的聚合表达式既算值又做筛选）。
`sql/09_filter_only_column.sql` 是反证用例：`city`/`tier` 只在 WHERE 出现，
因此只进 control，证明两类依赖确实可分。

### 行存在性约束的独立上带（row_filters）

关系层谓词（`WHERE`/`JOIN ON`/`HAVING`/`QUALIFY`）决定的是 **本关系的行能否
存在**，与外层最终引用了哪个输出列无关。因此派生表/CTE 的这类来源单独记为
`row_filters` 并随绑定上带，而不是绑死在被选中的列上：

* 外层 `COUNT(*)`、输出固定常量标记、或连接后只取对侧列（一个内层列都不引用）
  时，内层筛选仍出现在外层字段的 `control_sources`（见
  `sql/12_filtered_count.sql`、`sql/13_join_name_and_scalar_role.sql`）；
* 约束可穿透任意层嵌套，逐层前置 `relation_filter` 路径，不是单层补丁；
* 表达式内部的局部条件不享受这条规则：`CASE WHEN status=... THEN amount` 中的
  `status` 只约束该字段本身，不会泄漏到同层相邻的固定标记字段。关系层 WHERE
  与表达式级 CASE 条件据此严格区分，不做"汇总所有列来源"的近似。

### EXISTS / IN 与行集标记

`EXISTS`/`IN (subquery)` 只做存在性判断，内部来源整体归 control。即使子查询
只选常量（`SELECT 1 FROM orders`），其 FROM 关系的行集标记
`row_set:shop.main.orders` 仍作为 control 上带：删空该表会改变外层保留的行，
这条因果因此可追溯（见 `sql/14_exists_row_set.sql`）。

标量子查询严格沿用内层字段的角色分桶：内层 value 重定基为外层 value，内层
control 重定基为外层 control。因此按付款状态取最大金额
`(SELECT MAX(amount) ... WHERE status='PAID')` 中 `amount` 仅是 value、
`status` 仅是 control，不会出现角色互换。

## 4. 错误定位方式

错误载荷统一结构：

```json
{
  "code": "ambiguous_column | unknown_column | unknown_relation | parse_error | unsupported_syntax",
  "message": "人类可读说明",
  "location": { "line": 5, "col": 14, "snippet": "=" },
  "column": "customer_id",
  "candidates": ["c.customer_id", "o.customer_id"]
}
```

* 行列号为 **1 起始**，与 DuckDB、sqlglot 的编号习惯一致。
* `parse_error` 直接采用 sqlglot tokenizer 报告的 line/col 与高亮片段。
* 语义错误（歧义列、unsupported 节点）由 `positions.py` 用 token 流建立
  "节点 → 原文偏移 → 行列号"映射：对节点的标识名（列名/表名/命令关键字）按
  出现次序匹配 token 起点；复合节点退化为其首个具名叶子。
* `status` 取值：
  * `ok`：完整血缘，字段列表可信；
  * `error`：可解析但名字绑定失败（歧义/未知列/未知表），不产出字段；
  * `unsupported`：方言范围外或解析失败，不产出字段。

三种非 ok 状态都保证 `fields` 为空，杜绝"半截血缘伪装完整"。
