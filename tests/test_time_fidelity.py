"""
Fidelity tests for the dialect date/time format-string conversion pipeline.

The pipeline shared by every dialect transpilation is:

  source format --(dialect.TIME_MAPPING / TIME_TRIE)--> canonical strftime IR
  canonical IR --(dialect.INVERSE_TIME_MAPPING / INVERSE_TIME_TRIE)--> target

Both directions are driven by data tables (plus runtime-derived inverses in
``Dialect.__new__``) and consumed by ``sqlglot.time.format_time``'s longest-match
trie walk. ``tests/test_time.py`` only performs structural checks on those
tables; the tests below pin the *semantic* content of the mappings against the
vendor documentation, the algorithm's observable behavior, and end-to-end
transpilation results.

Ground truth here is expressed in "semantic slots" (year, padded day, 12-hour
hour, ...) rather than copied from the implementation, so a wrong value in a
mapping table breaks a test.
"""

from __future__ import annotations

import re
import unittest

from sqlglot import Dialect, exp, parse_one
from sqlglot.dialects.dialect import STRICT_TIME_FORMATS
from sqlglot.generators.hive import _lenient_parse_format
from sqlglot.time import format_time

# ---------------------------------------------------------------------------
# Semantic model
# ---------------------------------------------------------------------------
#
# Each format atom means a date/time component plus a rendering:
#   kind:  what it prints (year, month, hour, ...)
#   pad:   "p" zero-padded fixed width, "n" no padding, "s" space-padded
#   width: digit count / name width / precision (None when not applicable)

CANONICAL_SEMANTICS: dict[str, tuple] = {
    # date
    "%Y": ("year", "p", 4),
    "%y": ("year2", "p", 2),
    "%m": ("month", "p", 2),
    "%-m": ("month", "n", None),
    "%d": ("day", "p", 2),
    "%-d": ("day", "n", None),
    "%e": ("day", "s", 2),
    "%j": ("yearday", "p", 3),
    "%-j": ("yearday", "n", None),
    "%u": ("weekday1", "n", None),
    "%w": ("weekday0", "n", None),
    "%A": ("dayname", None, "long"),
    "%a": ("dayname", None, "short"),
    "%B": ("monthname", None, "long"),
    "%b": ("monthname", None, "short"),
    # time
    "%H": ("hour24", "p", 2),
    "%-H": ("hour24", "n", None),
    "%I": ("hour12", "p", 2),
    "%-I": ("hour12", "n", None),
    "%M": ("minute", "p", 2),
    "%-M": ("minute", "n", None),
    "%S": ("second", "p", 2),
    "%-S": ("second", "n", None),
    "%p": ("ampm", None, None),
    # fractional seconds
    "%f": ("fraction", "p", 6),
    "%f_zero": ("fraction", None, 0),
    "%f_one": ("fraction", "p", 1),
    "%f_two": ("fraction", "p", 2),
    "%f_three": ("fraction", "p", 3),
    "%f_four": ("fraction", "p", 4),
    "%f_five": ("fraction", "p", 5),
    "%f_seven": ("fraction", "p", 7),
    "%f_eight": ("fraction", "p", 8),
    "%f_nine": ("fraction", "p", 9),
    "%n": ("fraction_ns", "p", 9),
    "%g": ("fraction_ms", "p", 3),
    # time zone
    "%z": ("tzoffset", None, "hhmm"),
    "%:z": ("tzoffset", None, "hh:mm"),
    "%-z": ("tzoffset", None, "hh"),
    "%Z": ("tzname", None, None),
    # ISO week / year, week numbering
    "%V": ("isoweek", "p", 2),
    "%G": ("isoyear", "p", 4),
    "%U": ("week_sun", "p", 2),
    "%W": ("week_mon", "p", 2),
}

_CANONICAL_ATOM = re.compile(r"%(?:f_\w+|[mdHIMS]strict|[-:][A-Za-z]|[A-Za-z%])")

# ---------------------------------------------------------------------------
# Documentation-derived ground truth per dialect
# ---------------------------------------------------------------------------
#
# Only atoms whose vendor-documented meaning is unambiguous are listed; this is
# a curated table, not a dump of the implementation, so any entry that drifts
# from the documentation is caught.

