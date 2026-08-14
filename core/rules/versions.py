"""SEP version constraints on rules.

Config properties come and go across Starburst releases, so a rule states
which versions it applies to. Applicability is deliberately three-valued:

* ``True``  -- the rule applies, evaluate it
* ``False`` -- the rule does not apply to this release, skip it (not a failure)
* ``None``  -- we do not know the cluster's version, so we cannot say

The third case is the one that matters. Treating "unknown" as "does not
apply" would let a cluster with an undetectable version sail through every
rule and report a clean bill of health. Instead the engine counts these
separately and records a blind spot, so the parent can see that the silence
was ignorance rather than health.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_CONSTRAINT = re.compile(r"^\s*(>=|<=|>|<|==|=)?\s*(\d+)\s*$")

# Starburst versions look like 429, 429-e, 429-e.3, 413.0.0. The leading
# integer is the SEP release and is the only part rules ever constrain.
_RELEASE = re.compile(r"^\s*v?(\d+)")

ANY: Final = "*"


class VersionSpecError(ValueError):
    """A version constraint in the catalog could not be understood."""


def parse_release(version: str | None) -> int | None:
    """Extract the numeric SEP release from a version string."""
    if not version:
        return None
    match = _RELEASE.match(version)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class VersionRange:
    """A conjunction of constraints, as written in ``applies_to.sep_version``."""

    spec: str
    bounds: tuple[tuple[str, int], ...]

    @classmethod
    def parse(cls, spec: str | None) -> VersionRange:
        """Parse ``">=413"``, ``">=413,<440"``, ``"*"`` or nothing."""
        if spec is None or not str(spec).strip() or str(spec).strip() == ANY:
            return cls(spec=ANY, bounds=())
        bounds: list[tuple[str, int]] = []
        for part in str(spec).split(","):
            match = _CONSTRAINT.match(part)
            if not match:
                raise VersionSpecError(
                    f"Cannot parse version constraint {part!r} in {spec!r}. "
                    f"Expected forms like '>=413', '<440', '>=413,<440' or '*'."
                )
            operator = match.group(1) or "=="
            bounds.append(("==" if operator == "=" else operator, int(match.group(2))))
        return cls(spec=str(spec).strip(), bounds=tuple(bounds))

    @property
    def unconstrained(self) -> bool:
        """Whether this rule applies regardless of version."""
        return not self.bounds

    def applies_to(self, version: str | None) -> bool | None:
        """Three-valued applicability. ``None`` means the version is unknown."""
        if self.unconstrained:
            return True
        release = parse_release(version)
        if release is None:
            return None
        return all(
            _satisfies(release, operator, bound) for operator, bound in self.bounds
        )


def _satisfies(release: int, operator: str, bound: int) -> bool:
    match operator:
        case ">=":
            return release >= bound
        case ">":
            return release > bound
        case "<=":
            return release <= bound
        case "<":
            return release < bound
        case "==":
            return release == bound
    raise VersionSpecError(f"Unsupported operator {operator!r}.")
