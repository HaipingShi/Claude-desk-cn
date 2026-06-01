import tempfile
from pathlib import Path

from patches.verification import scan_for_stale_display_names
from patches.constants import OPUS_DISPLAY_NAME, STALE_OPUS_VARIANTS


def test_scan_finds_stale_residue():
    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp)
        js_dir = app_dir / "Contents/Resources/ion-dist/assets/v1"
        js_dir.mkdir(parents=True)
        (js_dir / "index.js").write_text(
            f'zhOpus={{...{{name:"{STALE_OPUS_VARIANTS[0]}"}}'
        )
        findings = scan_for_stale_display_names(app_dir)
        assert len(findings) == 1
        assert findings[0]["file"].endswith("index.js")
        assert STALE_OPUS_VARIANTS[0] in findings[0]["matches"]


def test_scan_returns_empty_when_clean():
    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp)
        js_dir = app_dir / "Contents/Resources/ion-dist/assets/v1"
        js_dir.mkdir(parents=True)
        (js_dir / "index.js").write_text(
            f'zhOpus={{...{{name:"{OPUS_DISPLAY_NAME}"}}'
        )
        findings = scan_for_stale_display_names(app_dir)
        assert findings == []


def test_scan_skips_non_js_files():
    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp)
        css_dir = app_dir / "Contents/Resources/ion-dist/assets/v1"
        css_dir.mkdir(parents=True)
        (css_dir / "style.css").write_text(
            f'.model::before{{content:"{STALE_OPUS_VARIANTS[0]}"}}'
        )
        findings = scan_for_stale_display_names(app_dir)
        assert findings == []