DIALECT_ATOM_SEMANTICS: dict[str, dict[str, tuple]] = {
    # https://dev.mysql.com/doc/refman/8.4/en/date-and-time-functions.html#function_date-format
    "mysql": {
        "%Y": ("year", "p", 4),
        "%y": ("year2", "p", 2),
        "%m": ("month", "p", 2),
        "%c": ("month", "n", None),
        "%d": ("day", "p", 2),
        "%e": ("day", "n", None),
        "%M": ("monthname", None, "long"),
        "%b": ("monthname", None, "short"),
        "%W": ("dayname", None, "long"),
        "%a": ("dayname", None, "short"),
        "%H": ("hour24", "p", 2),
        "%k": ("hour24", "n", None),
        "%h": ("hour12", "p", 2),
        "%I": ("hour12", "p", 2),
        "%l": ("hour12", "n", None),
        "%i": ("minute", "p", 2),
        "%S": ("second", "p", 2),
        "%s": ("second", "p", 2),
        "%f": ("fraction", "p", 6),
        "%p": ("ampm", None, None),
        "%j": ("yearday", "p", 3),
        "%U": ("week_sun", "p", 2),
        "%T": ("timeiso", None, None),
        "%r": ("time12", None, None),
    },
    # https://docs.snowflake.com/en/sql-reference/functions-conversion
    "snowflake": {
        "YYYY": ("year", "p", 4),
        "yyyy": ("year", "p", 4),
        "YY": ("year2", "p", 2),
        "yy": ("year2", "p", 2),
        "MMMM": ("monthname", None, "long"),
        "mmmm": ("monthname", None, "long"),
        "MON": ("monthname", None, "short"),
        "mon": ("monthname", None, "short"),
        "MM": ("month", "p", 2),
        "mm": ("month", "p", 2),
        "DD": ("day", "p", 2),
        "dd": ("day", "n", None),
        "DY": ("dayname", None, "short"),
        "HH24": ("hour24", "p", 2),
        "hh24": ("hour24", "p", 2),
        "HH12": ("hour12", "p", 2),
        "hh12": ("hour12", "p", 2),
        "MI": ("minute", "p", 2),
        "mi": ("minute", "p", 2),
        "SS": ("second", "p", 2),
        "ss": ("second", "p", 2),
        "FF6": ("fraction", "p", 6),
        "ff6": ("fraction", "p", 6),
        "FF3": ("fraction", "p", 3),
        "ff3": ("fraction", "p", 3),
        "FF9": ("fraction", "p", 9),
        "ff9": ("fraction", "p", 9),
        "AM": ("ampm", None, None),
        "PM": ("ampm", None, None),
        "am": ("ampm", None, None),
        "pm": ("ampm", None, None),
        "TZHTZM": ("tzoffset", None, "hhmm"),
        "tzhtzm": ("tzoffset", None, "hhmm"),
        "TZH:TZM": ("tzoffset", None, "hh:mm"),
        "tzh:tzm": ("tzoffset", None, "hh:mm"),
        "TZH": ("tzoffset", None, "hh"),
        "tzh": ("tzoffset", None, "hh"),
    },
    # https://duckdb.org/docs/sql/functions/timestamp.html (strftime patterns)
    "duckdb": {
        "%Y": ("year", "p", 4),
        "%y": ("year2", "p", 2),
        "%m": ("month", "p", 2),
        "%-m": ("month", "n", None),
        "%d": ("day", "p", 2),
        "%-d": ("day", "n", None),
        "%e": ("day", "s", 2),
        "%H": ("hour24", "p", 2),
        "%-H": ("hour24", "n", None),
        "%I": ("hour12", "p", 2),
        "%-I": ("hour12", "n", None),
        "%M": ("minute", "p", 2),
        "%S": ("second", "p", 2),
        "%p": ("ampm", None, None),
        "%f": ("fraction", "p", 6),
        "%g": ("fraction_ms", "p", 3),
        "%n": ("fraction_ns", "p", 9),
        "%j": ("yearday", "p", 3),
        "%A": ("dayname", None, "long"),
        "%a": ("dayname", None, "short"),
        "%B": ("monthname", None, "long"),
        "%b": ("monthname", None, "short"),
        "%z": ("tzoffset", None, "hhmm"),
        "%Z": ("tzname", None, None),
        "%u": ("weekday1", "n", None),
        "%w": ("weekday0", "n", None),
    },
    # https://www.postgresql.org/docs/current/functions-formatting.html
    "postgres": {
        "YYYY": ("year", "p", 4),
        "yyyy": ("year", "p", 4),
        "YY": ("year2", "p", 2),
        "yy": ("year2", "p", 2),
        "MM": ("month", "p", 2),
        "mm": ("month", "p", 2),
        "DD": ("day", "p", 2),
        "dd": ("day", "p", 2),
        "FMMM": ("month", "n", None),
        "FMDD": ("day", "n", None),
        "HH24": ("hour24", "p", 2),
        "HH12": ("hour12", "p", 2),
        "MI": ("minute", "p", 2),
        "mi": ("minute", "p", 2),
        "SS": ("second", "p", 2),
        "ss": ("second", "p", 2),
        "FMMI": ("minute", "n", None),
        "FMSS": ("second", "n", None),
        "FMHH24": ("hour24", "n", None),
        "FMHH12": ("hour12", "n", None),
        "US": ("fraction", "p", 6),
        "DDD": ("yearday", "p", 3),
        "FMDDD": ("yearday", "n", None),
        "TMMonth": ("monthname", None, "long"),
        "TMMon": ("monthname", None, "short"),
        "TMDay": ("dayname", None, "long"),
        "TMDy": ("dayname", None, "short"),
        "WW": ("week_sun", "p", 2),
        "OF": ("tzoffset", None, "hhmm"),
        "TZ": ("tzname", None, None),
    },
    # https://docs.oracle.com/database/121/SQLRF/sql_elements004.htm
    "oracle": {
        "YYYY": ("year", "p", 4),
        "YY": ("year2", "p", 2),
        "MM": ("month", "p", 2),
        "DD": ("day", "p", 2),
        "DDD": ("yearday", "p", 3),
        "HH24": ("hour24", "p", 2),
        "HH": ("hour12", "p", 2),
        "HH12": ("hour12", "p", 2),
        "MI": ("minute", "p", 2),
        "SS": ("second", "p", 2),
        "FF6": ("fraction", "p", 6),
        "MONTH": ("monthname", None, "long"),
        "MON": ("monthname", None, "short"),
        "DAY": ("dayname", None, "long"),
        "DY": ("dayname", None, "short"),
        "IW": ("isoweek", "p", 2),
        "WW": ("week_mon", "p", 2),
        "D": ("weekday1", "n", None),
    },
    # https://cloud.google.com/bigquery/docs/reference/standard-sql/format-elements
    "bigquery": {
        "%Y": ("year", "p", 4),
        "%y": ("year2", "p", 2),
        "%m": ("month", "p", 2),
        "%d": ("day", "p", 2),
        "%e": ("day", "n", None),
        "%H": ("hour24", "p", 2),
        "%M": ("minute", "p", 2),
        "%S": ("second", "p", 2),
        "%F": ("dateiso", None, None),
        "%T": ("timeiso", None, None),
        "%D": ("dateus", None, None),
        "%x": ("dateus", None, None),
        "%a": ("dayname", None, "short"),
        "%b": ("monthname", None, "short"),
        "%E6S": ("second_fraction6", None, None),
    },
    # Spark 3 (strict java.time): https://spark.apache.org/docs/latest/sql-ref-datetime-pattern.html
    "spark": {
        "yyyy": ("year", "p", 4),
        "yy": ("year2", "p", 2),
        "MM": ("month", "p", 2),
        "M": ("month", "n", None),
        "dd": ("day", "p", 2),
        "d": ("day", "n", None),
        "HH": ("hour24", "p", 2),
        "H": ("hour24", "n", None),
        "hh": ("hour12", "p", 2),
        "h": ("hour12", "n", None),
        "mm": ("minute", "p", 2),
        "m": ("minute", "n", None),
        "ss": ("second", "p", 2),
        "s": ("second", "n", None),
        "SSSSSS": ("fraction", "p", 6),
        "a": ("ampm", None, None),
        "MMMM": ("monthname", None, "long"),
        "MMM": ("monthname", None, "short"),
        "EEEE": ("dayname", None, "long"),
        "EEE": ("dayname", None, "short"),
        "DD": ("yearday", "p", 3),
        "D": ("yearday", "n", None),
        "z": ("tzname", None, None),
        "Z": ("tzoffset", None, "hhmm"),
    },
    # https://learn.microsoft.com/dotnet/standard/base-types/custom-date-and-time-format-strings
    "tsql": {
        "yyyy": ("year", "p", 4),
        "YYYY": ("year", "p", 4),
        "yy": ("year2", "p", 2),
        "YY": ("year2", "p", 2),
        "MM": ("month", "p", 2),
        "M": ("month", "n", None),
        "dd": ("day", "p", 2),
        "d": ("day", "n", None),
        "HH": ("hour24", "p", 2),
        "H": ("hour24", "n", None),
        "hh": ("hour12", "p", 2),
        "h": ("hour12", "n", None),
        "mm": ("minute", "p", 2),
        "m": ("minute", "n", None),
        "ss": ("second", "p", 2),
        "s": ("second", "n", None),
        "ffffff": ("fraction", "p", 6),
        "MMMM": ("monthname", None, "long"),
        "MMM": ("monthname", None, "short"),
        "dddd": ("dayname", None, "long"),
    },
}

