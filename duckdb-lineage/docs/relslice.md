# relslice：关系数据裁取

排查报表问题时，常常只拿到几笔有问题的订单，却不得不复制整个业务库才能
重现。`relslice` 从一个 DuckDB 业务库里 **裁出能独立查询的小库**：指定少量
起始记录，工具自动带上这些记录必需的关联资料，同事拿到这个小库就能直接
查询。

裁取依据是 **业务表之间的引用关系**（子表列组 -> 父表身份），不是 SQL 字段
血缘，也不是按比例抽样。源库不需要建过外键约束——引用完全由配方描述。

- 给定 JSON 配方：参与的基础表、各表身份列、子表->父表引用、起始身份；
- 从起点出发 **只沿子表到父表方向** 追踪，直到没有新记录（最小闭包）；
- 全部读取在 **同一个读取事务** 里完成，源库以只读方式打开；
- 产出新的 DuckDB 文件（拒绝覆盖已有文件）与一份 **追查说明**；
- 可设总记录预算，确认超限即失败，**不截断关联、不产出残缺样本**。

## 1. 配方格式

配方是一个 JSON 文件，根对象三个字段：`tables`（必填非空）、
`references`（可省略）、`seeds`（可省略或为空数组）。

```json
{
  "tables": [
    {"name": "shop.customers", "identity": ["customer_id"]},
    {"name": "shop.orders", "identity": ["order_id"]},
    {"name": "shop.order_items", "identity": ["order_id", "line_no"]},
    {"name": "shop.price_book", "identity": ["region", "sku"]}
  ],
  "references": [
    {"name": "orders_customer",
     "child": "shop.orders", "child_columns": ["customer_id"],
     "parent": "shop.customers"},
    {"name": "items_pricebook",
     "child": "shop.order_items", "child_columns": ["price_region", "sku"],
     "parent": "shop.price_book"}
  ],
  "seeds": [
    {"table": "shop.order_items", "identity": [1001, 1]}
  ]
}
```

### 表（`tables[i]`）

| 字段 | 说明 |
| --- | --- |
| `name` | `schema.table` 或 `table`（缺省 schema 为 `main`）；至多一个点号 |
| `identity` | 身份列名数组，非空、不重复；可由多列组成（复合身份） |

身份列约束（与源库对照时校验，见第 3 节）：

- 列类型限 **整数家族**（TINYINT/SMALLINT/INTEGER/BIGINT/HUGEINT 及无符号
  变体）或 **VARCHAR**；
- 身份列在源表中 **不允许空值**，且身份值 **确实唯一**（不满足时给出示例
  重复值）；
- 不同 schema 下的同名表是不同表（`a.t` 与 `b.t` 互不混淆）。

### 引用（`references[i]`）

| 字段 | 说明 |
| --- | --- |
| `name` | 引用名，配方内唯一；追查说明与错误链路里用它指代这条引用 |
| `child` / `parent` | 子表 / 父表，都必须在 `tables` 里声明 |
| `child_columns` | 子表列组，**按位置一一对应父表身份列** |

- **列的排列有意义**：`child_columns[k]` 对应父表 `identity[k]`，两侧列数
  必须一致，对应位置的 DuckDB 逻辑类型必须完全相等（INTEGER 与 BIGINT 也
  不相容，不做隐式转换）；
- 子表列的物理顺序与父表身份列顺序可以不同——按父表身份顺序写
  `child_columns` 即可（演示里的 `items_pricebook`、`notes_item` 正是如此）；
- 完全相同的引用（子表+列组+父表）重复声明会被拒绝；
- 父表身份列由 `tables` 里的声明给出，引用不重复描述。

### 起点（`seeds[i]`）

| 字段 | 说明 |
| --- | --- |
| `table` | 已声明的表 |
| `identity` | 该表身份各成分的值；只允许 JSON 整数或字符串（null、布尔、小数、数组、对象都拒绝），个数与身份列数一致，类型与列类型匹配（整数列配整数、字符串列配字符串） |

- 起点可以重复（多起点重复），只收一次；`seeds` 为空数组时产出全部表
  结构、零记录的库；
- 起点身份在源表中找不到时，整次裁取失败（`seed_not_found`）。

## 2. 裁取语义

1. 先放入全部起始记录；
2. 对已经放入的每一条记录，沿每条 **以它为子表** 的引用补入父记录；
3. 重复第 2 步直到再也没有新记录。

