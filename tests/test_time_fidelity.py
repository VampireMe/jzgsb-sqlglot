"""
Systematic fidelity tests for the cross-dialect date/time format-string pipeline.

The pipeline shared by every dialect is:

    source format string
        -- Dialect.TIME_MAPPING + TIME_TRIE (parse / normalization) -->
    canonical strftime-style format (sqlglot.time.format_time)
        -- Dialect.INVERSE_TIME_MAPPING + INVERSE_TIME_TRIE (generation) -->
    target format string

These tests guard both layers independently:

* token level: the contents of every dialect's (possibly runtime-derived) mapping
  tables and the behavior of the trie-driven ``format_time`` conversion algorithm;
* end to end: real ``sqlglot.transpile`` calls, asserting that the *rendered
  timestamp* is preserved across dialects - not merely that the format text
  survives (it often legitimately changes, e.g. MySQL's ``%H:%i:%s`` -> ``%T``).

The semantic oracle is a self-contained renderer for canonical formats. It is
cross-validated against a real DuckDB engine (its ``strftime`` accepts the same
canonical atoms) whenever duckdb is importable; without it, the end-to-end
assertions still run on the hand-rolled renderer, whose outputs for padded /
non-padded numeric fields are mechanical.
"""

from __future__ import annotations

import datetime
import re
import unittest

from sqlglot import parse_one
from sqlglot.dialects.dialect import Dialect
from sqlglot.generators.hive import _lenient_parse_format
from sqlglot.time import format_time
from sqlglot.trie import new_trie

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:  # pragma: no cover
    HAS_DUCKDB = False


# --------------------------------------------------------------------------- #
# Independent canonical (strftime-style) format renderer
# --------------------------------------------------------------------------- #

MONTHS_FULL = (
    "January February March April May June July August September October November December"
).split()
MONTHS_ABBR = tuple(name[:3] for name in MONTHS_FULL)
DAYS_FULL = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
DAYS_ABBR = tuple(name[:3] for name in DAYS_FULL)

# Internal fractional-second pseudo-atoms: %f_<precision> -> number of digits.
_FRACTION_PRECISION = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}

# Canonical atoms that may appear after normalizing any of the documented
# mappings under test. Kept explicit so an atom we don't know how to render
# fails a test loudly instead of silently leaking into the output.
CANONICAL_ATOMS = (
    "%Y %y %C "
    "%m %-m %mstrict "
    "%d %-d %dstrict %e "
    "%H %-H %Hstrict "
    "%I %-I %Istrict "
    "%M %-M %Mstrict "
    "%S %-S %Sstrict "
    "%j %-j %u %w %U %W %V %G "
    "%B %b %A %a %Aenlower %aenlower %p "
    "%f %g %n %z %:z %-z %Z T"
).split()
CANONICAL_ATOMS.extend(f"%f_{name}" for name in _FRACTION_PRECISION)
CANONICAL_ATOMS.append("%Ythree")

_ATOM_RE = re.compile(r"%(?:[mdHIMS]strict|-?[mdHIMSjgez]|:[z]|f_[a-z]+|Ythree|.)")


def render_canonical(fmt, dt, tz_offset="+0000"):
    """Render a canonical format string against ``dt`` using documented semantics."""
    iso_year, iso_week, _ = dt.isocalendar()
    weekday_mon = dt.isoweekday()  # Monday = 1 ... Sunday = 7
    day_of_year = dt.timetuple().tm_yday
    micro = f"{dt.microsecond:06d}"
    hour12 = dt.hour % 12 or 12

    out = []
    pos = 0
    for match in _ATOM_RE.finditer(fmt):
        if match.start() > pos:
            out.append(fmt[pos : match.start()])
        out.append(
            _render_atom(
                match.group(0),
                dt=dt,
                iso_year=iso_year,
                iso_week=iso_week,
                weekday_mon=weekday_mon,
                day_of_year=day_of_year,
                micro=micro,
                hour12=hour12,
                tz_offset=tz_offset,
            )
        )
        pos = match.end()
    if pos < len(fmt):
        out.append(fmt[pos:])
    return "".join(out)


def _render_atom(
    atom,
    *,
    dt,
    iso_year,
    iso_week,
    weekday_mon,
    day_of_year,
    micro,
    hour12,
    tz_offset,
):
    # Sunday-based week number (%U): the first Sunday starts week 1; preceding
    # days are week 0. weekday_sun is 0 on Sunday ... 6 on Saturday.
    weekday_sun = weekday_mon % 7
    week_sun = (day_of_year + 6 - weekday_sun) // 7

    values = {
        "%Y": f"{dt.year:04d}",
        "%y": f"{dt.year % 100:02d}",
        "%C": f"{dt.year // 100:02d}",
        "%m": f"{dt.month:02d}",
        "%d": f"{dt.day:02d}",
        "%e": f"{dt.day:2d}",
        "%H": f"{dt.hour:02d}",
        "%I": f"{hour12:02d}",
        "%M": f"{dt.minute:02d}",
        "%S": f"{dt.second:02d}",
        "%j": f"{day_of_year:03d}",
        "%-j": str(day_of_year),
        "%u": str(weekday_mon),
        "%w": str(weekday_sun),
        "%U": f"{week_sun:02d}",
        "%W": f"{iso_week:02d}",
        "%V": f"{iso_week:02d}",
        "%G": f"{iso_year:04d}",
        "%B": MONTHS_FULL[dt.month - 1],
        "%b": MONTHS_ABBR[dt.month - 1],
        "%A": DAYS_FULL[weekday_mon - 1],
        "%a": DAYS_ABBR[weekday_mon - 1],
        "%Aenlower": DAYS_FULL[weekday_mon - 1].lower(),
        "%aenlower": DAYS_ABBR[weekday_mon - 1].lower(),
        "%p": "AM" if dt.hour < 12 else "PM",
        "%f": micro,
        "%z": tz_offset,
        "%:z": f"{tz_offset[:3]}:{tz_offset[3:]}",
        "%-z": tz_offset[:3],
        "%Z": "UTC",
        "T": "T",
        # Strict dialects parse padded MM/dd/HH/...; they render like the padded
        # canonical atoms - only their parse behavior differs.
        "%mstrict": f"{dt.month:02d}",
        "%dstrict": f"{dt.day:02d}",
        "%Hstrict": f"{dt.hour:02d}",
        "%Istrict": f"{hour12:02d}",
        "%Mstrict": f"{dt.minute:02d}",
        "%Sstrict": f"{dt.second:02d}",
        "%-m": str(dt.month),
        "%-d": str(dt.day),
        "%-H": str(dt.hour),
        "%-I": str(hour12),
        "%-M": str(dt.minute),
        "%-S": str(dt.second),
        # DuckDB-only strftime fractional-second tokens.
        "%g": (micro + "000")[:3],
        "%n": (micro + "000")[:9],
    }
    if atom in values:
        return values[atom]

    match = re.fullmatch(r"%f_([a-z]+)", atom)
    if match:
        digits = _FRACTION_PRECISION[match.group(1)]
        return (micro + "000")[:digits]

    if atom == "%Ythree":
        return f"{dt.year % 1000:03d}"

    raise AssertionError(f"unknown canonical atom {atom!r} - extend the semantic oracle")