# Multi-atom canonical constructs and their semantic slot expansion. The trie
# walk emits these atomically (a single mapping entry), so semantic encoding
# expands them into the ordered slots they print.
COMPOSITE_SEMANTICS: dict[str, tuple[tuple, ...]] = {
    "%H:%M:%S": (
        ("hour24", "p", 2),
        ("lit", ":"),
        ("minute", "p", 2),
        ("lit", ":"),
        ("second", "p", 2),
    ),
    "%I:%M:%S %p": (
        ("hour12", "p", 2),
        ("lit", ":"),
        ("minute", "p", 2),
        ("lit", ":"),
        ("second", "p", 2),
        ("lit", " "),
        ("ampm", None, None),
    ),
    "%Y-%m-%d": (("year", "p", 4), ("lit", "-"), ("month", "p", 2), ("lit", "-"), ("day", "p", 2)),
    "%m/%d/%y": (("month", "p", 2), ("lit", "/"), ("day", "p", 2), ("lit", "/"), ("year2", "p", 2)),
    "%a %b %e %H:%M:%S %Y": (
        ("dayname", None, "short"),
        ("lit", " "),
        ("monthname", None, "short"),
        ("lit", " "),
        ("day", "s", 2),
        ("lit", " "),
        ("hour24", "p", 2),
        ("lit", ":"),
        ("minute", "p", 2),
        ("lit", ":"),
        ("second", "p", 2),
        ("lit", " "),
        ("year", "p", 4),
    ),
    "%S.%f": (("second", "p", 2), ("lit", "."), ("fraction", "p", 6)),
}

# Dialect atoms that are documented multi-slot constructs (expanded same way).
DIALECT_COMPOSITES: dict[str, dict[str, tuple[tuple, ...]]] = {
    "mysql": {
        "%T": COMPOSITE_SEMANTICS["%H:%M:%S"],
        "%r": COMPOSITE_SEMANTICS["%I:%M:%S %p"],
    },
    "bigquery": {
        "%F": COMPOSITE_SEMANTICS["%Y-%m-%d"],
        "%T": COMPOSITE_SEMANTICS["%H:%M:%S"],
        "%D": COMPOSITE_SEMANTICS["%m/%d/%y"],
        "%x": COMPOSITE_SEMANTICS["%m/%d/%y"],
        "%c": COMPOSITE_SEMANTICS["%a %b %e %H:%M:%S %Y"],
        "%E6S": COMPOSITE_SEMANTICS["%S.%f"],
    },
}


def _canonical_slots(text: str) -> list[tuple]:
    """Encode a canonical (strftime IR) format string into ordered semantic slots."""
    slots: list[tuple] = []
    pos = 0
    while pos < len(text):
        match = _CANONICAL_ATOM.match(text, pos)
        if not match:
            slots.append(("lit", text[pos]))
            pos += 1
            continue
        atom = match.group(0)
        if atom in COMPOSITE_SEMANTICS:
            slots.extend(COMPOSITE_SEMANTICS[atom])
        else:
            semantic = CANONICAL_SEMANTICS.get(STRICT_TIME_FORMATS.get(atom, atom))
            if semantic is None:
                raise AssertionError(f"unknown canonical atom {atom!r} in {text!r}")
            slots.append(semantic)
        pos = match.end()
    return slots


def _dialect_slots(dialect_name: str, text: str) -> list[tuple]:
    """
    Encode a *dialect-native* format string into ordered semantic slots by
    walking it through the dialect's own mapping (the parse/normalization step),
    then encoding the resulting canonical form.
    """
    dialect = Dialect[dialect_name]
    normalized = format_time(text, dialect.TIME_MAPPING, dialect.TIME_TRIE)
    return _canonical_slots(normalized)


def _expected_dialect_slots(dialect_name: str, atom: str) -> list[tuple]:
    composites = DIALECT_COMPOSITES.get(dialect_name, {})
    if atom in composites:
        return list(composites[atom])
    return [DIALECT_ATOM_SEMANTICS[dialect_name][atom]]


