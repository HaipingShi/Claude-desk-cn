#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-click zh-CN patcher for Claude Desktop on macOS.

What it does:
1. Copies /Applications/Claude.app to a temporary working app.
2. Adds zh-CN to Claude Desktop's language whitelist.
3. Installs Chinese desktop-shell and frontend i18n resources.
4. Sets the current user's Claude config locale to zh-CN.
5. Moves the original app to a timestamped backup and installs the patched app.

Run from this folder:
    sudo /usr/bin/python3 patch_claude_zh_cn.py --user-home "$HOME"
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from patches.cleanup import cleanup_stale_display_names
from patches.verification import scan_for_stale_display_names
from patches.constants import (
    OPUS_DISPLAY_NAME,
    SAFE_OPUS_MODEL_ID,
    LEGACY_1M_OPUS_MODEL_ID,
)
from patches.gateway import (
    active_claude_code_processes,
    active_claude_code_sessions,
    active_gateway_config,
    backup_shared_claude_settings,
    classify_recent_code_error,
    claude_code_gateway_env_status,
    collect_project_env_candidates,
    configured_model_list,
    configured_runtime_model_overrides,
    context_window_from_gateway_models,
    context_window_from_metadata,
    context_window_from_model,
    desktop_code_env_status,
    fetch_gateway_models,
    find_claude_code_transcript,
    gateway_auth_headers,
    gateway_config_candidates,
    gateway_credential_mode,
    gateway_messages_auth_probe,
    gateway_model_probe,
    gateway_probe_message,
    is_opus_display_alias,
    migrate_saved_session_dynamic_model,
    model_id_from_gateway_model,
    model_matches_id,
    normalize_gateway_auth_scheme,
    normalize_gateway_base_url,
    parse_env_assignments,
    parse_positive_int,
    pre_repair_active_code_env_status,
    pre_repair_active_code_process_status,
    preferred_gateway_model_id,
    project_env_override_status,
    project_env_values_from_file,
    quit_claude,
    read_claude_code_context_window,
    read_process_environment,
    recent_code_api_error_status,
    safe_gateway_endpoint_for_log,
    safe_runtime_model_id,
    set_claude_code_dynamic_defaults,
    sync_claude_code_context_window,
    sync_claude_code_gateway_env,
    terminal_cli_env_status,
    terminate_claude_code_children,
    vscode_claude_extension_status,
)

from patches.asar import (
    align4,
    calculate_file_integrity,
    encode_asar_header,
    get_asar_file_entry,
    patch_asar_file_with_replacements,
    read_asar_header,
    read_asar_text,
    require_file,
    update_electron_asar_integrity,
    walk_asar_file_entries,
)

from patches.signing import (
    clear_quarantine,
    is_signable_file,
    load_entitlements,
    read_entitlements,
    require_virtualization_entitlement,
    resign_app,
    sign_path,
)

from patches.utils import (
    sanitize_active_oversized_sessions,
    sanitize_transcript,
    shrink_session_value,
)

from patches.frontend import (
    check_custom3p_setup_i18n,
    check_developer_menu_i18n,
    check_known_frontend_i18n,
    check_localizable_menu_i18n,
    check_recent_hardcoded_frontend_i18n,
    clear_frontend_cache,
    find_frontend_bundles,
    install_desktop_locale,
    install_statsig_locale,
    load_localizable_strings,
    merge_frontend_locale,
    patch_native_menu_role_labels,
    set_locale_config,
    set_user_locale,
    check_frontend_invariants,
    patch_context_usage_percent,
    patch_cowork_model_menu,
    patch_epitaxy_cache_bust,
    patch_epitaxy_model_menu,
    patch_hardcoded_frontend_strings,
    patch_kimi_gateway_health_banner,
    patch_permission_defaults,
    patch_safe_opus_context,
)

APP_DEFAULT = Path("/Applications/Claude.app")
LANG_CODE = "zh-CN"
ROOT = Path(__file__).resolve().parent
RESOURCES = ROOT / "resources"

FRONTEND_TRANSLATION = RESOURCES / "frontend-zh-CN.json"
DESKTOP_TRANSLATION = RESOURCES / "desktop-zh-CN.json"
LOCALIZABLE_STRINGS = RESOURCES / "Localizable.strings"

APP_ASAR_REL = Path("Contents/Resources/app.asar")
FRONTEND_I18N_REL = Path("Contents/Resources/ion-dist/i18n")
FRONTEND_ASSETS_REL = Path("Contents/Resources/ion-dist/assets/v1")
DESKTOP_RESOURCES_REL = Path("Contents/Resources")
ASAR_PATCH_TARGET = ".vite/build/index.js"
ASAR_INTEGRITY_BLOCK_SIZE = 4 * 1024 * 1024
REPORT_DIR = ROOT / "Logs"
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
SESSION_TEXT_KEEP_CHARS = 12_000
SESSION_SNIPPET_KEEP_CHARS = 4_000
SESSION_LONG_FIELD_KEEP_CHARS = 1_000