# Timestamps exercise padded/non-padded fields, 12 vs 24 hour clocks, year/week
# boundaries, leap days, Sundays and weekday-name differences.
SAMPLE_TIMESTAMPS = (
    datetime.datetime(2024, 3, 5, 14, 7, 9, 123456),
    datetime.datetime(2024, 1, 1, 1, 2, 3, 1),
    datetime.datetime(2021, 12, 31, 23, 59, 59, 999999),
    datetime.datetime(2020, 2, 29, 0, 30, 0),
    datetime.datetime(2023, 11, 12, 12, 0, 0, 500000),
)


# --------------------------------------------------------------------------- #
# Token level: documented TIME_MAPPINGs (parse / normalization direction)
# --------------------------------------------------------------------------- #

# Hand-authored from each database's documentation - deliberately NOT copied
# from sqlglot's tables, so a wrong value in the shipped mapping fails here.

MYSQL_TIME_MAPPING = {
    # https://dev.mysql.com/doc/refman/8.4/en/date-and-time-functions.html#function_date-format
    "%M": "%B",  # month name, full
    "%c": "%-m",  # numeric month, no padding
    "%e": "%-d",  # day of month, space-like unpadded -> non padded
    "%h": "%I",  # hour 01..12
    "%i": "%M",  # minutes
    "%s": "%S",  # seconds
    "%u": "%W",  # week number, Monday first
    "%k": "%-H",  # hour 0..23, non padded
    "%l": "%-I",  # hour 1..12, non padded
    "%r": "%I:%M:%S %p",  # 12-hour hh:mm:ss AM/PM
    "%T": "%H:%M:%S",  # 24-hour hh:mm:ss
    "%W": "%A",  # weekday name, full
    "%x": "%G",  # year for the week, four digits, Monday-first weeks
}

# https://docs.snowflake.com/en/sql-reference/functions/to_char
# Snowflake format models are case-sensitive in the documented cases: e.g.
# uppercase DD is zero-padded day while lowercase dd is non-padded, and DY/dy
# select abbreviated-name vs numeric weekday.
SNOWFLAKE_TIME_MAPPING = {
    "YYYY": "%Y",
    "yyyy": "%Y",
    "YY": "%y",
    "yy": "%y",
    "MMMM": "%B",
    "mmmm": "%B",
    "MON": "%b",
    "mon": "%b",
    "MM": "%m",
    "mm": "%m",
    "DD": "%d",
    "dd": "%-d",
    "DY": "%a",
    "dy": "%w",
    "HH24": "%H",
    "hh24": "%H",
    "HH12": "%I",
    "hh12": "%I",
    "MI": "%M",
    "mi": "%M",
    "SS": "%S",
    "ss": "%S",
    "FF": "%f_nine",
    "ff": "%f_nine",
    "FF0": "%f_zero",
    "ff0": "%f_zero",
    "FF1": "%f_one",
    "ff1": "%f_one",
    "FF2": "%f_two",
    "ff2": "%f_two",
    "FF3": "%f_three",
    "ff3": "%f_three",
    "FF4": "%f_four",
    "ff4": "%f_four",
    "FF5": "%f_five",
    "ff5": "%f_five",
    "FF6": "%f",
    "ff6": "%f",
    "FF7": "%f_seven",
    "ff7": "%f_seven",
    "FF8": "%f_eight",
    "ff8": "%f_eight",
    "FF9": "%f_nine",
    "ff9": "%f_nine",
    "TZHTZM": "%z",
    "tzhtzm": "%z",
    "TZH:TZM": "%:z",
    "tzh:tzm": "%:z",
    "TZH": "%-z",
    "tzh": "%-z",
    '"T"': "T",
    "AM": "%p",
    "am": "%p",
    "PM": "%p",
    "pm": "%p",
}

# A representative, documented subset of additional dialect tables. They are
# used both for direct content checks and as extra consensus oracles in the
# cross-dialect matrix.
HIVE_TIME_MAPPING = {
    # https://cwiki.apache.org/confluence/display/Hive/LanguageManual+Types#LanguageManualTypes-DateTypes
    "y": "%Y",
    "Y": "%Y",
    "YYYY": "%Y",
    "yyyy": "%Y",
    "YY": "%y",
    "yy": "%y",
    "MMMM": "%B",
    "MMM": "%b",
    "MM": "%mstrict",
    "M": "%-m",
    "dd": "%dstrict",
    "d": "%-d",
    "HH": "%Hstrict",
    "H": "%-H",
    "hh": "%Istrict",
    "h": "%-I",
    "mm": "%Mstrict",
    "m": "%-M",
    "ss": "%Sstrict",
    "s": "%-S",
    "SSSSSS": "%f",
    "a": "%p",
    "DD": "%j",
    "D": "%-j",
    "E": "%a",
    "EE": "%a",
    "EEE": "%a",
    "EEEE": "%A",
    "z": "%Z",
    "Z": "%z",
}