class TestTimeMappingForward(unittest.TestCase):
    """Parse/normalization direction: dialect token -> canonical strftime IR."""

    def test_atom_semantics_match_documentation(self):
        # Every documented token in DIALECT_ATOM_SEMANTICS must normalize, via the
        # dialect's actual TIME_MAPPING trie walk, to the same semantic slots.
        # A wrong value (or a collision winner changing) fails this test.
        for dialect_name, atoms in DIALECT_ATOM_SEMANTICS.items():
            dialect = Dialect[dialect_name]
            for atom in atoms:
                normalized = format_time(atom, dialect.TIME_MAPPING, dialect.TIME_TRIE)
                expected = _expected_dialect_slots(dialect_name, atom)
                actual = _canonical_slots(normalized)
                self.assertEqual(
                    expected,
                    actual,
                    msg=f"{dialect_name}: {atom!r} normalized to {normalized!r} "
                    f"(semantics {actual}, expected {expected})",
                )

    def test_case_variants_have_distinct_semantics(self):
        # The same letters in different case can mean different things; pin the
        # known case-sensitive pairs so they cannot silently collapse.
        snowflake = Dialect["snowflake"]
        self.assertEqual(format_time("DD", snowflake.TIME_MAPPING), "%d")
        self.assertEqual(format_time("dd", snowflake.TIME_MAPPING), "%-d")
        self.assertEqual(format_time("DY", snowflake.TIME_MAPPING), "%a")
        self.assertEqual(format_time("HH12", snowflake.TIME_MAPPING), "%I")
        self.assertEqual(format_time("HH24", snowflake.TIME_MAPPING), "%H")

        oracle = Dialect["oracle"]
        self.assertEqual(format_time("HH", oracle.TIME_MAPPING), "%I")
        self.assertEqual(format_time("HH24", oracle.TIME_MAPPING), "%H")
        self.assertEqual(format_time("WW", oracle.TIME_MAPPING), "%W")
        self.assertEqual(format_time("IW", oracle.TIME_MAPPING), "%V")

    def test_fractional_second_precision_tokens(self):
        snowflake = Dialect["snowflake"]
        expected = {
            "FF0": "%f_zero",
            "FF1": "%f_one",
            "FF2": "%f_two",
            "FF3": "%f_three",
            "FF4": "%f_four",
            "FF5": "%f_five",
            "FF6": "%f",
            "FF7": "%f_seven",
            "FF8": "%f_eight",
            "FF9": "%f_nine",
        }
        for token, canonical in expected.items():
            self.assertEqual(
                format_time(token, snowflake.TIME_MAPPING),
                canonical,
                msg=f"snowflake: {token} precision mapping is wrong",
            )
            self.assertEqual(
                format_time(token.lower(), snowflake.TIME_MAPPING),
                canonical,
                msg=f"snowflake: {token.lower()} precision mapping is wrong",
            )

    def test_composite_tokens_normalize_as_atoms(self):
        # %T / %r / %F ... are single mapping entries spanning multiple slots
        mysql = Dialect["mysql"]
        self.assertEqual(format_time("%T", mysql.TIME_MAPPING), "%H:%M:%S")
        self.assertEqual(format_time("%r", mysql.TIME_MAPPING), "%I:%M:%S %p")

        bigquery = Dialect["bigquery"]
        self.assertEqual(format_time("%F", bigquery.TIME_MAPPING), "%Y-%m-%d")
        self.assertEqual(format_time("%T", bigquery.TIME_MAPPING), "%H:%M:%S")
        self.assertEqual(format_time("%D", bigquery.TIME_MAPPING), "%m/%d/%y")
        self.assertEqual(format_time("%E6S", bigquery.TIME_MAPPING), "%S.%f")

    def test_known_unmapped_atoms_pass_through(self):
        # MySQL documents %v (ISO week) but deliberately leaves it unmapped
        # because it collides with %V in the round trip; pin that behavior so
        # it cannot be silently half-mapped.
        mysql = Dialect["mysql"]
        self.assertNotIn("%v", mysql.TIME_MAPPING)
        self.assertEqual(format_time("%v", mysql.TIME_MAPPING), "%v")

        # .NET's AM/PM marker 'tt' is likewise not in TSQL's TIME_MAPPING
        tsql = Dialect["tsql"]
        self.assertNotIn("tt", tsql.TIME_MAPPING)
        self.assertEqual(format_time("tt", tsql.TIME_MAPPING), "tt")
        self.assertNotIn("ddd", tsql.TIME_MAPPING)
        self.assertEqual(format_time("ddd", tsql.TIME_MAPPING), "ddd")

    def test_case_sensitive_pairs_are_not_substring_collisions(self):
        # Trie tokenization must pick the whole atom even when a shorter key is
        # also present (prefix sharing), in both directions.
        snowflake = Dialect["snowflake"]
        self.assertEqual(format_time("HH24", snowflake.TIME_MAPPING), "%H")
        self.assertEqual(format_time("HH12", snowflake.TIME_MAPPING), "%I")
        self.assertEqual(format_time("FF3", snowflake.TIME_MAPPING), "%f_three")
        self.assertEqual(format_time("FF30", snowflake.TIME_MAPPING), "%f_three0")

        postgres = Dialect["postgres"]
        self.assertEqual(format_time("FMHH24", postgres.TIME_MAPPING), "%-H")
        self.assertEqual(format_time("HH24MI", postgres.TIME_MAPPING), "%H%M")

        oracle = Dialect["oracle"]
        self.assertEqual(format_time("HH24MI", oracle.TIME_MAPPING), "%H%M")