LANG_LIST_RE = re.compile(
    r'\["en-US","de-DE","fr-FR","ko-KR","ja-JP","es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID"(.*?)\]'
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LOCALIZABLE_ENTRY_RE = re.compile(r'"((?:\\.|[^"\\])*)"\s*=\s*"((?:\\.|[^"\\])*)";')

KNOWN_FRONTEND_I18N_KEYS: dict[str, str] = {
    "0rLmv1esFb": "隐私页：更新检查请求说明",
    "0X/lTj2Hvv": "第三方推理设置：阻止非必要遥测说明",
    "0hPFsTuQ1X": "第三方推理设置：自定义请求头说明",
    "1QKV7FS8FM": "第三方推理设置：允许用户添加 MCP 服务器",
    "1v9Ga0vYPM": "第三方推理设置：内置工具移除说明",
    "16+ehubl/n": "隐私页：崩溃报告说明",
    "/m0q/Dre6A": "第三方推理设置：模型 ID",
    "3WKlYlcDGA": "第三方推理设置：组织插件",
    "3iLLaW8pc5": "第三方推理设置：令牌窗口说明",
    "4RwYWBmY40": "第三方推理设置：非必要服务说明",
    "4tdiEppQ3S": "第三方推理设置：基础遥测分组",
    "4RK/3dvxuB": "删除会话弹窗：多会话删除说明",
    "5oTa1gWQsk": "第三方推理设置：允许的出站主机",
    "6T78KTXhBM": "第三方推理设置：自定义推理请求头",
    "6TgDeF8iRs": "第三方推理设置：添加策略按钮",
    "6cmRKZgiFv": "第三方推理设置：网关 URL 说明",
    "8Jc9WEC0S8": "第三方推理设置：桌面扩展出站标签",
    "8TQoLRh7Ea": "第三方推理设置：秒",
    "83Dth0tmbB": "第三方推理设置：下载 txt",
    "8c6iN3kiDX": "第三方推理设置：交互式登录选项",
    "2/wYS2KHSQ": "Cowork/Code provider 横幅：配置缺失说明",
    "/06iwcQHPz": "Claude Code 设置：大号文字",
    "9WVmruBamk": "Claude Code 设置：代码外观",
    "G/QQvx0Tsd": "第三方推理设置：覆盖模型列表说明",
    "HIjCnaQF93": "第三方推理设置：辅助脚本缓存 TTL",
    "Amxb69AvfR": "第三方推理设置：连接页说明",
    "BPnT3TVya+": "Claude Code 设置：小号文字",
    "Ba3MtjwP5h": "第三方推理设置：遥测与更新",
    "CCUxBOb3va": "第三方推理设置：禁用深度链接",
    "CbPYtuP6+N": "隐私页：身份和账号说明",
    "CwADEGuH8H": "第三方推理设置：沙盒与工作区",
    "D7jTww3yOv": "Claude Code 设置：对话记录文字大小",
    "DHdnIxD7G9": "第三方推理设置：禁用 Claude.ai 登录",
    "DY0aw7svrD": "第三方推理设置：要求扩展签名说明",
    "DnXPcFgmqb": "第三方推理设置：凭据辅助脚本",
    "DC+lIM7C8k": "第三方推理设置：托管 MCP 服务器",
    "DDvPN+i/t2": "插件页：个人插件",
    "GhgU+/h0oI": "实时工件：共享链接导入说明",
    "IxbsWX4wj4": "第三方推理设置：添加请求头按钮",
    "JQs8c3pGcl": "第三方推理设置：网关基础 URL",
    "KZbdbvaU9V": "第三方推理设置：插件与技能",
    "KtZV9pULgo": "第三方推理设置：连接",
    "L+geN0DrtO": "第三方推理设置：网关登录",
    "MYYAX2WEkL": "隐私页：Anthropic 可能收到的内容标题",
    "NA4SBfPMeA": "第三方推理设置：网关 API 密钥",
    "OX1+jdVwLL": "第三方推理设置：阻止自动更新",
    "PNFwYup600": "第三方推理设置：自动更新分组",
    "PO+0DdDIId": "查看菜单：后台任务",
    "OrzZiyVwtN": "Claude Code 设置：界面字体说明",
    "QhJxtbMJfB": "会话批量菜单：归档",
    "RtLYfLZ2bT": "第三方推理设置：辅助脚本 TTL",
    "SKeCK+7hmh": "Claude Code 设置：本地会话标题",
    "StnRZmM3Xn": "第三方推理设置：绝对路径",
    "TGyeqFZWHH": "第三方推理设置：辅助脚本缓存说明",
    "TkA72ubrGt": "第三方推理设置：测试连接",
    "TU4G1seELu": "第三方推理设置：阻止非必要服务说明",
    "U5lBq+CZ7G": "隐私页：匿名使用指标说明",
    "ULnTQCHxiV": "隐私页：Anthropic 看不到的内容标题",
    "UmH4IX1ER9": "会话批量菜单：标为未读",
    "UntW78doSE": "第三方推理设置：连接失败提示",
    "UzLHrala3Q": "第三方推理设置：非必要遥测分组",
    "WCrJjVZDsS": "实时工件：功能说明",
    "W41+8Xj7fP": "第三方推理设置：复制主机名",
    "WsT/E/qNoC": "第三方推理设置：网关认证传输说明",
    "XtXm3euW3d": "第三方推理设置：非必要遥测原因",
    "YRNJssxSp5": "实时工件：新建工件按钮",
    "Yk0+YjpaDc": "第三方推理设置：深度链接禁用说明",
    "ZON8uMn14w": "第三方推理设置：令牌单位",
    "ZRMqH+j2yz": "第三方推理设置：测试模型发现",
    "a87VTwtQw3": "第三方推理设置：1M 上下文变体",
    "aRuqK/KXrl": "第三方推理设置：推理后端说明",
    "aTTY7rU6Bh": "第三方推理设置：每窗口最大令牌",
    "akXG4ChYkN": "Claude Code 设置：默认启用远程控制",
    "bmP92ZCban": "第三方推理设置：要求扩展已签名",
    "c9fsVL9wrY": "Cowork/Code provider 横幅：配置需要修复",
    "dzVdmB+VtN": "第三方推理设置：Code 标签页说明",
    "dYlenA3UP7": "第三方推理设置：OpenTelemetry 分组",
    "eMV1kF66ej": "Claude Code 设置：代码字体说明",
    "eF+Y8JJNCJ": "第三方推理设置：允许 Code 标签页",
    "eZ5nPbhsce": "第三方推理设置：未找到组织插件",
    "EATQrlttOw": "第三方推理设置：组织 UUID",
    "EGOuVJosgc": "第三方推理设置：基础遥测说明",
    "FSyyISlTnS": "第三方推理设置：令牌限制窗口",
    "GYrwE5ehu1": "第三方推理设置：工具出站标签",
    "HA2UIzGsaT": "第三方推理设置：阻止非必要服务",
    "1oztgcYddf": "第三方推理设置：阻止自动更新风险说明",
    "fVfPjDIwfi": "隐私页：对话内容说明",
    "geOrzylJdv": "Claude Code 设置：组织策略禁用远程控制",
    "g8BMTiGHB6": "第三方推理设置：凭据类型",
    "gshbVTjZni": "连接器页：迁移到自定义提示",
    "gkoSAmTJDl": "会话批量菜单：删除",
    "hhKxQ3MtxT": "第三方推理设置：内置工具策略",
    "h3IJeFcbkv": "Claude Code 设置：远程控制自动连接说明",
    "hcpszlrtjU": "删除会话弹窗：单会话删除说明",
    "hv0F38ESRM": "第三方推理设置：模型列表",
    "i8fSuvZDK/": "第三方推理设置：固定模型列表说明",
    "iGPHC9Tm20": "第三方推理设置：禁用的内置工具",
    "jU4z+3Uk7+": "第三方推理设置：模型发现",
    "jA6GVIoYuc": "实时工件：首页说明",
    "jJz20QocAd": "第三方推理设置：自动更新强制窗口",
    "4MengK4xQ/": "第三方推理设置：小时单位",
    "k27Sp3+4kq": "定时任务页：说明",
    "knHnvzpkOf": "第三方推理设置：允许桌面扩展",
    "kgHEpmxl05": "第三方推理设置：静态 API 密钥",
    "lixQFBgPLo": "定时任务页：本地唤醒提示",
    "lJMkinm1YS": "项目页：新建项目",
    "nBQeVvSeP7": "Claude Code 设置：自动创建拉取请求说明",
    "nOBN85iT+Z": "第三方推理设置：组织插件目录说明",
    "oo4Av05fBn": "第三方推理设置：阻止非必要遥测",
    "ozzKmITBMv": "第三方推理设置：令牌软限制说明",
    "kT5Jg7Fz/u": "删除会话弹窗：标题",
    "ll3OMXtx55": "第三方推理设置：网关连接说明",
    "pBgZotXlmX": "第三方推理设置：凭据辅助脚本说明",
    "p0a72nIBLb": "Claude Code 设置：界面字体",
    "pMWMhEu56d": "第三方推理设置：无效响应",
    "qgN98bidUV": "隐私页：文件和工作区内容说明",
    "rdKIIOydC8": "第三方推理设置：阻止基础遥测",
    "recCg9im82": "第三方推理设置：更新分组",
    "siNA1noRXi": "第三方推理设置：网关认证说明",
    "slIZF8X6Sk": "第三方推理设置：添加服务器策略",
    "t0NuWXREIj": "第三方推理设置：出站域名说明",
    "tNxpKP4AlE": "第三方推理设置：阻止更新说明",
    "tgblN/n/hK": "第三方推理设置：工作区文件夹说明",
    "tgkg69DKCl": "隐私页：第三方推理提供商说明",
    "tmwK1KjFte": "第三方推理设置：网关认证方案",
    "uY5OjtEg/e": "插件页：浏览插件",
    "UxTJRaKagI": "项目页：项目",
    "x+MG25XWVf": "第三方推理设置：请求头辅助脚本",
    "x8r3+rMaHq": "第三方推理设置：凭据辅助脚本超时",
    "xXr+rGIv+A": "第三方推理设置：凭据类型说明",
    "xovdJlXIM6": "第三方推理设置：OpenTelemetry 收集器端点",
    "xWiIy0pAlB": "第三方推理设置：自动模型发现说明",
    "xY1EE6Ndl5": "第三方推理设置：出站要求",
    "xyS7d891o+": "隐私页：诊断报告发送说明",
    "y8c8KzJEws": "第三方推理设置：显示名称说明",
    "y3gym+4SMI": "第三方推理设置：允许的工作区文件夹",
    "y/6sGoi9YF": "第三方推理设置：连接器与扩展",
    "ypzSrbPbG/": "Cowork 模型菜单：强度说明",
    "w1A4zeclNH": "Claude Code 设置：对话记录文字大小说明",
    "zNarR/5dRy": "定时任务页：本机运行标签",
    "zUO6Ii5EAT": "第三方推理设置：添加模型",
    "zPhYdevJ+s": "第三方推理设置：工具策略说明",
}

KNOWN_LOCALIZABLE_MENU_STRINGS: dict[str, str] = {
    "Background tasks": "后台任务",
    "Background Tasks": "后台任务",
}

DEV_MENU_LABEL_REPLACEMENTS: dict[str, str] = {
    'label:"Enable Main Process Debugger"': 'label:"启用主进程调试器"',
    'label:"Record Performance Trace"': 'label:"录制性能跟踪"',
    'label:"Write Main Process Heap Snapshot"': 'label:"写入主进程堆快照"',
    'label:"Record Memory Trace (auto-stop)"': 'label:"录制内存跟踪（自动停止）"',
}

CUSTOM3P_SETUP_REPLACEMENTS: dict[str, str] = {
    'defaultMessage:"Connection"': 'defaultMessage:"连接"',
    'defaultMessage:"Choose where Claude Desktop sends inference requests."': 'defaultMessage:"选择 Claude Desktop 发送推理请求的位置。"',
    'defaultMessage:"Workspace restrictions"': 'defaultMessage:"沙盒与工作区"',
    'defaultMessage:"Connectors & extensions"': 'defaultMessage:"连接器与扩展"',
    'defaultMessage:"Telemetry & updates"': 'defaultMessage:"遥测与更新"',
    'defaultMessage:"Usage limits"': 'defaultMessage:"使用限制"',
    'defaultMessage:"Appearance"': 'defaultMessage:"外观"',
    'defaultMessage:"Plugins & skills"': 'defaultMessage:"插件与技能"',
    'defaultMessage:"Egress Requirements"': 'defaultMessage:"出站要求"',
    'defaultMessage:"Gateway base URL"': 'defaultMessage:"网关基础 URL"',
    'defaultMessage:"Gateway API key"': 'defaultMessage:"网关 API 密钥"',
    'defaultMessage:"Gateway auth scheme"': 'defaultMessage:"网关认证方案"',
    'defaultMessage:"Credential kind"': 'defaultMessage:"凭据类型"',
    'defaultMessage:"Static API key"': 'defaultMessage:"静态 API 密钥"',
    'defaultMessage:"Interactive sign-in"': 'defaultMessage:"交互式登录"',
    'defaultMessage:"Selects the credential source. When set, only that source is used (no fallback)."': 'defaultMessage:"选择凭据来源。设置后只使用该来源（不会回退）。"',
    'defaultMessage:"Full URL of the inference gateway endpoint."': 'defaultMessage:"推理网关端点的完整 URL。"',
    'defaultMessage:"How the gateway credential is sent. Choose Bearer or x-api-key for a static key or credential-helper output; choose SSO to have each user sign in via your identity provider."': 'defaultMessage:"网关凭据的发送方式。静态密钥或凭据辅助脚本输出请选择 Bearer 或 x-api-key；如需每个用户通过身份提供商登录，请选择 SSO。"',
    'defaultMessage:"How the gateway credential is sent on the wire (Authorization: Bearer vs x-api-key header)."': 'defaultMessage:"网关凭据在请求中如何发送（Authorization: Bearer 或 x-api-key 请求头）。"',
    'defaultMessage:"Custom inference headers"': 'defaultMessage:"自定义推理请求头"',
    'defaultMessage:"Extra HTTP headers sent on every inference request to the configured provider. For tenant routing, org IDs, Bedrock Guardrails, etc."': 'defaultMessage:"每次推理请求都会发送到已配置提供商的额外 HTTP 请求头。可用于租户路由、组织 ID、Bedrock Guardrails 等。"',
    'defaultMessage:"Helper script"': 'defaultMessage:"凭据辅助脚本"',
    'defaultMessage:"Helper script TTL"': 'defaultMessage:"辅助脚本 TTL"',
    'defaultMessage:"Credential helper timeout"': 'defaultMessage:"凭据辅助脚本超时"',
    'defaultMessage:"Test connection"': 'defaultMessage:"测试连接"',
    'defaultMessage:"Test connection is not available for local (stdio) servers."': 'defaultMessage:"本地（stdio）服务器不支持测试连接。"',
    'defaultMessage:"Add header"': 'defaultMessage:"添加请求头"',
    'defaultMessage:"Models"': 'defaultMessage:"模型"',
    'defaultMessage:"Model discovery"': 'defaultMessage:"模型发现"',
    'defaultMessage:"Test model discovery"': 'defaultMessage:"测试模型发现"',
    'defaultMessage:"Auto-populate the model picker from {url} at launch."': 'defaultMessage:"启动时从 {url} 自动填充模型选择器。"',
    'defaultMessage:"Model list"': 'defaultMessage:"模型列表"',
    'defaultMessage:"Override the auto-discovered model list. First entry is the default."': 'defaultMessage:"覆盖自动发现的模型列表。第一项为默认值。"',
    'defaultMessage:"Models to show in the picker. First entry is the default."': 'defaultMessage:"在选择器中显示的模型。第一项为默认值。"',
    'defaultMessage:"Add model"': 'defaultMessage:"添加模型"',
    'defaultMessage:"Extensions"': 'defaultMessage:"扩展"',
    'defaultMessage:"MCP servers"': 'defaultMessage:"MCP 服务器"',
    'defaultMessage:"Organization banner"': 'defaultMessage:"组织横幅"',
    'defaultMessage:"A persistent banner across the top of the app window after sign-in."': 'defaultMessage:"登录后在应用窗口顶部显示的常驻横幅。"',
    'defaultMessage:"Show banner"': 'defaultMessage:"显示横幅"',
    'defaultMessage:"Banner text"': 'defaultMessage:"横幅文本"',
    'defaultMessage:"Internal use only"': 'defaultMessage:"仅供内部使用"',
    'defaultMessage:"Allow desktop extensions"': 'defaultMessage:"允许桌面扩展"',
    'defaultMessage:"Require signed extensions"': 'defaultMessage:"要求扩展已签名"',
    'defaultMessage:"Reject desktop extensions that are not signed by a trusted publisher."': 'defaultMessage:"拒绝未由受信任发布者签名的桌面扩展。"',
    'description:"Reject desktop extensions that are not signed by a trusted publisher."': 'description:"拒绝未由受信任发布者签名的桌面扩展。"',
    'defaultMessage:"Allow user-added MCP servers"': 'defaultMessage:"允许用户添加 MCP 服务器"',
    'defaultMessage:"Managed MCP servers"': 'defaultMessage:"托管的 MCP 服务器"',
    'defaultMessage:"No organization plugins found"': 'defaultMessage:"未找到组织插件"',
    'defaultMessage:"Organization plugins"': 'defaultMessage:"组织插件"',
    'defaultMessage:"+ Add server policy"': 'defaultMessage:"+ 添加服务器策略"',
    'defaultMessage:"Mount plugin bundles to this folder using your device-management tool and Cowork will load them at launch. The folder is read-only; tool policies you set below are saved in this configuration."': 'defaultMessage:"请使用你的设备管理工具将插件包挂载到此文件夹，Cowork 会在启动时加载它们。此文件夹为只读；你在下方设置的工具策略会保存在此配置中。"',
    'defaultMessage:"Users see only this provider at the login screen. The option to sign in to Claude.ai is hidden."': 'defaultMessage:"用户在登录页只会看到此提供商，登录 Claude.ai 的选项会被隐藏。"',
    'defaultMessage:"Your provider setup needs a fix"': 'defaultMessage:"提供商配置需要修复"',
    'defaultMessage:"Some required fields are missing or malformed. Open Setup to finish configuring it."': 'defaultMessage:"部分必填字段缺失或格式不正确。请打开设置完成配置。"',
    'defaultMessage:"Configuration can\'t be used"': 'defaultMessage:"配置无法使用"',
    'defaultMessage:"An administrator configured Cowork with settings we can\'t use. You won\'t be able to start tasks until your IT team fixes it."': 'defaultMessage:"管理员配置的 Cowork 设置当前无法使用。在 IT 团队修复前，你将无法启动任务。"',
    'defaultMessage:"Configuration sync issue"': 'defaultMessage:"配置同步问题"',
    'defaultMessage:"Couldn\'t fetch your organization\'s configuration. Open Setup to see details and sign in."': 'defaultMessage:"无法获取组织配置。请打开设置查看详情并登录。"',
    'defaultMessage:"Couldn\'t sign in to {provider}"': 'defaultMessage:"无法登录 {provider}"',
    'defaultMessage:"The provider rejected the credentials IT configured. This usually means an expired key or wrong region."': 'defaultMessage:"提供商拒绝了 IT 配置的凭据，通常是密钥过期或区域错误。"',
    'defaultMessage:"The provider rejected your credentials. Re-enter them in Setup."': 'defaultMessage:"提供商拒绝了你的凭据。请在设置中重新输入。"',
    'defaultMessage:"Can\'t reach {host}"': 'defaultMessage:"无法连接到 {host}"',
    'defaultMessage:"The provider didn\'t respond. Check your network or VPN. If the issue persists, your IT team may need to allowlist the host."': 'defaultMessage:"提供商没有响应。请检查网络或 VPN。如果问题持续存在，IT 团队可能需要将该主机加入允许列表。"',
    'defaultMessage:"The provider didn\'t respond. Check your network or VPN, then try again."': 'defaultMessage:"提供商没有响应。请检查网络或 VPN，然后重试。"',
    'defaultMessage:"Configured model not available"': 'defaultMessage:"配置的模型不可用"',
    'defaultMessage:"Your gateway couldn\'t serve {model}. This model may not be configured on your gateway, or access may be restricted."': 'defaultMessage:"你的网关无法提供 {model}。该模型可能未在网关中配置，或访问受限。"',
    'defaultMessage:"{provider} returned an error"': 'defaultMessage:"{provider} 返回错误"',
    'defaultMessage:"Your connection works, but the provider rejected a test request. This is often a model-access or quota issue your admin can resolve."': 'defaultMessage:"连接可用，但提供商拒绝了测试请求。这通常是模型访问或额度问题，管理员可以处理。"',
    'defaultMessage:"Your connection works, but the provider rejected a test request. Often a model-access or quota issue."': 'defaultMessage:"连接可用，但提供商拒绝了测试请求。通常是模型访问或额度问题。"',
    'defaultMessage:"How deeply Claude thinks"': 'defaultMessage:"Claude 的思考深度"',
    'defaultMessage:"Block essential telemetry"': 'defaultMessage:"阻止基础遥测"',
    'defaultMessage:"Block nonessential telemetry"': 'defaultMessage:"阻止非必要遥测"',
    'defaultMessage:"Block nonessential services"': 'defaultMessage:"阻止非必要服务"',
    'defaultMessage:"Personal plugins"': 'defaultMessage:"个人插件"',
    'defaultMessage:"Browse plugins"': 'defaultMessage:"浏览插件"',
    'defaultMessage:"Add plugin"': 'defaultMessage:"添加插件"',
    'defaultMessage:"Live artifacts"': 'defaultMessage:"实时工件"',
    'label:"Live artifacts"': 'label:"实时工件"',
    'defaultMessage:"Create dynamic artifacts that stay up-to-date using live data from <link>your connectors</link>."': 'defaultMessage:"创建使用<link>你的连接器</link>实时数据并保持最新的动态工件。"',
    'defaultMessage:"New artifact"': 'defaultMessage:"新建工件"',
    'defaultMessage:"Live artifacts are interactive pages that stay up-to-date using live data from connectors. <b>Cancel</b> to create a normal file instead."': 'defaultMessage:"实时工件是使用连接器实时数据并保持最新的交互式页面。<b>取消</b>可改为创建普通文件。"',
    'defaultMessage:"Paste a shared artifact link to add a copy to your Live artifacts."': 'defaultMessage:"粘贴共享工件链接以添加一份副本到你的实时工件。"',
    'title:"Scheduled tasks"': 'title:"定时任务"',
    '"Run tasks on a schedule or whenever you need them. Type /schedule in any existing task to set one up."': '"按计划运行任务，也可在需要时手动运行。在任意现有任务中输入 /schedule 即可设置。"',
    '"Create your first scheduled task"': '"创建你的第一个定时任务"',
    '"No scheduled tasks match your search."': '"没有匹配的定时任务。"',
    'placeholder:"Filter scheduled tasks"': 'placeholder:"筛选定时任务"',
    '"aria-label":"Sort by"': '"aria-label":"排序方式"',
    'IVt={nextRun:"Next run",name:"Name"}': 'IVt={nextRun:"下次运行",name:"名称"}',
    'label:"Daily brief"': 'label:"每日简报"',
    'label:"Weekly review"': 'label:"每周回顾"',
    'label:"Email digest"': 'label:"邮件摘要"',
    'label:"Meeting prep"': 'label:"会议准备"',
    'label:"Create with Claude"': 'label:"用 Claude 创建"',
    'label:"Set up manually"': 'label:"手动设置"',
    'title:"Projects"': 'title:"项目"',
    'const Xe="New project"': 'const Xe="新建项目"',
    'defaultMessage:"New project"': 'defaultMessage:"新建项目"',
    'defaultMessage:"Projects"': 'defaultMessage:"项目"',
    '"No projects match your search."': '"没有匹配的项目。"',
    '"Your projects"': '"你的项目"',
    '"Team"': '"团队"',
    '"Shared with you"': '"与你共享"',
    '"You don\'t have any projects yet."': '"你还没有任何项目。"',
    '"No team projects yet."': '"还没有团队项目。"',
    '"No projects have been shared with you."': '"还没有与你共享的项目。"',
    '"Recent"': '"最近"',
    '"Created"': '"创建时间"',
    '"Alphabetical"': '"按字母排序"',
    '"Untitled"': '"未命名"',
    '"Looking to start a project?"': '"想开始一个项目？"',
    '"Point Claude at a folder on your machine and work on it together."': '"将 Claude 指向你电脑上的文件夹，然后一起处理它。"',
    '"Upload materials, set custom instructions, and organize conversations in one space."': '"上传资料、设置自定义说明，并在一个空间中组织对话。"',
    'defaultMessage:"Interface font"': 'defaultMessage:"界面字体"',
    'defaultMessage:"Font for the Claude Code interface — menus, sidebar, and chat."': 'defaultMessage:"Claude Code 界面的字体：菜单、侧边栏和聊天。"',
    'defaultMessage:"Transcript text size"': 'defaultMessage:"对话记录文字大小"',
    'defaultMessage:"Size of the conversation transcript text."': 'defaultMessage:"对话记录文本的大小。"',
    'defaultMessage:"Small"': 'defaultMessage:"小"',
    'defaultMessage:"Large"': 'defaultMessage:"大"',
    'defaultMessage:"Code appearance"': 'defaultMessage:"代码外观"',
    'defaultMessage:"Set a custom monospace font for code and terminal."': 'defaultMessage:"为代码和终端设置自定义等宽字体。"',
    'defaultMessage:"When Claude pushes changes to a branch, it automatically opens a pull request without asking first. Applies to remote sessions only."': 'defaultMessage:"当 Claude 将更改推送到分支时，会自动创建拉取请求，不再另行询问。仅适用于远程会话。"',
    'description:"When Claude pushes changes to a branch, it automatically opens a pull request without asking first. Applies to remote sessions only."': 'description:"当 Claude 将更改推送到分支时，会自动创建拉取请求，不再另行询问。仅适用于远程会话。"',
    'hint:"When Claude pushes changes to a branch, it automatically opens a pull request without asking first. Applies to remote sessions only."': 'hint:"当 Claude 将更改推送到分支时，会自动创建拉取请求，不再另行询问。仅适用于远程会话。"',
    'category:"appearance"': 'category:"外观"',
    'group:"Organization banner"': 'group:"组织横幅"',
    'hint:"A persistent banner across the top of the app window after sign-in."': 'hint:"登录后在应用窗口顶部显示的常驻横幅。"',
    'label:"Show banner"': 'label:"显示横幅"',
    'label:"Banner text"': 'label:"横幅文本"',
    'placeholder:"Internal use only"': 'placeholder:"仅供内部使用"',
    'hint:"Single line, truncated on overflow. Maximum 200 characters."': 'hint:"单行显示，超出部分会截断。最多 200 个字符。"',
    'label:"Background color"': 'label:"背景颜色"',
    'hint:"Six-digit hex (#RRGGBB). Applied exactly as configured; not theme-adapted."': 'hint:"六位十六进制颜色（#RRGGBB）。会按配置原样应用，不随主题自适应。"',
    'label:"Text color"': 'label:"文本颜色"',
    'hint:"Org-pushed MCP servers — remote (HTTP/SSE) or local (stdio command). May embed bearer tokens."': 'hint:"组织推送的 MCP 服务器，可以是远程（HTTP/SSE）或本地（stdio 命令）。可能包含 Bearer 令牌。"',
    '"None configured."': '"未配置。"',
    'hint:"Reject desktop extensions that are not signed by a trusted publisher."': 'hint:"拒绝未由受信任发布者签名的桌面扩展。"',
    'hint:"Show the Code tab (terminal-based coding sessions). Sessions run on the host, not inside the VM."': 'hint:"显示 Code 标签页（基于终端的编码会话）。会话在主机上运行，而不是在虚拟机内运行。"',
    'title:"Disable claude:// deep-link handling"': 'title:"禁用 claude:// 深度链接处理"',
    'hint:"Stop external apps and websites from opening Cowork via claude:// links."': 'hint:"阻止外部应用和网站通过 claude:// 链接打开 Cowork。"',
    'hint:"Full URL of the inference gateway endpoint."': 'hint:"推理网关端点的完整 URL。"',
    'hint:"How the gateway credential is sent. Choose Bearer or x-api-key for a static key or credential-helper output; choose SSO to have each user sign in via your identity provider."': 'hint:"网关凭据的发送方式。静态密钥或凭据辅助脚本输出请选择 Bearer 或 x-api-key；如需每个用户通过身份提供商登录，请选择 SSO。"',
    'hint:"Extra headers sent to the gateway. One value per header name. For tenant routing, org IDs, etc."': 'hint:"发送到网关的额外请求头。每个请求头名称对应一个值。可用于租户路由、组织 ID 等。"',
    'hint:"Users see only this provider at the login screen — the option to sign in to Anthropic is hidden."': 'hint:"用户在登录界面只会看到此提供商，登录 Anthropic 的选项会被隐藏。"',
    'title:"Hide Anthropic sign-in"': 'title:"隐藏 Anthropic 登录"',
}

RECENT_HARDCODED_FRONTEND_I18N_CHECKS: dict[str, str] = {
    'defaultMessage:"Your provider setup needs a fix"': "Cowork/Code 黄色 provider 配置横幅标题",
    'defaultMessage:"Some required fields are missing or malformed. Open Setup to finish configuring it."': "Cowork/Code 黄色 provider 配置横幅说明",
    'defaultMessage:"How deeply Claude thinks"': "Cowork 模型菜单强度说明",
    'defaultMessage:"Personal plugins"': "插件页个人插件标题",
    'defaultMessage:"Browse plugins"': "插件页浏览插件按钮",
    'defaultMessage:"Add plugin"': "插件页添加插件菜单",
    'defaultMessage:"Live artifacts"': "实时工件入口",
    'label:"Live artifacts"': "实时工件侧边栏",
    'defaultMessage:"Create dynamic artifacts that stay up-to-date using live data from <link>your connectors</link>."': "实时工件页面说明",
    'defaultMessage:"New artifact"': "实时工件新建按钮",
    'title:"Scheduled tasks"': "定时任务页标题",
    '"Run tasks on a schedule or whenever you need them. Type /schedule in any existing task to set one up."': "定时任务页说明",
    '"Create your first scheduled task"': "定时任务空状态",
    'label:"Daily brief"': "定时任务模板：每日简报",
    'label:"Weekly review"': "定时任务模板：每周回顾",
    'title:"Projects"': "项目页标题",
    'const Xe="New project"': "项目页新建项目按钮",
    'defaultMessage:"New project"': "项目页新建项目消息",
    'defaultMessage:"Projects"': "项目页标题消息",
    '"No projects match your search."': "项目页无搜索结果",
    'defaultMessage:"Interface font"': "Claude Code 设置：界面字体",
    'defaultMessage:"Transcript text size"': "Claude Code 设置：对话文字大小",
    'defaultMessage:"Code appearance"': "Claude Code 设置：代码外观",
    'defaultMessage:"When Claude pushes changes to a branch, it automatically opens a pull request without asking first. Applies to remote sessions only."': "Claude Code 设置：自动创建 PR 说明",
    'defaultMessage:"Require signed extensions"': "第三方推理设置：要求扩展签名",
    'defaultMessage:"Reject desktop extensions that are not signed by a trusted publisher."': "第三方推理设置：要求扩展签名说明",
    'defaultMessage:"Interactive sign-in"': "第三方推理设置：交互式登录",
    'defaultMessage:"Block nonessential telemetry"': "第三方推理设置：阻止非必要遥测",
    'defaultMessage:"Block nonessential services"': "第三方推理设置：阻止非必要服务",
}


@dataclass
class PatchEvent:
    name: str
    status: str
    message: str = ""
    file: str | None = None
    count: int | None = None
    required: bool = False


@dataclass
class PatchReport:
    app: str
    claude_version: str
    mode: str
    started_at: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    events: list[PatchEvent] = field(default_factory=list)

    def add(
        self,
        name: str,
        status: str,
        message: str = "",
        *,
        file: Path | str | None = None,
        count: int | None = None,
        required: bool = False,
    ) -> None:
        self.events.append(
            PatchEvent(
                name=name,
                status=status,
                message=message,
                file=Path(file).name if isinstance(file, Path) else file,
                count=count,
                required=required,
            )
        )

    def has_required_failures(self) -> bool:
        return any(event.required and event.status in {"missing", "failed"} for event in self.events)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.status] = counts.get(event.status, 0) + 1
        return {
            "app": self.app,
            "claude_version": self.claude_version,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
            "summary": counts,
            "required_failures": [
                event.__dict__
                for event in self.events
                if event.required and event.status in {"missing", "failed"}
            ],
            "events": [event.__dict__ for event in self.events],
        }


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def get_claude_version(app: Path) -> str:
    info_plist = app / "Contents/Info.plist"
    if not info_plist.exists():
        return "unknown"
    try:
        with info_plist.open("rb") as f:
            info = plistlib.load(f)
        return str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "unknown")
    except Exception:
        return "unknown"


