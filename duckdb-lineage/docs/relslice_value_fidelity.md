# 根因说明：relslice 裁取链上的原值损失（TIMESTAMP_NS 纳秒 / INTERVAL 月份）

可执行复现见
[`repro/repro_relslice_value_fidelity.py`](../repro/repro_relslice_value_fidelity.py)；
回归见 `tests/test_relslice_fidelity.py`；使用文档见
[`relslice.md`](relslice.md) 第 5 节。

## 1. 现象

单行例子：源表 `events(id INTEGER, occurred TIMESTAMP_NS, term INTERVAL)`，
插入 `id=1`、`2026-02-01 12:00:00.123456789`、`INTERVAL '1 month'`，配方以
`id` 为身份并选择 1。裁取返回成功、身份也对，但裁取库里：

* `occurred` 变成 `2026-02-01 12:00:00.123456000`（纳秒被截断）；
* `term` 变成 `30 days`（月份成分丢失）；
* `DATE '2026-02-01' + term` 在源库是 **3 月 1 日**，在裁取库是 **3 月 3 日**——
  拿裁取库排查日期条件会查出和原库不同的结果。

## 2. 根因：值在 "DuckDB -> Python 对象 -> DuckDB" 边界改变

旧搬运链分两段：读取事务里 `fetchone()` 把整行取成 **Python 对象** 存进闭包
结果，事务结束、连接关闭后，再由写出端用 `executemany` 把这些 Python 对象
重新插入目标库。两个边界各丢一次：

| DuckDB 原值 | Python 对象（第一边界） | 再插入（第二边界） |
| --- | --- | --- |
| `TIMESTAMP_NS ...123456789` | `datetime`（只到微秒） | `...123456000`，纳秒丢失 |
| `INTERVAL '1 month'`（月/日/微秒三元组） | `timedelta(days=30)`（月份被换算成固定 30 天） | `INTERVAL '30 days'` |

身份列不受影响（整数/字符串经 Python 往返无损），所以"记录身份都对、业务
值已经变了"。

为什么这些"省事"路线都不成立：

* **只改显示格式**——丢的是存储值本身，不是显示；
* **把月份换算成固定天数**——正是缺陷本身（`1 month` ≠ `30 days`，
  跨月末加法结果不同）；
* **先转成字符串再按猜测恢复**——要按类型逐个猜测格式，嵌套类型（列表、
  结构体里的时间/间隔）无法可靠还原，本质还是另一套会丢信息的编解码。

## 3. 修复：同一次一致读取里的 DuckDB 原生搬运

让值 **始终留在 DuckDB 内部**，不经过 Python 对象：

1. 引擎建一个纯内存会话，把源库以 `READ_ONLY` 挂载为目录 `source`，把
   目标临时库以可写挂载为目录 `target`（`src/relslice/source.py`、
   `src/relslice/writer.py`）；
2. **同一个事务** 里完成结构核对、身份校验、起点取行、闭包追踪 **以及**
   把入选行搬进目标临时库——记录选择与数据搬运来自同一次一致读取，
   事务不结束、快照不变，不会"先算完身份、结束读取，再回源库捞一遍可能
   已变化的字段"；
3. 搬运用 `INSERT INTO target... SELECT ... FROM source... WHERE (身份列)
   IN (VALUES ...)`：身份值（整数/字符串）只作 WHERE 参数，业务列整行由
   DuckDB 内部直接复制——`TIMESTAMP_NS` 的纳秒、`INTERVAL` 的月/日/微秒
   成分、DECIMAL 刻度、BLOB 字节、列表/结构体里的同样成分全部原样保留；
4. 提交并正常关闭后才把临时库 **硬链接原子发布** 到目标名（不覆盖语义
   兜底）；任一环节失败只删临时文件，目标位置不留半成品，也不产出成功
   说明。写出中途的真实失败（如磁盘写满）收口为 `target_write_error`
   （退出码 2）。

闭包追踪仍按身份取行（引用列必为整数/字符串，经 Python 对象无损），
追查说明仍来自同一份闭包结果——配方、闭包语义、报告格式、CLI 调用方式
全部不变。

## 4. 验证

* `repro/repro_relslice_value_fidelity.py`：单行复现例；先演示值在
  "取成 Python 对象再插回"边界改变（对照组），再实际裁取，**重新打开**
  源库与目标库，全部用数据库内精确表达式核对（`epoch_ns`、`datepart`
  月/日/微秒成分、`DATE + term`），不把两边取成可能已丢信息的 Python
  对象再比较；
* `tests/test_relslice_fidelity.py`：负值/跨月末/空值、列表与结构体里的
  时间/间隔、含无关记录与父记录补入与空表的配方、精确整数/DECIMAL/BLOB；
  一致读取的实际检验（闭包算完后经硬链接别名改写源库，目标库仍必须是
  读取事务快照里的原值；事务边界记录器钉死所有读取与搬运落在同一个
  BEGIN..COMMIT 窗口）；写出中途真实失败（RLIMIT_FSIZE 制造真实 IO
  失败）后不留半成品/成功说明、恢复后正常、源库不变。

```bash
python3 -I repro/repro_relslice_value_fidelity.py
python3 -I scripts/run_pytest.py tests/test_relslice_fidelity.py -q
```