INVERSE_EXPECTATIONS: dict[str, dict[str, str]] = {
    "mysql": {
        "%Y": "%Y",
        "%y": "%y",
        "%m": "%m",
        "%-m": "%c",
        "%d": "%d",
        "%-d": "%e",
        "%H": "%H",
        "%-H": "%k",
        "%I": "%h",
        "%-I": "%l",
        "%M": "%i",
        "%S": "%s",
        "%p": "%p",
        "%f": "%f",
        "%B": "%M",
        "%b": "%b",
        "%A": "%W",
        "%a": "%a",
        "%j": "%j",
        # composite canonical constructs
        "%H:%M:%S": "%T",
        "%I:%M:%S %p": "%r",
    },
    "snowflake": {
        "%Y": "yyyy",
        "%y": "yy",
        "%m": "mm",
        "%d": "DD",
        "%-d": "dd",
        "%H": "hh24",
        "%I": "hh12",
        "%M": "mi",
        "%S": "ss",
        "%p": "pm",
        "%B": "mmmm",
        "%b": "mon",
        "%a": "DY",
        "%f": "ff6",
        "%f_three": "ff3",
        "%f_nine": "ff9",
        "%z": "tzhtzm",
        "%:z": "tzh:tzm",
        "%-z": "tzh",
    },
    "duckdb": {
        "%Y": "%Y",
        "%m": "%m",
        "%-m": "%-m",
        "%d": "%d",
        "%-d": "%-d",
        "%e": "%-d",
        "%H": "%H",
        "%I": "%I",
        "%M": "%M",
        "%S": "%S",
        "%p": "%p",
        "%f": "%f",
        "%f_three": "%g",
        "%f_nine": "%n",
        "%:z": "%z",
        "%-z": "%z",
    },
    "postgres": {
        "%Y": "YYYY",
        "%y": "YY",
        "%m": "MM",
        "%-m": "FMMM",
        "%d": "DD",
        "%-d": "FMDD",
        "%H": "HH24",
        "%-H": "FMHH24",
        "%I": "HH12",
        "%M": "MI",
        "%-M": "FMMI",
        "%S": "SS",
        "%f": "US",
        "%B": "TMMonth",
        "%b": "TMMon",
        "%A": "TMDay",
        "%a": "TMDy",
        "%j": "DDD",
        "%z": "OF",
        "%Z": "TZ",
    },
    "oracle": {
        "%Y": "YYYY",
        "%y": "YY",
        "%m": "MM",
        "%d": "DD",
        "%H": "HH24",
        "%I": "HH12",
        "%M": "MI",
        "%S": "SS",
        "%f": "FF6",
        "%B": "MONTH",
        "%b": "MON",
        "%A": "DAY",
        "%a": "DY",
        "%j": "DDD",
        "%V": "IW",
        "%W": "WW",
        "%u": "D",
    },
    "bigquery": {
        "%Y": "%Y",
        "%m": "%m",
        "%d": "%d",
        "%-d": "%e",
        "%H:%M:%S": "%T",
        "%Y-%m-%d": "%F",
        "%m/%d/%y": "%D",
        "%S.%f": "%E6S",
    },
    "spark": {
        "%Y": "yyyy",
        "%y": "yy",
        "%mstrict": "MM",
        "%dstrict": "dd",
        "%Hstrict": "HH",
        "%Istrict": "hh",
        "%Mstrict": "mm",
        "%Sstrict": "ss",
        "%-m": "M",
        "%-d": "d",
        "%-H": "H",
        "%-I": "h",
        "%-M": "m",
        "%-S": "s",
        "%f": "SSSSSS",
        "%p": "a",
        "%B": "MMMM",
        "%b": "MMM",
        "%A": "EEEE",
        "%a": "EEE",
        "%j": "DD",
        "%-j": "D",
        "%Z": "z",
        "%z": "Z",
    },
    "tsql": {
        "%Y": "yyyy",
        "%y": "yy",
        "%m": "MM",
        "%-m": "M",
        "%d": "dd",
        "%-d": "d",
        "%H": "HH",
        "%-H": "H",
        "%I": "hh",
        "%-I": "h",
        "%M": "mm",
        "%-M": "m",
        "%S": "ss",
        "%-S": "s",
        "%f": "ffffff",
        "%B": "MMMM",
        "%b": "MMM",
        "%A": "dddd",
    },
}


class TestTimeMappingInverse(unittest.TestCase):
    """Generation direction: canonical strftime IR -> dialect token.

    Expected outputs are written explicitly (documented dialect spellings),
    independently of the implementation tables.
    """

    def test_inverse_atoms_render_documented_tokens(self):
        for dialect_name, expectations in INVERSE_EXPECTATIONS.items():
            dialect = Dialect[dialect_name]
            for canonical, expected in expectations.items():
                actual = format_time(
                    canonical, dialect.INVERSE_TIME_MAPPING, dialect.INVERSE_TIME_TRIE
                )
                self.assertEqual(
                    expected,
                    actual,
                    msg=f"{dialect_name}: canonical {canonical!r} rendered as {actual!r}, "
                    f"expected {expected!r}",
                )

    def test_inverse_atom_preserves_semantics(self):
        # Independent (implementation-free) check: round-trip each documented
        # canonical atom through the inverse mapping and decode it back with the
        # *dialect's documented* meaning. Catches an inverse value that happens
        # to be a valid token but means something else.
        #
        # A few renderings intentionally relax the width/padding because the
        # target dialect has no exact token (e.g. DuckDB's strftime has no
        # space-padded day); the kind must still match.
        kind_only_allowed = {
            ("duckdb", "%e", "%-d"),
            ("duckdb", "%:z", "%z"),
            ("duckdb", "%-z", "%z"),
        }
        strict_to_lax = {strict: lax for strict, lax in STRICT_TIME_FORMATS.items()}
        for dialect_name, expectations in INVERSE_EXPECTATIONS.items():
            atom_meaning = DIALECT_ATOM_SEMANTICS.get(dialect_name, {})
            for canonical, rendered in expectations.items():
                if canonical in COMPOSITE_SEMANTICS or " " in rendered or ":" in rendered:
                    continue
                semantic = CANONICAL_SEMANTICS.get(strict_to_lax.get(canonical, canonical))
                if semantic is None:
                    continue
                documented = atom_meaning.get(rendered)
                # DuckDB formats are themselves strftime
                if documented is None and rendered in CANONICAL_SEMANTICS:
                    documented = CANONICAL_SEMANTICS[rendered]
                self.assertIsNotNone(
                    documented,
                    msg=f"{dialect_name}: no documented meaning for rendered token "
                    f"{rendered!r} (from {canonical!r}) - extend ground truth",
                )
                # DuckDB's %n (nanoseconds)/%g (milliseconds) are its closest
                # renderings for arbitrary/3-digit fractions: same component
                # family (fractional seconds), precision may widen or narrow.
                fraction_family = {"fraction", "fraction_ns", "fraction_ms"}
                is_fraction_relaxation = (
                    dialect_name == "duckdb"
                    and semantic[0] in fraction_family
                    and documented[0] in fraction_family
                )
                if (
                    is_fraction_relaxation
                    or (dialect_name, canonical, rendered) in kind_only_allowed
                ):
                    if is_fraction_relaxation:
                        self.assertIn(semantic[0], fraction_family)
                        self.assertIn(documented[0], fraction_family)
                    else:
                        self.assertEqual(
                            semantic[0],
                            documented[0],
                            msg=f"{dialect_name}: {canonical!r} -> {rendered!r} changed kind "
                            f"{semantic[0]!r} -> {documented[0]!r}",
                        )
                else:
                    self.assertEqual(
                        semantic,
                        documented,
                        msg=f"{dialect_name}: {canonical!r} -> {rendered!r} changed meaning "
                        f"{semantic} -> {documented}",
                    )


