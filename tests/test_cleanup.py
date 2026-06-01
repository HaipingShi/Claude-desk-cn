import tempfile
from pathlib import Path

from patches.cleanup import cleanup_stale_display_names
from patches.constants import OPUS_DISPLAY_NAME, STALE_OPUS_VARIANTS


def test_cleanup_replaces_stale_names():
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp)
        js_file = assets_dir / "index-test.js"
        stale_name = STALE_OPUS_VARIANTS[0]  # "Opus 4.71M"
        js_file.write_text(
            f'zhOpus={{...{{model:"opus",name:"{stale_name}",label_override:"{stale_name}"}},'
            f'zhReal={{...{{model:"kimi-for-coding",name:"Kimi-k2.6"}},'
            f'const e={{...{{label:"{OPUS_DISPLAY_NAME}"}}'
        )
        patched_files, patched_strings = cleanup_stale_display_names(assets_dir)
        assert patched_files == 1
        assert patched_strings >= 2
        result = js_file.read_text()
        assert stale_name not in result
        assert OPUS_DISPLAY_NAME in result


def test_cleanup_no_false_positives_on_unrelated_files():
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp)
        js_file = assets_dir / "clean.js"
        js_file.write_text('console.log("hello world")')
        patched_files, patched_strings = cleanup_stale_display_names(assets_dir)
        assert patched_files == 0
        assert patched_strings == 0


def test_cleanup_skips_non_js_files():
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp)
        json_file = assets_dir / "config.json"
        json_file.write_text('{"model":"Opus 4.71M"}')
        patched_files, patched_strings = cleanup_stale_display_names(assets_dir)
        assert patched_files == 0
        assert patched_strings == 0


def test_cleanup_replaces_epitaxy_ternary_patterns():
    """清理 patch_epitaxy_model_menu 旧版 target 中的三元表达式残留。"""
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp)
        js_file = assets_dir / "code-menu.js"
        stale_name = STALE_OPUS_VARIANTS[0]
        js_file.write_text(
            f'const G=V?null:("opus"===K||"opus[1m]"===K?"{stale_name}":'
            f'("kimi-for-coding"===K||/kimi/i.test(String(K))&&/k2\\.6/i.test(String(K))?'
            f'"Kimi-k2.6":zs(K))),'
            f'Q=e.useMemo(()=>("opus"===K||"opus[1m]"===K)?"{stale_name}":'
            f'("kimi-for-coding"===K||/kimi/i.test(String(K))&&/k2\\.6/i.test(String(K)))?'
            f'"Kimi-k2.6":V?Om(V):G,[V,G,K]),'
            f'const e={{{{label:"{stale_name}",checked:"opus"===K||"opus[1m]"===K,onSelect:()=>pe.current("opus")}}}}'
        )
        patched_files, patched_strings = cleanup_stale_display_names(assets_dir)
        assert patched_files == 1
        assert patched_strings >= 3
        result = js_file.read_text()
        assert stale_name not in result
        assert OPUS_DISPLAY_NAME in result
        assert '"Kimi-k2.6"' in result  # Kimi 显示名不应被误替换
