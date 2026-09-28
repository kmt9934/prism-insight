import logging

import pytest

from prism_core.env_config import (
    clean_env_value,
    env_bool,
    env_choice,
    env_float,
    env_int,
)


@pytest.mark.parametrize("raw, expected", [
    (None, None),
    ("", None),
    ("   ", None),
    (" true ", "true"),
    ('"true"', "true"),
    ("'off'", "off"),
    ("true # enabled 2026-09-03", "true"),
    ('"2"  # confirm checks', "2"),
    ("# only a comment", None),
    ("abc#def", "abc#def"),
])
def test_clean_env_value(raw, expected):
    assert clean_env_value(raw) == expected


@pytest.mark.parametrize("raw, default, expected", [
    ("TRUE", False, True),
    (" yes ", False, True),
    ("off", True, False),
    ('"0"', True, False),
    ("true # note", False, True),
])
def test_env_bool_valid(monkeypatch, raw, default, expected):
    monkeypatch.setenv("PRISM_TEST_FLAG", raw)
    assert env_bool("PRISM_TEST_FLAG", default) is expected


@pytest.mark.parametrize("default", [True, False])
def test_env_bool_invalid_keeps_default_and_warns(monkeypatch, caplog, default):
    monkeypatch.setenv("PRISM_TEST_FLAG", "ture")
    with caplog.at_level(logging.WARNING):
        assert env_bool("PRISM_TEST_FLAG", default) is default
    assert "PRISM_TEST_FLAG" in caplog.text


def test_env_bool_unset_or_empty_uses_default(monkeypatch):
    monkeypatch.delenv("PRISM_TEST_FLAG", raising=False)
    assert env_bool("PRISM_TEST_FLAG", True) is True
    monkeypatch.setenv("PRISM_TEST_FLAG", "")
    assert env_bool("PRISM_TEST_FLAG", True) is True


def test_env_int_and_float(monkeypatch, caplog):
    monkeypatch.setenv("PRISM_TEST_INT", " '3' # three")
    assert env_int("PRISM_TEST_INT", 2) == 3
    monkeypatch.setenv("PRISM_TEST_INT", "3.0")
    assert env_int("PRISM_TEST_INT", 2) == 3
    monkeypatch.setenv("PRISM_TEST_INT", "two")
    with caplog.at_level(logging.WARNING):
        assert env_int("PRISM_TEST_INT", 2) == 2
    assert "PRISM_TEST_INT" in caplog.text
    monkeypatch.setenv("PRISM_TEST_FLOAT", "0.5 # pct")
    assert env_float("PRISM_TEST_FLOAT", 0.3) == 0.5
    monkeypatch.setenv("PRISM_TEST_FLOAT", "0,5")
    assert env_float("PRISM_TEST_FLOAT", 0.3) == 0.3


def test_env_choice(monkeypatch):
    monkeypatch.setenv("PRISM_TEST_MODE", ' "SHADOW" ')
    assert env_choice("PRISM_TEST_MODE", "active", ("active", "shadow", "off")) == "shadow"
    monkeypatch.setenv("PRISM_TEST_MODE", "actve")
    assert env_choice("PRISM_TEST_MODE", "active", ("active", "shadow", "off")) == "active"


def test_regime_min_score_floor_reader_is_tolerant(monkeypatch):
    from prism_core.entry_score_policy import regime_min_score_floor_enabled

    monkeypatch.delenv("REGIME_MIN_SCORE_FLOOR", raising=False)
    assert regime_min_score_floor_enabled() is True
    monkeypatch.setenv("REGIME_MIN_SCORE_FLOOR", "false  # rollback")
    assert regime_min_score_floor_enabled() is False
    monkeypatch.setenv("REGIME_MIN_SCORE_FLOOR", "garbage")
    assert regime_min_score_floor_enabled() is True
