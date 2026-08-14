"""Parser tests, including the line numbers that findings depend on."""

from __future__ import annotations

import pytest

from core.parsers import (
    ConfigParseError,
    parse,
    parse_jvm_config,
    parse_properties,
    parse_site_xml,
    parser_for,
)
from core.units import (
    UnitParseError,
    format_data_size,
    parse_boolean,
    parse_data_size,
    parse_duration,
)


class TestProperties:
    def test_reads_keys_values_and_line_numbers(self) -> None:
        parsed = parse_properties(
            "config.properties",
            "# comment\ncoordinator=true\n\nquery.max-memory=200GB\n",
        )
        assert parsed.values["coordinator"] == "true"
        assert parsed.values["query.max-memory"] == "200GB"
        assert parsed.line_of("query.max-memory") == 4

    def test_ignores_both_comment_markers(self) -> None:
        parsed = parse_properties("c.properties", "# one\n! two\nkey=value\n")
        assert list(parsed.values) == ["key"]

    def test_accepts_colon_as_a_separator(self) -> None:
        assert parse_properties("c.properties", "key: value\n").values["key"] == "value"

    def test_keeps_equals_signs_inside_a_value(self) -> None:
        parsed = parse_properties("c.properties", "url=jdbc:x?a=1&b=2\n")
        assert parsed.values["url"] == "jdbc:x?a=1&b=2"

    def test_joins_continuations_and_reports_the_starting_line(self) -> None:
        parsed = parse_properties("c.properties", "a=1\nlong=one\\\n  two\n")
        assert parsed.values["long"] == "one  two"
        assert parsed.line_of("long") == 2

    def test_a_key_with_no_value_is_empty_not_missing(self) -> None:
        assert parse_properties("c.properties", "key=\n").values["key"] == ""


class TestJvmConfig:
    @pytest.mark.parametrize(
        ("line", "key", "value"),
        [
            ("-Xmx80G", "-Xmx", "80G"),
            ("-Xms16G", "-Xms", "16G"),
            ("-XX:+UseG1GC", "-XX:UseG1GC", "true"),
            (
                "-XX:-OmitStackTraceInFastThrow",
                "-XX:OmitStackTraceInFastThrow",
                "false",
            ),
            ("-XX:G1HeapRegionSize=32M", "-XX:G1HeapRegionSize", "32M"),
            ("-Dcom.sun.foo=bar", "-Dcom.sun.foo", "bar"),
            ("-server", "-server", "true"),
        ],
    )
    def test_normalises_flag_forms(self, line: str, key: str, value: str) -> None:
        parsed = parse_jvm_config("jvm.config", line + "\n")
        assert parsed.values[key] == value

    def test_records_line_numbers(self) -> None:
        parsed = parse_jvm_config("jvm.config", "-server\n-Xmx80G\n")
        assert parsed.line_of("-Xmx") == 2


class TestSiteXml:
    def test_flattens_properties(self) -> None:
        xml = (
            "<configuration>\n"
            "  <property><name>fs.s3a.endpoint</name>"
            "<value>s3.example</value></property>\n"
            "</configuration>\n"
        )
        parsed = parse_site_xml("hive-site.xml", xml)
        assert parsed.values["fs.s3a.endpoint"] == "s3.example"
        assert parsed.line_of("fs.s3a.endpoint") == 2

    def test_malformed_xml_raises_a_parse_error(self) -> None:
        with pytest.raises(ConfigParseError):
            parse_site_xml("hive-site.xml", "<configuration>")

    def test_oversized_input_is_refused(self) -> None:
        with pytest.raises(ConfigParseError, match="parse limit"):
            parse_site_xml("hive-site.xml", "<a/>" + " " * (9 * 1024 * 1024))


class TestRegistry:
    @pytest.mark.parametrize(
        "filename",
        ["config.properties", "jvm.config", "hive-site.xml", "catalog/hive.properties"],
    )
    def test_known_formats_have_a_parser(self, filename: str) -> None:
        assert parser_for(filename) is not None

    def test_unknown_format_has_none(self) -> None:
        assert parser_for("resource-groups.json") is None

    def test_parsing_an_unknown_format_raises(self) -> None:
        with pytest.raises(ConfigParseError, match="No parser"):
            parse("notes.txt", "hello")


class TestUnits:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1B", 1),
            ("1kB", 1024),
            ("40GB", 40 * 1024**3),
            ("80G", 80 * 1024**3),
            ("81920m", 81920 * 1024**2),
            ("1.5GB", int(1.5 * 1024**3)),
        ],
    )
    def test_data_sizes_normalise_to_bytes(self, text: str, expected: int) -> None:
        assert parse_data_size(text) == expected

    def test_equivalent_sizes_compare_equal(self) -> None:
        """The reason normalisation exists: 40GB and 40960MB are one setting."""
        assert parse_data_size("40GB") == parse_data_size("40960MB")

    @pytest.mark.parametrize(
        ("text", "seconds"), [("30s", 30), ("5m", 300), ("1h", 3600), ("500ms", 0.5)]
    )
    def test_durations_normalise_to_seconds(self, text: str, seconds: float) -> None:
        assert parse_duration(text) == pytest.approx(seconds)

    @pytest.mark.parametrize("text", ["", "GB", "40 furlongs", "abc"])
    def test_unreadable_quantities_raise(self, text: str) -> None:
        with pytest.raises(UnitParseError):
            parse_data_size(text)

    @pytest.mark.parametrize(
        ("text", "expected"), [("true", True), ("YES", True), ("off", False)]
    )
    def test_booleans(self, text: str, expected: bool) -> None:
        assert parse_boolean(text) is expected

    @pytest.mark.parametrize(
        ("size", "rendered"), [(1024, "1kB"), (24 * 1024**3, "24GB"), (0, "0B")]
    )
    def test_formatting_round_trips_for_reading(self, size: int, rendered: str) -> None:
        assert format_data_size(size) == rendered