SPARK2_TIME_MAPPING = {
    **HIVE_TIME_MAPPING,
    "MM": "%m",
    "dd": "%d",
    "HH": "%H",
    "hh": "%I",
    "mm": "%M",
    "ss": "%S",
}

BIGQUERY_TIME_MAPPING = {
    # https://cloud.google.com/bigquery/docs/reference/standard-sql/format-elements
    "%x": "%m/%d/%y",
    "%D": "%m/%d/%y",
    "%E6S": "%S.%f",
    "%e": "%-d",
    "%F": "%Y-%m-%d",
    "%T": "%H:%M:%S",
    "%c": "%a %b %e %H:%M:%S %Y",
}

POSTGRES_TIME_MAPPING = {
    # https://www.postgresql.org/docs/current/functions-formatting.html
    "d": "%u",
    "D": "%u",
    "dd": "%d",
    "DD": "%d",
    "ddd": "%j",
    "DDD": "%j",
    "FMDD": "%-d",
    "FMDDD": "%-j",
    "FMHH12": "%-I",
    "FMHH24": "%-H",
    "FMMI": "%-M",
    "FMMM": "%-m",
    "FMSS": "%-S",
    "HH12": "%I",
    "HH24": "%H",
    "mi": "%M",
    "MI": "%M",
    "mm": "%m",
    "MM": "%m",
    "OF": "%z",
    "ss": "%S",
    "SS": "%S",
    "TMDay": "%A",
    "TMDy": "%a",
    "TMMon": "%b",
    "TMMonth": "%B",
    "day": "%Aenlower",
    "dy": "%aenlower",
    "TZ": "%Z",
    "US": "%f",
    "ww": "%U",
    "WW": "%U",
    "yy": "%y",
    "YY": "%y",
    "yyy": "%Ythree",
    "YYY": "%Ythree",
    "yyyy": "%Y",
    "YYYY": "%Y",
}

DOCUMENTED_TIME_MAPPINGS = {
    "mysql": MYSQL_TIME_MAPPING,
    "snowflake": SNOWFLAKE_TIME_MAPPING,
    "hive": HIVE_TIME_MAPPING,
    "spark2": SPARK2_TIME_MAPPING,
    "bigquery": BIGQUERY_TIME_MAPPING,
    "postgres": POSTGRES_TIME_MAPPING,
}


class TimeMappingTableTest(unittest.TestCase):
    """Every documented token must normalize to exactly the canonical atom below."""

    def _dialect(self, name):
        return Dialect[name]

    def test_documented_time_mappings(self):
        for name, expected in DOCUMENTED_TIME_MAPPINGS.items():
            dialect = self._dialect(name)
            for token, canonical in expected.items():
                self.assertEqual(
                    canonical,
                    dialect.TIME_MAPPING.get(token),
                    msg=f"{name}: {token!r} must normalize to {canonical!r}",
                )
                # And it must actually be consumed that way by the trie walk,
                # including when the token is embedded among literal text.
                self.assertEqual(
                    canonical,
                    format_time(token, dialect.TIME_MAPPING, dialect.TIME_TRIE),
                    msg=f"{name}: trie walk disagrees for token {token!r}",
                )

    def test_snowflake_case_sensitive_tokens(self):
        # The exact case pairs that have bitten production: DD vs dd, DY vs dy.
        mapping = Dialect["snowflake"].TIME_MAPPING
        self.assertEqual(("%d", "%-d"), (mapping["DD"], mapping["dd"]))
        self.assertEqual(("%a", "%w"), (mapping["DY"], mapping["dy"]))
        self.assertEqual(("%m", "%m"), (mapping["MM"], mapping["mm"]))

    def test_strict_dialects_use_strict_atoms(self):
        # Hive/Spark3 parse padded MM/dd/HH/... strictly; Spark2/Hive-lax don't.
        strict = Dialect["spark"].TIME_MAPPING
        lax = Dialect["spark2"].TIME_MAPPING
        self.assertEqual("%mstrict", strict["MM"])
        self.assertEqual("%dstrict", strict["dd"])
        self.assertEqual("%Hstrict", strict["HH"])
        self.assertEqual("%Mstrict", strict["mm"])
        self.assertEqual("%Sstrict", strict["ss"])
        self.assertEqual("%m", lax["MM"])
        self.assertEqual("%d", lax["dd"])

    def test_duckdb_speaks_canonical_directly(self):
        # DuckDB has no TIME_MAPPING of its own: strftime tokens pass through.
        dialect = Dialect["duckdb"]
        self.assertEqual({}, dialect.TIME_MAPPING)
        for token in ("%Y", "%m", "%-d", "%H:%M:%S", "%Y-%m-%d"):
            self.assertEqual(token, format_time(token, dialect.TIME_MAPPING, dialect.TIME_TRIE))

    def test_document_mapping_completeness(self):
        # The hand-authored oracle must cover the full shipped table for the
        # three priority dialects, otherwise a newly added token could silently
        # ship unverified.
        for name in ("mysql", "snowflake"):
            shipped = Dialect[name].TIME_MAPPING
            documented = DOCUMENTED_TIME_MAPPINGS[name]
            self.assertEqual(
                set(shipped),
                set(documented),
                msg=f"{name}: update the documented mapping oracle in this test",
            )


# --------------------------------------------------------------------------- #
# Token level: runtime-derived INVERSE_TIME_MAPPINGs (generation direction)
# --------------------------------------------------------------------------- #