def write_patch_report(report: PatchReport) -> Path:
    report_dir = REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    data = report.to_dict()
    report_path = report_dir / f"patch-report-{stamp}.json"
    latest_path = report_dir / "latest.json"
    save_json(report_path, data)
    save_json(latest_path, data)
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        for path in [report_dir, report_path, latest_path]:
            try:
                os.chown(path, int(sudo_uid), int(sudo_gid))
            except PermissionError:
                pass
    print(f"Patch report written: {latest_path}")
    return latest_path


def infer_mode_from_argv(argv: list[str]) -> str:
    if "--repair-code-runtime" in argv:
        return "repair-code-runtime"
    if "--prepare-official-update" in argv:
        return "prepare-official-update"
    if "--diagnose" in argv:
        return "diagnose"
    if "--dry-run" in argv:
        return "dry-run"
    return "install"


def infer_app_from_argv(argv: list[str]) -> Path:
    for index, item in enumerate(argv):
        if item == "--app" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if item.startswith("--app="):
            return Path(item.split("=", 1)[1])
    return APP_DEFAULT


def write_failure_report(mode: str, app: Path, exc: BaseException) -> None:
    report = PatchReport(str(app), get_claude_version(app), mode)
    report.add(
        "script.exception",
        "failed",
        f"type={exc.__class__.__name__}; message={str(exc)}",
        required=True,
    )
    try:
        write_patch_report(report)
    except Exception as report_exc:
        print(f"Failed to write patch report: {report_exc}", file=sys.stderr)


