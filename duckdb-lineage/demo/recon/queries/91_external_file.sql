-- 读外部文件的表函数：对账查询只能引用库内表/视图
SELECT * FROM read_csv_auto('/tmp/forbidden.csv')