class TestFormatTimeAlgorithm(unittest.TestCase):
    """Observable behavior of the longest-prefix trie walker itself."""

    def test_empty_input(self):
        self.assertIsNone(format_time("", {"a": "b"}))

    def test_literal_text_is_preserved(self):
        self.assertEqual(format_time("hello", {}), "hello")
        self.assertEqual(format_time(" x ", {"%Y": "YYYY"}), " x ")
        self.assertEqual(format_time("'%Y'", {"%Y": "YYYY"}), "'YYYY'")

    def test_longest_key_wins(self):
        mapping = {"a": "X", "ab": "Y", "abc": "Z"}
        self.assertEqual(format_time("abc", mapping), "Z")
        self.assertEqual(format_time("ab", mapping), "Y")
        self.assertEqual(format_time("abd", mapping), "Yd")
        self.assertEqual(format_time("ac", mapping), "Xc")

    def test_failed_prefix_backtracks_to_last_match(self):
        # "abcdX": "abc" matches, then "d" (no key) is emitted literally
        mapping = {"ab": "AB", "abc": "ABC"}
        self.assertEqual(format_time("abcd", mapping), "ABCd")
        # "aba" -> "ab" then "a"
        self.assertEqual(format_time("aba", mapping), "ABa")

    def test_unknown_percent_token_stays_literal(self):
        # An unmapped %-token must pass through unchanged (never crash, never
        # get partially rewritten), e.g. MySQL's unmapped %v round trip
        mapping = {"%Y": "YYYY", "%m": "MM"}
        self.assertEqual(format_time("%Q", mapping), "%Q")
        self.assertEqual(format_time("%Y-%Q", mapping), "YYYY-%Q")

    def test_explicit_trie_is_reused(self):
        from sqlglot.trie import new_trie

        mapping = {"%Y": "YYYY"}
        trie = new_trie(mapping)
        self.assertEqual(format_time("%Y", mapping, trie), "YYYY")

    def test_snowflake_quoted_t_separator(self):
        # '"T"' maps to the literal T, while an unquoted T must not be rewritten
        # (it appears inside ordinary words like AUTO)
        snowflake = Dialect["snowflake"]
        self.assertEqual(format_time('"T"', snowflake.TIME_MAPPING), "T")
        self.assertEqual(format_time("AUTO", snowflake.INVERSE_TIME_MAPPING), "AUTO")
        self.assertEqual(format_time("T", snowflake.INVERSE_TIME_MAPPING), "T")

    def test_lenient_parse_rewrite_only_touches_delimited_specifiers(self):
        # Delimited padded fields are relaxed so single-digit values parse
        self.assertEqual(_lenient_parse_format("%Y-%m-%d %H:%M:%S"), "%Y-%-m-%-d %-H:%-M:%-S")
        # Adjacent (undelimited) fields must stay padded: java.time parses them
        # greedily and the relaxed form cannot consume fixed-width runs
        self.assertEqual(_lenient_parse_format("%Y%m%d"), "%Y%m%d")
        self.assertEqual(_lenient_parse_format("%Y%m%d %H"), "%Y%m%d %-H")
        # A digit literal touching the specifier blocks the rewrite as well
        self.assertEqual(_lenient_parse_format("%m1/%d"), "%m1/%-d")
        # strict atoms and other specifiers are left alone
        self.assertEqual(_lenient_parse_format("%mstrict %f"), "%mstrict %f")


class TestInverseMappingDerivation(unittest.TestCase):
    """The runtime-derived inverse tables in ``Dialect.__new__``."""

    def test_inverse_is_auto_generated_from_time_mapping(self):
        # Every dialect that defines TIME_MAPPING gets its inverse values
        for dialect_name in DIALECT_ATOM_SEMANTICS:
            dialect = Dialect[dialect_name]
            for source, canonical in dialect.TIME_MAPPING.items():
                if canonical in COMPOSITE_SEMANTICS or len(source) <= 1:
                    continue
                # An explicit INVERSE override may win collisions; otherwise the
                # auto-derived entry exists
                self.assertIn(
                    canonical,
                    dialect.INVERSE_TIME_MAPPING,
                    msg=f"{dialect_name}: {canonical!r} missing from INVERSE_TIME_MAPPING",
                )

    def test_explicit_inverse_overrides_collide_deterministically(self):
        # BigQuery preserves %E6S instead of expanding it to %T.%f
        bigquery = Dialect["bigquery"]
        self.assertEqual(bigquery.INVERSE_TIME_MAPPING["%S.%f"], "%E6S")
        self.assertEqual(
            format_time("%H:%M:%S.%f", bigquery.INVERSE_TIME_MAPPING),
            "%H:%M:%E6S",
        )

        # DuckDB collapses foreign fractional precisions to %n/%g and
        # space-padded day (%e) to unpadded (%-d)
        duckdb = Dialect["duckdb"]
        self.assertEqual(duckdb.INVERSE_TIME_MAPPING["%e"], "%-d")
        self.assertEqual(duckdb.INVERSE_TIME_MAPPING["%f_three"], "%g")
        self.assertEqual(duckdb.INVERSE_TIME_MAPPING["%f_nine"], "%n")
        self.assertEqual(duckdb.INVERSE_TIME_MAPPING["%:z"], "%z")
        self.assertEqual(duckdb.INVERSE_TIME_MAPPING["%-z"], "%z")

        # Snowflake keeps T literal rather than re-quoting
        self.assertEqual(Dialect["snowflake"].INVERSE_TIME_MAPPING["T"], "T")

    def test_strict_and_lax_atoms_coexist_in_strict_dialects(self):
        # Spark 3+ maps MM -> %mstrict; a foreign lax %m must still render (as MM)
        spark = Dialect["spark"]
        self.assertEqual(spark.TIME_MAPPING["MM"], "%mstrict")
        self.assertEqual(spark.INVERSE_TIME_MAPPING["%mstrict"], "MM")
        self.assertEqual(spark.INVERSE_TIME_MAPPING["%m"], "MM")
        self.assertEqual(spark.INVERSE_TIME_MAPPING["%Hstrict"], "HH")
        self.assertEqual(spark.INVERSE_TIME_MAPPING["%H"], "HH")

        # Spark 2 (lax SimpleDateFormat) maps MM -> %m directly
        spark2 = Dialect["spark2"]
        self.assertEqual(spark2.TIME_MAPPING["MM"], "%m")

        # A non-strict dialect degrades strict atoms to the lax spelling so they
        # never leak through generation
        mysql = Dialect["mysql"]
        self.assertEqual(mysql.INVERSE_TIME_MAPPING["%mstrict"], "%m")
        self.assertEqual(mysql.INVERSE_TIME_MAPPING["%Hstrict"], "%H")

    def test_strict_format_table_contents(self):
        # Pin the strict<->lax correspondence itself
        self.assertEqual(
            STRICT_TIME_FORMATS,
            {
                "%mstrict": "%m",
                "%dstrict": "%d",
                "%Hstrict": "%H",
                "%Istrict": "%I",
                "%Mstrict": "%M",
                "%Sstrict": "%S",
            },
        )


