"""Session sanitization and transcript cleanup utilities."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from patches.gateway import active_claude_code_sessions, find_claude_code_transcript

# ---------------------------------------------------------------------------
# Constants (mirrored from main script to avoid circular imports)
# ---------------------------------------------------------------------------

SESSION_TEXT_KEEP_CHARS = 12_000
SESSION_SNIPPET_KEEP_CHARS = 4_000
SESSION_LONG_FIELD_KEEP_CHARS = 1_000

TOKEN_LIMIT_ERROR_RE = re.compile(
    r"exceeded model token limit:\s*(?P<limit>\d+)\s*\(requested:\s*(?P<requested>\d+)\)",
    re.IGNORECASE,
)

REPORT_DIR = Path(__file__).resolve().parent.parent / "Logs"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Session value shrinking
# ---------------------------------------------------------------------------


def shrink_session_value(value: Any, stats: dict[str, int]) -> Any:
    """瘦身 Claude Code 历史中的大图、thinking 和超长工具结果，保留可读线索。"""
    if isinstance(value, list):
        new_items: list[Any] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "thinking":
                stats["thinking_removed"] = stats.get("thinking_removed", 0) + 1
                continue
            new_items.append(shrink_session_value(item, stats))
        if not new_items and value:
            return [
                {
                    "type": "text",
                    "text": "[历史思考内容已瘦身移除，以避免超过当前真实模型上下文上限。]",
                }
            ]
        return new_items

    if isinstance(value, dict):
        if value.get("isApiErrorMessage") is True:
            stats["api_errors_compacted"] = stats.get("api_errors_compacted", 0) + 1
            compacted = dict(value)
            message = compacted.get("message")
            if isinstance(message, dict):
                message["content"] = [
                    {
                        "type": "text",
                        "text": "[历史 API 错误已瘦身；原错误通常为超过当前真实模型上下文上限。]",
                    }
                ]
                message["usage"] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }
            compacted["error"] = "compacted"
            return compacted
        if value.get("type") == "image" and isinstance(value.get("source"), dict):
            source = value["source"]
            if source.get("type") == "base64" and source.get("data"):
                stats["images_removed"] = stats.get("images_removed", 0) + 1
                return {
                    "type": "text",
                    "text": "[历史截图已瘦身移除，以避免超过当前真实模型上下文上限。]",
                }
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "signature" and isinstance(item, str) and len(item) > 256:
                stats["signatures_removed"] = stats.get("signatures_removed", 0) + 1
                continue
            if (
                key in {"base64", "data"}
                and isinstance(item, str)
                and len(item) > SESSION_LONG_FIELD_KEEP_CHARS
            ):
                stats["binary_fields_removed"] = stats.get("binary_fields_removed", 0) + 1
                result[key] = "[历史二进制内容已瘦身移除，以避免超过当前真实模型上下文上限。]"
                continue
            if (
                key in {"stdout", "stderr"}
                and isinstance(item, str)
                and len(item) > SESSION_LONG_FIELD_KEEP_CHARS
            ):
                stats["streams_truncated"] = stats.get("streams_truncated", 0) + 1
                result[key] = item[:SESSION_LONG_FIELD_KEEP_CHARS] + "\n...[历史命令输出已截断]"
                continue
            if (
                key == "snippet"
                and isinstance(item, str)
                and len(item) > SESSION_SNIPPET_KEEP_CHARS
            ):
                stats["snippets_truncated"] = stats.get("snippets_truncated", 0) + 1
                result[key] = item[:SESSION_SNIPPET_KEEP_CHARS] + "\n...[历史代码片段已截断]"
                continue
            if (
                key == "content"
                and isinstance(item, str)
                and len(item) > SESSION_TEXT_KEEP_CHARS
            ):
                stats["text_truncated"] = stats.get("text_truncated", 0) + 1
                result[key] = item[:SESSION_TEXT_KEEP_CHARS] + "\n...[历史工具输出已截断]"
                continue
            result[key] = shrink_session_value(item, stats)
        return result

    return value


# ---------------------------------------------------------------------------
# Transcript sanitization
# ---------------------------------------------------------------------------


def sanitize_transcript(path: Path, backup_dir: Path) -> tuple[bool, dict[str, int]]:
    before_size = path.stat().st_size
    stats: dict[str, int] = {"before_bytes": before_size}
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    new_lines: list[str] = []
    changed = False
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            new_lines.append(line)
            continue
        shrunk = shrink_session_value(obj, stats)
        new_line = json.dumps(shrunk, ensure_ascii=False, separators=(",", ":"))
        if new_line != line:
            changed = True
        new_lines.append(new_line)

    new_text = "\n".join(new_lines) + ("\n" if new_lines else "")
    after_size = len(new_text.encode("utf-8"))
    stats["after_bytes"] = after_size
    if not changed or after_size >= before_size:
        return False, stats

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / path.name
    shutil.copy2(path, backup_path)
    path.write_text(new_text, encoding="utf-8")
    stats["backup"] = str(backup_path)
    return True, stats


# ---------------------------------------------------------------------------
# Active session sanitization
# ---------------------------------------------------------------------------


def sanitize_active_oversized_sessions(
    user_home: Path,
) -> tuple[int, list[dict[str, Any]]]:
    """只瘦身已经出现 token-limit 错误的当前会话，不按固定模型窗口预判。"""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = REPORT_DIR / "session-backups" / stamp
    changed = 0
    details: list[dict[str, Any]] = []
    for session in active_claude_code_sessions(user_home):
        cli_id = session["cliSessionId"]
        transcript = find_claude_code_transcript(user_home, cli_id)
        if transcript is None or not transcript.exists():
            continue
        size = transcript.stat().st_size
        text = transcript.read_text(encoding="utf-8", errors="ignore")
        token_limit_errors = [
            {
                "limit": int(match.group("limit")),
                "requested": int(match.group("requested")),
            }
            for match in TOKEN_LIMIT_ERROR_RE.finditer(text)
        ]
        detail: dict[str, Any] = {
            "cliSessionId": cli_id,
            "metadata": str(session["metadata"]),
            "transcript": str(transcript),
            "bytes": size,
            "tokenLimitErrors": token_limit_errors[-5:],
        }
        if not token_limit_errors:
            detail["status"] = "skipped_no_token_limit_error"
            details.append(detail)
            continue
        ok, stats = sanitize_transcript(transcript, backup_dir)
        detail.update(stats)
        detail["status"] = "sanitized" if ok else "unchanged"
        if ok:
            changed += 1
        details.append(detail)

    if details:
        report_path = REPORT_DIR / "session-sanitize-latest.json"
        _save_json(
            report_path,
            {
                "trigger": "explicit_token_limit_error",
                "note": "不使用固定上下文窗口；只在历史中已经出现模型 token limit 错误时瘦身。",
                "sessions": details,
            },
        )
    if changed:
        print(f"Sanitized oversized active Claude Code sessions: {changed}")
    return changed, details
