"""ASAR archive operations for patching Claude Desktop app bundles."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import struct
from pathlib import Path
from typing import Any

# Constants used by this module (mirrored from main script)
APP_ASAR_REL = Path("Contents/Resources/app.asar")
ASAR_PATCH_TARGET = ".vite/build/index.js"
ASAR_INTEGRITY_BLOCK_SIZE = 4 * 1024 * 1024


def require_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Missing required file: {path}")


def align4(value: int) -> int:
    return value + ((4 - (value % 4)) % 4)


def read_asar_header(data: bytes, path: Path) -> tuple[int, str, dict[str, Any]]:
    if len(data) < 16:
        raise SystemExit(f"Unsupported app.asar header in {path}")

    size_pickle_payload = struct.unpack_from("<I", data, 0)[0]
    header_size = struct.unpack_from("<I", data, 4)[0]
    if size_pickle_payload != 4 or header_size <= 0 or len(data) < 8 + header_size:
        raise SystemExit(f"Unsupported app.asar size pickle in {path}")

    header_pickle = data[8 : 8 + header_size]
    header_payload_size = struct.unpack_from("<I", header_pickle, 0)[0]
    header_string_size = struct.unpack_from("<i", header_pickle, 4)[0]
    expected_payload_size = align4(4 + header_string_size)
    if header_payload_size != expected_payload_size or header_size != 4 + header_payload_size:
        raise SystemExit(f"Unsupported app.asar header pickle in {path}")

    header_start = 8
    header_end = header_start + header_string_size
    header_string = header_pickle[header_start:header_end].decode("utf-8")
    header = json.loads(header_string)
    if not isinstance(header, dict):
        raise SystemExit(f"Unsupported app.asar header JSON in {path}")
    return header_size, header_string, header


def encode_asar_header(header_string: str, expected_header_size: int | None = None) -> bytes:
    header_bytes = header_string.encode("utf-8")
    header_payload_size = align4(4 + len(header_bytes))
    header_pickle = (
        struct.pack("<I", header_payload_size)
        + struct.pack("<i", len(header_bytes))
        + header_bytes
        + b"\0" * (header_payload_size - 4 - len(header_bytes))
    )
    if expected_header_size is not None and len(header_pickle) != expected_header_size:
        raise SystemExit("Internal patch error: app.asar header length changed.")
    return struct.pack("<I", 4) + struct.pack("<I", len(header_pickle)) + header_pickle


def get_asar_file_entry(header: dict[str, Any], file_path: str) -> dict[str, Any]:
    node: dict[str, Any] = header
    for part in file_path.split("/"):
        files = node.get("files")
        if not isinstance(files, dict) or part not in files:
            raise SystemExit(f"Could not find {file_path} in app.asar header.")
        child = files[part]
        if not isinstance(child, dict):
            raise SystemExit(f"Unsupported app.asar header entry for {file_path}.")
        node = child
    for key in ["size", "offset", "integrity"]:
        if key not in node:
            raise SystemExit(f"Missing {key} for {file_path} in app.asar header.")
    return node


def calculate_file_integrity(data: bytes) -> dict[str, Any]:
    blocks = [
        hashlib.sha256(data[offset : offset + ASAR_INTEGRITY_BLOCK_SIZE]).hexdigest()
        for offset in range(0, len(data), ASAR_INTEGRITY_BLOCK_SIZE)
    ]
    if not blocks:
        blocks.append(hashlib.sha256(data).hexdigest())
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(data).hexdigest(),
        "blockSize": ASAR_INTEGRITY_BLOCK_SIZE,
        "blocks": blocks,
    }


def update_electron_asar_integrity(app: Path, header_string: str) -> None:
    info_plist = app / "Contents/Info.plist"
    require_file(info_plist)
    with info_plist.open("rb") as f:
        info = plistlib.load(f)

    integrity = info.get("ElectronAsarIntegrity")
    if not isinstance(integrity, dict):
        raise SystemExit("Info.plist is missing ElectronAsarIntegrity.")
    app_asar = integrity.get("Resources/app.asar")
    if not isinstance(app_asar, dict) or app_asar.get("algorithm") != "SHA256":
        raise SystemExit("Info.plist has unsupported ElectronAsarIntegrity format.")

    app_asar["hash"] = hashlib.sha256(header_string.encode("utf-8")).hexdigest()
    tmp = info_plist.with_suffix(info_plist.suffix + ".tmp")
    with tmp.open("wb") as f:
        plistlib.dump(info, f, fmt=plistlib.FMT_XML)
    os.replace(tmp, info_plist)


def read_asar_text(app: Path, file_path: str) -> str:
    path = app / APP_ASAR_REL
    require_file(path)
    data = path.read_bytes()
    header_size, _header_string, header = read_asar_header(data, path)
    entry = get_asar_file_entry(header, file_path)
    content_offset = 8 + header_size + int(entry["offset"])
    content_size = int(entry["size"])
    return data[content_offset : content_offset + content_size].decode("utf-8", errors="ignore")


def walk_asar_file_entries(header: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        files = node.get("files")
        if not isinstance(files, dict):
            return
        for child in files.values():
            if not isinstance(child, dict):
                continue
            if "files" in child:
                walk(child)
            elif "offset" in child and "size" in child:
                entries.append(child)

    walk(header)
    return entries


def patch_asar_file_with_replacements(app: Path, file_path: str, replacements: dict[str, str]) -> int:
    path = app / APP_ASAR_REL
    require_file(path)
    data = path.read_bytes()
    header_size, _header_string, header = read_asar_header(data, path)
    entry = get_asar_file_entry(header, file_path)
    content_offset = 8 + header_size + int(entry["offset"])
    content_size = int(entry["size"])
    content_end = content_offset + content_size
    if content_offset < 0 or content_end > len(data):
        raise SystemExit(f"Unsupported app.asar file bounds for {file_path}.")

    content = data[content_offset:content_end].decode("utf-8")
    patched = content
    count = 0
    for source, target in replacements.items():
        occurrences = patched.count(source)
        if occurrences:
            patched = patched.replace(source, target)
            count += occurrences

    if patched == content:
        return 0

    patched_bytes = patched.encode("utf-8")
    delta = len(patched_bytes) - content_size
    original_offset = int(entry["offset"])
    for item in walk_asar_file_entries(header):
        item_offset = int(item["offset"])
        if item is entry:
            item["size"] = len(patched_bytes)
            item["integrity"] = calculate_file_integrity(patched_bytes)
        elif item_offset > original_offset:
            item["offset"] = str(item_offset + delta)

    updated_header_string = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
    updated_header = encode_asar_header(updated_header_string)
    body = data[8 + header_size :]
    body = body[:original_offset] + patched_bytes + body[original_offset + content_size :]
    path.write_bytes(updated_header + body)
    update_electron_asar_integrity(app, updated_header_string)
    return count


__all__ = [
    "align4",
    "calculate_file_integrity",
    "encode_asar_header",
    "get_asar_file_entry",
    "patch_asar_file_with_replacements",
    "read_asar_header",
    "read_asar_text",
    "update_electron_asar_integrity",
    "walk_asar_file_entries",
]