def find_report_event(report: PatchReport, name: str) -> PatchEvent | None:
    for event in reversed(report.events):
        if event.name == name:
            return event
    return None


REPAIR_PASSED_CONCLUSIONS = {"已修复", "桌面版和 CLI 都正常"}


def repair_conclusion_status(conclusion: str) -> str:
    return "passed" if conclusion in REPAIR_PASSED_CONCLUSIONS else "missing"


def is_stale_anthropic_runtime_model(model: Any) -> bool:
    """识别会绕开第三方网关默认模型的旧 Claude/Anthropic 会话模型。"""
    value = str(model or "").strip().lower()
    if not value:
        return False
    if value in {
        "default",
        "opus",
        "opus[1m]",
        SAFE_OPUS_MODEL_ID,
        LEGACY_1M_OPUS_MODEL_ID,
    }:
        return True
    return (
        value.startswith("claude-")
        or value.startswith("anthropic/")
        or value.startswith("anthropic.")
        or value in {"sonnet", "opus", "haiku"}
    )


def active_code_process_model_status(
    processes: list[dict[str, str]], preferred_model: str | None = None
) -> tuple[str, str, int]:
    if not processes:
        return "passed", "active=0", 0
    stale: list[str] = []
    details: list[str] = []
    for item in processes[:8]:
        model = item.get("model") or ""
        is_stale = is_stale_anthropic_runtime_model(model)
        if is_stale:
            stale.append(item.get("pid") or "unknown")
        details.append(
            f"pid={item.get('pid')}; model={model or 'unknown'}; stale={str(is_stale).lower()}"
        )
    status = "missing" if stale else "passed"
    return (
        status,
        f"active={len(processes)}; preferred_model={preferred_model or 'unknown'}; " + " | ".join(details),
        len(stale),
    )


def summarize_repair_conclusion(report: PatchReport) -> tuple[str, str]:
    project_event = find_report_event(report, "runtime.active_project_env_overrides")
    if project_event and project_event.status == "missing" and (project_event.count or 0) > 0:
        return "项目级配置覆盖导致异常", project_event.message

    messages_event = find_report_event(report, "runtime.gateway_messages_auth_check")
    models_event = find_report_event(report, "runtime.gateway_auth_check")
    combined = " ".join(
        event.message
        for event in [messages_event, models_event]
        if event and isinstance(event.message, str)
    )
    if re.search(r"status=(401|403)", combined):
        return "共享 API Key 无效或过期", combined
    if re.search(r"status=(url_error|timeout|os_error)", combined):
        return "网关不可达", combined

    stale_model_event = find_report_event(report, "runtime.pre_repair_active_code_model")
    if stale_model_event and stale_model_event.status == "missing" and (stale_model_event.count or 0) > 0:
        migrated_event = find_report_event(report, "runtime.saved_session_dynamic_model")
        if migrated_event and migrated_event.status in {"applied", "passed"}:
            return "已修复", stale_model_event.message
        return "桌面版活动子进程使用旧模型", stale_model_event.message

    pre_env_event = find_report_event(report, "runtime.pre_repair_active_code_env")
    if pre_env_event and pre_env_event.status == "missing" and (pre_env_event.count or 0) > 0:
        return "桌面版活动子进程未继承网关环境", pre_env_event.message

    recent_error_event = find_report_event(report, "runtime.recent_code_api_errors")
    if (
        recent_error_event
        and recent_error_event.status == "missing"
        and "auth_401" in recent_error_event.message
        and pre_env_event
        and pre_env_event.status == "passed"
        and (pre_env_event.count or 0) > 0
    ):
        return "活动子进程环境正常但仍 401", recent_error_event.message

    desktop_event = find_report_event(report, "runtime.desktop_code_env")
    terminal_event = find_report_event(report, "runtime.terminal_cli_env")
    if desktop_event and desktop_event.status == "passed" and terminal_event and terminal_event.status == "passed":
        return "桌面版和 CLI 都正常", "桌面版启动层、共享网关配置、终端 CLI 检查均通过。"

    launch_event = find_report_event(report, "asar.code_gateway_env_injection")
    if launch_event and launch_event.status != "passed" and terminal_event and terminal_event.status == "passed":
        return "CLI 正常，桌面版启动层未注入", launch_event.message

    if desktop_event and desktop_event.status == "passed" and terminal_event and terminal_event.status != "passed":
        return "桌面版正常，CLI 配置可能受影响", terminal_event.message

    code_env_event = find_report_event(report, "runtime.claude_code_gateway_env")
    if code_env_event and "static_sync_supported=false" in code_env_event.message:
        return "需要手动处理凭据", code_env_event.message

    return "脚本异常", "未能确认 Code CLI 网关环境已经同步。"




