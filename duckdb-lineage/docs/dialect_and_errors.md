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
| 子查询谓词 | 相关/非相关 `EXISTS`、`NOT EXISTS`、`IN (subquery)`、标量子查询 | EXISTS/NOT EXISTS 只取匹配列(control)与行集(existence)；IN 另取投影列(control)；标量子查询保留 value/control 并补 existence |
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

## 3. 三类依赖的语义边界

依赖按 **变化原因** 分三类，不能用"把所有来源并进一个桶"的方式让边数看起来完整：

* `value_sources`：参与输出字段 **值计算** 的来源。修改它会改变字段取值
  （算术、聚合参数、投影列）。
* `control_sources`：只影响 **哪些行能匹配/被选中/被分组/被排序** 的来源，
  包括 `WHERE`、`HAVING`、`QUALIFY`、`JOIN ON`、`GROUP BY`、CASE 条件、窗口
  `PARTITION BY`/`ORDER BY`、`EXISTS`/`IN` 的匹配/比较列。修改它会改变"哪些
  行出现/如何分框"，但不改变单行内字段的计算公式。
* `existence_sources`：只影响 **行集/子结果是否为空** 的来源，即
  `row_set:catalog.schema.table` 标记。它回答"这里是否还有行/记录能否找到"，
  而不是"按什么条件匹配"。

同一基础列若真的同时承担多种角色（如 HAVING 引用的聚合表达式既算值又做筛选），
可以同时出现在多个桶；但不得把只承担一种角色的来源挪进另一个桶来凑边。
`sql/09_filter_only_column.sql` 是反证用例：`city`/`tier` 只在 WHERE 出现，
因此只进 control，证明 value/control 确实可分。

### EXISTS / NOT EXISTS：谓词能否找到记录

`EXISTS`/`NOT EXISTS` 只问"谓词能否找到记录"，子查询 SELECT 列表是 **占位**，
不是筛选条件：

* 处理 `NOT EXISTS` 与 `EXISTS` 对称；`NOT` 只翻转布尔值，不引入来源；
* 内层 WHERE/JOIN ON 的匹配列、GROUP BY 键 -> `control_sources`；
* 底层 FROM 关系的行集标记 -> `existence_sources`（删空表会让 EXISTS 翻转，
  这是"集合是否为空"而非"匹配条件"的变化）；
* 只出现在 SELECT 列表里的列（如 `SELECT o.amount, o.status FROM orders o`）
  **不进入任何桶**：单独修改它们，数据库结果不变。见
  `sql/15_exists_projection_placeholder.sql`、`sql/16_not_exists.sql`；
* 内层是恒单行的无分组聚合（`EXISTS (SELECT COUNT(*) FROM orders)`）时，
  无论底层表是否有行 EXISTS 恒为真 -> 不产生任何来源，更不把 `COUNT` 的行集
  标记误报为存在性来源。

### 标量子查询：返回一个值时，空输入会让结果变 NULL

标量子查询用于返回一个值：内层 value 原样重定基为外层 value，内层匹配条件
重定基为 control。它还会在"输入集合为空"时把具体值变成 NULL（或整行消失），
这条变化必须能追到行集：

* 非恒单行内层（普通明细/未聚合子查询）：底层行集标记进 `existence_sources`；
  见 `sql/17_scalar_constant_with_predicate.sql`（只返回常量 1，删掉最后一笔
  匹配订单后结果从 1 变成 NULL）；
* 恒单行内层（无分组聚合）：SUM/MIN/MAX/AVG 空输入返回 NULL -> 行集进
  `existence_sources`；COUNT 空输入返回 0（不是 NULL），行集作为取值来源留在
  `value_sources`，不重复上报为 existence。见 `sql/13_join_name_and_scalar_role.sql`；
* 无论哪种情况，内层具体的匹配条件（如 `o.status='PAID'`）都保留为 control，
  不能只留下一个"行集为空"的粗粒度来源。