class InverseTimeMappingTest(unittest.TestCase):
    def test_inverse_is_derived_from_time_mapping(self):
        for name in ("mysql", "snowflake", "hive", "spark", "spark2", "bigquery", "postgres"):
            dialect = Dialect[name]
            # Every canonical value produced on parse must be invertible for
            # generation, otherwise generated SQL leaks canonical atoms (which
            # is exactly why explicit INVERSE_TIME_MAPPING overrides exist).
            for token, canonical in dialect.TIME_MAPPING.items():
                self.assertIn(
                    canonical,
                    dialect.INVERSE_TIME_MAPPING,
                    msg=f"{name}: canonical {canonical!r} (from {token!r}) has no inverse",
                )

            # Roundtrip every parse-produced canonical fragment: generate the
            # dialect text and re-parse it. The result must render identically
            # to the original canonical for every sample timestamp - i.e. the
            # emitted text is a valid target token, not a leaked pseudo-atom.
            for canonical in set(dialect.TIME_MAPPING.values()):
                inverted = format_time(
                    canonical, dialect.INVERSE_TIME_MAPPING, dialect.INVERSE_TIME_TRIE
                )
                reparsed = format_time(inverted, dialect.TIME_MAPPING, dialect.TIME_TRIE)
                for dt in SAMPLE_TIMESTAMPS[:2]:
                    self.assertEqual(
                        render_canonical(canonical, dt),
                        render_canonical(reparsed, dt),
                        msg=(
                            f"{name}: {canonical!r} generates {inverted!r} which re-parses to "
                            f"{reparsed!r} instead of {canonical!r}"
                        ),
                    )

    def test_mysql_inverse_composites(self):
        inverse = Dialect["mysql"].INVERSE_TIME_MAPPING
        self.assertEqual("%T", inverse["%H:%M:%S"])
        self.assertEqual("%r", inverse["%I:%M:%S %p"])
        self.assertEqual("%i", inverse["%M"])
        self.assertEqual("%s", inverse["%S"])
        self.assertEqual("%k", inverse["%-H"])
        self.assertEqual("%l", inverse["%-I"])

    def test_snowflake_inverse_prefers_lowercase(self):
        inverse = Dialect["snowflake"].INVERSE_TIME_MAPPING
        self.assertEqual("yyyy", inverse["%Y"])
        self.assertEqual("mm", inverse["%m"])
        self.assertEqual("DD", inverse["%d"])
        self.assertEqual("dd", inverse["%-d"])
        self.assertEqual("hh24", inverse["%H"])
        self.assertEqual("hh12", inverse["%I"])
        self.assertEqual("mi", inverse["%M"])
        self.assertEqual("ss", inverse["%S"])
        self.assertEqual("ff6", inverse["%f"])
        self.assertEqual("ff3", inverse["%f_three"])
        self.assertEqual("ff9", inverse["%f_nine"])
        self.assertEqual("pm", inverse["%p"])

    def test_snowflake_literal_t_is_guarded(self):
        # Without the explicit {"T": "T"} guard, the inverse of '"T"' would turn
        # the literal AUTO into AU"T"O.
        inverse = Dialect["snowflake"].INVERSE_TIME_MAPPING
        trie = Dialect["snowflake"].INVERSE_TIME_TRIE
        self.assertEqual("T", inverse["T"])
        self.assertEqual("AUTO", format_time("AUTO", inverse, trie))
        self.assertEqual(
            'yyyy-mm-dd"T"hh24',
            format_time('yyyy-mm-dd"T"hh24', inverse, trie),
        )

    def test_strict_inverse_fallback_for_non_strict_dialects(self):
        # DuckDB knows no strict atoms: they must degrade to their lax form so a
        # strict canonical atom never leaks into generated SQL.
        inverse = Dialect["duckdb"].INVERSE_TIME_MAPPING
        self.assertEqual("%m", inverse["%mstrict"])
        self.assertEqual("%d", inverse["%dstrict"])
        self.assertEqual("%H", inverse["%Hstrict"])
        self.assertEqual("%I", inverse["%Istrict"])
        self.assertEqual("%M", inverse["%Mstrict"])
        self.assertEqual("%S", inverse["%Sstrict"])

    def test_strict_inverse_pads_in_strict_dialects(self):
        for name in ("hive", "spark"):
            inverse = Dialect[name].INVERSE_TIME_MAPPING
            self.assertEqual("MM", inverse["%m"])
            self.assertEqual("dd", inverse["%d"])
            self.assertEqual("HH", inverse["%H"])
            self.assertEqual("hh", inverse["%I"])
            self.assertEqual("mm", inverse["%M"])
            self.assertEqual("ss", inverse["%S"])
        # Spark2 is lax on parse but padded generation still wins on inverse.
        self.assertEqual("MM", Dialect["spark2"].INVERSE_TIME_MAPPING["%mstrict"])

    def test_bigquery_explicit_inverse_overrides_auto_reversal(self):
        # %E6S normalizes to %S.%f; naively inverting would emit %T.%f instead
        # of the atomic %E6S. The explicit override prevents that.
        inverse = Dialect["bigquery"].INVERSE_TIME_MAPPING
        self.assertEqual("%H:%M:%E6S", inverse["%H:%M:%S.%f"])
        self.assertEqual("%E6S", inverse["%S.%f"])

    def test_duckdb_fractional_and_timezone_inverses(self):
        inverse = Dialect["duckdb"].INVERSE_TIME_MAPPING
        self.assertEqual("%g", inverse["%f_three"])
        self.assertEqual("%n", inverse["%f_nine"])
        self.assertEqual("%z", inverse["%:z"])
        self.assertEqual("%z", inverse["%-z"])


# --------------------------------------------------------------------------- #
# Token level: format_time trie algorithm behavior
# --------------------------------------------------------------------------- #


