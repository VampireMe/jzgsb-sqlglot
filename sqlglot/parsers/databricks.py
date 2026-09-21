from __future__ import annotations

from sqlglot import exp, parser
from sqlglot.dialects.dialect import build_date_delta, build_formatted_time
from sqlglot.helper import seq_get
from sqlglot.parsers.spark import SparkParser
from sqlglot.tokens import Token, TokenType


class DatabricksParser(SparkParser):
    LOG_DEFAULTS_TO_LN = True
    STRICT_CAST = True
    COLON_IS_VARIANT_EXTRACT = True
    COLON_CHAIN_IS_SINGLE_EXTRACT = False

    # Ordered by length so that "MODEL PROVIDER SERVICES" takes precedence over "MODEL SERVICES"
    POLICY_GRANT_TARGETS = (
        (("MODEL", "PROVIDER", "SERVICES"), "MODEL PROVIDER SERVICES"),
        (("MODEL", "SERVICES"), "MODEL SERVICES"),
        (("MODEL_SERVICES",), "MODEL SERVICES"),
        (("MCP", "SERVICES"), "MCP SERVICES"),
        (("MCP_SERVICES",), "MCP SERVICES"),
        (("AGENT", "SERVICES"), "AGENT SERVICES"),
        (("AGENT_SERVICES",), "AGENT SERVICES"),
        (("MODELS",), "MODELS"),
    )
    POLICY_PRIVILEGE_FOLLOW_TOKENS = {TokenType.FOR, TokenType.COMMA}

    FUNCTIONS = {
        **SparkParser.FUNCTIONS,
        "IFF": exp.If.from_arg_list,
        "GETDATE": exp.CurrentTimestamp.from_arg_list,
        "DATEDIFF": build_date_delta(exp.DateDiff),
        "DATE_DIFF": build_date_delta(exp.DateDiff),
        "NOW": exp.CurrentTimestamp.from_arg_list,
        "TO_DATE": build_formatted_time(exp.TsOrDsToDate),
        "UNIFORM": lambda args: exp.Uniform(
            this=seq_get(args, 0), expression=seq_get(args, 1), seed=seq_get(args, 2)
        ),
    }

    NO_PAREN_FUNCTION_PARSERS = {
        **SparkParser.NO_PAREN_FUNCTION_PARSERS,
        "CURDATE": lambda self: self._parse_curdate(),
    }

    FUNCTION_PARSERS = {
        **SparkParser.FUNCTION_PARSERS,
        "REGR_AVGX": lambda self: self._parse_distinct_arg_function(exp.RegrAvgx, distinct_index=1),
        "REGR_AVGY": lambda self: self._parse_distinct_arg_function(exp.RegrAvgy),
        "REGR_SXX": lambda self: self._parse_distinct_arg_function(exp.RegrSxx, distinct_index=1),
        "REGR_SXY": lambda self: self._parse_distinct_arg_function(exp.RegrSxy),
        "REGR_SYY": lambda self: self._parse_distinct_arg_function(exp.RegrSyy, distinct_index=1),
    }

    FACTOR = {
        **SparkParser.FACTOR,
        TokenType.COLON: exp.JSONExtract,
    }

    COLUMN_OPERATORS = {
        **parser.Parser.COLUMN_OPERATORS,
        TokenType.QDCOLON: lambda self, this, to: self.build_cast(
            False,
            this=this,
            to=to,
        ),
    }
    CAST_COLUMN_OPERATORS = {
        *SparkParser.CAST_COLUMN_OPERATORS,
        TokenType.QDCOLON,
    }

    def _parse_curdate(self) -> exp.CurrentDate:
        # CURDATE, an alias for CURRENT_DATE, has optional parentheses
        if self._match(TokenType.L_PAREN):
            self._match_r_paren()
        return self.expression(exp.CurrentDate())

    def _parse_primary_key_part(self) -> exp.Expr | None:
        this = super()._parse_primary_key_part()
        if this and self._match_text_seq("TIMESERIES"):
            return self.expression(exp.TimeseriesKey(this=this))
        return this

    def _parse_cluster_property(self):
        if self._match_texts(("AUTO", "NONE")):
            return self.expression(exp.ClusterProperty(this=self._prev.text.upper()))
        return super()._parse_cluster_property()

    def _parse_create(self) -> exp.Create | exp.Command:
        # https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-policy
        start = self._prev
        index = self._index
        replace = self._match(TokenType.REPLACE) or self._match_pair(
            TokenType.OR, TokenType.REPLACE
        )
        if self._match_text_seq("POLICY"):
            return self._parse_create_policy(start, replace=replace)
        self._retreat(index)

        return super()._parse_create()

    def _parse_create_policy(self, start: Token, replace: bool = False) -> exp.Create | exp.Command:
        policy_name = self._parse_id_var()

        securable_kind = None
        securable = None
        if self._match(TokenType.ON):
            if self._match_text_seq("METASTORE"):
                securable_kind = "METASTORE"
            elif self._match_texts(("CATALOG", "SCHEMA", "TABLE")):
                securable_kind = self._prev.text.upper()
                securable = self._parse_table_parts()

        comment = self._parse_string() if self._match(TokenType.COMMENT) else None

        if not self._match_texts(("ROW", "COLUMN", "TO"), advance=False):
            return self._parse_as_command(start)

        policy = self._parse_policy()
        policy.set(
            "securable_kind",
            exp.var(securable_kind) if securable_kind else None,
        )
        policy.set("securable", securable)
        policy.set("comment", comment)

        return self.expression(
            exp.Create(
                this=policy_name,
                kind="POLICY",
                replace=replace,
                expression=policy,
            )
        )

    def _parse_policy_principal(self) -> exp.Expr | None:
        if self._match(TokenType.STRING, advance=False):
            return self._parse_string()
        return self._parse_id_var()

    def _parse_policy_when(self) -> exp.Expr:
        # The WHEN condition must not consume the following MATCH COLUMNS clause, which the
        # expression parser may interpret as an implicit alias
        when = self._parse_expression()
        if (
            isinstance(when, exp.Alias)
            and self._prev.text.upper() == "MATCH"
            and self._curr.token_type == TokenType.VAR
            and self._curr.text.upper() == "COLUMNS"
        ):
            self._retreat(self._index - 1)
            return when.this
        return when

    def _parse_policy_privilege(self) -> exp.GrantPrivilege:
        privilege_parts = []
        while self._curr and not self._match_set(
            self.POLICY_PRIVILEGE_FOLLOW_TOKENS, advance=False
        ):
            privilege_parts.append(self._curr.text.upper())
            self._advance()

        if not privilege_parts:
            self.raise_error("Expected a privilege after GRANT")

        return self.expression(exp.GrantPrivilege(this=exp.var(" ".join(privilege_parts))))

    def _parse_policy(self) -> exp.Policy:
        kind: str | None = None
        function_name = None

        if self._match_text_seq("ROW", "FILTER"):
            kind = "ROW FILTER"
            function = self._parse_column()
        elif self._match_text_seq("COLUMN", "MASK"):
            kind = "COLUMN MASK"
            function = self._parse_column()
        else:
            function = None

        if function is not None:
            function_name = function.to_dot()

        principals = (
            self._parse_csv(self._parse_policy_principal) if self._match_text_seq("TO") else None
        )
        except_ = (
            self._parse_csv(self._parse_policy_principal) if self._match(TokenType.EXCEPT) else None
        )

        privileges = None
        target_kind = None
        when = None
        match_columns = None
        on_column = None
        using_columns = None

        if kind:
            self._match_text_seq("FOR", "TABLES")
            if self._match(TokenType.WHEN):
                when = self._parse_policy_when()
            if self._match_text_seq("MATCH", "COLUMNS"):
                match_columns = self._parse_csv(self._parse_expression)
            if kind == "COLUMN MASK" and self._match_text_seq("ON", "COLUMN"):
                on_column = self._parse_id_var()
            if self._match_text_seq("USING", "COLUMNS"):
                using_columns = self._parse_wrapped_csv(self._parse_assignment)
        else:
            if self._match(TokenType.GRANT):
                privileges = self._parse_csv(self._parse_policy_privilege)

                if self._match(TokenType.FOR):
                    target_kind = self._parse_policy_target_kind()

            if self._match(TokenType.WHEN):
                when = self._parse_expression()

        return self.expression(
            exp.Policy(
                kind=exp.var(kind) if kind else None,
                this=function_name,
                principals=principals,
                except_=except_,
                when=when,
                match_columns=match_columns,
                on_column=on_column,
                using_columns=using_columns,
                privileges=privileges,
                target_kind=target_kind,
            )
        )

    def _parse_policy_target_kind(self) -> exp.Var | None:
        for tokens, target in self.POLICY_GRANT_TARGETS:
            if self._match_text_seq(*tokens):
                return exp.var(target)
        return None
