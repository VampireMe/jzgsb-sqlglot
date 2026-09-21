# 12 条 optimizer 说法核实结果

- 仓库：sqlglot（本地工作树，仅探测，未改代码）
- 统一探针：`probes/verify.py`（仓库根目录），运行方式 `PYTHONPATH=. python probes/verify.py [编号...]`
- 方言：snowflake（除第 10 条特别注明 bigquery/duckdb）

## 1
结论: 伪
依据: sqlglot/optimizer/qualify.py:98-113（qualify 只调用 qualify_columns / quote_identifiers / validate_qualify_columns，不调用 annotate_types）；sqlglot/optimizer/qualify_columns.py:65,122-123（内部虽构造 TypeAnnotator，但仅在 dialect.ANNOTATE_ALL_SCOPES 为真时 annotate_scope）；sqlglot/dialects/dialect.py:541 默认 False，只有 sqlglot/dialects/bigquery.py:36 覆盖为 True；annotate_types 是独立规则，见 sqlglot/optimizer/optimizer.py:40-54（排在管线末尾）
验证: PYTHONPATH=. python probes/verify.py 1
输出: after qualify: [('"D"."ID"', 'None'), ('"DIM_GEO"."ID"', 'None'), ('"F"."VALUE"', 'None'), ('"RAW_EVENTS"."ID"', 'None'), ('"RAW_EVENTS"."PAYLOAD"', 'None'), ('"S"."ID"', 'None'), ('"S"."PAYLOAD"', 'None')] / after annotate_types: [..., ('"S"."ID"', 'INT'), ('"S"."PAYLOAD"', 'VARIANT')]
说明: snowflake 下 qualify 后所有 Column.type 仍为 None，显式再跑 annotate_types 才有 INT/VARIANT；qualify 并不内嵌等价于 annotate_types 的全量标注。

## 2
结论: 真
依据: sqlglot/optimizer/qualify_columns.py:66（infer_schema 缺省取 schema.empty，空 schema 时自动推断）与 sqlglot/optimizer/resolver.py:63（infer_schema 时从未知表名解析列）；对 Q 而言推断出的列名/类型与所给 schema 产出的 SQL 相同
验证: PYTHONPATH=. python probes/verify.py 2
输出: equal: True（两份输出逐字符相同，均为 ... SELECT "S"."ID" AS "ID", CAST("F"."VALUE" AS TEXT) AS "V" ... UNION ALL SELECT "DIM_GEO"."ID" AS "ID", NULL AS "_COL_1" ...）

## 3
结论: 真
依据: sqlglot/optimizer/qualify_columns.py:66（带 schema 时 infer_schema=False，未限定的裸 id 在 s/d 多源间无法解析；空 schema 时走推断路径先解析到 s，故能过）；join 歧义例中 a、b 都含 id，推断路径同样无法消歧。报错由校验步骤抛出（见第 4 条）
验证: PYTHONPATH=. python probes/verify.py 3
输出: with-schema OptimizeError -> Column 'ID' could not be resolved. Line: 5, Col: 9 / no-schema OK -> SELECT "S"."ID" AS "ID", CAST("F"."VALUE" AS VARCHAR) AS "V" FROM "SRC" AS "S" ... / join no-schema OptimizeError -> Column 'id' could not be resolved. Line: 1, Col: 65

## 4
结论: 伪
依据: sqlglot/optimizer/qualify.py:98-105 的 qualify_columns_func 调用已正常返回；异常实际在 sqlglot/optimizer/qualify.py:111 调用的 validate_qualify_columns 中抛出，位置为 sqlglot/optimizer/qualify_columns.py:143-150（"Column ... could not be resolved" 分支）
验证: PYTHONPATH=. python probes/verify.py 4
输出: File ".../sqlglot/optimizer/qualify.py", line 111, in qualify / File ".../sqlglot/optimizer/qualify_columns.py", line 150, in validate_qualify_columns

## 5
结论: 伪
依据: sqlglot/parsers/snowflake.py:872（FLATTEN_COLUMNS = ["SEQ","KEY","PATH","INDEX","VALUE","THIS"]）与 sqlglot/parsers/snowflake.py:1097-1108（Snowflake 解析器的 _parse_lateral 在解析期就把六列写进 TableAlias.columns），属于 parse 阶段而非 qualify
验证: PYTHONPATH=. python probes/verify.py 5（只 parse_one，未跑 qualify）
输出: parsed lateral SQL: LATERAL FLATTEN(input => s.payload) AS f(SEQ, KEY, PATH, INDEX, VALUE, THIS) / alias columns at parse: ['SEQ', 'KEY', 'PATH', 'INDEX', 'VALUE', 'THIS']