class FormatTimeAlgorithmTest(unittest.TestCase):
    def test_empty_and_literal_only(self):
        self.assertIsNone(format_time("", {"a": "b"}))
        self.assertEqual(" ", format_time(" ", {"a": "b"}))
        self.assertEqual("lit", format_time("lit", {}))

    def test_longest_match_wins(self):
        mapping = {"a": "X", "aa": "Y", "aaa": "Z"}
        trie = new_trie(mapping)
        self.assertEqual("Z", format_time("aaa", mapping, trie))
        self.assertEqual(
            "YX", format_time("aaa", {"a": "X", "aa": "Y"}, new_trie({"a": "X", "aa": "Y"}))
        )
        self.assertEqual("ZY", format_time("aaaaa", mapping, trie))
        self.assertEqual("ZZ", format_time("aaaaaa", mapping, trie))

    def test_backtracking_after_prefix_failure(self):
        mapping = {"ab": "X", "abc": "Y"}
        self.assertEqual("Xd", format_time("abd", mapping))
        self.assertEqual("Y", format_time("abc", mapping))

    def test_unmapped_text_is_preserved(self):
        mapping = {"%Y": "YYYY"}
        trie = new_trie(mapping)
        self.assertEqual("YYYY-MM-DD", format_time("%Y-MM-DD", mapping, trie))
        self.assertEqual("YYYY/MM/DD HH:MI", format_time("%Y/MM/DD HH:MI", mapping, trie))

    def test_case_sensitivity(self):
        mapping = {"DD": "%d", "dd": "%-d"}
        trie = new_trie(mapping)
        self.assertEqual("%d", format_time("DD", mapping, trie))
        self.assertEqual("%-d", format_time("dd", mapping, trie))
        self.assertEqual("%d/%-d", format_time("DD/dd", mapping, trie))

    def test_real_tries_tokenize_composite_specifiers(self):
        # %T must win over a stray '%' followed by 'T'-adjacent text, and the
        # composite %r must come out as one canonical fragment.
        mysql = Dialect["mysql"]
        self.assertEqual("%H:%M:%S", format_time("%T", mysql.TIME_MAPPING, mysql.TIME_TRIE))
        self.assertEqual("%I:%M:%S %p", format_time("%r", mysql.TIME_MAPPING, mysql.TIME_TRIE))
        self.assertEqual(
            "%Y-%m-%d %H:%M:%S",
            format_time("%Y-%m-%d %T", mysql.TIME_MAPPING, mysql.TIME_TRIE),
        )

    def test_snowflake_quoted_t_separator_roundtrip(self):
        snowflake = Dialect["snowflake"]
        self.assertEqual(
            "%Y-%m-%dT%H:%M:%S",
            format_time('YYYY-MM-DD"T"HH24:MI:SS', snowflake.TIME_MAPPING, snowflake.TIME_TRIE),
        )


class StrictAndLenientParseTest(unittest.TestCase):
    """HiveGenerator's parse-side lenient rewrite (strict dialects only)."""

    def test_delimited_lax_specifiers_become_non_padded_for_parsing(self):
        self.assertEqual("%Y-%-m-%-d", _lenient_parse_format("%Y-%m-%d"))
        self.assertEqual("%-H:%-M:%-S", _lenient_parse_format("%H:%M:%S"))

    def test_adjacent_specifiers_are_not_rewritten(self):
        # java.time greedily consumes adjacent digit runs, so yyyyMMdd cannot be
        # made lenient and must stay exactly as given.
        self.assertEqual("%Y%m%d", _lenient_parse_format("%Y%m%d"))
        self.assertEqual("%Y%mstrict%dstrict", _lenient_parse_format("%Y%mstrict%dstrict"))

    def test_digit_neighbor_blocks_rewrite(self):
        self.assertEqual("%Y1%m", _lenient_parse_format("%Y1%m"))
        self.assertEqual("%m1", _lenient_parse_format("%m1"))

    def test_strict_formats_are_left_untouched(self):
        for atom in ("%mstrict", "%dstrict", "%Hstrict", "%Istrict", "%Mstrict", "%Sstrict"):
            self.assertEqual(atom, _lenient_parse_format(atom))

    def test_end_to_end_strict_spark_parse_format(self):
        # Padded input parses strictly in Spark 3 and must render back padded
        # for parse functions, while single-digit input renders non-padded.
        padded = parse_one("SELECT TO_TIMESTAMP(x, 'yyyy-MM-dd HH:mm:ss')", read="spark")
        self.assertEqual(
            "SELECT TO_TIMESTAMP(x, 'yyyy-MM-dd HH:mm:ss')", padded.sql(dialect="spark")
        )
        non_padded = parse_one("SELECT TO_TIMESTAMP(x, 'yyyy-M-d H:m:s')", read="spark")
        self.assertEqual(
            "SELECT TO_TIMESTAMP(x, 'yyyy-M-d H:m:s')", non_padded.sql(dialect="spark")
        )
        # Formatting (TimeToStr) always pads, regardless of input strictness.
        formatting = parse_one("SELECT DATE_FORMAT(x, 'yyyy-MM-dd')", read="spark")
        self.assertEqual("SELECT DATE_FORMAT(x, 'yyyy-MM-dd')", formatting.sql(dialect="spark"))
        # Default parse format for a strict dialect elides as CAST-able.
        self.assertEqual(
            "SELECT TO_DATE(x)",
            parse_one("SELECT TO_DATE(x, 'yyyy-MM-dd')", read="spark").sql(dialect="spark"),
        )


# --------------------------------------------------------------------------- #
# End to end: cross-dialect semantic fidelity
# --------------------------------------------------------------------------- #


def _source_sql(dialect, fmt):
    if dialect == "mysql":
        return f"SELECT DATE_FORMAT(x, '{fmt}')"
    if dialect == "snowflake":
        return f"SELECT TO_CHAR(x::TIMESTAMP, '{fmt}')"
    if dialect == "duckdb":
        return f"SELECT STRFTIME(x, '{fmt}')"
    if dialect in ("spark", "spark2", "hive"):
        return f"SELECT DATE_FORMAT(x, '{fmt}')"
    raise ValueError(dialect)