def check_js_syntax(path: Path) -> tuple[bool, str]:
    if not shutil.which("node"):
        return False, "node command not found"
    result = run(["node", "--check", str(path)], check=False)
    return result.returncode == 0, result.stdout.strip()


def check_custom3p_validation_patched(app: Path) -> bool:
    path = app / APP_ASAR_REL
    if not path.exists():
        return False
    try:
        data = path.read_bytes()
        header_size, _header_string, header = read_asar_header(data, path)
        entry = get_asar_file_entry(header, ASAR_PATCH_TARGET)
        content_offset = 8 + header_size + int(entry["offset"])
        content_size = int(entry["size"])
        content = data[content_offset : content_offset + content_size]
    except Exception:
        return False
    if (
        b"const Hte=false" in content
        or b"const FLA=false" in content
        or b"function _Zt(e,A){return null;" in content
    ):
        return True
    known_validation_gates = [
        b'const Hte=process.env.NODE_ENV!=="production"||!1,eRt=',
        b'const FLA=process.env.NODE_ENV!=="production"||!1,Yxe=',
        b"function _Zt(e,A){if(!bbA||!(A!=null&&A.length))return null;",
    ]
    return not any(anchor in content for anchor in known_validation_gates)
























def copy_app(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    print(f"Copying app to temporary workspace: {dst}")
    run(["ditto", str(src), str(dst)])


def patch_language_whitelist(app: Path) -> Path:
    assets_dir = app / FRONTEND_ASSETS_REL
    candidates = sorted(assets_dir.glob("index-*.js"))
    if not candidates:
        raise SystemExit(f"Cannot find frontend index bundle in {assets_dir}")

    for path in candidates:
        text = path.read_text(encoding="utf-8")
        if '"zh-CN"' in text:
            print(f"Language whitelist already contains zh-CN: {path.name}")
            return path
        if LANG_LIST_RE.search(text):
            patched = LANG_LIST_RE.sub(
                '["en-US","de-DE","fr-FR","ko-KR","ja-JP","es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID","zh-CN"]',
                text,
                count=1,
            )
            path.write_text(patched, encoding="utf-8")
            print(f"Patched language whitelist: {path.name}")
            return path

    raise SystemExit("Could not patch language whitelist. Claude's bundle format may have changed.")






























def patch_custom3p_model_validation(app: Path) -> bool:
    path = app / APP_ASAR_REL
    require_file(path)

    old_expr = b'process.env.NODE_ENV!=="production"'
    new_expr = b"false"
    replacement = new_expr + b" " * (len(old_expr) - len(new_expr))
    anchor = b"const Hte=" + old_expr + b"||!1,eRt="
    patched = b"const Hte=" + replacement + b"||!1,eRt="
    if len(anchor) != len(patched):
        raise SystemExit("Internal patch error: custom 3P validation replacement changed length.")

    data = bytearray(path.read_bytes())
    header_size, _header_string, header = read_asar_header(data, path)
    entry = get_asar_file_entry(header, ASAR_PATCH_TARGET)
    content_offset = 8 + header_size + int(entry["offset"])
    content_size = int(entry["size"])
    content_end = content_offset + content_size
    if content_offset < 0 or content_end > len(data):
        raise SystemExit(f"Unsupported app.asar file bounds for {ASAR_PATCH_TARGET}.")

    content = bytes(data[content_offset:content_end])
    if content.count(patched) == 1:
        print("Custom 3P model-name validation already patched in app.asar")
        return True

    count = content.count(anchor)
    if count == 1:
        patched_content = content.replace(anchor, patched, 1)
    else:
        # Claude 1.6608.2 moved the 3P model-name validation gate to FLA.
        fla_anchor = b'const FLA=process.env.NODE_ENV!=="production"||!1,Yxe='
        fla_replacement = b"const FLA=" + replacement + b"||!1,Yxe="
        if len(fla_anchor) != len(fla_replacement):
            raise SystemExit("Internal patch error: custom 3P FLA replacement changed length.")
        if content.count(fla_replacement) == 1:
            print("Custom 3P model-name validation already patched in app.asar")
            return True
        if content.count(fla_anchor) == 1:
            patched_content = content.replace(fla_anchor, fla_replacement, 1)
        else:
            # Claude 1.6608+ temporarily moved the model-name validation into _Zt().
            # Make that validator a no-op while preserving app.asar file length.
            new_anchor = b"function _Zt(e,A){if(!bbA||!(A!=null&&A.length))return null;"
            new_expr = b"return null;"
            new_patched = b"function _Zt(e,A){" + new_expr + b" " * (len(new_anchor) - len(b"function _Zt(e,A){") - len(new_expr))
            if len(new_anchor) != len(new_patched):
                raise SystemExit("Internal patch error: custom 3P validation replacement changed length.")
            if content.count(new_patched) == 1:
                print("Custom 3P model-name validation already patched in app.asar")
                return True
            if content.count(new_anchor) != 1:
                print(
                    "Custom 3P model-name validation gate not found in app.asar; "
                    "treating it as not needed for this Claude version."
                )
                return True
            patched_content = content.replace(new_anchor, new_patched, 1)

    if len(patched_content) != len(content):
        raise SystemExit("Internal patch error: app.asar length changed during custom 3P patch.")
    data[content_offset:content_end] = patched_content

    entry["integrity"] = calculate_file_integrity(patched_content)
    updated_header_string = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
    updated_header = encode_asar_header(updated_header_string, header_size)
    data[: len(updated_header)] = updated_header

    path.write_bytes(data)
    update_electron_asar_integrity(app, updated_header_string)
    print("Patched custom 3P model-name validation in app.asar")
    return True


def patch_claude_code_gateway_env_injection(app: Path) -> bool:
    """让 Claude Desktop 启动 Code 子进程时动态继承 ~/.claude/settings.json 的网关 env。"""
    helper = (
        'function zhClaudeCodeGatewayEnv(){try{'
        'const e=tA.join(Bi.homedir(),".claude","settings.json");'
        'if(!jA.existsSync(e))return{};'
        'const A=JSON.parse(jA.readFileSync(e,"utf8")),t=A&&A.env;'
        'if(!t||typeof t!="object")return{};'
        'const i={},r=["ANTHROPIC_BASE_URL","ANTHROPIC_AUTH_TOKEN","ANTHROPIC_API_KEY",'
        '"ANTHROPIC_CUSTOM_HEADERS","CLAUDE_CODE_ATTRIBUTION_HEADER",'
        '"CLAUDE_CODE_DISABLE_TERMINAL_TITLE","CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"];'
        'for(const n of r)typeof t[n]=="string"&&t[n]&&(i[n]=t[n]);'
        'return i.ANTHROPIC_BASE_URL&&(i.ANTHROPIC_AUTH_TOKEN||i.ANTHROPIC_API_KEY)'
        '&&(i.CLAUDE_CODE_OAUTH_TOKEN=void 0,i.CLAUDE_CODE_ENTRYPOINT="sdk-ts"),i'
        '}catch{return{}}}'
    )
    try:
        content = read_asar_text(app, ASAR_PATCH_TARGET)
    except Exception as exc:
        raise SystemExit(f"读取 app.asar 失败：{exc}") from exc

    helper_present = "function zhClaudeCodeGatewayEnv()" in content
    spread_markers = (
        "...t.sessionEnvVars(),...zhClaudeCodeGatewayEnv()}}",
        "Object.assign(g,zhClaudeCodeGatewayEnv()),g.CLAUDE_CODE_ENTRYPOINT",
        "Object.assign(EA,zhClaudeCodeGatewayEnv()),EA.CLAUDE_CODE_ENTRYPOINT",
    )
    spread_present = any(marker in content for marker in spread_markers)
    stale_desktop_entrypoint = 'i.CLAUDE_CODE_OAUTH_TOKEN=void 0,i.CLAUDE_CODE_ENTRYPOINT="claude-desktop-3p"'
    sdk_entrypoint_marker = 'i.CLAUDE_CODE_ENTRYPOINT="sdk-ts"'
    if helper_present and spread_present and stale_desktop_entrypoint not in content and sdk_entrypoint_marker in content:
        print("Claude Code gateway env injection already patched in app.asar")
        return True

    replacements: dict[str, str] = {}
    if stale_desktop_entrypoint in content:
        replacements[stale_desktop_entrypoint] = 'i.CLAUDE_CODE_OAUTH_TOKEN=void 0,i.CLAUDE_CODE_ENTRYPOINT="sdk-ts"'
    no_entrypoint_marker = "i.CLAUDE_CODE_OAUTH_TOKEN=void 0),i"
    if no_entrypoint_marker in content:
        replacements[no_entrypoint_marker] = 'i.CLAUDE_CODE_OAUTH_TOKEN=void 0,i.CLAUDE_CODE_ENTRYPOINT="sdk-ts"),i'
    if not helper_present:
        source = "function lj(e){"
        if source not in content:
            source = "function lj(e,A,t){"
        if source not in content:
            print("Claude Code gateway env injection target function not found in app.asar")
            return False
        replacements[source] = helper + source
    if not spread_present:
        spread_replacements = {
            "...t.sessionEnvVars()}}": "...t.sessionEnvVars(),...zhClaudeCodeGatewayEnv()}}",
            'g.CLAUDE_CODE_ENTRYPOINT||(g.CLAUDE_CODE_ENTRYPOINT="sdk-ts"),delete g.NODE_OPTIONS': (
                'Object.assign(g,zhClaudeCodeGatewayEnv()),g.CLAUDE_CODE_ENTRYPOINT||(g.CLAUDE_CODE_ENTRYPOINT="sdk-ts"),delete g.NODE_OPTIONS'
            ),
            'EA.CLAUDE_CODE_ENTRYPOINT||(EA.CLAUDE_CODE_ENTRYPOINT="sdk-ts"),': (
                'Object.assign(EA,zhClaudeCodeGatewayEnv()),EA.CLAUDE_CODE_ENTRYPOINT||(EA.CLAUDE_CODE_ENTRYPOINT="sdk-ts"),'
            ),
        }
        for source, target in spread_replacements.items():
            if source in content:
                replacements[source] = target
        if not any(source in content for source in spread_replacements):
            print("Claude Code gateway env injection spread point not found in app.asar")
            return False

    count = patch_asar_file_with_replacements(app, ASAR_PATCH_TARGET, replacements)
    if count:
        print(f"Patched Claude Code gateway env injection in app.asar: {count} replacements")
    return count > 0 or check_claude_code_gateway_env_injection(app)


def check_claude_code_gateway_env_injection(app: Path) -> bool:
    ok, _message, _count = claude_code_gateway_env_injection_status(app)
    return ok


def claude_code_gateway_env_injection_status(app: Path) -> tuple[bool, str, int]:
    try:
        content = read_asar_text(app, ASAR_PATCH_TARGET)
    except Exception as exc:
        return False, f"read_failed={exc.__class__.__name__}", 0
    helper_count = content.count("function zhClaudeCodeGatewayEnv()")
    spread_count = (
        content.count("...t.sessionEnvVars(),...zhClaudeCodeGatewayEnv()}}")
        + content.count("Object.assign(g,zhClaudeCodeGatewayEnv()),g.CLAUDE_CODE_ENTRYPOINT")
        + content.count("Object.assign(EA,zhClaudeCodeGatewayEnv()),EA.CLAUDE_CODE_ENTRYPOINT")
    )
    settings_path_count = content.count('tA.join(Bi.homedir(),".claude","settings.json")')
    stale_entrypoint_count = content.count('i.CLAUDE_CODE_ENTRYPOINT="claude-desktop-3p"')
    sdk_entrypoint_count = content.count('i.CLAUDE_CODE_ENTRYPOINT="sdk-ts"')
    ok = (
        helper_count > 0
        and spread_count > 0
        and settings_path_count > 0
        and stale_entrypoint_count == 0
        and sdk_entrypoint_count > 0
    )
    return (
        ok,
        (
            f"helper_count={helper_count}; spread_count={spread_count}; "
            f"settings_path_count={settings_path_count}; "
            f"stale_desktop_entrypoint_count={stale_entrypoint_count}; "
            f"sdk_entrypoint_count={sdk_entrypoint_count}; "
            "static_marker_only=true; runtime_env_must_be_checked_by_pre_repair_active_code_env"
        ),
        helper_count + spread_count + settings_path_count + stale_entrypoint_count + sdk_entrypoint_count,
    )


def install_disclaimer_gateway_wrapper(app: Path) -> tuple[bool, str]:
    """给 Desktop 实际执行命令的 disclaimer helper 加兜底 env wrapper。

    Claude 1.8555 的 Code 启动路径可能不经过 app.asar 中已命中的 env 注入点。
    wrapper 只读取用户态 ~/.claude/settings.json，不把 API Key 写进 app bundle。
    """
    helper = app / "Contents/Helpers/disclaimer"
    real = app / "Contents/Helpers/disclaimer.real"
    wrapper_template = Path(__file__).with_name("claude-disclaimer-gateway-wrapper")
    if not helper.exists():
        return False, "helper=missing"
    if not wrapper_template.exists():
        return False, f"wrapper_template=missing; path={wrapper_template}"
    try:
        current = helper.read_bytes()
        is_current_wrapper = b"desktop-gateway-wrapper.log" in current and b"CLAUDE_CODE_ENTRYPOINT" in current
    except OSError as exc:
        return False, f"helper=unreadable; error={exc.__class__.__name__}"
    if not real.exists():
        if is_current_wrapper:
            return False, "helper=wrapper; real_helper=missing"
        shutil.copy2(helper, real)
    wrapper = wrapper_template.read_text(encoding="utf-8")
    try:
        existing = helper.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        existing = ""
    if existing == wrapper:
        return False, "wrapper=already_installed; real_helper=present"
    helper.write_text(wrapper, encoding="utf-8")
    helper.chmod(0o755)
    return True, "wrapper=installed; real_helper=present; entrypoint=sdk-ts; model_rewrite=true; api_key_not_logged=true"
























def set_app_language_defaults(user_home: Path) -> None:
    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        user = user_home.name

    defaults_prefix: list[str] = []
    if os.geteuid() == 0 and user and user != "root":
        defaults_prefix = ["sudo", "-u", user]

    domain = "com.anthropic.claudefordesktop"
    run(
        defaults_prefix
        + [
            "defaults",
            "write",
            domain,
            "AppleLanguages",
            "-array",
            "zh-Hans",
            "zh-Hans-CN",
            "zh-CN",
            "en-CN",
            "en-US",
        ],
        check=False,
    )
    run(defaults_prefix + ["defaults", "write", domain, "AppleLocale", "-string", "zh_CN"], check=False)
    print(f"Set Claude app language defaults for user: {user}")
















def check_runtime_invariants(
    user_home: Path,
    report: PatchReport,
    *,
    require: bool = False,
    project_paths: list[Path] | None = None,
) -> None:
    """检查用户态运行数据；这里只记录风险，不阻断安装。"""
    preferred_model, model_metadata = preferred_gateway_model_id(user_home)
    provider_context_window = context_window_from_metadata(model_metadata)
    runtime_context_window, runtime_context_source = read_claude_code_context_window(user_home)
    report.add(
        "runtime.provider_default_model",
        "passed" if preferred_model else "missing",
        f"model={preferred_model or 'unavailable'}; source={model_metadata.get('source')}",
        count=int(model_metadata.get("model_count", 0) or 0),
        required=False,
    )
    gateway_status, gateway_message = gateway_probe_message(model_metadata)
    report.add(
        "runtime.gateway_auth_check",
        gateway_status,
        gateway_message,
        required=False,
    )
    messages_status, messages_message = gateway_messages_auth_probe(user_home, preferred_model)
    report.add(
        "runtime.gateway_messages_auth_check",
        messages_status,
        messages_message,
        required=False,
    )
    report.add(
        "runtime.provider_default_ignores_opus_alias",
        "passed" if preferred_model and not is_opus_display_alias(preferred_model) else "missing",
        (
            f"model={preferred_model or 'unavailable'}; "
            f"ignored_opus_alias_count={model_metadata.get('ignored_opus_alias_count', 0)}; "
            f"configured_model_count={model_metadata.get('configured_model_count', 0)}"
        ),
        required=False,
    )
    code_env_status, code_env_message = claude_code_gateway_env_status(user_home)
    report.add(
        "runtime.claude_code_gateway_env",
        code_env_status,
        code_env_message,
        required=False,
    )
    desktop_env_status, desktop_env_message = desktop_code_env_status(Path(report.app), user_home)
    report.add(
        "runtime.desktop_code_env",
        desktop_env_status,
        desktop_env_message,
        required=False,
    )
    terminal_env_status, terminal_env_message = terminal_cli_env_status(user_home)
    report.add(
        "runtime.terminal_cli_env",
        terminal_env_status,
        terminal_env_message,
        required=False,
    )
    vscode_status, vscode_message, vscode_count = vscode_claude_extension_status(user_home)
    report.add(
        "runtime.vscode_claude_extension",
        vscode_status,
        vscode_message,
        count=vscode_count,
        required=False,
    )
    project_env_status, project_env_message, project_env_count = project_env_override_status(user_home)
    report.add(
        "runtime.active_project_env_overrides",
        project_env_status,
        project_env_message,
        count=project_env_count,
        required=False,
    )
    if project_paths:
        explicit_env_status, explicit_env_message, explicit_env_count = project_env_override_status(
            user_home, project_paths
        )
        report.add(
            "runtime.project_env_overrides",
            explicit_env_status,
            explicit_env_message,
            count=explicit_env_count,
            required=False,
        )
    report.add(
        "runtime.provider_context_window",
        "passed" if provider_context_window else "missing",
        f"context_window={provider_context_window or 'unavailable'}; source={model_metadata.get('context_source') or model_metadata.get('source')}",
        required=False,
    )
    report.add(
        "runtime.claude_code_context_window",
        "passed" if runtime_context_window and runtime_context_source.startswith("root_") else "missing",
        f"context_window={runtime_context_window or 'unavailable'}; source={runtime_context_source}",
        required=False,
    )
    report.add(
        "runtime.context_window_root_configured",
        "passed" if runtime_context_source.startswith("root_") else "missing",
        f"source={runtime_context_source}",
        required=False,
    )
    if provider_context_window and runtime_context_window:
        matched = provider_context_window == runtime_context_window and runtime_context_source.startswith("root_")
        report.add(
            "runtime.context_window_match",
            "passed" if matched else "missing",
            f"provider={provider_context_window}; runtime={runtime_context_window}; source={runtime_context_source}",
            required=False,
        )
    else:
        report.add(
            "runtime.context_window_match",
            "missing",
            f"provider={provider_context_window or 'unavailable'}; runtime={runtime_context_window or 'unavailable'}",
            required=False,
        )
    active_processes = active_claude_code_processes(user_home)
    active_models = [item["model"] for item in active_processes if item.get("model")]
    bad_models = [
        f"{item['pid']}:{item['model']}"
        for item in active_processes
        if item.get("model", "").lower() in {SAFE_OPUS_MODEL_ID, LEGACY_1M_OPUS_MODEL_ID}
    ]
    report.add(
        "runtime.active_cli_model",
        "passed" if not bad_models else "missing",
        (
            "active_models=" + ",".join(active_models[:8])
            if active_models and not bad_models
            else "bad=" + ",".join(bad_models[:8])
            if bad_models
            else "active=0"
        ),
        count=len(active_processes),
        required=require,
    )
    active = active_claude_code_sessions(user_home)
    token_errors: list[str] = []
    for session in active:
        transcript = find_claude_code_transcript(user_home, session["cliSessionId"])
        if not transcript or not transcript.exists():
            continue
        text = transcript.read_text(encoding="utf-8", errors="ignore")
        match = None
        for match in TOKEN_LIMIT_ERROR_RE.finditer(text):
            pass
        if match:
            token_errors.append(
                f"{session['cliSessionId']}:limit={match.group('limit')},requested={match.group('requested')}"
            )
    report.add(
        "runtime.active_sessions_token_limit_errors",
        "passed" if not token_errors else "missing",
        "errors=" + ",".join(token_errors[:5]) if token_errors else f"active={len(active)}",
        count=len(token_errors),
        required=require,
    )


def print_report_summary(report: PatchReport) -> None:
    data = report.to_dict()
    summary = data["summary"]
    print("Patch diagnostics summary:")
    for key in ["passed", "applied", "already_patched", "missing", "failed"]:
        if key in summary:
            print(f"  {key}: {summary[key]}")
    failures = data["required_failures"]
    if failures:
        print("Required failures:")
        for item in failures:
            location = f" ({item['file']})" if item.get("file") else ""
            print(f"  - {item['name']}{location}: {item['status']}")


def backup_and_replace(original: Path, patched: Path, dry_run: bool) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = original.with_name(f"Claude.backup-before-zh-CN-{stamp}.app")
    if dry_run:
        print(f"[dry-run] Would move {original} -> {backup}")
        print(f"[dry-run] Would move {patched} -> {original}")
        return backup

    print(f"Backing up current app: {backup}")
    shutil.move(str(original), str(backup))
    print(f"Installing patched app: {original}")
    shutil.move(str(patched), str(original))
    return backup


def prune_old_backups(original: Path, keep: Path, user_home: Path, dry_run: bool, keep_count: int = 1) -> None:
    backups = sorted(original.parent.glob(f"{original.stem}.backup-before-zh-CN-*.app"))
    if not backups:
        return

    keep_paths = set(backups[-keep_count:])
    keep_paths.add(keep)
    stale = [path for path in backups if path not in keep_paths]
    if not stale:
        return

    trash_dir = user_home / ".Trash" / f"Claude-old-backups-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if dry_run:
        for path in stale:
            print(f"[dry-run] Would move old backup to trash: {path} -> {trash_dir / path.name}")
        return

    trash_dir.mkdir(parents=True, exist_ok=True)
    print(f"Moving {len(stale)} old backup(s) to: {trash_dir}")
    for path in stale:
        shutil.move(str(path), str(trash_dir / path.name))

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        for path in [trash_dir, *trash_dir.iterdir()]:
            os.chown(path, int(sudo_uid), int(sudo_gid))


def prepare_official_update(app: Path, user_home: Path, dry_run: bool) -> Path:
    """解除当前补丁版 Claude.app 的覆盖阻碍，允许 Finder 直接拖官方 DMG 覆盖安装。"""
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")

    if dry_run:
        print(f"[dry-run] Would prepare {app} for Finder overwrite without moving or deleting it")
        print("[dry-run] Would clear uchg/schg flags, Gatekeeper attributes, owner and user-writable permissions")
        print(f"[dry-run] Would not touch user config under {user_home}/Library/Application Support/Claude*")
        return app

    print(f"Preparing current Claude.app for official DMG overwrite: {app}")
    run(["chflags", "-R", "nouchg,noschg", str(app)], check=False)
    clear_quarantine(app)

    if sudo_uid and sudo_gid:
        uid = int(sudo_uid)
        gid = int(sudo_gid)
        for root, dirs, files in os.walk(app):
            for name in [root, *[str(Path(root) / item) for item in dirs], *[str(Path(root) / item) for item in files]]:
                try:
                    os.chown(name, uid, gid)
                except PermissionError:
                    pass

    run(["chmod", "-R", "u+rwX", str(app)], check=False)
    print("Claude.app remains in /Applications. It is now prepared for Finder overwrite.")
    print("Now drag the official Claude.app from the DMG into Applications and choose Replace.")
    print("Your API, gateway and model settings under Application Support were not changed.")
    return app


def verify(app: Path) -> None:
    frontend = app / FRONTEND_I18N_REL / "zh-CN.json"
    data = load_json(frontend)
    values = [v for v in data.values() if isinstance(v, str)]
    chinese = sum(1 for v in values if re.search(r"[\u4e00-\u9fff]", v))
    print(f"Verified frontend zh-CN JSON: {chinese}/{len(values)} strings contain Chinese")

    verify_result = run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=False)
    if verify_result.returncode == 0:
        print("Verified app signature")
    else:
        print("App signature verification failed:")
        print(verify_result.stdout, end="")

    entitlements = read_entitlements(app)
    if "com.apple.security.virtualization" in entitlements:
        print("Verified virtualization entitlement")
    else:
        print("Warning: virtualization entitlement is missing")

    result = run(["codesign", "-dv", str(app)], check=False).stdout
    for line in result.splitlines():
        if line.startswith("TeamIdentifier="):
            print(line)


