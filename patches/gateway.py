"""Gateway, auth, and Claude Code runtime management utilities.

Extracted from patch_claude_zh_cn.py.  Contains:
- Authentication / authorization helpers
- Environment variable probing / sync
- Claude Code process management
- Runtime model probing
- Health check / gateway status
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from patches.constants import SAFE_OPUS_MODEL_ID, LEGACY_1M_OPUS_MODEL_ID

# ---------------------------------------------------------------------------
# Shared helpers (mirrored from the main script to keep this module self-contained)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Constants used only within this module
# ---------------------------------------------------------------------------

CLAUDE_CODE_CONTEXT_WINDOW_KEY = "tengu_hawthorn_window"
CONTEXT_WINDOW_KEYS = (
    "context_length",
    "contextWindow",
    "max_input_tokens",
    "max_context_length",
    "input_token_limit",
    "max_tokens",
)
TOKEN_LIMIT_ERROR_RE = re.compile(
    r"exceeded model token limit:\s*(?P<limit>\d+)\s*\(requested:\s*(?P<requested>\d+)\)",
    re.IGNORECASE,
)
AUTH_401_ERROR_RE = re.compile(
    r"(Failed to authenticate|API Error:\s*401|status=401|appears to be invalid)",
    re.IGNORECASE,
)
FORBIDDEN_403_ERROR_RE = re.compile(r"(API Error:\s*403|status=403)", re.IGNORECASE)
POLICY_BLOCK_ERROR_RE = re.compile(
    r"(Usage Policy|unable to respond to this request)",
    re.IGNORECASE,
)
PROJECT_ENV_OVERRIDE_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
}

# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def quit_claude() -> None:
    _run(["osascript", "-e", 'tell application "Claude" to quit'], check=False)


def active_claude_code_processes(user_home: Path) -> list[dict[str, str]]:
    """列出 Claude Desktop 拉起的 Claude Code / disclaimer 子进程。"""
    result = _run(["ps", "ax", "-o", "pid=,command="], check=False)
    processes: list[dict[str, str]] = []
    support_prefix = str(user_home / "Library/Application Support/Claude-3p/claude-code")
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        pid, _, command = line.partition(" ")
        if not pid.isdigit():
            continue
        if support_prefix not in command:
            continue
        if "/Contents/MacOS/claude" not in command and "/Contents/Helpers/disclaimer" not in command:
            continue
        model_match = re.search(r"(?:^|\s)--model\s+(\S+)", command)
        processes.append({"pid": pid, "model": model_match.group(1) if model_match else "", "command": command})
    return processes


def read_process_environment(pid: str | int) -> tuple[dict[str, str], str]:
    """读取 macOS 进程环境；只给诊断层使用，调用方不得记录密钥值。"""
    try:
        pid_int = int(pid)
    except Exception:
        return {}, "pid_invalid"
    if sys.platform != "darwin":
        return {}, "unsupported_platform"

    libc = ctypes.CDLL(None, use_errno=True)
    ctl = (ctypes.c_int * 3)(1, 49, pid_int)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t(0)
    if libc.sysctl(ctl, 3, None, ctypes.byref(size), None, 0) != 0 or size.value <= 0:
        errno = ctypes.get_errno()
        return {}, f"size_unreadable_errno_{errno}"

    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(ctl, 3, buf, ctypes.byref(size), None, 0) != 0:
        errno = ctypes.get_errno()
        return {}, f"data_unreadable_errno_{errno}"

    data = bytes(buf.raw[: size.value])
    if len(data) < 4:
        return {}, "data_too_short"
    try:
        argc = struct.unpack_from("i", data, 0)[0]
    except Exception:
        return {}, "argc_unreadable"
    if argc < 0 or argc > 4096:
        return {}, f"argc_invalid_{argc}"

    parts = [part.decode("utf-8", errors="ignore") for part in data[4:].split(b"\0") if part]
    # KERN_PROCARGS2 的常见布局为 exec path、argv[argc]、env[]。
    env_parts = parts[1 + argc :] if len(parts) > 1 + argc else []
    env: dict[str, str] = {}
    for item in env_parts:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key:
            env[key] = value
    if not env:
        return {}, "env_empty"
    return env, "passed"


def pre_repair_active_code_process_status(
    user_home: Path, processes: list[dict[str, str]] | None = None
) -> tuple[str, str, int]:
    active = processes if processes is not None else active_claude_code_processes(user_home)
    if not active:
        return "passed", "active=0", 0
    details: list[str] = []
    for item in active[:8]:
        command = item.get("command", "")
        is_desktop = str(user_home / "Library/Application Support/Claude-3p/claude-code") in command
        details.append(
            f"pid={item.get('pid')}; model={item.get('model') or 'unknown'}; desktop={str(is_desktop).lower()}"
        )
    return "passed", f"active={len(active)}; " + " | ".join(details), len(active)


def pre_repair_active_code_env_status(
    user_home: Path, processes: list[dict[str, str]] | None = None
) -> tuple[str, str, int]:
    active = processes if processes is not None else active_claude_code_processes(user_home)
    if not active:
        return "passed", "active=0", 0
    gateway = active_gateway_config(user_home)
    if not gateway:
        return "missing", f"active={len(active)}; gateway_config=missing", len(active)
    expected_base = normalize_gateway_base_url(gateway.get("base_url"))
    expected_key = gateway.get("api_key")
    credential_mode = gateway_credential_mode(gateway)
    auth_scheme = normalize_gateway_auth_scheme(gateway)

    failures = 0
    details: list[str] = []
    for item in active[:8]:
        env, env_status = read_process_environment(item["pid"])
        current_base = normalize_gateway_base_url(env.get("ANTHROPIC_BASE_URL"))
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        api_key = env.get("ANTHROPIC_API_KEY")
        base_match = bool(expected_base and current_base == expected_base)
        token_present = isinstance(token, str) and bool(token.strip())
        api_key_present = isinstance(api_key, str) and bool(api_key.strip())
        token_match = isinstance(expected_key, str) and isinstance(token, str) and token.strip() == expected_key.strip()
        api_key_match = (
            isinstance(expected_key, str) and isinstance(api_key, str) and api_key.strip() == expected_key.strip()
        )
        oauth_present = bool(str(env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip())
        if credential_mode in {"sso", "credential_helper"}:
            credential_ok = True
        elif auth_scheme == "x-api-key":
            credential_ok = api_key_match
        else:
            credential_ok = token_match or api_key_match
        ok = env_status == "passed" and base_match and credential_ok
        if not ok:
            failures += 1
        details.append(
            (
                f"pid={item['pid']}; model={item.get('model') or 'unknown'}; env_status={env_status}; "
                f"base_url_match={str(base_match).lower()}; auth_scheme={auth_scheme}; "
                f"credential_mode={credential_mode}; auth_token_present={str(token_present).lower()}; "
                f"auth_token_matches_gateway={str(token_match).lower()}; "
                f"api_key_present={str(api_key_present).lower()}; api_key_matches_gateway={str(api_key_match).lower()}; "
                f"oauth_token_present={str(oauth_present).lower()}; api_key_not_logged=true"
            )
        )
    status = "passed" if failures == 0 else "missing"
    return status, f"active={len(active)}; " + " | ".join(details), len(active)


def terminate_claude_code_children(user_home: Path, dry_run: bool) -> int:
    """退出 Claude 后清掉旧 Claude Code 子进程，避免继续沿用旧 --model opus。"""
    processes = active_claude_code_processes(user_home)
    if not processes:
        return 0
    pids = [item["pid"] for item in processes]
    if dry_run:
        print(f"[dry-run] Would terminate Claude Code child process(es): {', '.join(pids)}")
        return len(pids)
    print(f"Terminating Claude Code child process(es): {', '.join(pids)}")
    _run(["kill", *pids], check=False)
    return len(pids)


# ---------------------------------------------------------------------------
# Runtime model helpers
# ---------------------------------------------------------------------------

def safe_runtime_model_id(model: str | None) -> str:
    """返回可传给 Claude Code CLI 的真实 provider 模型，不能是 Opus 显示别名。"""
    if not isinstance(model, str) or not model.strip():
        return "kimi-for-coding"
    lowered = model.strip().lower()
    if lowered in {SAFE_OPUS_MODEL_ID, LEGACY_1M_OPUS_MODEL_ID}:
        return "kimi-for-coding"
    return model.strip()


def is_opus_display_alias(model_id: str | None) -> bool:
    """只识别本补丁注入的 Opus 显示别名；真实 provider 模型不能从这些值推断。"""
    if not isinstance(model_id, str):
        return False
    normalized = re.sub(r"[\s_-]+", "", model_id.strip().lower())
    return normalized in {
        "opus",
        "opus[1m]",
        "opus4.71m",
        "opus4.7m",
        "opus4.71m默认",
        "opus4.7m默认",
    }


# ---------------------------------------------------------------------------
# Gateway config probing
# ---------------------------------------------------------------------------

def gateway_config_candidates(user_home: Path) -> list[dict[str, Any]]:
    """读取第三方推理配置；返回值禁止写入密钥到日志。"""
    config_library = user_home / "Library/Application Support/Claude-3p/configLibrary"
    support_dir = user_home / "Library/Application Support/Claude-3p"
    files: list[Path] = []
    if config_library.exists():
        applied_id = None
        meta_path = config_library / "_meta.json"
        if meta_path.exists():
            try:
                meta = _load_json(meta_path)
                if isinstance(meta, dict) and isinstance(meta.get("appliedId"), str):
                    applied_id = meta["appliedId"]
            except Exception:
                applied_id = None
        if applied_id:
            applied_path = config_library / f"{applied_id}.json"
            if applied_path.exists():
                files.append(applied_path)
        files.extend(
            path
            for path in sorted(config_library.glob("*.json"))
            if path.name != "_meta.json"
            and ".before-" not in path.name
            and path not in files
        )
    if support_dir.exists():
        files.extend(
            path
            for path in sorted(support_dir.glob("*.json"))
            if path.name != "_meta.json" and ".before-" not in path.name and path not in files
        )

    configs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in files:
        try:
            data = _load_json(path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        base_url = data.get("inferenceGatewayBaseUrl") or data.get("gatewayBaseUrl")
        api_key = data.get("inferenceGatewayApiKey") or data.get("gatewayApiKey")
        auth_scheme = data.get("inferenceGatewayAuthScheme") or data.get("gatewayAuthScheme")
        credential_helper = data.get("inferenceCredentialHelper") or data.get("gatewayCredentialHelper")
        extra_headers = data.get("inferenceGatewayExtraHeaders") or data.get("gatewayExtraHeaders")
        if not isinstance(base_url, str) or not base_url:
            continue
        key = (base_url.rstrip("/"), str(path))
        if key in seen:
            continue
        seen.add(key)
        configs.append(
            {
                "path": path,
                "base_url": base_url,
                "api_key": api_key,
                "auth_scheme": auth_scheme,
                "credential_helper": credential_helper,
                "extra_headers": extra_headers,
            }
        )
    return configs


def configured_model_list(user_home: Path) -> list[str]:
    """读取用户手动配置的 provider 模型列表。第一项通常代表 provider 默认值。

    注意：Claude Desktop 1.8555+ 的 `inferenceModels` 是企业配置里的 Anthropic
    路由名列表，不能当作底层 provider 模型使用。
    """
    candidates = [
        user_home / "Library/Application Support/Claude-3p/config.json",
        user_home / "Library/Application Support/Claude-3p/claude_desktop_config.json",
    ]
    config_dir = user_home / "Library/Application Support/Claude-3p/configLibrary"
    if config_dir.exists():
        candidates.extend(
            path
            for path in sorted(config_dir.glob("*.json"))
            if path.name != "_meta.json" and ".before" not in path.name
        )

    models: list[str] = []
    seen: set[str] = set()
    model_keys = {
        "models",
        "modelList",
        "model_list",
        "gatewayModels",
        "inferenceGatewayModels",
        "customModels",
        "allowedModels",
    }

    def collect(value: Any) -> None:
        if isinstance(value, str):
            for part in re.split(r"[\n,]+", value):
                model = part.strip()
                if model and model not in seen:
                    seen.add(model)
                    models.append(model)
            return
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    collect(item)
                elif isinstance(item, dict):
                    model = item.get("model") or item.get("id") or item.get("name")
                    if isinstance(model, str):
                        collect(model)

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in model_keys:
                    collect(value)
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for path in candidates:
        if not path.exists():
            continue
        try:
            walk(_load_json(path))
        except Exception:
            continue
    return models


def configured_runtime_model_overrides(user_home: Path) -> list[str]:
    """读取 Claude Code 运行时里显式写入的真实 provider 模型。"""
    settings = user_home / ".claude/settings.json"
    if not settings.exists():
        return []
    try:
        data = _load_json(settings)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    candidates = [
        env.get("ANTHROPIC_MODEL"),
        env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
        env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
        env.get("ANTHROPIC_REASONING_MODEL"),
        data.get("model"),
    ]
    models: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if not isinstance(value, str):
            continue
        model = value.strip()
        if model and model not in seen and not is_opus_display_alias(model):
            seen.add(model)
            models.append(model)
    return models


# ---------------------------------------------------------------------------
# Gateway model / context-window probing
# ---------------------------------------------------------------------------

def safe_gateway_endpoint_for_log(base_url: str) -> str:
    """日志里只记录网关地址，不记录密钥、查询参数或请求内容。"""
    try:
        parsed = urllib.parse.urlsplit(base_url)
    except Exception:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.netloc:
        return "<invalid-url>"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def gateway_model_probe(user_home: Path, timeout: float = 5.0) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """从当前第三方网关读取模型能力；失败原因进入诊断日志但不包含密钥。"""
    errors: list[dict[str, str]] = []
    for config in gateway_config_candidates(user_home):
        base_url = str(config["base_url"]).rstrip("/")
        url = base_url + "/v1/models"
        endpoint = safe_gateway_endpoint_for_log(base_url)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        api_key = config.get("api_key")
        if isinstance(api_key, str) and api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            errors.append({"endpoint": endpoint, "status": str(exc.code), "reason": str(exc.reason)})
            continue
        except urllib.error.URLError as exc:
            errors.append({"endpoint": endpoint, "status": "url_error", "reason": str(exc.reason)})
            continue
        except TimeoutError:
            errors.append({"endpoint": endpoint, "status": "timeout", "reason": "timeout"})
            continue
        except json.JSONDecodeError:
            errors.append({"endpoint": endpoint, "status": "invalid_json", "reason": "invalid_json"})
            continue
        except OSError as exc:
            errors.append({"endpoint": endpoint, "status": "os_error", "reason": exc.__class__.__name__})
            continue
        raw_models = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(raw_models, list):
            errors.append({"endpoint": endpoint, "status": "invalid_schema", "reason": "missing_data_array"})
            continue
        models = [item for item in raw_models if isinstance(item, dict)]
        if models:
            return models, errors
        errors.append({"endpoint": endpoint, "status": "empty_models", "reason": "empty_data_array"})
    return [], errors


def fetch_gateway_models(user_home: Path, timeout: float = 5.0) -> list[dict[str, Any]]:
    """从当前第三方网关读取模型能力；兼容旧调用，仅返回模型列表。"""
    models, _errors = gateway_model_probe(user_home, timeout=timeout)
    return models


def model_id_from_gateway_model(model: dict[str, Any]) -> str | None:
    for key in ("id", "model", "name"):
        value = model.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0:
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed > 0 else None
    return None


def context_window_from_model(model: dict[str, Any]) -> tuple[int | None, str | None]:
    for key in CONTEXT_WINDOW_KEYS:
        parsed = parse_positive_int(model.get(key))
        if parsed:
            return parsed, key
    return None, None


def model_matches_id(model: dict[str, Any], model_id: str) -> bool:
    needle = model_id.strip().lower()
    if not needle:
        return False
    for key in ("id", "model", "name", "display_name", "label", "label_override"):
        value = model.get(key)
        if isinstance(value, str) and value.strip().lower() == needle:
            return True
    return False


def context_window_from_gateway_models(
    gateway_models: list[dict[str, Any]],
    *,
    preferred_id: str | None = None,
) -> tuple[int | None, str | None]:
    """从真实 provider 模型里取上下文窗口，跳过 Opus 显示别名。"""
    candidates: list[dict[str, Any]] = []
    if preferred_id:
        candidates.extend(model for model in gateway_models if model_matches_id(model, preferred_id))
    candidates.extend(gateway_models)
    for model in candidates:
        model_id = model_id_from_gateway_model(model)
        if is_opus_display_alias(model_id):
            continue
        context_window, context_key = context_window_from_model(model)
        if context_window:
            prefix = "gateway_/v1/models"
            if preferred_id and model_matches_id(model, preferred_id):
                prefix += ".matched"
            else:
                prefix += ".first_real_model"
            return context_window, f"{prefix}.{context_key}"
    return None, None


def preferred_gateway_model_id(user_home: Path) -> tuple[str | None, dict[str, Any]]:
    """返回真实 provider 默认模型 id；不返回 Opus 伪装名。"""
    runtime_overrides = configured_runtime_model_overrides(user_home)
    configured_all = runtime_overrides + [
        model for model in configured_model_list(user_home) if model not in runtime_overrides
    ]
    ignored_opus_aliases = [model for model in configured_all if is_opus_display_alias(model)]
    configured = [model for model in configured_all if not is_opus_display_alias(model)]
    gateway_models, gateway_errors = gateway_model_probe(user_home)
    if configured:
        metadata: dict[str, Any] = {
            "source": "runtime_model_override" if runtime_overrides else "configured_model_list",
            "model_count": len(configured),
            "configured_model_count": len(configured_all),
            "runtime_override_count": len(runtime_overrides),
            "gateway_model_count": len(gateway_models),
            "ignored_opus_alias_count": len(ignored_opus_aliases),
            "gateway_probe_errors": gateway_errors,
        }
        context_window, context_source = context_window_from_gateway_models(
            gateway_models,
            preferred_id=configured[0],
        )
        if context_window:
            metadata["context_window"] = context_window
            metadata["context_source"] = context_source
        return configured[0], metadata

    for model in gateway_models:
        model_id = model_id_from_gateway_model(model)
        if model_id and not is_opus_display_alias(model_id):
            context_window, context_source = context_window_from_gateway_models(
                gateway_models,
                preferred_id=model_id,
            )
            metadata = {
                "source": "gateway_/v1/models",
                "model_count": len(gateway_models),
                "configured_model_count": len(configured_all),
                "ignored_opus_alias_count": len(ignored_opus_aliases),
                "gateway_probe_errors": gateway_errors,
            }
            if context_window:
                metadata["context_window"] = context_window
                metadata["context_source"] = context_source
            return model_id, metadata
    return None, {
        "source": "unavailable",
        "model_count": 0,
        "configured_model_count": len(configured_all),
        "ignored_opus_alias_count": len(ignored_opus_aliases),
        "gateway_probe_errors": gateway_errors,
    }


def context_window_from_metadata(metadata: dict[str, Any]) -> int | None:
    parsed = parse_positive_int(metadata.get("context_window"))
    if parsed:
        return parsed
    for key in CONTEXT_WINDOW_KEYS:
        parsed = parse_positive_int(metadata.get(key))
        if parsed:
            return parsed
    return None


def gateway_probe_message(metadata: dict[str, Any]) -> tuple[str, str]:
    errors = metadata.get("gateway_probe_errors")
    if not isinstance(errors, list) or not errors:
        return "passed", "models_discovered_or_no_probe_error"
    first = errors[0] if isinstance(errors[0], dict) else {}
    status = str(first.get("status") or "unknown")
    endpoint = str(first.get("endpoint") or "unknown")
    reason = str(first.get("reason") or "")
    return "missing", f"status={status}; endpoint={endpoint}; reason={reason}; api_key_not_logged=true"


# ---------------------------------------------------------------------------
# Auth / authorization helpers
# ---------------------------------------------------------------------------

def normalize_gateway_auth_scheme(config: dict[str, Any] | None) -> str:
    """归一化第三方推理认证方案；默认按 Bearer 处理，直连 Anthropic 默认 x-api-key。"""
    if not config:
        return "bearer"
    raw = config.get("auth_scheme")
    scheme = str(raw or "").strip().lower().replace("_", "-")
    if scheme in {"x-api-key", "x-api", "api-key", "apikey"}:
        return "x-api-key"
    if scheme in {"sso", "oauth", "oauth2"}:
        return "sso"
    if scheme in {"bearer", "authorization", "auth-bearer"}:
        return "bearer"
    base_url = str(config.get("base_url") or "")
    try:
        host = urllib.parse.urlparse(base_url).hostname or ""
    except Exception:
        host = ""
    if host.endswith("anthropic.com"):
        return "x-api-key"
    return "bearer"


def gateway_credential_mode(config: dict[str, Any] | None) -> str:
    if not config:
        return "missing"
    if normalize_gateway_auth_scheme(config) == "sso":
        return "sso"
    helper = config.get("credential_helper")
    if isinstance(helper, str) and helper.strip():
        return "credential_helper"
    api_key = config.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        return "static_key"
    return "missing_static_key"


def gateway_auth_headers(config: dict[str, Any], api_key: str) -> dict[str, str]:
    scheme = normalize_gateway_auth_scheme(config)
    if scheme == "x-api-key":
        return {"x-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def gateway_messages_auth_probe(user_home: Path, model: str | None, timeout: float = 8.0) -> tuple[str, str]:
    """用极小 /v1/messages 请求验证消息接口鉴权；不记录密钥和请求内容。"""
    if not model:
        return "missing", "model=missing"
    config = active_gateway_config(user_home)
    if not config:
        return "missing", "gateway_config=missing"
    base_url = str(config.get("base_url") or "").rstrip("/")
    endpoint = safe_gateway_endpoint_for_log(base_url)
    api_key = config.get("api_key")
    auth_scheme = normalize_gateway_auth_scheme(config)
    credential_mode = gateway_credential_mode(config)
    if not base_url:
        return "missing", "gateway_base_url=missing"
    if credential_mode in {"sso", "credential_helper"}:
        return (
            "missing",
            (
                f"endpoint={endpoint}; auth_scheme={auth_scheme}; credential_mode={credential_mode}; "
                "static_probe_supported=false; api_key_not_logged=true"
            ),
        )
    if not isinstance(api_key, str) or not api_key.strip():
        return (
            "missing",
            f"endpoint={endpoint}; auth_scheme={auth_scheme}; credential_mode={credential_mode}; static_api_key=missing",
        )
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/v1/messages",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            **gateway_auth_headers(config, api_key.strip()),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            response.read(1024)
        return (
            "passed",
            (
                f"status={status}; endpoint={endpoint}; model={model}; auth_scheme={auth_scheme}; "
                f"credential_mode={credential_mode}; api_key_not_logged=true"
            ),
        )
    except urllib.error.HTTPError as exc:
        auth_failed = exc.code in {401, 403}
        return (
            "missing" if auth_failed else "passed",
            (
                f"status={exc.code}; endpoint={endpoint}; model={model}; auth_scheme={auth_scheme}; "
                f"credential_mode={credential_mode}; reason={exc.reason}; api_key_not_logged=true"
            ),
        )
    except urllib.error.URLError as exc:
        return (
            "missing",
            (
                f"status=url_error; endpoint={endpoint}; auth_scheme={auth_scheme}; "
                f"credential_mode={credential_mode}; reason={exc.reason}; api_key_not_logged=true"
            ),
        )
    except TimeoutError:
        return (
            "missing",
            f"status=timeout; endpoint={endpoint}; auth_scheme={auth_scheme}; credential_mode={credential_mode}; api_key_not_logged=true",
        )
    except OSError as exc:
        return (
            "missing",
            (
                f"status=os_error; endpoint={endpoint}; auth_scheme={auth_scheme}; "
                f"credential_mode={credential_mode}; reason={exc.__class__.__name__}; api_key_not_logged=true"
            ),
        )


# ---------------------------------------------------------------------------
# Gateway env sync / status
# ---------------------------------------------------------------------------

def normalize_gateway_base_url(base_url: Any) -> str | None:
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    stripped = base_url.strip()
    return stripped.rstrip("/") + "/"


def active_gateway_config(user_home: Path) -> dict[str, Any] | None:
    configs = gateway_config_candidates(user_home)
    return configs[0] if configs else None


def claude_code_gateway_env_status(user_home: Path) -> tuple[str, str]:
    """检查 Claude Code CLI 运行时 env 是否和当前第三方推理配置一致，不记录密钥。"""
    gateway = active_gateway_config(user_home)
    if not gateway:
        return "missing", "gateway_config=missing"
    base_url = normalize_gateway_base_url(gateway.get("base_url"))
    api_key = gateway.get("api_key")
    auth_scheme = normalize_gateway_auth_scheme(gateway)
    credential_mode = gateway_credential_mode(gateway)
    settings = user_home / ".claude/settings.json"
    if not settings.exists():
        return "missing", f"settings=missing; gateway={safe_gateway_endpoint_for_log(str(gateway.get('base_url') or ''))}"
    try:
        data = _load_json(settings)
    except Exception:
        return "missing", "settings=unreadable"
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return "missing", "env=missing"
    current_base = normalize_gateway_base_url(env.get("ANTHROPIC_BASE_URL"))
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    api_key_env = env.get("ANTHROPIC_API_KEY")
    base_match = bool(base_url and current_base == base_url)
    token_present = isinstance(token, str) and bool(token.strip())
    api_key_present = isinstance(api_key_env, str) and bool(api_key_env.strip())
    token_match = isinstance(api_key, str) and isinstance(token, str) and token.strip() == api_key.strip()
    api_key_match = (
        isinstance(api_key, str) and isinstance(api_key_env, str) and api_key_env.strip() == api_key.strip()
    )
    helper = gateway.get("credential_helper")
    helper_configured = isinstance(helper, str) and bool(helper.strip())
    if credential_mode in {"sso", "credential_helper"}:
        return (
            "missing",
            (
                f"base_url_match={str(base_match).lower()}; auth_scheme={auth_scheme}; "
                f"credential_mode={credential_mode}; static_sync_supported=false; "
                f"helper_configured={str(helper_configured).lower()}"
            ),
        )
    if base_match and ((token_match and api_key_match) or helper_configured):
        return (
            "passed",
            (
                f"base_url_match=true; auth_scheme={auth_scheme}; credential_mode={credential_mode}; "
                f"auth_token_present={str(token_present).lower()}; "
                f"auth_token_matches_gateway={str(token_match).lower()}; "
                f"api_key_present={str(api_key_present).lower()}; api_key_matches_gateway={str(api_key_match).lower()}; "
                f"helper_configured={str(helper_configured).lower()}"
            ),
        )
    return (
        "missing",
        (
            f"base_url_match={str(base_match).lower()}; auth_scheme={auth_scheme}; credential_mode={credential_mode}; "
            f"auth_token_present={str(token_present).lower()}; "
            f"auth_token_matches_gateway={str(token_match).lower()}; "
            f"api_key_present={str(api_key_present).lower()}; api_key_matches_gateway={str(api_key_match).lower()}; "
            f"helper_configured={str(helper_configured).lower()}"
        ),
    )


def backup_shared_claude_settings(settings: Path) -> str:
    """写共享 settings 前备份原文件；日志只记录路径，不记录内容。"""
    if not settings.exists():
        return "none"
    backup_dir = settings.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"settings.before-zh-CN-{stamp}.json"
    shutil.copy2(settings, backup)
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            os.chown(backup, int(sudo_uid), int(sudo_gid))
        except PermissionError:
            pass
    return str(backup)


def sync_claude_code_gateway_env(
    user_home: Path, settings_backup_path: str | None = None
) -> tuple[bool, str, str, str, str, str]:
    """把第三方推理网关同步到 Claude Code CLI settings.env，解决 Cowork 可用但 Code 401。"""
    gateway = active_gateway_config(user_home)
    if not gateway:
        return False, "missing", "gateway_config=missing", "none", "missing", "settings_not_changed=true"
    base_url = normalize_gateway_base_url(gateway.get("base_url"))
    api_key = gateway.get("api_key")
    credential_mode = gateway_credential_mode(gateway)
    if not base_url:
        return False, "missing", "gateway_base_url=missing", "none", "missing", "settings_not_changed=true"
    if credential_mode in {"sso", "credential_helper"}:
        return (
            False,
            "missing",
            f"credential_mode={credential_mode}; static_sync_supported=false; manual_sync_required=true",
            "none",
            "missing",
            "settings_not_changed=true",
        )
    if not isinstance(api_key, str) or not api_key.strip():
        return False, "missing", "static_api_key=missing", "none", "missing", "settings_not_changed=true"

    settings = user_home / ".claude/settings.json"
    data: dict[str, Any]
    before_top_keys: set[str] = set()
    before_env_keys: set[str] = set()
    if settings.exists():
        try:
            loaded = _load_json(settings)
            data = loaded if isinstance(loaded, dict) else {}
            before_top_keys = set(data.keys())
            current_env = data.get("env")
            if isinstance(current_env, dict):
                before_env_keys = set(current_env.keys())
        except Exception as exc:
            return (
                False,
                "missing",
                f"settings=unreadable; error={exc.__class__.__name__}",
                "none",
                "missing",
                "settings_unreadable=true",
            )
    else:
        data = {}
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
        data["env"] = env

    changed = False
    desired = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": api_key.strip(),
        "ANTHROPIC_API_KEY": api_key.strip(),
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    }
    for key, value in desired.items():
        if env.get(key) != value:
            env[key] = value
            changed = True

    backup_path = "none"
    settings.parent.mkdir(parents=True, exist_ok=True)
    if changed or not settings.exists():
        backup_path = settings_backup_path or backup_shared_claude_settings(settings)
        _save_json(settings, data)
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            os.chown(settings, int(sudo_uid), int(sudo_gid))
    status, message = claude_code_gateway_env_status(user_home)
    after_top_keys = set(data.keys())
    after_env_keys = set(env.keys())
    removed_top_keys = sorted(before_top_keys - after_top_keys)
    removed_env_keys = sorted(before_env_keys - after_env_keys)
    protected_top_keys = sorted(before_top_keys & after_top_keys)
    merge_safe = not removed_top_keys and not removed_env_keys
    merge_message = (
        f"settings_exists_before={str(bool(before_top_keys or before_env_keys)).lower()}; "
        f"removed_top_keys={','.join(removed_top_keys) or 'none'}; "
        f"removed_env_keys={','.join(removed_env_keys) or 'none'}; "
        f"protected_top_keys={','.join(protected_top_keys[:12]) or 'none'}; "
        f"updated_env_keys={','.join(desired.keys())}; api_key_not_logged=true"
    )
    return changed, status, message, backup_path, "passed" if merge_safe else "missing", merge_message


def desktop_code_env_status(app: Path, user_home: Path) -> tuple[str, str]:
    """检查桌面版 Code 启动层和共享 settings env 是否同时就绪。"""
    from patch_claude_zh_cn import claude_code_gateway_env_injection_status
    injection_ok, injection_message, injection_count = claude_code_gateway_env_injection_status(app)
    env_status, env_message = claude_code_gateway_env_status(user_home)
    status = "passed" if injection_ok and env_status == "passed" else "missing"
    return (
        status,
        (
            f"launch_injection={str(injection_ok).lower()}; "
            f"launch_marker_count={injection_count}; {injection_message}; "
            f"shared_env_status={env_status}; {env_message}"
        ),
    )


def terminal_cli_env_status(user_home: Path) -> tuple[str, str]:
    """检查终端 Claude Code CLI 是否存在，并记录它将共享的配置状态。"""
    env_status, env_message = claude_code_gateway_env_status(user_home)
    which = _run(["zsh", "-lc", "command -v claude || true"], check=False).stdout.strip()
    if not which:
        return "missing", f"cli_path=missing; shared_env_status={env_status}; {env_message}"
    version = _run(["zsh", "-lc", "claude --version 2>&1 || true"], check=False).stdout.strip()
    version = re.sub(r"\s+", " ", version)[:120] or "unknown"
    status = "passed" if env_status == "passed" else "missing"
    return (
        status,
        (
            f"cli_path={which}; cli_version={version}; "
            f"shared_settings={user_home / '.claude/settings.json'}; shared_env_status={env_status}; {env_message}"
        ),
    )


def vscode_claude_extension_status(user_home: Path) -> tuple[str, str, int]:
    """检测 VS Code/Cursor Claude 插件，不读取任何凭据。"""
    candidates: list[Path] = []
    for base in [
        user_home / ".vscode/extensions",
        user_home / ".cursor/extensions",
        user_home / "Library/Application Support/Code/User/globalStorage",
        user_home / "Library/Application Support/Cursor/User/globalStorage",
    ]:
        if not base.exists():
            continue
        for path in base.glob("*claude*"):
            candidates.append(path)
        anthropic_dir = base / "anthropic.claude-code"
        if anthropic_dir.exists():
            candidates.append(anthropic_dir)
    unique = sorted({path.resolve() for path in candidates})
    details: list[str] = []
    for path in unique[:6]:
        version = "unknown"
        package_json = path / "package.json"
        if package_json.exists():
            try:
                package = _load_json(package_json)
                if isinstance(package, dict):
                    version = str(package.get("version") or "unknown")
            except Exception:
                version = "unreadable"
        details.append(f"{path.name}:version={version}")
    if unique:
        return (
            "passed",
            f"extensions={';'.join(details)}; shared_settings_possible=true; shared_settings={user_home / '.claude/settings.json'}",
            len(unique),
        )
    return "missing", "extensions=missing; shared_settings_possible=unknown", 0


# ---------------------------------------------------------------------------
# Context window management
# ---------------------------------------------------------------------------

def read_claude_code_context_window(user_home: Path) -> tuple[int | None, str]:
    config_path = user_home / ".claude.json"
    if not config_path.exists():
        return None, "missing"
    try:
        data = _load_json(config_path)
    except Exception:
        return None, "unreadable"

    if isinstance(data, dict):
        root_value = parse_positive_int(data.get(CLAUDE_CODE_CONTEXT_WINDOW_KEY))
        if root_value:
            return root_value, "root_configured"

    def find_value(value: Any, path: str = "$") -> tuple[int | None, str | None]:
        if isinstance(value, dict):
            parsed = parse_positive_int(value.get(CLAUDE_CODE_CONTEXT_WINDOW_KEY))
            if parsed:
                return parsed, path
            for key, child in value.items():
                parsed, source = find_value(child, f"{path}.{key}")
                if parsed:
                    return parsed, source
        elif isinstance(value, list):
            for index, child in enumerate(value):
                parsed, source = find_value(child, f"{path}[{index}]")
                if parsed:
                    return parsed, source
        return None, None

    current, source = find_value(data)
    return current, f"nested:{source}" if current and source else "missing_key"


def sync_claude_code_context_window(user_home: Path, context_window: int | None) -> tuple[int | None, str, bool]:
    """同步 Claude Code 运行时上下文窗口；没有 provider 能力时不硬猜。"""
    if not context_window:
        current, source = read_claude_code_context_window(user_home)
        return current, source, False

    config_path = user_home / ".claude.json"
    if not config_path.exists():
        _save_json(config_path, {CLAUDE_CODE_CONTEXT_WINDOW_KEY: context_window})
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            os.chown(config_path, int(sudo_uid), int(sudo_gid))
        print(f"Created Claude Code runtime JSON with context window: {context_window}")
        return context_window, "root_created", True
    try:
        data = _load_json(config_path)
    except Exception as exc:
        print(f"Warning: cannot update Claude Code runtime JSON: {config_path}: {exc}")
        return None, "unreadable", False

    changed = False

    def update(value: Any) -> None:
        nonlocal changed
        if isinstance(value, dict):
            if CLAUDE_CODE_CONTEXT_WINDOW_KEY in value:
                if value.get(CLAUDE_CODE_CONTEXT_WINDOW_KEY) != context_window:
                    value[CLAUDE_CODE_CONTEXT_WINDOW_KEY] = context_window
                    changed = True
            for child in value.values():
                update(child)
        elif isinstance(value, list):
            for child in value:
                update(child)

    if isinstance(data, dict):
        if data.get(CLAUDE_CODE_CONTEXT_WINDOW_KEY) != context_window:
            data[CLAUDE_CODE_CONTEXT_WINDOW_KEY] = context_window
            changed = True
        update(data)

    if changed:
        _save_json(config_path, data)
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            os.chown(config_path, int(sudo_uid), int(sudo_gid))
        print(f"Set Claude Code context window: {context_window}")
        return context_window, "root_synced", True
    return context_window, "root_already_synced", False


def set_claude_code_dynamic_defaults(user_home: Path) -> tuple[str | None, dict[str, Any]]:
    """同步 Claude Code 默认值：模型跟随真实 provider，强度仍默认最大。"""
    settings = user_home / ".claude/settings.json"
    if settings.exists():
        try:
            loaded = _load_json(settings)
            data = loaded if isinstance(loaded, dict) else {}
        except Exception as exc:
            print(f"Warning: cannot update Claude Code settings JSON: {settings}: {exc}")
            return None, {"source": "settings_unreadable"}
    else:
        data = {}

    preferred_model, metadata = preferred_gateway_model_id(user_home)
    changed = False
    if preferred_model and data.get("model") != preferred_model:
        data["model"] = preferred_model
        changed = True
    if data.get("effortLevel") != "max":
        data["effortLevel"] = "max"
        changed = True
    if not changed and settings.exists():
        print(f"Claude Code defaults already match provider/default effort: {settings}")
        return data.get("model"), metadata

    settings.parent.mkdir(parents=True, exist_ok=True)
    _save_json(settings, data)
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        os.chown(settings, int(sudo_uid), int(sudo_gid))
    print(f"Set Claude Code defaults: model={data.get('model')}, effort=max")
    return data.get("model"), metadata


def migrate_saved_session_dynamic_model(user_home: Path, preferred_model: str | None) -> int:
    """把旧伪装/官方默认会话迁移到真实 provider 默认模型。

    旧 Code 会话可能保存了 default、claude-sonnet-*、opus 等模型。即使 install 已同步
    settings，新打开这些会话时仍会用旧模型启动子进程，导致第三方网关 401。
    """
    if not preferred_model:
        return 0
    roots = [
        user_home / "Library/Application Support/Claude-3p/claude-code-sessions",
        user_home / "Library/Application Support/Claude-3p/local-agent-mode-sessions",
    ]
    changed = 0
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            try:
                data = _load_json(path)
            except Exception:
                continue
            dirty = False

            def update_models(value: Any) -> None:
                nonlocal dirty
                if isinstance(value, dict):
                    for key, child in list(value.items()):
                        if key in {"model", "sessionModel"} and is_stale_anthropic_runtime_model(child):
                            value[key] = preferred_model
                            dirty = True
                        else:
                            update_models(child)
                elif isinstance(value, list):
                    for child in value:
                        update_models(child)

            update_models(data)
            if dirty:
                _save_json(path, data)
                changed += 1
    if changed:
        print(f"Migrated saved Claude Code sessions from stale model to provider model {preferred_model}: {changed}")
    return changed


# ---------------------------------------------------------------------------
# Session / transcript helpers
# ---------------------------------------------------------------------------

def find_claude_code_transcript(user_home: Path, cli_session_id: str) -> Path | None:
    """根据 Claude Code session id 找到磁盘上的 jsonl 历史。"""
    projects = user_home / ".claude/projects"
    if not projects.exists():
        return None
    direct = list(projects.glob(f"*/{cli_session_id}.jsonl"))
    if direct:
        return direct[0]
    try:
        matches = _run(
            ["find", str(projects), "-type", "f", "-name", f"{cli_session_id}.jsonl"],
            check=False,
        ).stdout.splitlines()
    except Exception:
        return None
    return Path(matches[0]) if matches else None


def active_claude_code_sessions(user_home: Path) -> list[dict[str, Any]]:
    roots = [
        user_home / "Library/Application Support/Claude-3p/claude-code-sessions",
        user_home / "Library/Application Support/Claude-3p/local-agent-mode-sessions",
    ]
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            try:
                data = _load_json(path)
            except Exception:
                continue
            cli_id = data.get("cliSessionId")
            if not isinstance(cli_id, str) or not cli_id:
                continue
            if data.get("isArchived") is True:
                continue
            if cli_id in seen:
                continue
            seen.add(cli_id)
            sessions.append({"metadata": path, "data": data, "cliSessionId": cli_id})
    return sessions


# ---------------------------------------------------------------------------
# Error classification / project env overrides
# ---------------------------------------------------------------------------

def classify_recent_code_error(text: str) -> str | None:
    if AUTH_401_ERROR_RE.search(text):
        return "auth_401"
    if FORBIDDEN_403_ERROR_RE.search(text):
        return "forbidden_403"
    if TOKEN_LIMIT_ERROR_RE.search(text):
        return "token_limit"
    if POLICY_BLOCK_ERROR_RE.search(text):
        return "policy_block"
    return None


def recent_code_api_error_status(user_home: Path) -> tuple[str, str, int]:
    active = active_claude_code_sessions(user_home)
    transcript_candidates: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for session in active:
        transcript = find_claude_code_transcript(user_home, session["cliSessionId"])
        if transcript and transcript.exists() and transcript not in seen:
            transcript_candidates.append((session["cliSessionId"], transcript))
            seen.add(transcript)

    projects_root = user_home / ".claude/projects"
    if projects_root.exists():
        try:
            recent_files = sorted(
                projects_root.glob("*/*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            recent_files = []
        for transcript in recent_files[:12]:
            if transcript in seen:
                continue
            transcript_candidates.append((transcript.stem, transcript))
            seen.add(transcript)
            if len(transcript_candidates) >= 16:
                break

    errors: list[str] = []
    for session_id, transcript in transcript_candidates:
        try:
            text = transcript.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        tail = text[-80_000:]
        category = classify_recent_code_error(tail)
        if not category:
            continue
        errors.append(f"{session_id}:{category}:transcript={transcript}")
        if len(errors) >= 8:
            break

    if errors:
        return "missing", "errors=" + " | ".join(errors), len(errors)
    return "passed", f"errors=0; active_sessions={len(active)}; checked={len(transcript_candidates)}", 0


def parse_env_assignments(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in PROJECT_ENV_OVERRIDE_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def collect_project_env_candidates(cwd: Path) -> list[Path]:
    candidates: list[Path] = []
    claude_dir = cwd / ".claude"
    for name in ["settings.json", "settings.local.json"]:
        path = claude_dir / name
        if path.exists():
            candidates.append(path)
    if claude_dir.exists():
        candidates.extend(
            path
            for path in sorted(claude_dir.glob("settings.*.json"))
            if path not in candidates and path.is_file()
        )
    for name in [".env", ".env.local", ".env.development", ".env.production"]:
        path = cwd / name
        if path.exists():
            candidates.append(path)
    candidates.extend(
        path for path in sorted(cwd.glob(".env.*")) if path not in candidates and path.is_file()
    )
    return candidates


def project_env_values_from_file(path: Path) -> dict[str, str]:
    if path.name.endswith(".json"):
        try:
            data = _load_json(path)
        except Exception:
            return {"__unreadable__": "1"}
        env = data.get("env") if isinstance(data, dict) else None
        if not isinstance(env, dict):
            return {}
        return {key: str(value) for key, value in env.items() if key in PROJECT_ENV_OVERRIDE_KEYS}
    return parse_env_assignments(path)


def project_env_override_status(user_home: Path, project_paths: list[Path] | None = None) -> tuple[str, str, int]:
    """检查未归档 Code 会话的项目目录是否用项目级配置覆盖了网关认证环境。"""
    gateway = active_gateway_config(user_home)
    base_url = normalize_gateway_base_url(gateway.get("base_url")) if gateway else None
    api_key = gateway.get("api_key") if gateway else None
    explicit = bool(project_paths)
    sessions = [] if explicit else active_claude_code_sessions(user_home)
    cwd_candidates: list[Path] = []
    if project_paths:
        cwd_candidates = [Path(item).expanduser() for item in project_paths]
    else:
        for session in sessions:
            data = session.get("data")
            if not isinstance(data, dict):
                continue
            cwd_raw = data.get("cwd") or data.get("originCwd")
            if isinstance(cwd_raw, str) and cwd_raw:
                cwd_candidates.append(Path(cwd_raw).expanduser())
    seen_cwds: set[Path] = set()
    checked_files = 0
    issues: list[str] = []
    for cwd in cwd_candidates:
        try:
            cwd = cwd.resolve()
        except Exception:
            pass
        if cwd in seen_cwds:
            continue
        seen_cwds.add(cwd)
        if not cwd.exists() or not cwd.is_dir():
            if explicit:
                issues.append(f"{cwd}:project_not_found")
            continue
        for path in collect_project_env_candidates(cwd):
            checked_files += 1
            values = project_env_values_from_file(path)
            if "__unreadable__" in values:
                issues.append(f"{path}:unreadable")
                continue
            if not values:
                continue
            rel = str(path)
            current_base = normalize_gateway_base_url(values.get("ANTHROPIC_BASE_URL"))
            if current_base and base_url and current_base != base_url:
                issues.append(f"{rel}:ANTHROPIC_BASE_URL_mismatch")
            for token_key in ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"]:
                token = values.get(token_key)
                if isinstance(api_key, str) and isinstance(token, str) and token.strip():
                    if token.strip() != api_key.strip():
                        issues.append(f"{rel}:{token_key}_mismatch")
            custom_headers = values.get("ANTHROPIC_CUSTOM_HEADERS")
            if isinstance(custom_headers, str) and re.search(
                r"authorization|x-api-key|api[-_]?key", custom_headers, re.IGNORECASE
            ):
                issues.append(f"{rel}:ANTHROPIC_CUSTOM_HEADERS_auth_present")
    if issues:
        return (
            "missing",
            f"source={'explicit' if explicit else 'active_sessions'}; sessions={len(sessions)}; "
            f"cwd_checked={len(seen_cwds)}; files_checked={checked_files}; issues="
            + ",".join(issues[:8]),
            len(issues),
        )
    return (
        "passed",
        f"source={'explicit' if explicit else 'active_sessions'}; sessions={len(sessions)}; "
        f"cwd_checked={len(seen_cwds)}; files_checked={checked_files}; issues=0",
        0,
    )
