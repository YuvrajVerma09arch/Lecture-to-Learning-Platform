"""Logging tests.

The logging layer has two jobs: never leak a secret, and never break the
request it is logging about. Both are tested here.
"""

from __future__ import annotations

import io
import json
import logging

from app.logging_setup import (
    JsonFormatter,
    RedactingFilter,
    configure_logging,
)


def _record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestRedaction:
    def test_redacts_sensitive_extra_fields(self):
        record = _record("hello", api_key="super-secret-value")
        RedactingFilter().filter(record)
        assert record.api_key == "***redacted***"

    def test_redacts_a_secret_named_field(self):
        record = _record("hello", user_password="hunter2")
        RedactingFilter().filter(record)
        assert record.user_password == "***redacted***"

    def test_redacts_inline_key_value_pairs(self):
        record = _record("calling with api_key=abcdef123 and x=1")
        RedactingFilter().filter(record)
        assert "abcdef123" not in record.msg
        assert "x=1" in record.msg

    def test_leaves_ordinary_fields_alone(self):
        record = _record("hello", segment_count=5, duration=1.5)
        RedactingFilter().filter(record)
        assert record.segment_count == 5
        assert record.duration == 1.5


class TestExtraCollisions:
    """A colliding ``extra`` key used to raise inside a request handler."""

    def test_a_colliding_key_is_renamed_not_raised(self):
        record = _record("hello", filename="chunk000.mp3")
        # Would raise KeyError without the safe makeRecord.
        assert record.filename == "chunk000.mp3"

    def test_logging_with_a_colliding_key_does_not_raise(self):
        logger = logging.getLogger("test.collision")
        logger.addHandler(logging.NullHandler())
        # This must not raise.
        logger.info("upload validated", extra={"filename": "x.mp3", "size": 1})

    def test_the_colliding_value_is_preserved_under_a_safe_name(self):
        logger = logging.getLogger("test.collision.keep")
        captured: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record)

        logger.handlers.clear()
        logger.addHandler(Capture())
        logger.setLevel(logging.INFO)
        logger.info("upload validated", extra={"filename": "x.mp3"})

        assert captured[0].x_filename == "x.mp3"
        # The record's own message is untouched.
        assert captured[0].msg == "upload validated"


class TestJsonFormatter:
    def test_emits_one_json_object_per_line(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.json")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.info("chunked", extra={"chunk_count": 3, "silence_aligned_cuts": 2})

        line = stream.getvalue().strip()
        payload = json.loads(line)
        assert payload["msg"] == "chunked"
        assert payload["level"] == "INFO"
        assert payload["chunk_count"] == 3
        assert payload["silence_aligned_cuts"] == 2

    def test_unicode_is_not_escaped(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.unicode")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.info("नमस्ते")
        assert "नमस्ते" in stream.getvalue()

    def test_a_non_serialisable_value_does_not_break_the_record(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.nonjson")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.info("odd", extra={"obj": object()})
        payload = json.loads(stream.getvalue().strip())
        assert "obj" in payload


class TestConfiguredLogging:
    def test_no_secret_reaches_the_stream(self, capsys):
        configure_logging("INFO")
        logger = logging.getLogger("app.test")
        logger.info(
            "gnani call",
            extra={"gnani_api_key": "sk-live-abcdef", "chunk_count": 2},
        )
        out = capsys.readouterr().out
        assert "sk-live-abcdef" not in out
        assert "chunk_count" in out
