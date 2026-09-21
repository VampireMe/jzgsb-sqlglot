# sqlglot optimizer 说法核实结果

统一查询 Q（snowflake 方言）与 schema 见题目。探针脚本为 `/tmp/probe.py`，在仓库根目录用 `python /tmp/probe.py` 运行（sqlglot 直接从仓库源码导入）。

## 1
结论: 伪
依据: sqlglot/optimizer/qualify_columns.py:122（`if dialect.ANNOTATE_ALL_SCOPES: annotator.annotate_scope(scope)`）；默认值 sqlglot/dialects/dialect.py:541（`ANNOTATE_ALL_SCOPES = False`），仅 BigQuery 开启 sqlglot/dialects/bigquery.py:36
验证: python /tmp/probe.py（C1 段：对 Q 跑 snowflake qualify 后检查所有 Column 的 type）
输出: C1 snowflake qualify, column types: [('ID', 'None'), ('PAYLOAD', 'None'), ('VALUE', 'None')]
说明: qualify 内部虽构造了 TypeAnnotator（qualify_columns.py:63），但只用于 star 展开时的类型强制；逐 scope 标注仅在 `ANNOTATE_ALL_SCOPES=True` 的方言（BigQuery）下发生。snowflake 下 qualify 后列类型全是 None，不等价于内嵌 annotate_types。

## 2
结论: 真
依据: sqlglot/optimizer/qualify_columns.py:64（`infer_schema = schema.empty if infer_schema is None else infer_schema`，无 schema 时转为推断模式）
验证: python /tmp/probe.py（C2 段：Q 分别带/不带 schema 跑 qualify 后比较 SQL）
输出: C2 identical: True（两种输出均为 `... SELECT "S"."ID" AS "ID", CAST("F"."VALUE" AS VARCHAR) AS "V" ... NULL AS "_COL_1" ...`，逐字符相同）

## 3
结论: 真
依据: sqlglot/optimizer/qualify_columns.py:64（schema 为空时 infer_schema=True，未限定列按推断解析）；schema 非空时无法解析的列留待 validate 报错（qualify_columns.py:143）
验证: python /tmp/probe.py（C3/C3b 段）
输出: C3 Q3 with schema -> OptimizeError: Column 'ID' could not be resolved. Line: 5, Col: 9；C3 Q3 no schema -> 正常输出 `SELECT "S"."ID" AS "ID", ...`；C3b no schema -> OptimizeError: Column 'ID' could not be resolved. Line: 1, Col: 65
说明: 三个子现象全部复现：带 schema 报 OptimizeError；不带 schema 时 Q 的 id 被解析成 "S"."ID"；不带 schema 时 a/b 两 CTE 的查询反而报同类错。加 schema 确实可能让原本能跑的 qualify 报错。

## 4
结论: 伪
依据: sqlglot/optimizer/qualify.py:110-111（错误出自 `validate_qualify_columns` 步，而非 qualify_columns 步）；抛错点 sqlglot/optimizer/qualify_columns.py:150（`raise OptimizeError(error_msg)`，消息在 :143 构造）
验证: python /tmp/probe.py（C4 段打印 traceback）；另跑 `qualify(..., schema=SCHEMA, validate_qualify_columns=False)`
输出: traceback 末帧为 `File ".../sqlglot/optimizer/qualify.py", line 111, in qualify` -> `validate_qualify_columns_func` -> `File ".../sqlglot/optimizer/qualify_columns.py", line 150, in validate_qualify_columns`；关掉 validate 后输出 `validate_qualify_columns=False -> OK, no error`
说明: 报错的是 qualify() 里 qualify_columns 之后的 validate_qualify_columns 校验步；qualify_columns 本身把解析不了的列留成未限定，由校验步统一抛错。

## 5
结论: 伪
依据: sqlglot/parsers/snowflake.py:1098-1110（`_parse_lateral` 在解析期就把 `FLATTEN_COLUMNS` 写到 LATERAL 的 alias columns 上）；列清单定义在 sqlglot/parsers/snowflake.py:872
验证: python -c 探针：parse_one(Q, 'snowflake') 之后、qualify 之前直接读 `exp.Lateral` 节点的 alias columns
输出: alias columns right after parse: ['SEQ', 'KEY', 'PATH', 'INDEX', 'VALUE', 'THIS']（alias name: f）
说明: 六个输出列是 snowflake 解析器在 parse 阶段就挂到 AST 上的，qualify 只是保留/规范化它们，并非 qualify 执行时才添加。