精确规则：

- **只沿子表->父表方向**。带上了一名客户，不会因此带上这名客户的其他
  订单；带上了订单，也不会带上它的明细（要明细就把明细列为起点）；
- 引用列组中 **任意一列为 NULL**，即视为不需要父记录，跳过该引用
  （单列引用为空是 `k=1` 的特例）；
- 多条路指向同一个身份 **只收一次**；自引用、跨表成环都靠"已收不再收"
  正常收敛；
- 最终集合是这套规则的 **最小闭包**：恰好包含所有必需记录，不会为了
  方便直接复制某张维表；
- 已选中的记录引用了 **不存在的父记录** 时整次裁取失败
  （`broken_reference`），错误载荷给出从哪个起点、经哪些引用走到了断口
  （见第 5 节）；**未被选中部分** 的孤儿/断链记录不影响一次本来完整的
  裁取；
- 调整配方中表、关联或起点的排列，**不改变入选集合**（闭包是唯一的），
  也不改变追查说明（见第 4 节的稳定规则）。

### 记录预算

`--budget N` 设定总记录预算（各表入选记录数之和的上限，含起点）。追踪
过程中一旦确认必需记录将超过预算，立即以 `budget_exceeded` 失败，并给出
已确认条数与下一条必需记录；**绝不截断关联后留下看似成功的残缺样本**。
不设 `--budget` 表示不限。

## 3. 校验与错误

所有错误都带稳定 `code` 与具体位置（`location`，如
`references[2].child_columns[1]`），不默默忽略任何一项。

| code | 含义 | 退出码 |
| --- | --- | --- |
| `recipe_invalid` | 配方 JSON 形状不合法（表名/身份列/引用/起点值不合规、重复声明等） | 2 |
| `source_schema_error` | 配方与源库对不上：缺表/视图冒充基础表/缺列、身份列类型不合规、身份含空值或不唯一、引用两侧列数或类型不一致 | 2 |
| `database_open_error` | 源库文件不存在或无法只读打开 | 2 |
| `target_exists` | 目标文件已存在，拒绝覆盖 | 2 |
| `seed_not_found` | 起始身份在源表中找不到 | 1 |
| `broken_reference` | 已选中记录引用了不存在的父记录（带起点到断口的链路） | 1 |
| `budget_exceeded` | 必需记录数确认超过预算 | 1 |
| `io_error` | 其他 IO 错误 | 2 |

退出码约定：**0** 成功；**1** 裁取失败（配方与源结构合法，是数据内容
导致）；**2** 命令级错误。

`broken_reference` 载荷示例（从起点到断口的完整链路）：

```json
{
  "code": "broken_reference",
  "message": "记录 shop.orders 身份 [1004] 经引用 'orders_customer' 指向的父记录不存在: shop.customers 身份 [999]",
  "details": {
    "reference": "orders_customer",
    "child": "shop.orders",
    "child_identity": [1004],
    "parent": "shop.customers",
    "missing_parent_identity": [999],
    "trail": {
      "seed": {"table": "shop.order_items", "identity": [1004, 1]},
      "path": [
        {"reference": "items_order", "child": "shop.order_items",
         "child_identity": [1004, 1], "parent": "shop.orders",
         "parent_identity": [1004]}
      ]
    }
  }
}
```

## 4. 追查说明

成功时 stdout 输出 JSON 说明（`--report` 可同时写文件），与目标库内容
来自同一份闭包结果，二者必然一致（演示会实际开库逐表核对这一点）：

```json
{
  "status": "ok",
  "source": "demo/relslice/business.duckdb",
  "target": "out.duckdb",
  "budget": {"limit": 500, "used": 17},
  "tables": [
    {
      "table": "shop.customers",
      "identity_columns": ["customer_id"],
      "selected_count": 2,
      "selected": [
        {"identity": [1],
         "origin": {"kind": "derived",
                    "seed": {"table": "shop.order_items", "identity": [1001, 1]},
                    "path": [{"reference": "items_order", ...},
                             {"reference": "orders_customer", ...}]}},
        {"identity": [3], "origin": {"kind": "seed"}}
      ]
    }
  ]
}
```

- 每张表列出全部入选身份；`origin.kind=seed` 的是起点，`derived` 的给出
  它从 **哪个起点** 沿 **哪些引用**（`path` 逐步记录子表身份与父身份）
  到达；