def _parse_source_sql(dialect, fmt):
    if dialect == "mysql":
        return f"SELECT STR_TO_DATE(x, '{fmt}')"
    if dialect == "snowflake":
        return f"SELECT TO_TIMESTAMP(x, '{fmt}')"
    if dialect == "duckdb":
        return f"SELECT STRPTIME(x, '{fmt}')"
    if dialect in ("spark", "spark2", "hive"):
        return f"SELECT TO_TIMESTAMP(x, '{fmt}')"
    raise ValueError(dialect)


_STRING_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'")


def _last_string_literal(sql):
    literals = _STRING_LITERAL_RE.findall(sql)
    assert literals, f"no format literal found in generated SQL: {sql}"
    return literals[-1]


def _find_format_literal(sql):
    """Like _last_string_literal, but None when the parse format was elided.

    When a parse function's format matches the target dialect's default
    DATE_FORMAT/TIME_FORMAT, the generator drops it (plain CAST / TO_DATE), which
    is itself a semantic-preserving transformation.
    """
    literals = _STRING_LITERAL_RE.findall(sql)
    return literals[-1] if literals else None


def _canonicalize_target_format(target_dialect, target_fmt):
    dialect = Dialect[target_dialect]
    return format_time(target_fmt, dialect.TIME_MAPPING, dialect.TIME_TRIE)


def _assert_semantically_equal(
    testcase, source_canonical, target_canonical, *, source, target, source_fmt, target_fmt
):
    for dt in SAMPLE_TIMESTAMPS:
        expected = render_canonical(source_canonical, dt)
        actual = render_canonical(target_canonical, dt)
        testcase.assertEqual(
            expected,
            actual,
            msg=(
                f"{source}:{source_fmt!r} -> {target}:{target_fmt!r} renders differently "
                f"at {dt}: {expected!r} != {actual!r} "
                f"(canonical: {source_canonical!r} -> {target_canonical!r})"
            ),
        )


# Realistic, *valid* formats for each source dialect. Validity matters: e.g. in
# MySQL %M is the full month name, so '%H:%M:%S' is not a legal time format and
# must not be used as test input.
SOURCE_FORMATS = {
    "mysql": (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%i:%s",
        "%Y/%m/%d",
        "%H:%i:%s",
        "%h:%i:%s %p",
        "%a, %d %b %Y",
        "%Y%m%d",
        "%r",
        "%T",
        "%Y年%m月%d日",
        # %u (MySQL week, Monday=0) maps to %W, which differs from DuckDB's
        # Monday=1 %u; cross-dialect week-number pairs are covered token-level.
    ),
    "snowflake": (
        "yyyy-mm-dd",
        "yyyy-mm-dd hh24:mi:ss",
        "YYYY/MM/DD",
        "HH24:MI:SS",
        "YYYY MM DD",
        "DY, DD MON YYYY",
        "yyyy-mm-dd hh12:mi:ss am",
        'YYYY-MM-DD"T"HH24:MI:SS',
        "ff3",
        "ff6",
    ),
    "duckdb": (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%H:%M:%S",
        "%Y/%m/%d",
        "%a, %d %b %Y",
        "%Y%m%d",
        "%I:%M:%S %p",
    ),
    "spark": (
        "yyyy-MM-dd HH:mm:ss",
        "yyyyMMdd",
        "yyyy-M-d H:m:s",
    ),
}

# Target dialects exercised by the end-to-end matrix.
TARGET_DIALECTS = (
    "mysql",
    "snowflake",
    "duckdb",
    "postgres",
    "spark",
    "hive",
    "spark2",
    "bigquery",
    "oracle",
)

# Snowflake fractional-second precision formats: the render oracle can only
# re-canonicalize the DuckDB output for ff3/ff6; Hive-family generators leak
# the internal pseudo-atom (e.g. 'SSSSSS_three') because java time only supports
# microseconds, so those pairs are excluded from the generic render matrix and
# covered separately (explicit assertions + real DuckDB execution).
SNOWFLAKE_FRACTIONAL_FORMATS = {"ff3", "ff6"}
RENDER_SKIP_PAIRS = {
    (source, fmt, target)
    for source in ("snowflake",)
    for fmt in SNOWFLAKE_FRACTIONAL_FORMATS
    for target in ("spark", "hive", "spark2")
}


def _parse_semantic_canonical(canonical):
    """Lenientize a canonical format used as a parse specification.

    Strict dialects (Hive 4 / Spark 3) deliberately render parse formats with
    non-padded M/d/H/m/s so single-digit sources stay parseable, so for parse
    expressions padded and non-padded numeric fields are semantically equal.
    """
    for padded, strict in (
        ("%m", "%mstrict"),
        ("%d", "%dstrict"),
        ("%H", "%Hstrict"),
        ("%I", "%Istrict"),
        ("%M", "%Mstrict"),
        ("%S", "%Sstrict"),
    ):
        canonical = canonical.replace(strict, padded)
    return _lenient_parse_format(canonical)


