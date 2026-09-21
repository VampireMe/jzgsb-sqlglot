from __future__ import annotations

from sqlglot import exp, parser
from sqlglot.dialects.dialect import build_date_delta, build_formatted_time
from sqlglot.helper import seq_get
from sqlglot.parsers.spark import SparkParser
from sqlglot.tokens import TokenType


class DatabricksParser(SparkParser):
    LOG_DEFAULTS_TO_LN = True
    STRICT_CAST = True
    COLON_IS_VARIANT_EXTRACT = True
    COLON_CHAIN_IS_SINGLE_EXTRACT = False

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
        index = self._index
        replace = self._match_pair(TokenType.OR, TokenType.REPLACE)
        if self._match_text_seq("POLICY", advance=False):
            policy = self._parse_policy()
            if policy:
                return self.expression(exp.Create(this=policy, kind="POLICY", replace=replace))

        self._retreat(index)

        return super()._parse_create()

    def _parse_policy(self) -> exp.Policy | None:
        if not self._match_text_seq("POLICY"):
            return None

        name = self._parse_id_var()
        if not name or not self._match(TokenType.ON):
            return None

        securable = self._parse_policy_securable()
        comment = self._match(TokenType.COMMENT) and self._parse_string()

        if self._match_text_seq("ROW", "FILTER"):
            policy = self._parse_row_filter_policy(name, securable, comment)
        elif self._match_text_seq("COLUMN", "MASK"):
            policy = self._parse_column_mask_policy(name, securable, comment)
        else:
            policy = self._parse_grant_policy(name, securable, comment)

        if policy is None:
            return None

        if not policy.args.get("principals") or self._curr:
            self.raise_error(f"Malformed CREATE POLICY statement at {self._curr}")

        return policy

    def _parse_policy_securable(self) -> exp.PolicySecurable | None:
        if self._match_text_seq("METASTORE"):
            return self.expression(exp.PolicySecurable(kind="METASTORE"))

        elif self._match_texts(("CATALOG", "SCHEMA", "TABLE")):
            kind = self._prev.text.upper()
            this = self._parse_table_parts(schema=kind == "SCHEMA")
            return self.expression(exp.PolicySecurable(kind=kind, this=this))

        self.raise_error(f"Expected METASTORE, CATALOG, SCHEMA or TABLE after ON, got {self._curr}")

    def _parse_policy_principal(self) -> exp.Expression | None:
        if self._match(TokenType.STRING, advance=False):
            return self._parse_string()
        return self._parse_id_var()

    def _parse_policy_match_columns(self) -> list[exp.Expression] | None:
        if not self._match_text_seq("MATCH", "COLUMNS"):
            return None

        boundary_texts = {"TO", "ON", "EXCEPT", "USING", "MATCH", "COLUMNS"}

        def parse_match_column() -> exp.Expression:
            condition = self._parse_alias(self._parse_assignment(), explicit=True)

            # The AS in "condition AS alias" is optional ("condition alias")
            if self._match(TokenType.ALIAS):
                return self.expression(exp.Alias(this=condition, alias=self._parse_id_var()))

            if self._curr.token_type in (TokenType.VAR, TokenType.IDENTIFIER) and (
                self._curr.token_type == TokenType.IDENTIFIER
                or self._curr.text.upper() not in boundary_texts
            ):
                return self.expression(
                    exp.Alias(this=condition, alias=self._parse_id_var(any_token=False))
                )

            return condition

        return self._parse_csv(parse_match_column)

    def _parse_policy_using_columns(self) -> exp.Tuple | None:
        if not self._match_text_seq("USING", "COLUMNS"):
            return None
        return self.expression(
            exp.Tuple(expressions=self._parse_wrapped_csv(self._parse_expression))
        )

    def _parse_policy_to_except(self) -> tuple[list | None, list | None]:
        principals = self._match_text_seq("TO") and self._parse_csv(self._parse_policy_principal)
        except_ = self._match_text_seq("EXCEPT") and self._parse_csv(self._parse_policy_principal)
        return principals, except_

    def _parse_policy_condition(self) -> exp.Expression | None:
        # _parse_expression accepts implicit aliases, so boundary keywords like MATCH, USING
        # and ON would be consumed as aliases; only explicit aliases are allowed here
        return self._parse_alias(self._parse_assignment(), explicit=True)

    def _parse_row_filter_policy(
        self, name: exp.Expression, securable: exp.PolicySecurable, comment: exp.Expression | None
    ) -> exp.RowFilterPolicy | None:
        function = self._parse_table_parts(schema=True)

        principals, except_ = self._parse_policy_to_except()

        if not self._match_text_seq("FOR", "TABLES"):
            self.raise_error("Expected FOR TABLES in row filter policy")

        when = self._match_text_seq("WHEN") and self._parse_policy_condition()
        match_columns = self._parse_policy_match_columns()
        using_columns = self._parse_policy_using_columns()

        return self.expression(
            exp.RowFilterPolicy(
                name=name,
                securable=securable,
                comment=comment,
                function=function,
                principals=principals,
                except_=except_,
                when=when,
                match_columns=match_columns,
                using_columns=using_columns,
            )
        )

    def _parse_column_mask_policy(
        self, name: exp.Expression, securable: exp.PolicySecurable, comment: exp.Expression | None
    ) -> exp.ColumnMaskPolicy | None:
        function = self._parse_table_parts(schema=True)

        principals, except_ = self._parse_policy_to_except()

        if not self._match_text_seq("FOR", "TABLES"):
            self.raise_error("Expected FOR TABLES in column mask policy")

        when = self._match_text_seq("WHEN") and self._parse_policy_condition()
        match_columns = self._parse_policy_match_columns()

        if not self._match_text_seq("ON", "COLUMN"):
            self.raise_error("Expected ON COLUMN in column mask policy")

        on_column = self._parse_id_var()
        using_columns = self._parse_policy_using_columns()

        return self.expression(
            exp.ColumnMaskPolicy(
                name=name,
                securable=securable,
                comment=comment,
                function=function,
                principals=principals,
                except_=except_,
                when=when,
                match_columns=match_columns,
                on_column=on_column,
                using_columns=using_columns,
            )
        )

    def _parse_policy_privileges(self) -> list[exp.GrantPrivilege]:
        def parse_privilege() -> exp.GrantPrivilege:
            privilege_parts = []
            while self._curr and self._curr.token_type not in (TokenType.COMMA, TokenType.FOR):
                privilege_parts.append(self._curr.text.upper())
                self._advance()

            return self.expression(exp.GrantPrivilege(this=exp.var(" ".join(privilege_parts))))

        return self._parse_csv(parse_privilege)

    def _parse_policy_target(self) -> str | None:
        for target in (
            ("MODEL", "PROVIDER", "SERVICES"),
            ("MODEL", "SERVICES"),
            ("MCP", "SERVICES"),
            ("AGENT", "SERVICES"),
            ("MODELS",),
        ):
            if self._match_text_seq(*target, advance=False):
                self._match_text_seq(*target)
                return " ".join(target)

        # The underscored form (e.g. MODEL_SERVICES) is also accepted
        if self._curr.token_type in (TokenType.VAR, TokenType.IDENTIFIER):
            underscored = self._curr.text.upper()
            normalized = underscored.replace("_", " ")
            if normalized in {
                "MODEL PROVIDER SERVICES",
                "MODEL SERVICES",
                "MCP SERVICES",
                "AGENT SERVICES",
                "MODELS",
            }:
                self._advance()
                return normalized

        return None

    def _parse_grant_policy(
        self, name: exp.Expression, securable: exp.PolicySecurable, comment: exp.Expression | None
    ) -> exp.GrantPolicy | None:
        principals, except_ = self._parse_policy_to_except()

        if not self._match_text_seq("GRANT"):
            return None

        privileges = self._parse_policy_privileges()

        if not self._match(TokenType.FOR):
            self.raise_error("Expected FOR in grant policy")

        target = self._parse_policy_target()
        if not target:
            self.raise_error("Expected a valid grant target after FOR")

        when = self._match_text_seq("WHEN") and self._parse_policy_condition()

        return self.expression(
            exp.GrantPolicy(
                name=name,
                securable=securable,
                comment=comment,
                principals=principals,
                except_=except_,
                privileges=privileges,
                target=target,
                when=when,
            )
        )
