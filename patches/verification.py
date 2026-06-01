"""Post-installation verification — full-disk scan for stale patch residue.

Unlike check_frontend_invariants() which checks discrete markers in specific
bundles, this module brute-forces every *.js file under the app bundle and
reports any stale display names still present.
"""

from pathlib import Path

from patches.constants import STALE_OPUS_VARIANTS


def scan_for_stale_display_names(app: Path) -> list[dict[str, str]]:
    """Scan all *.js files under ``app`` for stale display names.

    Returns a list of findings, each dict with keys:
    - file: relative path to the js file
    - matches: list of stale display names found in that file
    """
    findings: list[dict[str, str]] = []

    for path in app.rglob("*.js"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        matched = [stale for stale in STALE_OPUS_VARIANTS if stale in text]
        if matched:
            findings.append({
                "file": str(path.relative_to(app)),
                "matches": matched,
            })

    return findings