# ---------------------------------------------------------------------------
# End-to-end transpilation fidelity
# ---------------------------------------------------------------------------

# (source sql, source dialect, target dialect, expected exact target sql)
EXACT_TRANSPILATIONS = [
    # MySQL -> Snowflake, the motivating case from production
    (
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %H:%i') FROM t",
        "mysql",
        "snowflake",
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'yyyy-mm-DD hh24:mi') FROM t",
    ),
    (
        "SELECT DATE_FORMAT(x, '%d/%m/%Y') FROM t",
        "mysql",
        "snowflake",
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'DD/mm/yyyy') FROM t",
    ),
    # composite time atom %T -> expanded hh24:mi:ss (semantic equivalent, text change)
    (
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %T') FROM t",
        "mysql",
        "snowflake",
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'yyyy-mm-DD hh24:mi:ss') FROM t",
    ),
    (
        "SELECT DATE_FORMAT(x, '%r') FROM t",
        "mysql",
        "snowflake",
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'hh12:mi:ss pm') FROM t",
    ),
    # MySQL -> DuckDB (strftime)
    (
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %H:%i') FROM t",
        "mysql",
        "duckdb",
        "SELECT STRFTIME(CAST(x AS TIMESTAMP), '%Y-%m-%d %H:%M') FROM t",
    ),
    (
        "SELECT DATE_FORMAT(x, '%k:%i:%s') FROM t",
        "mysql",
        "duckdb",
        "SELECT STRFTIME(CAST(x AS TIMESTAMP), '%-H:%M:%S') FROM t",
    ),
    # reverse direction: Snowflake -> MySQL (DD zero-padded stays %d)
    (
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'yyyy-mm-dd hh24:mi:ss') FROM t",
        "snowflake",
        "mysql",
        "SELECT DATE_FORMAT(CAST(x AS DATETIME), '%Y-%m-%e %T') FROM t",
    ),
    (
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'YYYY-MM-DD HH24:MI:SS') FROM t",
        "snowflake",
        "mysql",
        "SELECT DATE_FORMAT(CAST(x AS DATETIME), '%Y-%m-%d %T') FROM t",
    ),
    # Snowflake -> DuckDB, including the dd (unpadded day) case
    (
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'yyyy-mm-dd hh24:mi:ss') FROM t",
        "snowflake",
        "duckdb",
        "SELECT STRFTIME(CAST(x AS TIMESTAMP), '%Y-%m-%-d %H:%M:%S') FROM t",
    ),
    (
        "SELECT TO_CHAR(CAST(x AS DATE), 'YYYY-MM-DD') FROM t",
        "snowflake",
        "duckdb",
        "SELECT STRFTIME(CAST(x AS DATE), '%Y-%m-%d') FROM t",
    ),
    # Postgres/Oracle -> MySQL
    (
        "SELECT TO_CHAR(x, 'YYYY-MM-DD HH24:MI:SS') FROM t",
        "postgres",
        "mysql",
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %T') FROM t",
    ),
    (
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'DD.MM.YYYY HH24:MI:SS') FROM t",
        "oracle",
        "mysql",
        "SELECT DATE_FORMAT(CAST(x AS DATETIME), '%d.%m.%Y %T') FROM t",
    ),
    # DuckDB -> Snowflake and MySQL (strftime -> other token systems)
    (
        "SELECT STRFTIME(x, '%Y-%m-%d %H:%M:%S') FROM t",
        "duckdb",
        "snowflake",
        "SELECT TO_CHAR(x, 'yyyy-mm-DD hh24:mi:ss') FROM t",
    ),
    (
        "SELECT STRFTIME(x, '%Y-%m-%d %H:%M:%S') FROM t",
        "duckdb",
        "mysql",
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %T') FROM t",
    ),
    # Spark (strict) generation
    (
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %H:%i:%s') FROM t",
        "mysql",
        "spark",
        "SELECT DATE_FORMAT(CAST(x AS TIMESTAMP), 'yyyy-MM-dd HH:mm:ss') FROM t",
    ),
    # BigQuery composite atoms
    (
        "SELECT FORMAT_DATE('%F', x) FROM t",
        "bigquery",
        "mysql",
        "SELECT DATE_FORMAT(x, '%Y-%m-%d') FROM t",
    ),
    # TSQL FORMAT -> MySQL
    (
        "SELECT FORMAT(x, 'yyyy-MM-dd HH:mm:ss') FROM t",
        "tsql",
        "mysql",
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %T') FROM t",
    ),
    # Strict Spark 3: parse formats get a lenient rewrite (M/d) so single-digit
    # values stay parseable under java.time
    (
        "SELECT STR_TO_DATE(x, '%Y-%m-%d') FROM t",
        "mysql",
        "spark",
        "SELECT TO_DATE(x, 'yyyy-M-d') FROM t",
    ),
    (
        "SELECT STR_TO_DATE(x, '%Y/%m/%d') FROM t",
        "mysql",
        "databricks",
        "SELECT TO_DATE(x, 'yyyy/M/d') FROM t",
    ),
    # ... but generation (formatting) stays padded MM/dd
    (
        "SELECT DATE_FORMAT(x, '%Y-%m-%d %H') FROM t",
        "mysql",
        "spark",
        "SELECT DATE_FORMAT(CAST(x AS TIMESTAMP), 'yyyy-MM-dd HH') FROM t",
    ),
    # adjacent (undelimited) fields cannot be relaxed: %Y%m%d stays yyyyMMdd
    (
        "SELECT DATE_FORMAT(x, '%Y%m%d') FROM t",
        "mysql",
        "spark",
        "SELECT DATE_FORMAT(CAST(x AS TIMESTAMP), 'yyyyMMdd') FROM t",
    ),
]