### IN (subquery)：读取投影列做成员比较

与 EXISTS 不同，`IN` 会读取子查询投影列的取值做成员比较，因此投影列属于
`control_sources`（匹配过滤），底层行集是否还有行属于 `existence_sources`。

### 空输入的无分组聚合：行筛选不得扩散为外层行选择

无 GROUP BY 且投影含聚合的 SELECT（如 `SELECT COUNT(*) FROM orders WHERE ...`）
输出行数恒为 1：删空输入后 COUNT 返回 0，**仍然有一行**。因此：

* 它作为派生关系被外层消费时，不贡献基数行集（外层 `COUNT(*)` 恒为 1，与底层
  行数无关）；
* 内层 `WHERE`/`JOIN ON` 只改变聚合取值，不改变"是否有一行"，**不作为关系级
  行选择来源上带**，否则会把内层筛选扩散成外层每个输出字段（包括固定常量）
  的行选择来源；
* 内层筛选列仍是该聚合字段本身的 `control_sources`（它们确实改变取值）；
* `HAVING`/`QUALIFY` 是聚合后的行过滤，仍可能过滤掉那一行，照常上带。

见 `sql/18_empty_input_aggregate.sql`：外层固定标记 `marker` 不得因内层
`status` 产生任何依赖。

### 普通派生关系的行选择来源独立上带（row_filters）

普通关系（非恒单行）的 `WHERE`/`JOIN ON` 决定 **本关系的行能否存在**，与外层
最终引用了哪个输出列无关。因此派生表/CTE 的这类来源单独记为 `row_filters`
并随绑定上带，而不是绑死在被选中的列上：

* 外层 `COUNT(*)`、输出固定常量标记、或连接后只取对侧列（一个内层列都不引用）
  时，内层筛选仍出现在外层字段的 `control_sources`（见
  `sql/12_filtered_count.sql`、`sql/13_join_name_and_scalar_role.sql`）；
* 约束可穿透任意层嵌套，逐层前置 `relation_filter` 路径，不是单层补丁；
* 表达式内部的局部条件不享受这条规则：`CASE WHEN status=... THEN amount` 中的
  `status` 只约束该字段本身，不会泄漏到同层相邻的固定标记字段。关系层 WHERE
  与表达式级 CASE 条件据此严格区分，不做"汇总所有列来源"的近似；
* **例外**：恒单行的无分组聚合没有行选择语义，其内层 WHERE 不进入 `row_filters`
  （见上节"空输入的无分组聚合"）。

### 根因：为什么旧图会互相矛盾

线上两条 SQL 暴露的是同一个建模错误——把"不同变化原因"并进一个桶：

1. 旧实现让 `EXISTS` 上报子查询的 **全部输出字段来源**（含 SELECT 列表里的
   `amount`/`status`）。这些列只是占位，单独修改它们数据库结果不变，却被
   画成影响客户名单的 control 边，导致评审追查无关改动。修复：EXISTS 只取
   谓词内的匹配来源与行集，投影占位列不进入任何桶。
2. 旧实现没有"行集是否为空"这一独立原因，把 `row_set:` 并进 control；同时
   标量子查询只沿用内层字段的 value/control，常量投影没有基础列来源，于是
   "删掉最后一笔订单、结果 1 -> NULL"在图上完全找不到订单来源。修复：新增
   `existence_sources`，把行集为空的变化单列；标量子查询额外补上这一来源，
   并把内层匹配条件保留为 control。
3. 旧实现把派生关系的内层 WHERE 一律当作外层行选择来源，连 `COUNT(*)` 这种
   空输入仍返回一行的聚合也照搬，使内外层语义串味。修复：按基数语义区分，
   恒单行的无分组聚合不扩散内层筛选。

三类原因分别落在 value/control/existence，`deps` 与 `impact` 入口都按同一
分桶输出 `impact_types`。

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
