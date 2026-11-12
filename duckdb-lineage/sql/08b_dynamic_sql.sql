-- 场景 08b：动态 SQL -> unsupported
-- 语句文本在运行时才拼接，静态分析看不到真实 AST，必须拒绝而非猜测。
EXECUTE IMMEDIATE 'SELECT order_id, amount FROM orders WHERE status = ''PAID''';