# Cases where only semantic equality is asserted: the target may legitimately
# choose a different but equivalent spelling.
# (source sql, source dialect, target dialect, target native format string)
SEMANTIC_TRANSPILATIONS = [
    # %H:%M:%S in canonical IR collapses to MySQL's composite %T
    (
        "SELECT TO_CHAR(x, 'YYYY-MM-DD HH24:MI:SS') FROM t",
        "postgres",
        "mysql",
        "%Y-%m-%d %T",
    ),
    (
        "SELECT STRFTIME(x, '%Y-%m-%d %H:%M:%S') FROM t",
        "duckdb",
        "mysql",
        "%Y-%m-%d %T",
    ),
    # Snowflake lowercase dd is unpadded day -> DuckDB %-d, MySQL %e
    (
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'yyyy-mm-dd') FROM t",
        "snowflake",
        "duckdb",
        "%Y-%m-%-d",
    ),
    (
        "SELECT TO_CHAR(CAST(x AS TIMESTAMP), 'yyyy-mm-dd') FROM t",
        "snowflake",
        "mysql",
        "%Y-%m-%e",
    ),
    # 12-hour format + AM/PM marker survives (Snowflake spells the marker pm)
    (
        "SELECT DATE_FORMAT(x, '%h:%i:%s %p') FROM t",
        "mysql",
        "snowflake",
        "hh12:mi:ss pm",
    ),
    # unpadded variants
    (
        "SELECT DATE_FORMAT(x, '%e %c %k') FROM t",
        "mysql",
        "postgres",
        "FMDD FMMM FMHH24",
    ),
]

_STRING_LITERAL = re.compile(r"'((?:[^']|'')*)'")

# Expressions that carry a format-string argument at parse/normalize time
_FORMATTED_TIME_EXPRS = (
    exp.TimeToStr,
    exp.StrToTime,
    exp.StrToDate,
    exp.StrToUnix,
    exp.TsOrDsToDate,
)


def _normalized_formats(sql: str, dialect: str) -> list[str]:
    """Format literals as stored in the normalized AST (canonical IR)."""
    tree = parse_one(sql, read=dialect)
    return [
        node.args["format"].name
        for node in tree.walk()
        if isinstance(node, _FORMATTED_TIME_EXPRS)
        and isinstance(node.args.get("format"), exp.Literal)
    ]


class TestEndToEndFidelity(unittest.TestCase):
    """Transpiled format strings preserve date/time semantics."""

    def _transpile(self, sql: str, read: str, write: str) -> str:
        return parse_one(sql, read=read).sql(dialect=write)

    def test_exact_transpilation_outputs(self):
        for sql, read, write, expected in EXACT_TRANSPILATIONS:
            with self.subTest(read=read, write=write, sql=sql):
                self.assertEqual(self._transpile(sql, read, write), expected)

    def test_semantic_equivalence_across_dialects(self):
        for sql, read, write, target_fmt in SEMANTIC_TRANSPILATIONS:
            with self.subTest(read=read, write=write, sql=sql):
                output = self._transpile(sql, read, write)
                literals = _STRING_LITERAL.findall(output)
                self.assertTrue(
                    literals,
                    msg=f"no format literal in transpiled SQL: {output}",
                )
                rendered = literals[-1].replace("''", "'")
                self.assertEqual(rendered, target_fmt, msg=output)

                source_ir = _normalized_formats(sql, read)[-1]
                source_slots = _canonical_slots(source_ir)
                target_slots = _dialect_slots(write, target_fmt)
                self.assertEqual(
                    source_slots,
                    target_slots,
                    msg=f"semantic drift {read}:{source_ir!r} -> {write}:{target_fmt!r}",
                )

    def test_exact_cases_are_also_semantically_faithful(self):
        # Belt-and-braces: decode every pinned output's format literal back
        # through the target dialect and compare semantic slots to the
        # normalized IR produced at parse time.
        for sql, read, write, expected in EXACT_TRANSPILATIONS:
            with self.subTest(read=read, write=write):
                target_fmt = _STRING_LITERAL.findall(expected)[-1].replace("''", "'")
                source_ir = _normalized_formats(sql, read)[-1]
                source_slots = _canonical_slots(source_ir)
                target_slots = _dialect_slots(write, target_fmt)
                if Dialect[write].TIME_MAPPING.get("MM") == "%mstrict":
                    # Strict Spark/Hive deliberately relax padded month/day/...
                    # to unpadded on parse expressions (lenient java.time), so
                    # only the printed component must match, not the padding.
                    source_slots = [(s[0],) for s in source_slots]
                    target_slots = [(s[0],) for s in target_slots]
                self.assertEqual(
                    source_slots,
                    target_slots,
                    msg=f"{read}:{source_ir!r} vs {write}:{target_fmt!r}",
                )

    def test_text_identical_is_distinct_from_semantic_equivalent(self):
        # Some conversions are text-preserving, others only semantically equal:
        # assert both facts explicitly rather than conflating them.
        text_preserved = self._transpile(
            "SELECT DATE_FORMAT(x, '%Y-%m-%d') FROM t", "mysql", "mysql"
        )
        self.assertIn("'%Y-%m-%d'", text_preserved)

        # canonical %H:%M:%S arriving in MySQL is re-spelled as %T: not text
        # equal, but same semantic slots
        from_postgres = self._transpile(
            "SELECT TO_CHAR(x, 'HH24:MI:SS') FROM t", "postgres", "mysql"
        )
        self.assertIn("'%T'", from_postgres)
        self.assertNotIn("'%H:%M:%S'", from_postgres)
        self.assertEqual(
            _dialect_slots("postgres", "HH24:MI:SS"),
            _dialect_slots("mysql", "%T"),
        )

    def test_parse_direction_formats_survive_transpilation(self):
        # Format strings on parse functions (STR_TO_DATE / TO_TIMESTAMP / STRPTIME)
        cases = [
            (
                "SELECT STR_TO_DATE(x, '%Y-%m-%d') FROM t",
                "mysql",
                "snowflake",
                "yyyy-mm-DD",
            ),
            (
                "SELECT TO_TIMESTAMP(x, 'YYYY-MM-DD HH24:MI:SS') FROM t",
                "postgres",
                "mysql",
                "%Y-%m-%d %T",
            ),
            (
                "SELECT STRPTIME(x, '%Y-%m-%d %H:%M:%S') FROM t",
                "duckdb",
                "mysql",
                "%Y-%m-%d %T",
            ),
        ]
        for sql, read, write, target_fmt in cases:
            with self.subTest(read=read, write=write):
                output = self._transpile(sql, read, write)
                literals = _STRING_LITERAL.findall(output)
                self.assertTrue(literals, msg=output)
                rendered = literals[-1].replace("''", "'")
                self.assertEqual(rendered, target_fmt, msg=output)
