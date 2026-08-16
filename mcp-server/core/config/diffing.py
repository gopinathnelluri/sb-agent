"""Classification of differences between two clusters.

A raw property diff of two clusters is mostly noise: hostnames, node ids and
instance-sized memory values are *supposed* to differ, and burying the one
real divergence among forty expected ones makes the tool useless.

Classification is deterministic and explained by construction:

* ``expected``   -- the property identifies a machine or an environment, so
  two clusters differing on it says nothing
* ``unexpected`` -- some rule in the catalog governs this property, so a
  difference is a difference in policy and worth leading with
* ``unknown``    -- neither applies; reported honestly as unclassified rather
  than guessed either way
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Final, Literal, TypeAlias

Classification: TypeAlias = Literal["expected", "unexpected", "unknown"]

EXPECTED_TO_DIFFER: Final[tuple[str, ...]] = (
    "node.id",
    "node.internal-address",
    "node.environment",
    "*.host",
    "*.hostname",
    "*address*",
    "*uri*",
    "*url*",
    "*endpoint*",
    "discovery.uri",
    "http-server.http.port",
    "http-server.https.port",
    "*.path",
    "*data-dir*",
    "*truststore*",
    "*keystore*",
)


def classify(key: str, covered_by_rules: bool) -> Classification:
    """Classify one differing property.

    ``expected`` wins over ``unexpected``: ``node.environment`` is governed by
    a rule *within* a cluster, but two different clusters are meant to have
    different environments, so a cross-cluster difference is not a defect.
    """
    lowered = key.lower()
    if any(fnmatch(lowered, pattern) for pattern in EXPECTED_TO_DIFFER):
        return "expected"
    if covered_by_rules:
        return "unexpected"
    return "unknown"
