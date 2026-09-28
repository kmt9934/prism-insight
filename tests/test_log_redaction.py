import io
import logging

from prism_core.log_redaction import (
    TelegramTokenRedactingFilter,
    install_log_redaction,
)

TOKEN = "123456789:AAH-abc_DEF0123456789xyzXYZ"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def _httpx_style_record():
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", URL, "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )


def test_formatted_httpx_record_is_redacted():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s - %(message)s"))
    handler.addFilter(TelegramTokenRedactingFilter())

    handler.handle(_httpx_style_record())

    output = stream.getvalue()
    assert TOKEN not in output
    assert "api.telegram.org/bot<REDACTED>/sendMessage" in output


def test_install_is_idempotent_and_quiets_http_loggers():
    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    try:
        install_log_redaction()
        install_log_redaction()
        for target in (root, handler):
            count = sum(isinstance(f, TelegramTokenRedactingFilter) for f in target.filters)
            assert count == 1
        for name in ("httpx", "httpcore", "telegram", "telegram.ext"):
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        root.removeHandler(handler)
        for f in [f for f in root.filters if isinstance(f, TelegramTokenRedactingFilter)]:
            root.removeFilter(f)