## 6
结论: 真
依据: sqlglot/optimizer/scope.py:989（`_traverse_udtfs` 生成 UDTF scope）；UDTF scope 通过 `is_udtf`（scope.py:575-577）标识，lateral 源被并入其 sources
验证: python /tmp/probe.py（C6 段：对 Q 跑 traverse_scope 打印每个 scope 的类型/sources/selected_sources）
输出: C6 scope: ScopeType.UDTF | sources: ['s', 'd', 'src'] | selected: []
说明: LATERAL FLATTEN f 确实是 ScopeType.UDTF 的独立 scope，sources 含 s 和 d（还含 CTE 名 src），selected_sources 为空列表。

## 7
结论: 真
依据: sqlglot/optimizer/qualify_columns.py:1325（`qualify_outputs` 中 `alias=selection.output_name or f"_col_{i}"`，按 SELECT 位置编号）；函数起点 qualify_columns.py:1287
验证: python /tmp/probe.py（C7 段：打印 qualify 后的完整 SQL）
输出: ... UNION ALL SELECT "DIM_GEO"."ID" AS "ID", NULL AS "_COL_1" FROM "DIM_GEO" AS "DIM_GEO"
说明: 右支 NULL 被命名为 "_COL_1"（第 1 个位置、从 0 计），是 qualify_outputs 按位置生成的，与左支对应列的别名 V 无关。

## 8
结论: 真
依据: sqlglot/optimizer/qualify_tables.py:71-76（先收集 `cte_names`，walk 时 `node.name not in cte_names` 才加 db 前缀）
验证: python /tmp/probe.py（C8 段：db="mydb" 下分别带/不带 WITH 跑 qualify）
输出: C8 with CTE: `... FROM "DIM_GEO" AS "DIM_GEO"`（无 MYDB 前缀）；C8 no CTE: `SELECT "DIM_GEO"."ID" AS "ID" FROM "MYDB"."DIM_GEO" AS "DIM_GEO"`

## 9
结论: 真
依据: sqlglot/optimizer/annotate_types.py:399（`annotate_scope` 按 scope.sources 解析来源）与 :484（`self.schema.get_column_type(...)` 查 schema）；CTE 经其定义追溯到 raw_events（在 schema 中），无 WITH 时 src 是不在 schema 里的表
验证: python /tmp/probe.py（C9 段：两个查询都先 qualify 再 annotate_types，均带 schema）
输出: C9 CTE version ID type: INT；C9 no-CTE version id type: UNKNOWN

## 10
结论: 真
依据: sqlglot/dialects/bigquery.py:36（`ANNOTATE_ALL_SCOPES = True`）配合 sqlglot/optimizer/qualify_columns.py:122-123；duckdb 走默认值 False（dialect.py:541）
验证: python /tmp/probe.py（C10 段：`SELECT x FROM t` 分别在 bigquery/duckdb 上 qualify 后读列 type）
输出: C10 bigquery x type after qualify: BIGINT | sql: SELECT `t`.`x` AS `x` FROM `t` AS `t`；C10 duckdb  x type after qualify: None | sql: SELECT "t"."x" AS "x" FROM "t" AS "t"
说明: bigquery 下 qualify 顺带做了 scope 类型标注（INT64 显示为 BIGINT），duckdb 下列类型仍是 None。这正是第 1 条为伪的原因——该行为是方言条件化的。

## 11
结论: 伪
依据: sqlglot/optimizer/optimizer.py:40-55（RULES 默认顺序：pushdown_predicates 在 :45，annotate_types 在 :52，下推先于标注）；sqlglot/optimizer/pushdown_predicates.py 全文不引用 annotate_types/类型信息（grep 无命中）
验证: grep -n "RULES" -A 15 sqlglot/optimizer/optimizer.py；grep -n "annotate\|type" sqlglot/optimizer/pushdown_predicates.py
输出: RULES = (qualify, pushdown_projections, normalize, unnest_subqueries, pushdown_predicates, optimize_joins, eliminate_subqueries, merge_subqueries, eliminate_joins, eliminate_ctes, quote_identifiers, annotate_types, canonicalize, simplify)；pushdown_predicates.py 中 annotate/type 零命中
说明: 默认管线里 pushdown_predicates 跑在 annotate_types 之前，其输入是 normalize 后的 AST，不依赖类型标注。

## 12
结论: 真
依据: sqlglot/optimizer/qualify.py:80-111（管线各步对已限定/已加引号的 AST 幂等：normalize_identifiers、qualify_tables、qualify_columns、quote_identifiers 均不重复改写）
验证: python /tmp/probe.py（C12 段：qualify 一遍后的结果 copy 再 qualify 一遍，比较 SQL）
输出: C12 equal: True（两遍均输出同一字符串：`WITH "SRC" AS (...) SELECT "S"."ID" AS "ID", CAST("F"."VALUE" AS VARCHAR) AS "V" ... NULL AS "_COL_1" FROM "DIM_GEO" AS "DIM_GEO"`）
