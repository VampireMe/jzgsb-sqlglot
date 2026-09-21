from __future__ import annotations


from sqlglot import exp, transforms
from sqlglot.dialects.dialect import (
    date_delta_sql,
    timestamptrunc_sql,
    groupconcat_sql,
)
from sqlglot.generators.spark import SparkGenerator


class DatabricksGenerator(SparkGenerator):
    TABLESAMPLE_SEED_KEYWORD = "REPEATABLE"
    COPY_PARAMS_ARE_WRAPPED = False
    COPY_PARAMS_EQ_REQUIRED = True
    JSON_PATH_SINGLE_QUOTE_ESCAPE = False
    JSON_PATH_KEY_QUOTED_FORCES_BRACKETS = True
    SAFE_JSON_PATH_KEY_RE = exp.SAFE_IDENTIFIER_RE
    QUOTE_JSON_PATH = False
    PARSE_JSON_NAME: str | None = "PARSE_JSON"

    TRANSFORMS = {
        k: v
        for k, v in {
            **SparkGenerator.TRANSFORMS,
            exp.CurrentVersion: lambda *_: "CURRENT_VERSION()",
            exp.DateAdd: date_delta_sql("DATEADD"),
            exp.DateDiff: date_delta_sql("DATEDIFF"),
            exp.DatetimeAdd: lambda self, e: self.func(
                "TIMESTAMPADD", e.unit, e.expression, e.this
            ),
            exp.DatetimeSub: lambda self, e: self.func(
                "TIMESTAMPADD",
                e.unit,
                exp.Mul(this=e.expression, expression=exp.Literal.number(-1)),
                e.this,
            ),
            exp.DatetimeTrunc: timestamptrunc_sql(),
            exp.GroupConcat: groupconcat_sql,
            exp.Select: transforms.preprocess(
                [
                    transforms.eliminate_distinct_on,
                    transforms.unnest_to_explode,
                    transforms.any_to_exists,
                ]
            ),
            exp.JSONExtract: lambda self, e: f"{self.sql(e, 'this')}:{self.sql(e, 'expression')}",
            exp.JSONPathRoot: lambda self, e: (
                "$" if isinstance(e.parent and e.parent.parent, exp.JSONExtractScalar) else ""
            ),
            exp.ToChar: lambda self, e: (
                self.cast_sql(exp.Cast(this=e.this, to=exp.DataType(this="STRING")))
                if e.args.get("is_numeric")
                else self.function_fallback_sql(e)
            ),
            exp.CurrentCatalog: lambda *_: "CURRENT_CATALOG()",
            exp.RegexpLike: None,
            exp.TryCast: None,
            exp.RegrAvgx: lambda self, e: self._regr_sql(e),
            exp.RegrSxx: lambda self, e: self._regr_sql(e),
            exp.RegrSyy: lambda self, e: self._regr_sql(e),
        }.items()
        if v is not None
    }

    TYPE_MAPPING = {
        **SparkGenerator.TYPE_MAPPING,
        exp.DType.NULL: "VOID",
    }

    def create_sql(self, expression: exp.Create) -> str:
        body = expression.expression
        if (
            body
            and not isinstance(body, exp.Return)
            and expression.kind == "FUNCTION"
            and any(p.args.get("is_table") for p in expression.find_all(exp.ReturnsProperty))
        ):
            expression.set("expression", exp.Return(this=body))

        if expression.kind == "POLICY":
            replace = " OR REPLACE" if expression.args.get("replace") else ""
            return f"CREATE{replace} {self.sql(expression, 'this')}"

        return super().create_sql(expression)

    def columndef_sql(self, expression: exp.ColumnDef, sep: str = " ") -> str:
        constraint = expression.find(exp.GeneratedAsIdentityColumnConstraint)
        kind = expression.kind
        if (
            constraint
            and isinstance(kind, exp.DataType)
            and kind.this in exp.DataType.INTEGER_TYPES
        ):
            # only BIGINT generated identity constraints are supported
            expression.set("kind", exp.DType.BIGINT.into_expr())

        return super().columndef_sql(expression, sep)

    def timeserieskey_sql(self, expression: exp.TimeseriesKey) -> str:
        return f"{self.sql(expression, 'this')} TIMESERIES"

    def jsonpath_sql(self, expression: exp.JSONPath) -> str:
        path = super().jsonpath_sql(expression)

        if isinstance(expression.parent, exp.JSONExtractScalar):
            path = self.escape_str(path)
            return f"{self.dialect.QUOTE_START}{path}{self.dialect.QUOTE_END}"

        return path

    def uniform_sql(self, expression: exp.Uniform) -> str:
        gen = expression.args.get("gen")
        seed = expression.args.get("seed")

        # From Snowflake UNIFORM(min, max, gen) as RANDOM(), RANDOM(seed), or constant value -> Extract seed
        if gen:
            seed = gen.this

        return self.func("UNIFORM", expression.this, expression.expression, seed)

    def _regr_sql(self, expression: exp.RegrAvgx | exp.RegrSxx | exp.RegrSyy) -> str:
        name = expression.sql_name()
        x = expression.expression
        if isinstance(x, exp.Distinct):
            return self.func(name, exp.Distinct(expressions=[expression.this]), *x.expressions)
        return self.func(name, expression.this, x)

    def clusterproperty_sql(self, expression):
        this = self.sql(expression, "this") or f"({self.expressions(expression, flat=True)})"
        return f"CLUSTER BY {this}"

    def policysecurable_sql(self, expression: exp.PolicySecurable) -> str:
        kind = self.sql(expression, "kind")
        this = self.sql(expression, "this")
        return f"{kind} {this}".strip()

    def _policy_to_except_sql(self, expression: exp.Policy) -> str:
        principals = self.expressions(expression, key="principals", flat=True)
        principals = f"TO {principals}" if principals else ""

        except_ = self.expressions(expression, key="except_", flat=True)
        except_ = f" EXCEPT {except_}" if except_ else ""

        return f"{principals}{except_}".strip()

    def _policy_header_sql(self, expression: exp.Policy) -> str:
        securable = self.sql(expression, "securable")
        comment = self.sql(expression, "comment")
        comment = f" COMMENT {comment}" if comment else ""
        return f"POLICY {self.sql(expression, 'name')} ON {securable}{comment}"

    def _policy_sql(self, expression: exp.Policy, body: str, tail: str = "") -> str:
        to_except = self._policy_to_except_sql(expression)
        to_except = f" {to_except}" if to_except else ""
        return f"{self._policy_header_sql(expression)} {body}{to_except}{tail}"

    def rowfilterpolicy_sql(self, expression: exp.RowFilterPolicy) -> str:
        when = self.sql(expression, "when")
        when = f" WHEN {when}" if when else ""

        match_columns = self.expressions(expression, key="match_columns", flat=True)
        match_columns = f" MATCH COLUMNS {match_columns}" if match_columns else ""

        using_columns = self.sql(expression, "using_columns")
        using_columns = f" USING COLUMNS {using_columns}" if using_columns else ""

        body = f"ROW FILTER {self.sql(expression, 'function')}"
        tail = f" FOR TABLES{when}{match_columns}{using_columns}"
        return self._policy_sql(expression, body, tail)

    def columnmaskpolicy_sql(self, expression: exp.ColumnMaskPolicy) -> str:
        when = self.sql(expression, "when")
        when = f" WHEN {when}" if when else ""

        match_columns = self.expressions(expression, key="match_columns", flat=True)
        match_columns = f" MATCH COLUMNS {match_columns}" if match_columns else ""

        using_columns = self.sql(expression, "using_columns")
        using_columns = f" USING COLUMNS {using_columns}" if using_columns else ""

        body = f"COLUMN MASK {self.sql(expression, 'function')}"
        tail = (
            f" FOR TABLES{when}{match_columns} ON COLUMN "
            f"{self.sql(expression, 'on_column')}{using_columns}"
        )
        return self._policy_sql(expression, body, tail)

    def grantpolicy_sql(self, expression: exp.GrantPolicy) -> str:
        privileges = self.expressions(expression, key="privileges", flat=True)
        target = self.sql(expression, "target")

        when = self.sql(expression, "when")
        when = f" WHEN {when}" if when else ""

        to_except = self._policy_to_except_sql(expression)
        to_except = f"{to_except} " if to_except else ""
        return (
            f"{self._policy_header_sql(expression)} {to_except}"
            f"GRANT {privileges} FOR {target}{when}"
        )
