"""Centralized patch constants for version-bump safety.

When upgrading the Opus display version (e.g. 4.8 -> 4.9),
change OPUS_DISPLAY_NAME and append the old name to STALE_OPUS_VARIANTS.
Consumers may use these to build residue-scan patterns.
"""

__all__ = [
    "OPUS_DISPLAY_NAME",
    "STALE_OPUS_VARIANTS",
    "SAFE_OPUS_MODEL_ID",
    "LEGACY_1M_OPUS_MODEL_ID",
]

OPUS_DISPLAY_NAME: str = "Opus 4.8"

# Historical display names that may still linger in previously-patched bundles.
STALE_OPUS_VARIANTS: tuple[str, ...] = (
    "Opus 4.71M",
    "Opus 4.7 1M",
)

# Model IDs used internally (not display names). These rarely change.
SAFE_OPUS_MODEL_ID: str = "opus"
LEGACY_1M_OPUS_MODEL_ID: str = "opus[1m]"