- `budget.used` 是实际使用的记录预算（各表入选数之和）；
- **稳定路径规则**：追踪前把引用按引用名、起点按 (表名, 身份) 排序，
  按广度优先处理，每条记录的来历取"先达者"。有多条可解释路径时选定的
  就是这条规则，因此配方文件里表/关联/起点如何排列，说明都相同。

## 5. 一致性与输出承诺

- 源库以 `read_only=True` 打开；结构核对、身份合规校验、起点取行、闭包
  追踪取行全部在 **同一个读取事务** 里完成（同一次一致的读取范围）；
- 目标文件已存在任何内容时拒绝（预检 + 发布时硬链接不覆盖语义兜底）；
- 全部建表与插入先在同目录临时文件里完成，再原子发布到目标名；任何
  一步失败都只清理临时文件，**目标位置不留可被当成成功结果的库**；
- 输出包含配方所列 **全部表**：没有入选记录的表同样保留空表与字段类型
  （列名、顺序、DuckDB 逻辑类型照源表重建）；
- 入选行的 **全部字段及值照原样带过去**；不同 schema 的同名表各归各的
  schema；
- 不迁移视图、索引或原库的约束定义——输出只承诺 **数据与字段结构**。

## 6. 操作说明（从创建源库到裁取、查询结果）

```bash
# 1) 创建演示源库（客户/订单/明细/商品/价目/备注/互相引用的员工）
python3 -I demo/relslice/build_db.py            # 产出 demo/relslice/business.duckdb

# 2) 按配方裁取（配方示例：demo/relslice/recipe.json）
python3 -I src/relslice/cli.py extract \
  --source demo/relslice/business.duckdb \
  --recipe demo/relslice/recipe.json \
  --target /tmp/extract.duckdb \
  --budget 500 \
  --report /tmp/extract.report.json             # 退出码 0/1/2 见第 3 节

# 3) 同事直接查询裁出的小库
python3 - <<'PY'
import duckdb
con = duckdb.connect('/tmp/extract.duckdb', read_only=True)
print(con.execute(
    "SELECT o.order_id, c.name, i.line_no, i.qty "
    "FROM shop.order_items i "
    "JOIN shop.orders o ON o.order_id = i.order_id "
    "JOIN shop.customers c ON c.customer_id = o.customer_id "
    "ORDER BY o.order_id, i.line_no").fetchall())
PY

# 4) 端到端演示（建库/裁取/独立算法核对/全部场景）
python3 -I demo/relslice/run_demo.py
```

## 7. 演示数据与场景对照

`demo/relslice/build_db.py` 刻意安排的数据与对应验证点：

| 安排 | 验证点 |
| --- | --- |
| 订单 1002 与已选 1001 同属客户 1 | 同客户的无关订单不被带上（只沿子->父） |
| 明细 (1005,1) 与 (1001,1) 共用商品 P1 | 多条路指向同一身份只收一次 |
| 明细 (1001,3) sku 为 NULL；(1005,1) price_region 为 NULL | 引用列组任意一项为空即不需要父记录 |
| 价目表身份 (region, sku) vs 明细列 (sku, price_region)；备注经 (line_no, order_id) 引用明细身份 (order_id, line_no) | 复合身份列序不同，引用按父表身份顺序写列 |
| 员工 8<->9 互为经理；另有 4<->6 不连通孤儿环 | 自引用成环收敛；不连通部分不泄漏 |
| 订单 1004 -> 客户 999、明细 (1009,1) -> 订单 1009（均未选中） | 未被选中部分的断链孤儿不阻止完整裁取 |
| 以明细 (1004,1)/订单 1004/备注 502 为起点的三份配方 | 已选路径断裂：两步/空路径/跨两级链路 |
| 主配方 17 条入选 | 预算 17 刚好够用、16 报预算不足且不留文件 |
| 空 seeds 配方 | 全部 7 张表保留结构、零记录 |
| 重复起点（1001,1 出现两次）、反序配方 | 去重；排列不影响入选集合与说明 |

演示里的 `oracle_expected` 是一个 **不调用正式追踪代码** 的小型集合算法
（直接读配方 JSON 与源库自行做闭包），算出应选身份后实际打开生成的库
逐表核对身份集合与入选行的全部字段值，并核对追查说明与库内容一致。
