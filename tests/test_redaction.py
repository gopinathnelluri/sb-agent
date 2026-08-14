"""Redaction verification.

The threat is a regression in the backup pipeline turning this service into a
credential reader with a friendly API. These tests cover both halves of the
defence: keys that name a secret, and secrets hidden inside values.
"""

from __future__ import annotations

import pytest

from core.config.redaction import (
    find_unredacted,
    is_placeholder,
    is_secret_key,
    redact_text,
)


class TestSecretKeyNames:
    @pytest.mark.parametrize(
        "key",
        [
            "internal-communication.shared-secret",
            "http-server.authentication.krb5.keytab",
            "hive.s3.aws-secret-key",
            "connection-password",
            "ldap.bind.password",
            "-Djavax.net.ssl.trustStorePassword",
            "service.credential",
            "auth.token",
        ],
    )
    def test_recognised_as_secret(self, key: str) -> None:
        assert is_secret_key(key)

    @pytest.mark.parametrize(
        "key", ["query.max-memory", "node.environment", "hive.metastore.uri"]
    )
    def test_ordinary_keys_are_not(self, key: str) -> None:
        assert not is_secret_key(key)


class TestPlaceholders:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "***",
            "REDACTED",
            "<redacted>",
            "${VAULT_PASSWORD}",
            "{{ vault_pw }}",
            "CHANGEME",
        ],
    )
    def test_recognised_as_redacted(self, value: str) -> None:
        assert is_placeholder(value)

    @pytest.mark.parametrize("value", ["hunter2", "AKIAIOSFODNN7EXAMPLE", "s3cr3t!"])
    def test_real_values_are_not(self, value: str) -> None:
        assert not is_placeholder(value)


class TestFindUnredacted:
    def test_a_properly_redacted_file_passes(self) -> None:
        assert (
            find_unredacted({"connection-password": "***", "query.max-memory": "200GB"})
            == []
        )

    def test_a_live_secret_is_caught(self) -> None:
        hits = find_unredacted({"connection-password": "hunter2"})
        assert [hit.key for hit in hits] == ["connection-password"]

    def test_password_inside_a_jdbc_url_is_caught(self) -> None:
        """The case a key-name check alone would miss."""
        hits = find_unredacted(
            {"connection-url": "jdbc:postgresql://db/x?user=trino&password=hunter2"}
        )
        assert [hit.key for hit in hits] == ["connection-url"]

    def test_credentials_inside_a_uri_are_caught(self) -> None:
        hits = find_unredacted({"hive.metastore.uri": "thrift://user:pass@hms:9083"})
        assert [hit.key for hit in hits] == ["hive.metastore.uri"]

    def test_pem_private_key_is_caught(self) -> None:
        hits = find_unredacted({"notes": "-----BEGIN RSA PRIVATE KEY-----\nabc"})
        assert [hit.key for hit in hits] == ["notes"]

    def test_a_redacted_url_password_passes(self) -> None:
        assert (
            find_unredacted({"connection-url": "jdbc:postgresql://db/x?user=trino"})
            == []
        )

    def test_findings_never_carry_the_value(self) -> None:
        hits = find_unredacted({"connection-password": "hunter2realsecret"})
        assert all("hunter2realsecret" not in hit.reason for hit in hits)


class TestRedactText:
    def test_masks_secret_values_in_the_returned_text(self) -> None:
        values = {"connection-password": "hunter2", "query.max-memory": "200GB"}
        text = "connection-password=hunter2\nquery.max-memory=200GB\n"
        redacted, masked = redact_text(text, values)
        assert "hunter2" not in redacted
        assert "200GB" in redacted
        assert masked == ["connection-password"]

    def test_leaves_an_already_redacted_file_alone(self) -> None:
        text = "connection-password=***\n"
        redacted, masked = redact_text(text, {"connection-password": "***"})
        assert redacted == text
        assert masked == []