class EndToEndFidelityTest(unittest.TestCase):
    """transpile() must preserve the rendered timestamp semantics in both directions."""

    def _run_matrix(self, builder, *, parse_direction=False):
        for source, formats in SOURCE_FORMATS.items():
            for source_fmt in formats:
                expression = parse_one(builder(source, source_fmt), read=source)
                source_canonical = expression.expressions[0].args["format"].this
                compare_source_canonical = (
                    _parse_semantic_canonical(source_canonical)
                    if parse_direction
                    else source_canonical
                )
                for target in TARGET_DIALECTS:
                    if (source, source_fmt, target) in RENDER_SKIP_PAIRS:
                        continue
                    with self.subTest(source=source, fmt=source_fmt, target=target):
                        generated = expression.sql(dialect=target)
                        target_fmt = (
                            _find_format_literal(generated)
                            if parse_direction
                            else _last_string_literal(generated)
                        )
                        if target_fmt is None:
                            # Format elided as a plain CAST: only possible when
                            # it equals the target dialect's default date/time format.
                            dialect = Dialect[target]
                            default_canonicals = tuple(
                                _parse_semantic_canonical(
                                    format_time(
                                        default.strip("'"),
                                        dialect.TIME_MAPPING,
                                        dialect.TIME_TRIE,
                                    )
                                )
                                for default in (dialect.DATE_FORMAT, dialect.TIME_FORMAT)
                            )
                            self.assertIn(
                                compare_source_canonical,
                                default_canonicals,
                                msg=(
                                    f"{source}:{source_fmt!r} -> {target} elided its format but "
                                    f"{compare_source_canonical!r} is not one of the target "
                                    f"default formats {default_canonicals}"
                                ),
                            )
                            continue
                        target_canonical = _canonicalize_target_format(target, target_fmt)
                        if parse_direction:
                            target_canonical = _parse_semantic_canonical(target_canonical)
                        _assert_semantically_equal(
                            self,
                            compare_source_canonical,
                            target_canonical,
                            source=source,
                            target=target,
                            source_fmt=source_fmt,
                            target_fmt=target_fmt,
                        )

    def test_formatting_functions_preserve_semantics(self):
        # generation direction: TimeToStr / DATE_FORMAT / TO_CHAR / STRFTIME
        self._run_matrix(_source_sql)

    def test_parsing_functions_preserve_semantics(self):
        # parse direction: StrToTime / STR_TO_DATE / TO_TIMESTAMP / STRPTIME
        self._run_matrix(_parse_source_sql, parse_direction=True)

    def test_text_changes_but_semantics_preserved(self):
        # These pairs deliberately change format *text* while keeping semantics.
        text_changes = (
            ("mysql", "%Y-%m-%d %H:%i:%s", "mysql", "%Y-%m-%d %T"),
            ("mysql", "%H:%i:%s", "mysql", "%T"),
            ("mysql", "%h:%i:%s %p", "mysql", "%r"),
            ("mysql", "%Y-%m-%d", "snowflake", "yyyy-mm-DD"),
            ("mysql", "%Y-%m-%d %H:%i:%s", "snowflake", "yyyy-mm-DD hh24:mi:ss"),
            ("snowflake", "yyyy-mm-dd", "postgres", "YYYY-MM-FMDD"),
            ("mysql", "%Y-%m-%d", "bigquery", "%F"),
            ("mysql", "%Y-%m-%d %H:%i:%s", "bigquery", "%F %T"),
        )
        for source, source_fmt, target, expected_text in text_changes:
            with self.subTest(source=source, fmt=source_fmt, target=target):
                expression = parse_one(_source_sql(source, source_fmt), read=source)
                generated = expression.sql(dialect=target)
                target_fmt = _last_string_literal(generated)
                self.assertEqual(
                    expected_text,
                    target_fmt,
                    msg=f"expected canonical-equivalent rewrite to {expected_text!r}, got {target_fmt!r}",
                )
                self.assertNotEqual(
                    source_fmt,
                    target_fmt,
                    msg="this pair was chosen because the format text is supposed to change",
                )
                source_canonical = expression.expressions[0].args["format"].this
                _assert_semantically_equal(
                    self,
                    source_canonical,
                    _canonicalize_target_format(target, target_fmt),
                    source=source,
                    target=target,
                    source_fmt=source_fmt,
                    target_fmt=target_fmt,
                )

    def test_text_identity_when_equivalent_token_exists(self):
        # When the target dialect natively supports the exact same format text,
        # transpilation must not gratuitously rewrite it.
        identities = (
            ("duckdb", "%Y-%m-%d %H:%M:%S", "duckdb"),
            ("mysql", "%Y-%m-%d", "mysql"),
            ("snowflake", "yyyy-mm-dd hh24:mi:ss", "snowflake"),
        )
        for source, fmt, target in identities:
            expression = parse_one(_source_sql(source, fmt), read=source)
            self.assertEqual(fmt, _last_string_literal(expression.sql(dialect=target)))

    def test_fractional_second_precision(self):
        # ff6 is microseconds and survives on every target; ff3 (milliseconds)
        # maps to DuckDB's %g, while Hive-family generators only model
        # microseconds, so the pseudo-atom leaks (documented limitation).
        ff6 = parse_one(_source_sql("snowflake", "ff6"), read="snowflake")
        source_canonical = ff6.expressions[0].args["format"].this
        for target in ("snowflake", "duckdb", "mysql", "postgres"):
            target_fmt = _last_string_literal(ff6.sql(dialect=target))
            _assert_semantically_equal(
                self,
                source_canonical,
                _canonicalize_target_format(target, target_fmt),
                source="snowflake",
                target=target,
                source_fmt="ff6",
                target_fmt=target_fmt,
            )

        ff3 = parse_one(_source_sql("snowflake", "ff3"), read="snowflake")
        self.assertEqual("%g", _last_string_literal(ff3.sql(dialect="duckdb")))
        dt = datetime.datetime(2024, 3, 5, 14, 7, 9, 123456)
        self.assertEqual(
            render_canonical("%f_three", dt),
            render_canonical("%g", dt),
        )

    def test_parse_direction_strict_dialect_lenientization(self):
        # Padded canonical formats arrive at strict Spark parse functions as
        # non-padded text, so both '2024-03-05' and '2024-3-5' parse correctly.
        padded = parse_one(_parse_source_sql("mysql", "%Y-%m-%d"), read="mysql")
        self.assertEqual("SELECT TO_DATE(x, 'yyyy-M-d')", padded.sql(dialect="spark"))

        full = parse_one(_parse_source_sql("mysql", "%Y-%m-%d %H:%i:%s"), read="mysql")
        self.assertEqual("SELECT TO_TIMESTAMP(x, 'yyyy-M-d H:m:s')", full.sql(dialect="spark"))
        # Adjacent fields can't be made lenient (greedy java.time parsing).
        adjacent = parse_one(_parse_source_sql("mysql", "%Y%m%d"), read="mysql")
        self.assertEqual("SELECT TO_DATE(x, 'yyyyMMdd')", adjacent.sql(dialect="spark"))
        # Spark2 uses SimpleDateFormat, which is natively lenient on padding.
        self.assertEqual(
            "SELECT TO_TIMESTAMP(x, 'yyyy-MM-dd HH:mm:ss')",
            parse_one(_parse_source_sql("mysql", "%Y-%m-%d %H:%i:%s"), read="mysql").sql(
                dialect="spark2"
            ),
        )

    def test_literal_separators_survive(self):
        expression = parse_one(_source_sql("mysql", "%Y年%m月%d日 %H:%i:%s"), read="mysql")
        for target in ("mysql", "snowflake", "duckdb", "postgres"):
            generated = expression.sql(dialect=target)
            target_fmt = _last_string_literal(generated)
            source_canonical = expression.expressions[0].args["format"].this
            _assert_semantically_equal(
                self,
                source_canonical,
                _canonicalize_target_format(target, target_fmt),
                source="mysql",
                target=target,
                source_fmt="%Y年%m月%d日 %H:%i:%s",
                target_fmt=target_fmt,
            )

    def test_bidirectional_roundtrip_through_third_dialect(self):
        # A -> B -> A must keep semantics even when text changes on each leg.
        # DuckDB needs an explicit temporal type so Snowflake's TO_CHAR builder
        # recognizes the second argument as a format instead of a plain cast.
        cases = (
            ("mysql", "%Y-%m-%d %H:%i:%s", "SELECT DATE_FORMAT(x, '%Y-%m-%d %H:%i:%s')"),
            ("snowflake", "yyyy-mm-dd hh24:mi:ss", None),
            (
                "duckdb",
                "%Y-%m-%d %H:%M:%S",
                "SELECT STRFTIME(CAST(x AS TIMESTAMP), '%Y-%m-%d %H:%M:%S')",
            ),
        )
        for source, fmt, explicit_sql in cases:
            source_sql = explicit_sql or _source_sql(source, fmt)
            via_snowflake = parse_one(
                parse_one(source_sql, read=source).sql(dialect="snowflake"),
                read="snowflake",
            )
            back = via_snowflake.sql(dialect=source)
            back_fmt = _last_string_literal(back)
            original_canonical = parse_one(source_sql, read=source)
            _assert_semantically_equal(
                self,
                original_canonical.expressions[0].args["format"].this,
                _canonicalize_target_format(source, back_fmt),
                source=source,
                target=source,
                source_fmt=fmt,
                target_fmt=back_fmt,
            )

    @unittest.skipUnless(HAS_DUCKDB, "duckdb is required for execution-level verification")
    def test_duckdb_executes_generated_formats(self):
        # Ground truth from an actual database engine: transpile each source
        # format to DuckDB and execute it; the result must equal the canonical
        # renderer's output. Also covers tokens absent from DuckDB.TIME_MAPPING.
        connection = duckdb.connect()
        for source, formats in SOURCE_FORMATS.items():
            for source_fmt in formats:
                expression = parse_one(_source_sql(source, source_fmt), read=source)
                generated = expression.sql(dialect="duckdb")
                duckdb_fmt = _last_string_literal(generated)
                source_canonical = expression.expressions[0].args["format"].this
                for dt in SAMPLE_TIMESTAMPS[:3]:
                    literal = dt.strftime("%Y-%m-%d %H:%M:%S.%f")
                    try:
                        executed = connection.execute(
                            f"SELECT strftime(TIMESTAMP '{literal}', '{duckdb_fmt}')"
                        ).fetchone()[0]
                    except Exception as exc:  # pragma: no cover
                        self.fail(
                            f"{source}:{source_fmt!r} generated unexecutable DuckDB format "
                            f"{duckdb_fmt!r}: {exc}"
                        )
                    self.assertEqual(
                        render_canonical(source_canonical, dt),
                        executed,
                        msg=(
                            f"{source}:{source_fmt!r} -> duckdb:{duckdb_fmt!r} at {literal}: "
                            f"canonical {source_canonical!r}"
                        ),
                    )