## 6
结论: 真
依据: sqlglot/optimizer/scope.py:935-937（UDTF 节点以 lateral_sources=sources、scope_type=ScopeType.UDTF 建分支 scope，故父查询的 s/d 及 CTE src 都在其 sources 中）；sqlglot/optimizer/scope.py:416-441（selected_sources 由本 scope 的 references 与 sources 交集得到，LATERAL 内没有 FROM/JOIN 引用，故为空）；scope 类型枚举见 sqlglot/optimizer/scope.py:45
验证: PYTHONPATH=. python probes/verify.py 6
输出: scope_type: ScopeType.UDTF / sources: ['d', 's', 'src'] / selected_sources: {}

## 7
结论: 真
依据: sqlglot/optimizer/qualify_columns.py:1287-1326（qualify_outputs 对无别名的 select 项按枚举下标 i 赋名 f"_col_{i}"，NULL 位于右支第 2 列，i=1；不查左支 UNION 对应列名 V）
验证: PYTHONPATH=. python probes/verify.py 7
输出: NULL alias: _COL_1 / right select: SELECT "DIM_GEO"."ID" AS "ID", NULL AS "_COL_1" FROM "DIM_GEO" AS "DIM_GEO"

## 8
结论: 真
依据: sqlglot/optimizer/qualify_tables.py:69-75（仅当 node.name 不在 cte_names 集合时才对 exp.Table 调用 _qualify）；sqlglot/optimizer/qualify_tables.py:62-67（_qualify 才会写入 db）。故 CTE 名 dim_geo 不加 MYDB，普通表才加
验证: PYTHONPATH=. python probes/verify.py 8
输出: CTE  : WITH "DIM_GEO" AS (SELECT 1 AS "ID") SELECT "DIM_GEO"."ID" AS "ID" FROM "DIM_GEO" AS "DIM_GEO" / PLAIN: SELECT "DIM_GEO"."ID" AS "ID" FROM "MYDB"."DIM_GEO" AS "DIM_GEO"

## 9
结论: 真
依据: sqlglot/optimizer/annotate_types.py:484-509（列类型取自 schema 中该表的列类型；表在 schema 中查不到时回退为 exp.DType.UNKNOWN）。有 WITH 时 src 的列经 CTE 溯源到 raw_events（schema 已知，INT）；无 WITH 时表 src 不在 schema 中
验证: PYTHONPATH=. python probes/verify.py 9（两例均先 qualify 再 annotate_types，带同一 schema）
输出: with-CTE: [('"S"."ID"', 'INT'), ('"S"."PAYLOAD"', 'VARIANT')] / no-CTE : [('"S"."ID"', 'UNKNOWN')]

## 10
结论: 真
依据: sqlglot/dialects/bigquery.py:36（ANNOTATE_ALL_SCOPES = True）配合 sqlglot/optimizer/qualify_columns.py:122-123（qualify 遍历每个 scope 时即 annotator.annotate_scope，INT64 规范化显示为 BIGINT）；duckdb 未开启该开关，qualify 期间不标注，Column.type 为 None
验证: PYTHONPATH=. python probes/verify.py 10
输出: bigquery type: DataType(this=DType.BIGINT, nested=False) | sql: BIGINT / duckdb type: None | sql: None

## 11
结论: 伪
依据: sqlglot/optimizer/optimizer.py:40-54（RULES 顺序：pushdown_predicates 在第 45 行，annotate_types 在第 52 行——下推发生在类型标注之前）；sqlglot/optimizer/pushdown_predicates.py 全文不读取任何表达式 .type，只依赖 scope/selected_sources 做谓词归源
验证: PYTHONPATH=. python probes/verify.py 11
输出: column types before pushdown: {'None'}（随后谓词仍成功下推：子查询内出现 WHERE "a"."x" = 1，JOIN ON 上出现 "b"."y" = 2，外层 WHERE TRUE AND TRUE）

## 12
结论: 真
依据: sqlglot/optimizer/qualify.py:19-113（qualify 各步均为幂等重写：标识符规范化、表/列限定、输出列补名在已限定的 AST 上再跑结果不变）；实测两遍输出逐字符相同
验证: PYTHONPATH=. python probes/verify.py 12
输出: equal: True
