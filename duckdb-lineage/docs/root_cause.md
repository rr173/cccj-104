# 根因说明：EXISTS 占位投影 / 标量 NULL / 空输入聚合

本文对照线上评审遇到的两条互相矛盾的 SQL，说明旧血缘图为什么错、修复后
三类来源如何分开。可执行复现见
[`repro/repro_exists_scalar_aggregate.py`](../repro/repro_exists_scalar_aggregate.py)；
完整语义规则见 [`dialect_and_errors.md`](dialect_and_errors.md) 第 3 节。

## 1. 现象

| # | SQL 形态 | 数据库真实行为 | 旧图 | 后果 |
| --- | --- | --- | --- | --- |
| A | `EXISTS (SELECT o.amount, o.status FROM orders o WHERE o.customer_id=c.customer_id)` | 只改 `amount`/`status` 的值，客户名单**不变** | 把 `amount`/`status` 列成影响名单的 control 来源 | 评审追查与结果无关的改动 |
| B | `(SELECT 1 FROM orders o WHERE ... )` 标量常量 | 删掉最后一笔匹配订单，结果 **1 → NULL** | 图上找不到任何订单来源 | 真实影响被漏看 |

两者看似矛盾，根因相同：**把"不同变化原因"并进了同一个桶**。

## 2. 根因

1. **把子查询输出列当成筛选条件。** 旧实现让 `EXISTS` 上报子查询所有输出
   字段的来源，而 `EXISTS` 只看谓词能否找到记录；`SELECT` 列表里的列只是
   占位，既不算值、也不参与匹配。
2. **没有"行集是否为空"这一原因。** 旧实现只有 value/control 两桶，把
   `row_set:` 标记并进 control。标量子查询只沿用内层字段的 value/control，
   常量投影没有任何基础列来源，于是"删最后一行 → NULL"无处表达。
3. **忽略聚合的基数语义。** `SELECT COUNT(*) ... WHERE ...` 在空输入时仍返回
   一行（0），内层 `WHERE` 只改变聚合取值、不改变"是否有一行"；旧实现却把
   它当作派生关系的行选择来源，扩散成外层每个输出（包括固定常量）的依赖。

## 3. 修复后的三类原因

| 变化原因 | 桶 | 典型来源 |
| --- | --- | --- |
| 值计算 | `value_sources` | 投影列、聚合参数、`COUNT(*)` 的行集标记 |
| 匹配/选择/分组/排序 | `control_sources` | WHERE/JOIN ON/HAVING/QUALIFY/GROUP BY、CASE 条件、窗口 PARTITION/ORDER、EXISTS/IN 匹配列 |
| 行集是否为空 | `existence_sources` | `row_set:` 标记：EXISTS 依赖表是否还有行；标量子查询空输入变 NULL |

关键决策：

* `EXISTS`/`NOT EXISTS` 的 `SELECT` 投影列**不进入任何桶**；匹配列进 control，
  行集进 existence。`NOT` 只翻转布尔值，不引入来源。
* 恒单行内层（无分组聚合）对 `EXISTS` 恒为真 → 不产生来源；`COUNT` 空输入返回
  0 不是 NULL，其行集作为 value 留下，不重复上报为 existence；`SUM/MIN/MAX/AVG`
  空输入返回 NULL，行集进 existence。该规则对顶层无分组聚合同样成立（见
  `test_empty_input_aggregate_null_value_traces_row_set`）。
* 标量子查询保留内层具体匹配条件为 control，同时把"空输入"的行集挂到
  existence，两条原因并存，不漏看、不误报。
* 无分组聚合作为派生关系被消费时，不贡献基数行集；其内层 WHERE 不作为关系级
  行选择来源上带。`HAVING`/`QUALIFY` 是聚合后过滤，照常上带。
* 普通（非恒单行）派生关系的 WHERE/JOIN ON 仍独立上带，保证
  `COUNT(*)`、固定标记、只取连接对侧列等旧修复不回退。

## 4. 验证

`repro_exists_scalar_aggregate.py` 用 DuckDB 小数据做确定性因果实验：

* 逐次删除/恢复匹配订单，记录结果（EXISTS、NOT EXISTS、标量常量、标量 SUM）；
* 单独修改只出现在 EXISTS 投影里的 `status`/`amount`，断言结果不变；
* 删空无分组 COUNT 的输入，断言仍返回一行、固定标记不变；
* 对每个真实变化断言工具的 deps/impact 分桶与 `impact_types` 一致；
* 回归派生筛选上带（COUNT(*) / 固定标记 / 三层嵌套 / 连接只取对侧列）与
  非法 SQL 拒绝（歧义列、递归 CTE、动态 SQL、解析错误）。

```bash
bash scripts/run_all.sh        # 一步跑完全部验证
```