class MutationSensitivityTest(unittest.TestCase):
    """Sanity checks that the assertions above are capable of going red."""

    def test_wrong_mapping_value_is_detected(self):
        mysql = Dialect["mysql"]
        original = mysql.TIME_MAPPING["%i"]
        broken = dict(mysql.TIME_MAPPING)
        broken["%i"] = "%S"  # minutes -> seconds: a realistic production bug
        try:
            self.assertNotEqual(
                format_time("%H:%i:%s", mysql.TIME_MAPPING, mysql.TIME_TRIE),
                format_time("%H:%i:%s", broken, new_trie(broken)),
            )
        finally:
            mysql.TIME_MAPPING["%i"] = original

    def test_swapped_case_mapping_is_detected(self):
        # If DD/dd were accidentally swapped, the canonical semantics diverge.
        correct = render_canonical("%d", datetime.datetime(2024, 3, 5))
        wrong = render_canonical("%-d", datetime.datetime(2024, 3, 5))
        self.assertEqual(("05", "5"), (correct, wrong))

    def test_broken_inverse_mapping_is_detected(self):
        # Simulate the generator emitting a wrong token and confirm the semantic
        # comparison machinery catches it.
        dt = datetime.datetime(2024, 3, 5, 14, 7, 9)
        good_target = "%Y-%m-%d %H:%M:%S"
        bad_target = "%Y-%m-%d %H:%S:%M"  # minute/second swapped
        self.assertNotEqual(render_canonical(good_target, dt), render_canonical(bad_target, dt))

    def test_padding_loss_is_detected(self):
        dt = datetime.datetime(2024, 3, 5)
        self.assertNotEqual(render_canonical("%Y-%m-%d", dt), render_canonical("%Y-%-m-%-d", dt))

    def test_algorithm_regression_is_detected(self):
        # A trie walk that forgets longest-match would turn "aa" into "bb" here.
        mapping = {"a": "b", "aa": "c"}
        self.assertEqual("c", format_time("aa", mapping, new_trie(mapping)))
        self.assertNotEqual("bb", format_time("aa", mapping, new_trie(mapping)))
