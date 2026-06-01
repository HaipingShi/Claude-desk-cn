from patches.constants import (
    OPUS_DISPLAY_NAME,
    STALE_OPUS_VARIANTS,
    SAFE_OPUS_MODEL_ID,
    LEGACY_1M_OPUS_MODEL_ID,
)


def test_opus_display_name_is_string():
    assert isinstance(OPUS_DISPLAY_NAME, str)
    assert len(OPUS_DISPLAY_NAME) > 0


def test_stale_variants_are_non_empty_strings():
    assert isinstance(STALE_OPUS_VARIANTS, tuple)
    assert len(STALE_OPUS_VARIANTS) > 0
    for v in STALE_OPUS_VARIANTS:
        assert isinstance(v, str)
        assert len(v) > 0


def test_stale_variants_do_not_include_current():
    """旧版本列表不能包含当前版本号，否则清理逻辑会误删当前显示名。"""
    assert OPUS_DISPLAY_NAME not in STALE_OPUS_VARIANTS


def test_model_ids_are_strings():
    assert isinstance(SAFE_OPUS_MODEL_ID, str)
    assert isinstance(LEGACY_1M_OPUS_MODEL_ID, str)
    assert SAFE_OPUS_MODEL_ID == "opus"
    assert LEGACY_1M_OPUS_MODEL_ID == "opus[1m]"
