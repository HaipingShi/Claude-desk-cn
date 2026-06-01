"""Stale patch residue cleanup — removes old display names from previously-patched bundles.

Problem: when Claude Desktop updates, old patch old_strings may no longer exist in the
new bundle. The new patch therefore cannot "overwrite" the old patch's target, leaving
stale display names (e.g. "Opus 4.71M") behind. This module provides a brute-force
post-patch cleanup that scans every *.js file and replaces known stale patterns.
"""

from pathlib import Path

from patches.constants import OPUS_DISPLAY_NAME, STALE_OPUS_VARIANTS


def _build_cleanup_patterns() -> dict[str, str]:
    """Build old -> new replacement patterns from STALE_OPUS_VARIANTS."""
    current = OPUS_DISPLAY_NAME
    patterns: dict[str, str] = {}

    for stale in STALE_OPUS_VARIANTS:
        patterns[f'name:"{stale}"'] = f'name:"{current}"'
        patterns[f'label_override:"{stale}"'] = f'label_override:"{current}"'
        patterns[f'return"{stale}"'] = f'return"{current}"'
        patterns[f'"{stale}",inactive'] = f'"{current}",inactive'
        patterns[f'?"{stale}"'] = f'?"{current}"'
        patterns[f')?"{stale}"'] = f')?"{current}"'
        patterns[f'{{label:"{stale}",checked:'] = f'{{label:"{current}",checked:'
        # patch_epitaxy_model_menu 旧版 target 的三元表达式残留
        patterns[f'("opus"===K||"opus[1m]"===K?"{stale}"'] = f'("opus"===K||"opus[1m]"===K?"{current}"'
        patterns[f'("opus"===K||"opus[1m]"===K)?"{stale}"'] = f'("opus"===K||"opus[1m]"===K)?"{current}"'
        patterns[f'{{label:"{stale}",checked:"opus"===K||"opus[1m]"===K'] = f'{{label:"{current}",checked:"opus"===K||"opus[1m]"===K'

    return patterns


_CLEANUP_PATTERNS = _build_cleanup_patterns()


def cleanup_stale_display_names(assets_dir: Path) -> tuple[int, int]:
    """Scan all *.js files under ``assets_dir`` and replace stale display names.

    Returns (patched_files, patched_strings).
    """
    patched_files = 0
    patched_strings = 0

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched = text
        count = 0
        for source, target in _CLEANUP_PATTERNS.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    return patched_files, patched_strings