def repair_code_runtime(args: argparse.Namespace) -> int:
    report = PatchReport(str(args.app), get_claude_version(args.app), "repair-code-runtime")
    pre_processes = active_claude_code_processes(args.user_home)
    pre_preferred_model, _ = preferred_gateway_model_id(args.user_home)
    pre_process_status, pre_process_message, pre_process_count = pre_repair_active_code_process_status(
        args.user_home, pre_processes
    )
    report.add(
        "runtime.pre_repair_active_code_processes",
        pre_process_status,
        pre_process_message,
        count=pre_process_count,
        required=False,
    )
    pre_model_status, pre_model_message, pre_model_count = active_code_process_model_status(
        pre_processes, pre_preferred_model
    )
    report.add(
        "runtime.pre_repair_active_code_model",
        pre_model_status,
        pre_model_message,
        count=pre_model_count,
        required=False,
    )
    pre_env_status, pre_env_message, pre_env_count = pre_repair_active_code_env_status(args.user_home, pre_processes)
    report.add(
        "runtime.pre_repair_active_code_env",
        pre_env_status,
        pre_env_message,
        count=pre_env_count,
        required=False,
    )
    recent_error_status, recent_error_message, recent_error_count = recent_code_api_error_status(args.user_home)
    report.add(
        "runtime.recent_code_api_errors",
        recent_error_status,
        recent_error_message,
        count=recent_error_count,
        required=False,
    )
    if args.dry_run:
        print("[dry-run] Claude will not be quit and user config will not be changed.")
        gateway_env_injection_ok, gateway_env_injection_message, gateway_env_injection_count = (
            claude_code_gateway_env_injection_status(args.app)
        )
        report.add(
            "asar.code_gateway_env_injection",
            "passed" if gateway_env_injection_ok else "missing",
            gateway_env_injection_message,
            count=gateway_env_injection_count,
            required=False,
        )
        check_runtime_invariants(args.user_home, report, require=False)
        write_patch_report(report)
        print_report_summary(report)
        return 0

    quit_claude()
    terminated = terminate_claude_code_children(args.user_home, args.dry_run)
    report.add(
        "runtime.claude_code_children_terminated",
        "applied" if terminated else "passed",
        f"terminated={terminated}",
        required=False,
    )
    settings_backup_path = backup_shared_claude_settings(args.user_home / ".claude/settings.json")
    preferred_model, model_metadata = set_claude_code_dynamic_defaults(args.user_home)
    report.add(
        "runtime.provider_default_model",
        "passed" if preferred_model else "missing",
        f"model={preferred_model or 'unavailable'}; source={model_metadata.get('source')}",
        count=int(model_metadata.get("model_count", 0) or 0),
        required=False,
    )
    gateway_status, gateway_message = gateway_probe_message(model_metadata)
    report.add("runtime.gateway_auth_check", gateway_status, gateway_message, required=False)
    messages_status, messages_message = gateway_messages_auth_probe(args.user_home, preferred_model)
    report.add("runtime.gateway_messages_auth_check", messages_status, messages_message, required=False)
    (
        code_env_changed,
        code_env_status,
        code_env_message,
        settings_backup_path,
        merge_status,
        merge_message,
    ) = sync_claude_code_gateway_env(args.user_home, settings_backup_path)
    report.add(
        "runtime.shared_settings_backup_path",
        "passed",
        f"path={settings_backup_path}; contains_secrets=true; do_not_upload_backup=true",
        required=False,
    )
    report.add(
        "runtime.shared_settings_merge_safe",
        merge_status,
        merge_message,
        required=False,
    )
    report.add(
        "runtime.claude_code_gateway_env",
        "applied" if code_env_changed else code_env_status,
        code_env_message,
        required=False,
    )
    gateway_env_injection_ok, gateway_env_injection_message, gateway_env_injection_count = (
        claude_code_gateway_env_injection_status(args.app)
    )
    report.add(
        "asar.code_gateway_env_injection",
        "passed" if gateway_env_injection_ok else "missing",
        gateway_env_injection_message,
        count=gateway_env_injection_count,
        required=False,
    )
    desktop_env_status, desktop_env_message = desktop_code_env_status(args.app, args.user_home)
    report.add(
        "runtime.desktop_code_env",
        desktop_env_status,
        desktop_env_message,
        required=False,
    )
    terminal_env_status, terminal_env_message = terminal_cli_env_status(args.user_home)
    report.add(
        "runtime.terminal_cli_env",
        terminal_env_status,
        terminal_env_message,
        required=False,
    )
    vscode_status, vscode_message, vscode_count = vscode_claude_extension_status(args.user_home)
    report.add(
        "runtime.vscode_claude_extension",
        vscode_status,
        vscode_message,
        count=vscode_count,
        required=False,
    )
    provider_context_window = context_window_from_metadata(model_metadata)
    runtime_context_window, runtime_context_source, context_changed = sync_claude_code_context_window(
        args.user_home, provider_context_window
    )
    report.add(
        "runtime.provider_context_window",
        "passed" if provider_context_window else "missing",
        f"context_window={provider_context_window or 'unavailable'}; source={model_metadata.get('context_source') or model_metadata.get('source')}",
        required=False,
    )
    report.add(
        "runtime.claude_code_context_window",
        "applied" if context_changed else ("passed" if runtime_context_window else "missing"),
        f"context_window={runtime_context_window or 'unavailable'}; source={runtime_context_source}",
        required=False,
    )
    report.add(
        "runtime.context_window_match",
        "passed"
        if provider_context_window
        and runtime_context_window == provider_context_window
        and runtime_context_source.startswith("root_")
        else "missing",
        f"provider={provider_context_window or 'unavailable'}; runtime={runtime_context_window or 'unavailable'}; source={runtime_context_source}",
        required=False,
    )
    migrated_sessions = migrate_saved_session_dynamic_model(args.user_home, preferred_model)
    report.add(
        "runtime.saved_session_dynamic_model",
        "applied" if migrated_sessions else "passed",
        f"migrated={migrated_sessions}",
        count=migrated_sessions,
        required=False,
    )
    sanitized_sessions, sanitize_details = sanitize_active_oversized_sessions(args.user_home)
    report.add(
        "runtime.oversized_session_sanitize",
        "applied" if sanitized_sessions else "passed",
        f"sanitized={sanitized_sessions}; checked={len(sanitize_details)}",
        required=False,
    )
    check_runtime_invariants(args.user_home, report, require=False)
    conclusion, conclusion_detail = summarize_repair_conclusion(report)
    report.add(
        "runtime.repair_conclusion",
        repair_conclusion_status(conclusion),
        f"result={conclusion}; detail={conclusion_detail}",
        required=False,
    )
    write_patch_report(report)
    print_report_summary(report)
    print()
    print(f"修复结论：{conclusion}")
    if conclusion_detail:
        print(f"说明：{conclusion_detail}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch Claude Desktop with zh-CN language resources.")
    parser.add_argument("--app", type=Path, default=APP_DEFAULT, help="Path to Claude.app")
    parser.add_argument("--user-home", type=Path, default=Path.home(), help="Home directory whose Claude config should be updated")
    parser.add_argument("--dry-run", action="store_true", help="Prepare and verify a patched temp app, but do not replace /Applications/Claude.app")
    parser.add_argument("--diagnose", action="store_true", help="Only inspect the current Claude.app patch status and write a diagnostic report")
    parser.add_argument(
        "--project",
        action="append",
        type=Path,
        default=[],
        help="Project folder to inspect for Code-specific .claude/settings or .env gateway overrides during --diagnose",
    )
    parser.add_argument("--repair-code-runtime", action="store_true", help="Repair Claude Code user runtime settings from the active third-party inference config")
    parser.add_argument("--prepare-official-update", action="store_true", help="Prepare the patched Claude.app so Finder can overwrite it from the official DMG")
    parser.add_argument("--launch", action="store_true", help="Launch Claude after installation")
    args = parser.parse_args()

    if not args.app.exists():
        raise SystemExit(f"Claude.app not found: {args.app}")

    if args.diagnose:
        report = PatchReport(str(args.app), get_claude_version(args.app), "diagnose")
        check_frontend_invariants(args.app, report, require=True)
        check_runtime_invariants(args.user_home, report, require=False, project_paths=args.project)
        stale_findings = scan_for_stale_display_names(args.app)
        if stale_findings:
            for finding in stale_findings:
                report.add(
                    f"residue.{finding['file'].replace('/', '_')}",
                    "warning",
                    f"发现旧版补丁残留: {finding['matches']} in {finding['file']}",
                )
        write_patch_report(report)
        print_report_summary(report)
        return 1 if report.has_required_failures() else 0

    if args.repair_code_runtime:
        return repair_code_runtime(args)

    if args.prepare_official_update:
        report = PatchReport(str(args.app), get_claude_version(args.app), "prepare-official-update")
        if args.dry_run:
            print("[dry-run] Claude will not be quit.")
        else:
            quit_claude()
            terminated = terminate_claude_code_children(args.user_home, args.dry_run)
            report.add(
                "runtime.claude_code_children_terminated",
                "applied" if terminated else "passed",
                f"terminated={terminated}",
                required=False,
            )
        target = prepare_official_update(args.app, args.user_home, args.dry_run)
        report.add(
            "official_update.prepare_overwrite",
            "passed",
            f"prepared_app={target}; moved=false; user_config_untouched=true",
            required=True,
        )
        write_patch_report(report)
        print_report_summary(report)
        return 0

    require_file(FRONTEND_TRANSLATION)
    require_file(DESKTOP_TRANSLATION)
    require_file(LOCALIZABLE_STRINGS)
    require_virtualization_entitlement(args.app)
    report = PatchReport(str(args.app), get_claude_version(args.app), "dry-run" if args.dry_run else "install")

    try:
        in_applications = args.app.resolve().as_posix().startswith("/Applications/")
    except Exception:
        in_applications = str(args.app).startswith("/Applications/")
    if os.geteuid() != 0 and in_applications:
        print("This usually needs sudo because /Applications is protected.", file=sys.stderr)

    preferred_model, model_metadata = preferred_gateway_model_id(args.user_home)
    if args.dry_run:
        print("[dry-run] Claude will not be quit.")
    else:
        pre_processes = active_claude_code_processes(args.user_home)
        pre_model_status, pre_model_message, pre_model_count = active_code_process_model_status(
            pre_processes, preferred_model
        )
        report.add(
            "runtime.pre_install_active_code_model",
            pre_model_status,
            pre_model_message,
            count=pre_model_count,
            required=False,
        )
        quit_claude()
        terminated = terminate_claude_code_children(args.user_home, args.dry_run)
        report.add(
            "runtime.claude_code_children_terminated",
            "applied" if terminated else "passed",
            f"terminated={terminated}",
            required=False,
        )
    provider_context_window = context_window_from_metadata(model_metadata)
    tmp_root = Path(tempfile.mkdtemp(prefix="claude-zh-cn-patch."))
    patched_app = tmp_root / "Claude.app"

    copy_app(args.app, patched_app)
    patch_language_whitelist(patched_app)
    patch_hardcoded_frontend_strings(patched_app, provider_context_window, preferred_model)
    model_validation_patched = patch_custom3p_model_validation(patched_app)
    report.add(
        "asar.custom3p_validation.patch",
        "applied" if model_validation_patched else "missing",
        required=False,
    )
    gateway_env_injection_patched = patch_claude_code_gateway_env_injection(patched_app)
    gateway_env_injection_ok, gateway_env_injection_message, gateway_env_injection_count = (
        claude_code_gateway_env_injection_status(patched_app)
    )
    report.add(
        "asar.code_gateway_env_injection.patch",
        "applied" if gateway_env_injection_patched else ("passed" if gateway_env_injection_ok else "missing"),
        gateway_env_injection_message,
        count=gateway_env_injection_count,
        required=False,
    )
    disclaimer_wrapper_changed, disclaimer_wrapper_message = install_disclaimer_gateway_wrapper(patched_app)
    report.add(
        "runtime.disclaimer_gateway_wrapper",
        "applied" if disclaimer_wrapper_changed else "passed",
        disclaimer_wrapper_message,
        required=False,
    )
    patch_native_menu_role_labels(patched_app)
    merge_frontend_locale(patched_app)
    install_desktop_locale(patched_app)
    install_statsig_locale(patched_app)
    resign_app(patched_app)
    clear_quarantine(patched_app)
    if args.dry_run:
        print(f"[dry-run] Would set Claude config locale under: {args.user_home}")
    else:
        set_user_locale(args.user_home)
        settings_backup_path = backup_shared_claude_settings(args.user_home / ".claude/settings.json")
        preferred_model, model_metadata = set_claude_code_dynamic_defaults(args.user_home)
        report.add(
            "runtime.provider_default_model",
            "passed" if preferred_model else "missing",
            f"model={preferred_model or 'unavailable'}; source={model_metadata.get('source')}",
            count=int(model_metadata.get("model_count", 0) or 0),
            required=False,
        )
        gateway_status, gateway_message = gateway_probe_message(model_metadata)
        report.add(
            "runtime.gateway_auth_check",
            gateway_status,
            gateway_message,
            required=False,
        )
        messages_status, messages_message = gateway_messages_auth_probe(args.user_home, preferred_model)
        report.add(
            "runtime.gateway_messages_auth_check",
            messages_status,
            messages_message,
            required=False,
        )
        report.add(
            "runtime.provider_default_ignores_opus_alias",
            "passed" if preferred_model and not is_opus_display_alias(preferred_model) else "missing",
            (
                f"model={preferred_model or 'unavailable'}; "
                f"ignored_opus_alias_count={model_metadata.get('ignored_opus_alias_count', 0)}; "
                f"configured_model_count={model_metadata.get('configured_model_count', 0)}"
            ),
            required=False,
        )
        (
            code_env_changed,
            code_env_status,
            code_env_message,
            settings_backup_path,
            merge_status,
            merge_message,
        ) = sync_claude_code_gateway_env(args.user_home, settings_backup_path)
        report.add(
            "runtime.shared_settings_backup_path",
            "passed",
            f"path={settings_backup_path}; contains_secrets=true; do_not_upload_backup=true",
            required=False,
        )
        report.add(
            "runtime.shared_settings_merge_safe",
            merge_status,
            merge_message,
            required=False,
        )
        report.add(
            "runtime.claude_code_gateway_env",
            "applied" if code_env_changed else code_env_status,
            code_env_message,
            required=False,
        )
        desktop_env_status, desktop_env_message = desktop_code_env_status(patched_app, args.user_home)
        report.add(
            "runtime.desktop_code_env",
            desktop_env_status,
            desktop_env_message,
            required=False,
        )
        terminal_env_status, terminal_env_message = terminal_cli_env_status(args.user_home)
        report.add(
            "runtime.terminal_cli_env",
            terminal_env_status,
            terminal_env_message,
            required=False,
        )
        vscode_status, vscode_message, vscode_count = vscode_claude_extension_status(args.user_home)
        report.add(
            "runtime.vscode_claude_extension",
            vscode_status,
            vscode_message,
            count=vscode_count,
            required=False,
        )
        provider_context_window = context_window_from_metadata(model_metadata)
        runtime_context_window, runtime_context_source, context_changed = sync_claude_code_context_window(
            args.user_home, provider_context_window
        )
        report.add(
            "runtime.provider_context_window",
            "passed" if provider_context_window else "missing",
            f"context_window={provider_context_window or 'unavailable'}; source={model_metadata.get('context_source') or model_metadata.get('source')}",
            required=False,
        )
        report.add(
            "runtime.claude_code_context_window",
            "applied" if context_changed else ("passed" if runtime_context_window else "missing"),
            f"context_window={runtime_context_window or 'unavailable'}; source={runtime_context_source}",
            required=False,
        )
        report.add(
            "runtime.context_window_root_configured",
            "passed" if runtime_context_source.startswith("root_") else "missing",
            f"source={runtime_context_source}",
            required=False,
        )
        report.add(
            "runtime.context_window_match",
            "passed"
            if provider_context_window
            and runtime_context_window == provider_context_window
            and runtime_context_source.startswith("root_")
            else "missing",
            f"provider={provider_context_window or 'unavailable'}; runtime={runtime_context_window or 'unavailable'}; source={runtime_context_source}",
            required=False,
        )
        migrated_sessions = migrate_saved_session_dynamic_model(args.user_home, preferred_model)
        report.add(
            "runtime.saved_session_dynamic_model",
            "applied" if migrated_sessions else "passed",
            f"migrated={migrated_sessions}",
            count=migrated_sessions,
            required=False,
        )
        sanitized_sessions, sanitize_details = sanitize_active_oversized_sessions(args.user_home)
        report.add(
            "runtime.oversized_session_sanitize",
            "applied" if sanitized_sessions else "passed",
            f"sanitized={sanitized_sessions}; checked={len(sanitize_details)}",
            required=False,
        )
        clear_frontend_cache(args.user_home, args.dry_run)
    verify(patched_app)
    check_frontend_invariants(patched_app, report, require=True)
    stale_findings = scan_for_stale_display_names(patched_app)
    if stale_findings:
        for finding in stale_findings:
            report.add(
                f"residue.{finding['file'].replace('/', '_')}",
                "warning",
                f"发现旧版补丁残留: {finding['matches']} in {finding['file']}",
            )
    if report.has_required_failures():
        write_patch_report(report)
        print_report_summary(report)
        print("Required frontend invariants failed. Original Claude.app was left untouched.", file=sys.stderr)
        return 1

    backup = backup_and_replace(args.app, patched_app, args.dry_run)
    if not args.dry_run:
        print(f"Backup kept at: {backup}")
        prune_old_backups(args.app, backup, args.user_home, args.dry_run)
        if args.launch:
            run(["open", "-a", str(args.app)], check=False)

    if not model_validation_patched:
        print("Note: optional 3P model-name validation patch was skipped for this Claude version.")
    write_patch_report(report)
    print_report_summary(report)
    print("Done. Select Language -> 中文（中国） in Claude if it is not already selected.")
    return 0


def safe_main() -> int:
    try:
        return main()
    except SystemExit as exc:
        code = exc.code
        if code not in (None, 0):
            app = infer_app_from_argv(sys.argv[1:])
            mode = infer_mode_from_argv(sys.argv[1:])
            write_failure_report(mode, app, exc)
        raise
    except BaseException as exc:
        app = infer_app_from_argv(sys.argv[1:])
        mode = infer_mode_from_argv(sys.argv[1:])
        write_failure_report(mode, app, exc)
        print(f"脚本异常：{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(safe_main())
