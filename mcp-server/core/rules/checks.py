"""The check kinds a rule may use.

A rule does not carry an expression to evaluate; it names a check kind and
fills in that kind's fields. Nothing in the catalog is ever parsed as code,
so a YAML file cannot execute anything, and rule authors do not have to be
trusted the way a code contributor is.

Adding a *rule* is a YAML edit. Adding a *kind* is a small class here plus a
registry entry -- rarely needed, and reviewed like any other code.

Each kind reports three things the parent needs: whether it passed, the
actual and expected values as readable text, and a relative ``deviation`` so
that "300% over" outranks "5% over" when findings are sorted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from core.config.snapshot import ClusterSnapshot
from core.models import Role
from core.units import (
    UnitParseError,
    format_data_size,
    parse_data_size,
    parse_duration,
    parse_number,
)

HEAP_REFERENCE = "jvm_heap"
PROPERTY_PREFIX = "property:"


class RuleDefinitionError(ValueError):
    """A rule in the catalog is malformed. Raised at load time, never at run time."""


@dataclass(frozen=True)
class CheckOutcome:
    """The result of applying one check to one value."""

    passed: bool
    actual: str
    expected: str
    deviation: float | None = None
    missing: list[str] = field(default_factory=list)

    @classmethod
    def unavailable(
        cls, actual: str, expected: str, missing: list[str]
    ) -> CheckOutcome:
        """The check could not run because an input it needed was absent."""
        return cls(passed=True, actual=actual, expected=expected, missing=missing)


@dataclass(frozen=True)
class RuleContext:
    """What a check may look at besides the value under test.

    Deliberately narrow. A check can read other config from the same
    snapshot; it cannot reach storage, the network, or injected context.
    """

    snapshot: ClusterSnapshot
    role: Role

    def reference(self, name: str) -> tuple[float | None, str]:
        """Resolve a named quantity a ratio check compares against."""
        if name == HEAP_REFERENCE:
            heap = self.snapshot.heap_bytes(self.role)
            return (None if heap is None else float(heap)), "JVM heap (-Xmx)"
        if name.startswith(PROPERTY_PREFIX):
            key = name[len(PROPERTY_PREFIX) :]
            resolved = self.snapshot.resolve(key, self.role)
            if resolved is None:
                return None, key
            try:
                return float(parse_data_size(resolved.value)), key
            except UnitParseError:
                return None, key
        return None, name


class Check(Protocol):
    """A single comparison, already validated at catalog load time."""

    kind: ClassVar[str]

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        """Apply the check to a value known to be present."""
        ...

    def describe(self) -> str:
        """Human-readable statement of what this check requires."""
        ...


# --------------------------------------------------------------------------
# Value interpretation
# --------------------------------------------------------------------------

_Parser = Callable[[str], float]

_PARSERS: dict[str, _Parser] = {
    "bytes": lambda text: float(parse_data_size(text)),
    "duration": parse_duration,
    "number": parse_number,
}

_FORMATTERS: dict[str, Callable[[float], str]] = {
    "bytes": format_data_size,
    "duration": lambda seconds: f"{seconds:g}s",
    "number": lambda number: f"{number:g}",
}


_BYTE_SUFFIXES = frozenset({"b", "k", "kb", "m", "mb", "g", "gb", "t", "tb", "p", "pb"})
_DURATION_SUFFIXES = frozenset({"ns", "us", "ms", "s", "h", "d"})

_SUFFIX = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*([a-zA-Z]*)\s*$")


def infer_value_type(literal: str) -> str:
    """Decide how to interpret a threshold, from the unit it is written with.

    Resolved once at catalog load time, so a rule's meaning is fixed by its
    own text and can be asserted in a test -- it never depends on the value
    it happens to meet at run time.

    ``m`` is read as megabytes, not minutes, because that is what it means
    everywhere in a Trino or JVM config. Write ``60s`` for a duration, or set
    ``as: duration`` explicitly.
    """
    match = _SUFFIX.match(literal)
    if match is None:
        raise RuleDefinitionError(
            f"Threshold {literal!r} is not a number, data size or duration."
        )
    suffix = match.group(1).lower()
    if not suffix:
        return "number"
    if suffix in _BYTE_SUFFIXES:
        return "bytes"
    if suffix in _DURATION_SUFFIXES:
        return "duration"
    raise RuleDefinitionError(
        f"Threshold {literal!r} has unrecognised unit {suffix!r}."
    )


def _read(value: str, value_type: str) -> float | None:
    try:
        return _PARSERS[value_type](value)
    except (UnitParseError, ValueError):
        return None


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

CHECK_KINDS: dict[str, Callable[[dict[str, Any]], Check]] = {}


def register(kind: str) -> Callable[[type[Check]], type[Check]]:
    """Register a check kind under the name rule authors write in YAML."""

    def decorate(cls: type[Check]) -> type[Check]:
        CHECK_KINDS[kind] = cls.from_spec  # type: ignore[attr-defined]
        return cls

    return decorate


def build_check(spec: dict[str, Any]) -> Check:
    """Construct a check from its YAML mapping, validating it fully."""
    if not isinstance(spec, dict) or "kind" not in spec:
        raise RuleDefinitionError("A check must be a mapping with a 'kind' field.")
    kind = str(spec["kind"])
    factory = CHECK_KINDS.get(kind)
    if factory is None:
        known = ", ".join(sorted(CHECK_KINDS))
        raise RuleDefinitionError(f"Unknown check kind {kind!r}. Known kinds: {known}.")
    return factory(spec)


def _require(spec: dict[str, Any], field_name: str, kind: str) -> Any:
    if field_name not in spec:
        raise RuleDefinitionError(
            f"Check kind {kind!r} requires a {field_name!r} field."
        )
    return spec[field_name]


def _reject_unknown(spec: dict[str, Any], allowed: set[str], kind: str) -> None:
    """Fail loudly on a misspelled field rather than silently ignoring it."""
    unknown = set(spec) - allowed - {"kind"}
    if unknown:
        raise RuleDefinitionError(
            f"Check kind {kind!r} does not accept: {', '.join(sorted(unknown))}."
        )


# --------------------------------------------------------------------------
# Check kinds
# --------------------------------------------------------------------------


@register("required")
@dataclass(frozen=True)
class Required:
    """The property must be set. Presence is checked before this runs."""

    kind: ClassVar[str] = "required"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Required:
        _reject_unknown(spec, set(), cls.kind)
        return cls()

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        return CheckOutcome(passed=True, actual=value, expected="must be set")

    def describe(self) -> str:
        return "must be set"


@register("equals")
@dataclass(frozen=True)
class Equals:
    """The value must equal a literal, compared case-insensitively."""

    expected: str
    kind: ClassVar[str] = "equals"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Equals:
        _reject_unknown(spec, {"value"}, cls.kind)
        return cls(expected=str(_require(spec, "value", cls.kind)))

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        return CheckOutcome(
            passed=value.strip().lower() == self.expected.strip().lower(),
            actual=value,
            expected=self.describe(),
        )

    def describe(self) -> str:
        return f"= {self.expected}"


@register("one_of")
@dataclass(frozen=True)
class OneOf:
    """The value must be one of an allowed set."""

    allowed: tuple[str, ...]
    kind: ClassVar[str] = "one_of"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> OneOf:
        _reject_unknown(spec, {"values"}, cls.kind)
        raw = _require(spec, "values", cls.kind)
        if not isinstance(raw, list) or not raw:
            raise RuleDefinitionError("'values' must be a non-empty list.")
        return cls(allowed=tuple(str(item) for item in raw))

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        lowered = {item.lower() for item in self.allowed}
        return CheckOutcome(
            passed=value.strip().lower() in lowered,
            actual=value,
            expected=self.describe(),
        )

    def describe(self) -> str:
        return f"one of: {', '.join(self.allowed)}"


@register("matches")
@dataclass(frozen=True)
class Matches:
    """The value must match a regular expression."""

    pattern: re.Pattern[str]
    kind: ClassVar[str] = "matches"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Matches:
        _reject_unknown(spec, {"pattern"}, cls.kind)
        raw = str(_require(spec, "pattern", cls.kind))
        try:
            return cls(pattern=re.compile(raw))
        except re.error as exc:
            raise RuleDefinitionError(f"Invalid regex {raw!r}: {exc}") from exc

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        return CheckOutcome(
            passed=self.pattern.search(value) is not None,
            actual=value,
            expected=self.describe(),
        )

    def describe(self) -> str:
        return f"matches /{self.pattern.pattern}/"


@dataclass(frozen=True)
class _Bound:
    """Shared behaviour for the absolute threshold kinds."""

    literal: str
    value_type: str
    threshold: float

    @staticmethod
    def parse(literal: str, override: str | None = None) -> tuple[str, str, float]:
        text = str(literal)
        if override is not None and override not in _PARSERS:
            raise RuleDefinitionError(
                f"Unknown 'as' value {override!r}. Expected one of: "
                f"{', '.join(sorted(_PARSERS))}."
            )
        value_type = override or infer_value_type(text)
        parsed = _read(text, value_type)
        if parsed is None:
            raise RuleDefinitionError(f"Cannot read threshold {text!r}.")
        return text, value_type, parsed

    def render(self, number: float) -> str:
        return _FORMATTERS[self.value_type](number)


@register("max")
@dataclass(frozen=True)
class Max(_Bound):
    """The value must not exceed a threshold."""

    kind: ClassVar[str] = "max"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Max:
        _reject_unknown(spec, {"value", "as"}, cls.kind)
        literal, value_type, threshold = cls.parse(
            _require(spec, "value", cls.kind), spec.get("as")
        )
        return cls(literal=literal, value_type=value_type, threshold=threshold)

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        actual = _read(value, self.value_type)
        if actual is None:
            return CheckOutcome.unavailable(
                value, self.describe(), ["unparseable value"]
            )
        return CheckOutcome(
            passed=actual <= self.threshold,
            actual=value,
            expected=self.describe(),
            deviation=_relative(actual, self.threshold),
        )

    def describe(self) -> str:
        return f"<= {self.literal}"


@register("min")
@dataclass(frozen=True)
class Min(_Bound):
    """The value must be at least a threshold."""

    kind: ClassVar[str] = "min"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Min:
        _reject_unknown(spec, {"value", "as"}, cls.kind)
        literal, value_type, threshold = cls.parse(
            _require(spec, "value", cls.kind), spec.get("as")
        )
        return cls(literal=literal, value_type=value_type, threshold=threshold)

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        actual = _read(value, self.value_type)
        if actual is None:
            return CheckOutcome.unavailable(
                value, self.describe(), ["unparseable value"]
            )
        return CheckOutcome(
            passed=actual >= self.threshold,
            actual=value,
            expected=self.describe(),
            deviation=_relative(self.threshold, actual),
        )

    def describe(self) -> str:
        return f">= {self.literal}"


@register("range")
@dataclass(frozen=True)
class Range:
    """The value must fall between two inclusive bounds."""

    low: _Bound
    high: _Bound
    kind: ClassVar[str] = "range"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Range:
        _reject_unknown(spec, {"min", "max", "as"}, cls.kind)
        override = spec.get("as")
        low = _Bound(*_Bound.parse(_require(spec, "min", cls.kind), override))
        high = _Bound(*_Bound.parse(_require(spec, "max", cls.kind), override))
        if low.value_type != high.value_type:
            raise RuleDefinitionError(
                f"range bounds must be the same kind of quantity, got "
                f"{low.value_type} and {high.value_type}."
            )
        if low.threshold > high.threshold:
            raise RuleDefinitionError("range 'min' must not exceed 'max'.")
        return cls(low=low, high=high)

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        actual = _read(value, self.low.value_type)
        if actual is None:
            return CheckOutcome.unavailable(
                value, self.describe(), ["unparseable value"]
            )
        if actual < self.low.threshold:
            deviation = _relative(self.low.threshold, actual)
        elif actual > self.high.threshold:
            deviation = _relative(actual, self.high.threshold)
        else:
            deviation = None
        return CheckOutcome(
            passed=self.low.threshold <= actual <= self.high.threshold,
            actual=value,
            expected=self.describe(),
            deviation=deviation,
        )

    def describe(self) -> str:
        return f"between {self.low.literal} and {self.high.literal}"


@dataclass(frozen=True)
class _Ratio:
    """Shared behaviour for the kinds that compare against another quantity."""

    of: str
    ratio: float

    @classmethod
    def read_spec(cls, spec: dict[str, Any], kind: str) -> tuple[str, float]:
        _reject_unknown(spec, {"of", "ratio"}, kind)
        of = str(_require(spec, "of", kind))
        try:
            ratio = float(_require(spec, "ratio", kind))
        except (TypeError, ValueError) as exc:
            raise RuleDefinitionError("'ratio' must be a number.") from exc
        if ratio <= 0:
            raise RuleDefinitionError("'ratio' must be greater than zero.")
        return of, ratio

    def limit(self, context: RuleContext) -> tuple[float | None, str]:
        reference, label = context.reference(self.of)
        if reference is None:
            return None, label
        return reference * self.ratio, label


@register("max_ratio")
@dataclass(frozen=True)
class MaxRatio(_Ratio):
    """The value must not exceed a fraction of another quantity."""

    kind: ClassVar[str] = "max_ratio"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> MaxRatio:
        of, ratio = cls.read_spec(spec, cls.kind)
        return cls(of=of, ratio=ratio)

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        limit, label = self.limit(context)
        if limit is None:
            return CheckOutcome.unavailable(value, self.describe(), [label])
        actual = _read(value, "bytes")
        if actual is None:
            return CheckOutcome.unavailable(
                value, self.describe(), ["unparseable value"]
            )
        return CheckOutcome(
            passed=actual <= limit,
            actual=value,
            expected=f"<= {format_data_size(limit)} ({self.ratio:g} x {label})",
            deviation=_relative(actual, limit),
        )

    def describe(self) -> str:
        return f"<= {self.ratio:g} x {self.of}"


@register("min_ratio")
@dataclass(frozen=True)
class MinRatio(_Ratio):
    """The value must be at least a fraction of another quantity."""

    kind: ClassVar[str] = "min_ratio"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> MinRatio:
        of, ratio = cls.read_spec(spec, cls.kind)
        return cls(of=of, ratio=ratio)

    def evaluate(self, value: str, context: RuleContext) -> CheckOutcome:
        limit, label = self.limit(context)
        if limit is None:
            return CheckOutcome.unavailable(value, self.describe(), [label])
        actual = _read(value, "bytes")
        if actual is None:
            return CheckOutcome.unavailable(
                value, self.describe(), ["unparseable value"]
            )
        return CheckOutcome(
            passed=actual >= limit,
            actual=value,
            expected=f">= {format_data_size(limit)} ({self.ratio:g} x {label})",
            deviation=_relative(limit, actual),
        )

    def describe(self) -> str:
        return f">= {self.ratio:g} x {self.of}"


def _relative(larger: float, smaller: float) -> float | None:
    """How far past the limit a value sits, as a fraction of the limit."""
    if smaller <= 0:
        return None
    return max(0.0, (larger - smaller) / smaller)
