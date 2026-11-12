-- 执行期才失败的查询：引用不存在的表（静态形态合法，DuckDB 执行报错）
SELECT * FROM table_that_does_not_exist
