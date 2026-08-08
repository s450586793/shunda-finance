import pytest

from apps.core.masking import mask_account


def test_mask_account_keeps_only_last_four_digits():
    assert mask_account("121902307610001") == "***********0001"


def test_mask_account_does_not_hide_short_identifier_characters():
    assert mask_account("123") == "123"


def test_mask_account_preserves_existing_mask_and_last_four_characters():
    assert mask_account("****0001") == "****0001"


def test_mask_account_rejects_non_string_values():
    with pytest.raises(TypeError, match="字符串"):
        mask_account(None)
