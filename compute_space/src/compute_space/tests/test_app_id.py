"""Tests for the opaque app_id generator + validator."""

from __future__ import annotations

from compute_space.core.app_id import APP_ID_LENGTH
from compute_space.core.app_id import is_valid_app_id
from compute_space.core.app_id import new_app_id


def test_new_app_id_shape():
    for _ in range(50):
        app_id = new_app_id()
        assert len(app_id) == APP_ID_LENGTH
        assert is_valid_app_id(app_id)


def test_new_app_id_collision_resistance():
    """~70 bits of entropy — no collisions across a small batch should be the norm."""
    ids = {new_app_id() for _ in range(2000)}
    assert len(ids) == 2000


def test_is_valid_app_id_rejects_obvious_garbage():
    assert is_valid_app_id("") is False
    assert is_valid_app_id("short") is False
    assert is_valid_app_id("a" * (APP_ID_LENGTH + 1)) is False
    # 0, O, I, l are excluded from the bitcoin base58 alphabet.
    assert is_valid_app_id("0" * APP_ID_LENGTH) is False
    assert is_valid_app_id("O" * APP_ID_LENGTH) is False
    assert is_valid_app_id("I" * APP_ID_LENGTH) is False
    assert is_valid_app_id("l" * APP_ID_LENGTH) is False
    # Punctuation, spaces, etc.
    assert is_valid_app_id("a" * (APP_ID_LENGTH - 1) + " ") is False
    assert is_valid_app_id("a" * (APP_ID_LENGTH - 1) + "-") is False
