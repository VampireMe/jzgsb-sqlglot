"""Probes for the 12 claims. Run from repo root: PYTHONPATH=. python probes/verify.py [N ...]"""
import sys
import traceback

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.pushdown_predicates import pushdown_predicates
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope

Q = """
WITH src AS (
  SELECT id, payload FROM raw_events
)
SELECT s.id, f.value::STRING AS v
FROM src s
JOIN dim_geo d ON d.id = s.id
LATERAL FLATTEN(input => s.payload) f
UNION ALL
SELECT id, NULL FROM dim_geo
"""
SCHEMA = {
    "raw_events": {"id": "INT", "payload": "VARIANT"},
    "dim_geo": {"id": "INT", "region": "TEXT"},
}


def c1():
    q = qualify(sqlglot.parse_one(Q, dialect="snowflake"), schema=SCHEMA, dialect="snowflake")
    after_qualify = sorted({(c.sql(dialect="snowflake"), str(c.type)) for c in q.find_all(exp.Column)})
    print("after qualify:", after_qualify)
    q2 = qualify(sqlglot.parse_one(Q, dialect="snowflake"), schema=SCHEMA, dialect="snowflake")
    a = annotate_types(q2, schema=SCHEMA, dialect="snowflake")
    after_annotate = sorted({(c.sql(dialect="snowflake"), c.type.sql() if c.type else None)
                             for c in a.find_all(exp.Column)})
    print("after annotate_types:", after_annotate)


def c2():
    a = qualify(sqlglot.parse_one(Q, dialect="snowflake"), schema=SCHEMA, dialect="snowflake").sql()
    b = qualify(sqlglot.parse_one(Q, dialect="snowflake"), dialect="snowflake").sql()
    print("equal:", a == b)
    print(b)


def c3():
    q3 = Q.replace("SELECT s.id, f.value::STRING AS v", "SELECT id, f.value::STRING AS v")
    for label, sch in [("with-schema", SCHEMA), ("no-schema", None)]:
        try:
            out = qualify(sqlglot.parse_one(q3, dialect="snowflake"), schema=sch, dialect="snowflake")
            first = out.find(exp.Select).sql(dialect="snowflake")
            print(label, "OK ->", first[:130])
        except OptimizeError as e:
            print(label, "OptimizeError ->", e)
    q3b = "WITH a AS (SELECT id FROM t1), b AS (SELECT id FROM t2) SELECT id FROM a JOIN b ON a.id = b.id"
    try:
        qualify(sqlglot.parse_one(q3b, dialect="snowflake"))
        print("join no-schema OK")
    except OptimizeError as e:
        print("join no-schema OptimizeError ->", e)


def c4():
    q3 = Q.replace("SELECT s.id, f.value::STRING AS v", "SELECT id, f.value::STRING AS v")
    try:
        qualify(sqlglot.parse_one(q3, dialect="snowflake"), schema=SCHEMA, dialect="snowflake")
    except OptimizeError:
        tb = traceback.format_exc().strip().splitlines()
        for line in tb:
            if "sqlglot/optimizer" in line:
                print(line.strip())


def c5():
    tree = sqlglot.parse_one(Q, dialect="snowflake")
    lat = next(tree.find_all(exp.Lateral))
    print("parsed lateral SQL:", lat.sql(dialect="snowflake"))
    print("alias columns at parse:", [c.name for c in lat.args["alias"].columns])


def c6():
    root = build_scope(sqlglot.parse_one(Q, dialect="snowflake"))
    for sc in traverse_scope(root.expression):
        if isinstance(sc.expression, exp.Lateral):
            print("scope_type:", sc.scope_type)
            print("sources:", sorted(sc.sources))
            print("selected_sources:", sc.selected_sources)


def c7():
    out = qualify(sqlglot.parse_one(Q, dialect="snowflake"), dialect="snowflake")
    null_alias = next(out.find_all(exp.Null)).find_ancestor(exp.Alias)
    print("NULL alias:", null_alias.alias)
    print("right select:", out.expression.sql(dialect="snowflake"))


def c8():
    cte = "WITH dim_geo AS (SELECT 1 AS id) SELECT id FROM dim_geo"
    plain = "SELECT id FROM dim_geo"
    print("CTE  :", qualify(sqlglot.parse_one(cte, dialect="snowflake"), dialect="snowflake", db="mydb").sql(dialect="snowflake"))
    print("PLAIN:", qualify(sqlglot.parse_one(plain, dialect="snowflake"), dialect="snowflake", db="mydb").sql(dialect="snowflake"))


def c9():
    schema = {"raw_events": {"id": "INT", "payload": "VARIANT"}}
    q1 = annotate_types(
        qualify(sqlglot.parse_one(
            "WITH src AS (SELECT id, payload FROM raw_events) SELECT s.id, s.payload FROM src s",
            dialect="snowflake"), schema=schema, dialect="snowflake"),
        schema=schema, dialect="snowflake")
    print("with-CTE:", [(c.sql(dialect="snowflake"), c.type.sql()) for c in q1.find(exp.Select).find_all(exp.Column) if c.table == "S"])
    q2 = annotate_types(
        qualify(sqlglot.parse_one("SELECT s.id FROM src s", dialect="snowflake"),
                schema=schema, dialect="snowflake"),
        schema=schema, dialect="snowflake")
    print("no-CTE :", [(c.sql(dialect="snowflake"), c.type.sql() if c.type else None)
                       for c in q2.find_all(exp.Column)])


def c10():
    for dialect, schema in [("bigquery", {"t": {"x": "INT64"}}), ("duckdb", {"t": {"x": "INT"}})]:
        tree = qualify(sqlglot.parse_one("SELECT x FROM t", dialect=dialect), schema=schema, dialect=dialect)
        col = next(tree.find_all(exp.Column))
        print(dialect, "type:", col.type, "| sql:", col.type.sql() if col.type else None)


def c11():
    schema = {"a": {"id": "INT", "x": "INT"}, "b": {"id": "INT", "y": "INT"}}
    sql = "SELECT * FROM (SELECT id, x FROM a) sub JOIN b ON sub.id = b.id WHERE sub.x = 1 AND b.y = 2"
    tree = qualify(sqlglot.parse_one(sql), schema=schema)
    print("column types before pushdown:", {str(c.type) for c in tree.find_all(exp.Column)})
    out = pushdown_predicates(tree)
    print(out.sql(pretty=True))


def c12():
    t1 = qualify(sqlglot.parse_one(Q, dialect="snowflake"), schema=SCHEMA, dialect="snowflake")
    s1 = t1.sql(dialect="snowflake")
    s2 = qualify(t1, schema=SCHEMA, dialect="snowflake").sql(dialect="snowflake")
    print("equal:", s1 == s2)


CASES = {i: f for i, f in enumerate([c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12], start=1)}

if __name__ == "__main__":
    wanted = [int(x) for x in sys.argv[1:]] or list(range(1, 13))
    for i in wanted:
        print(f"----- claim {i} -----")
        CASES[i]()
