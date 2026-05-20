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
SAFE_OPUS_MODEL_ID = "opus"
LEGACY_1M_OPUS_MODEL_ID = "opus[1m]"
OPUS_DISPLAY_NAME = "Opus 4.71M"
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

KNOWN_FRONTEND_I18N_KEYS: dict[str, str] = {
    "0rLmv1esFb": "隐私页：更新检查请求说明",
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
    "G/QQvx0Tsd": "第三方推理设置：覆盖模型列表说明",
    "HIjCnaQF93": "第三方推理设置：辅助脚本缓存 TTL",
    "Amxb69AvfR": "第三方推理设置：连接页说明",
    "Ba3MtjwP5h": "第三方推理设置：遥测与更新",
    "CCUxBOb3va": "第三方推理设置：禁用深度链接",
    "CbPYtuP6+N": "隐私页：身份和账号说明",
    "CwADEGuH8H": "第三方推理设置：沙盒与工作区",
    "DHdnIxD7G9": "第三方推理设置：禁用 Claude.ai 登录",
    "DnXPcFgmqb": "第三方推理设置：凭据辅助脚本",
    "DC+lIM7C8k": "第三方推理设置：托管 MCP 服务器",
    "IxbsWX4wj4": "第三方推理设置：添加请求头按钮",
    "JQs8c3pGcl": "第三方推理设置：网关基础 URL",
    "KZbdbvaU9V": "第三方推理设置：插件与技能",
    "KtZV9pULgo": "第三方推理设置：连接",
    "L+geN0DrtO": "第三方推理设置：网关登录",
    "MYYAX2WEkL": "隐私页：Anthropic 可能收到的内容标题",
    "NA4SBfPMeA": "第三方推理设置：网关 API 密钥",
    "OX1+jdVwLL": "第三方推理设置：阻止自动更新",
    "PNFwYup600": "第三方推理设置：自动更新分组",
    "QhJxtbMJfB": "会话批量菜单：归档",
    "RtLYfLZ2bT": "第三方推理设置：辅助脚本 TTL",
    "SKeCK+7hmh": "Claude Code 设置：本地会话标题",
    "StnRZmM3Xn": "第三方推理设置：绝对路径",
    "TGyeqFZWHH": "第三方推理设置：辅助脚本缓存说明",
    "TkA72ubrGt": "第三方推理设置：测试连接",
    "U5lBq+CZ7G": "隐私页：匿名使用指标说明",
    "ULnTQCHxiV": "隐私页：Anthropic 看不到的内容标题",
    "UmH4IX1ER9": "会话批量菜单：标为未读",
    "UntW78doSE": "第三方推理设置：连接失败提示",
    "UzLHrala3Q": "第三方推理设置：非必要遥测分组",
    "W41+8Xj7fP": "第三方推理设置：复制主机名",
    "XtXm3euW3d": "第三方推理设置：非必要遥测原因",
    "Yk0+YjpaDc": "第三方推理设置：深度链接禁用说明",
    "ZON8uMn14w": "第三方推理设置：令牌单位",
    "ZRMqH+j2yz": "第三方推理设置：测试模型发现",
    "a87VTwtQw3": "第三方推理设置：1M 上下文变体",
    "aRuqK/KXrl": "第三方推理设置：推理后端说明",
    "aTTY7rU6Bh": "第三方推理设置：每窗口最大令牌",
    "akXG4ChYkN": "Claude Code 设置：默认启用远程控制",
    "bmP92ZCban": "第三方推理设置：要求扩展已签名",
    "dzVdmB+VtN": "第三方推理设置：Code 标签页说明",
    "dYlenA3UP7": "第三方推理设置：OpenTelemetry 分组",
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
    "gshbVTjZni": "连接器页：迁移到自定义提示",
    "gkoSAmTJDl": "会话批量菜单：删除",
    "hhKxQ3MtxT": "第三方推理设置：内置工具策略",
    "h3IJeFcbkv": "Claude Code 设置：远程控制自动连接说明",
    "hcpszlrtjU": "删除会话弹窗：单会话删除说明",
    "hv0F38ESRM": "第三方推理设置：模型列表",
    "i8fSuvZDK/": "第三方推理设置：固定模型列表说明",
    "iGPHC9Tm20": "第三方推理设置：禁用的内置工具",
    "jU4z+3Uk7+": "第三方推理设置：模型发现",
    "jJz20QocAd": "第三方推理设置：自动更新强制窗口",
    "4MengK4xQ/": "第三方推理设置：小时单位",
    "knHnvzpkOf": "第三方推理设置：允许桌面扩展",
    "nOBN85iT+Z": "第三方推理设置：组织插件目录说明",
    "oo4Av05fBn": "第三方推理设置：阻止非必要遥测",
    "ozzKmITBMv": "第三方推理设置：令牌软限制说明",
    "kT5Jg7Fz/u": "删除会话弹窗：标题",
    "ll3OMXtx55": "第三方推理设置：网关连接说明",
    "pBgZotXlmX": "第三方推理设置：凭据辅助脚本说明",
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
    "x+MG25XWVf": "第三方推理设置：请求头辅助脚本",
    "x8r3+rMaHq": "第三方推理设置：凭据辅助脚本超时",
    "xovdJlXIM6": "第三方推理设置：OpenTelemetry 收集器端点",
    "xWiIy0pAlB": "第三方推理设置：自动模型发现说明",
    "xY1EE6Ndl5": "第三方推理设置：出站要求",
    "xyS7d891o+": "隐私页：诊断报告发送说明",
    "y8c8KzJEws": "第三方推理设置：显示名称说明",
    "y3gym+4SMI": "第三方推理设置：允许的工作区文件夹",
    "y/6sGoi9YF": "第三方推理设置：连接器与扩展",
    "zUO6Ii5EAT": "第三方推理设置：添加模型",
    "zPhYdevJ+s": "第三方推理设置：工具策略说明",
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
    'defaultMessage:"Full URL of the inference gateway endpoint."': 'defaultMessage:"推理网关端点的完整 URL。"',
    'defaultMessage:"How the gateway credential is sent. Choose Bearer or x-api-key for a static key or credential-helper output; choose SSO to have each user sign in via your identity provider."': 'defaultMessage:"网关凭据的发送方式。静态密钥或凭据辅助脚本输出请选择 Bearer 或 x-api-key；如需每个用户通过身份提供商登录，请选择 SSO。"',
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


def find_frontend_bundles(app: Path) -> dict[str, Path | None]:
    assets_dir = app / FRONTEND_ASSETS_REL
    result: dict[str, Path | None] = {"index": None, "code": None}
    if not assets_dir.exists():
        return result
    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if result["index"] is None and (
            "const Jbt=({conversationUuid" in text
            or "Jbt=({models:e,currentModelOption" in text
            or ("k5=(e=\"ccr_model\"" in text and "Pht=({models:e,currentModelOption" in text)
            or ("cowork_model" in text and "currentModelOption" in text and "sticky_model_selector" in text)
        ):
            result["index"] = path
        if (
            result["code"] is None
            and (
                ('const um="ccd-effort-level' in text and "modelExtraSections:xs" in text)
                or ('const zm="ccd-effort-level' in text and "modelExtraSections:Ss" in text)
                or ("ccd-effort-level" in text and "modelExtraSections" in text)
            )
        ):
            result["code"] = path
    return result


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


def read_asar_text(app: Path, file_path: str) -> str:
    path = app / APP_ASAR_REL
    require_file(path)
    data = path.read_bytes()
    header_size, _header_string, header = read_asar_header(data, path)
    entry = get_asar_file_entry(header, file_path)
    content_offset = 8 + header_size + int(entry["offset"])
    content_size = int(entry["size"])
    return data[content_offset : content_offset + content_size].decode("utf-8", errors="ignore")


def check_developer_menu_i18n(app: Path) -> tuple[bool, str, int]:
    try:
        content = read_asar_text(app, ASAR_PATCH_TARGET)
    except Exception as exc:
        return False, f"读取 app.asar 失败：{exc}", 0
    missing: list[str] = []
    for source, target in DEV_MENU_LABEL_REPLACEMENTS.items():
        if target not in content:
            missing.append(target)
        if source in content:
            missing.append(f"仍存在英文：{source}")
    return not missing, "; ".join(missing), len(DEV_MENU_LABEL_REPLACEMENTS)


def check_custom3p_setup_i18n(app: Path) -> tuple[bool, str, int]:
    try:
        texts = [read_asar_text(app, ASAR_PATCH_TARGET)]
    except Exception as exc:
        return False, f"读取 app.asar 失败：{exc}", 0
    assets_dir = app / FRONTEND_ASSETS_REL
    if assets_dir.exists():
        for path in sorted(assets_dir.glob("*.js")):
            texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    combined = "\n".join(texts)

    failures: list[str] = []
    for source, _target in CUSTOM3P_SETUP_REPLACEMENTS.items():
        if source in combined:
            failures.append(f"仍存在英文：{source}")
    return not failures, "; ".join(failures), len(CUSTOM3P_SETUP_REPLACEMENTS)


def check_known_frontend_i18n(app: Path) -> tuple[bool, str, int]:
    i18n_dir = app / FRONTEND_I18N_REL
    en_path = i18n_dir / "en-US.json"
    zh_path = i18n_dir / f"{LANG_CODE}.json"
    if not zh_path.exists():
        return False, f"缺少 {zh_path}", 0
    try:
        zh_data = load_json(zh_path)
        en_data = load_json(en_path) if en_path.exists() else {}
    except Exception as exc:
        return False, f"读取 i18n JSON 失败：{exc}", 0

    failures: list[str] = []
    checked = 0
    for key, label in KNOWN_FRONTEND_I18N_KEYS.items():
        en_value = en_data.get(key)
        zh_value = zh_data.get(key)
        if en_value is None and zh_value is None:
            continue
        checked += 1
        if zh_value is None:
            failures.append(f"{key}({label}) 缺少 zh-CN 翻译")
            continue
        if zh_value == en_value:
            failures.append(f"{key}({label}) 仍等于 en-US 原文")
            continue
        if isinstance(zh_value, str) and not CJK_RE.search(zh_value):
            failures.append(f"{key}({label}) 不包含中文字符")
    return not failures, "; ".join(failures), checked


def require_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Missing required file: {path}")


def read_entitlements(path: Path) -> str:
    return run(["codesign", "-d", "--entitlements", "-", str(path)], check=False).stdout


def load_entitlements(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = plistlib.loads(result.stdout)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def require_virtualization_entitlement(app: Path) -> None:
    entitlements = read_entitlements(app)
    if "com.apple.security.virtualization" not in entitlements:
        raise SystemExit(
            "Claude.app does not have the required virtualization entitlement. "
            "Restore or reinstall the official Claude.app first, then run this patcher again."
        )


def quit_claude() -> None:
    run(["osascript", "-e", 'tell application "Claude" to quit'], check=False)


def active_claude_code_processes(user_home: Path) -> list[dict[str, str]]:
    """列出 Claude Desktop 拉起的 Claude Code / disclaimer 子进程。"""
    result = run(["ps", "ax", "-o", "pid=,command="], check=False)
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
        ok = (
            env_status == "passed"
            and base_match
            and (credential_mode in {"sso", "credential_helper"} or (token_match and api_key_match))
        )
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
    run(["kill", *pids], check=False)
    return len(pids)


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


def safe_runtime_model_id(model: str | None) -> str:
    """返回可传给 Claude Code CLI 的真实 provider 模型，不能是 Opus 显示别名。"""
    if not isinstance(model, str) or not model.strip():
        return "kimi-for-coding"
    lowered = model.strip().lower()
    if lowered in {SAFE_OPUS_MODEL_ID, LEGACY_1M_OPUS_MODEL_ID}:
        return "kimi-for-coding"
    return model.strip()


def patch_hardcoded_frontend_strings(
    app: Path,
    context_window: int | None = None,
    runtime_model: str | None = None,
) -> None:
    assets_dir = app / FRONTEND_ASSETS_REL
    replacements = {
        '"New task"': '"新建任务"',
        '"New session"': '"新会话"',
        '"Drag to pin"': '"拖到此处固定"',
        '"Drop here"': '"拖到此处"',
        '"Let go"': '"松开"',
        '"Recents"': '"最近使用"',
        '"View all"': '"查看全部"',
        'title:"Connection"': 'title:"连接"',
        'description:"Choose where Claude Desktop sends inference requests."': 'description:"选择 Claude Desktop 发送推理请求的位置。"',
        'title:"Sandbox & workspace"': 'title:"沙盒与工作区"',
        'title:"Connectors & extensions"': 'title:"连接器与扩展"',
        'title:"Telemetry & updates"': 'title:"遥测与更新"',
        'title:"Usage limits"': 'title:"使用限制"',
        'title:"Plugins & skills"': 'title:"插件与技能"',
        'banner:"Plugins and skills aren\'t set in this configuration. Mount plugin bundles to the folder below using your device-management tool and Cowork will load them at launch."': 'banner:"插件和技能未在此配置中设置。请使用你的设备管理工具将插件包挂载到下方文件夹，Cowork 会在启动时加载它们。"',
        'caption:"Drop plugin folders here. Read-only to the app."': 'caption:"将插件文件夹拖放到这里。应用对此目录为只读。"',
        'title:"Egress Requirements"': 'title:"出站要求"',
        'description:"Hosts your network firewall must allow, derived from your current settings. This list is read-only and updates as you make changes. Traffic is HTTPS on port 443 unless a custom port is specified (OTLP, gateway, or MCP server URLs)."': 'description:"根据当前设置推导出的、主机网络防火墙必须放行的主机。此列表为只读，并会随着你的更改自动更新。除非指定了自定义端口（OTLP、网关或 MCP 服务器 URL），否则流量均为 443 端口上的 HTTPS。"',
        'label:"macOS configuration profile"': 'label:"macOS 配置描述文件"',
        'label:"Windows registry file"': 'label:"Windows 注册表文件"',
        'label:"Plain JSON"': 'label:"纯 JSON"',
        'label:"Firewall allowlist (.txt)"': 'label:"防火墙允许列表（.txt）"',
        'label:"Copy to clipboard (redacted)"': 'label:"复制到剪贴板（已脱敏）"',
        'title:"Source"': 'title:"来源"',
        'group:"Identity & models"': 'group:"身份与模型"',
        'hint:"First entry is the picker default. Aliases like sonnet, opus accepted. Optional for gateway — when set, the picker shows exactly this list instead of /v1/models discovery. Turn on 1M context only for models your provider actually serves with the extended window."': 'hint:"第一项是选择器默认值。支持 sonnet、opus 等别名。网关可选；设置后，选择器会显示此列表，而不是通过 /v1/models 发现。仅当提供商实际支持扩展窗口时才开启 1M 上下文。"',
        'label:"Model ID"': 'label:"模型 ID"',
        'label:"Offer 1M-context variant"': 'label:"提供 1M 上下文变体"',
        'hint:"Tags telemetry events with your org so support can find them. Not used for auth."': 'hint:"为遥测事件标记你的组织，便于支持团队定位。不会用于认证。"',
        'title:"Skip login-mode chooser"': 'title:"启动时跳过登录方式选择"',
        'hint:"Go straight to this provider at launch — users won\\\'t see the option to sign in to Anthropic instead."': 'hint:"启动后直接进入这个提供商，用户将不会看到改为登录 Anthropic 的选项。"',
        'title:"Gateway base URL"': 'title:"网关基础 URL"',
        'description:"Full URL of the inference gateway endpoint."': 'description:"推理网关端点的完整地址。"',
        'title:"Gateway API key"': 'title:"网关 API 密钥"',
        'title:"Gateway auth scheme"': 'title:"网关认证方案"',
        'description:"How to send the gateway credential. \'bearer\' (default) sends Authorization: Bearer. Set \'x-api-key\' only if your gateway requires the x-api-key header instead (e.g. api.anthropic.com). Set \'sso\' to obtain the credential via the gateway\'s own browser-based sign-in (RFC 8414 discovery at `<inferenceGatewayBaseUrl>/.well-known/oauth-authorization-server` + RFC 8628 device-code grant); inferenceGatewayApiKey and inferenceCredentialHelper are not required."': 'description:"如何发送网关凭据。bearer（默认）发送 Authorization: Bearer。仅当网关要求 x-api-key 请求头时才设置为 x-api-key（例如 api.anthropic.com）。设置为 sso 时，将通过网关自己的浏览器登录获取凭据（RFC 8414 发现 + RFC 8628 设备码授权）；无需 inferenceGatewayApiKey 和 inferenceCredentialHelper。"',
        'title:"Gateway extra headers"': 'title:"网关额外请求头"',
        'description:"Extra HTTP headers sent on every inference request. JSON array of \'Name: Value\' strings."': 'description:"每次推理请求都会附带的额外 HTTP 请求头。格式为“名称: 值”字符串组成的 JSON 数组。"',
        'hint:"Bearer (default) sends Authorization: Bearer. x-api-key is for the Anthropic API directly — auto-selected when the URL is *.anthropic.com."': 'hint:"Bearer（默认）发送 Authorization: Bearer。x-api-key 用于直连 Anthropic API；当 URL 为 *.anthropic.com 时会自动选择。"',
        'hint:"Extra headers sent to the gateway, one \'Name: Value\' per entry. For tenant routing, org IDs, etc."': 'hint:"发送到网关的额外请求头，每项格式为“名称: 值”。可用于租户路由、组织 ID 等。"',
        'body:"Sent on every inference and `/v1/models` discovery request (joined into the CLI\'s `ANTHROPIC_CUSTOM_HEADERS`).\\n\\nUse this for fleet-wide constants. For per-user or per-session values, have the **credential helper script** emit JSON with a `headers` field — those are merged over these static entries (helper wins on conflict)."': 'body:"每次推理和 `/v1/models` 发现请求都会发送这些请求头（会合并到 CLI 的 `ANTHROPIC_CUSTOM_HEADERS`）。\\n\\n适合填写全局固定值。针对单个用户或会话的值，请让**凭据辅助脚本**输出包含 `headers` 字段的 JSON；这些值会覆盖此处的静态项（冲突时辅助脚本优先）。"',
        'title:"Inference provider"': 'title:"推理提供商"',
        'description:"Selects the inference backend. Setting this key activates third-party mode."': 'description:"选择推理后端。设置此项会启用第三方模式。"',
        'oGt={gateway:"Gateway",anthropic:"Anthropic API",bedrock:"Bedrock",vertex:"Vertex",foundry:"Foundry"}': 'oGt={gateway:"网关",anthropic:"Anthropic API",bedrock:"Bedrock",vertex:"Vertex",foundry:"Foundry"}',
        'title:"GCP project ID"': 'title:"GCP 项目 ID"',
        'title:"GCP region"': 'title:"GCP 区域"',
        'title:"GCP credentials file path"': 'title:"GCP 凭据文件路径"',
        'title:"Vertex OAuth client ID"': 'title:"Vertex OAuth 客户端 ID"',
        'title:"Vertex OAuth client secret"': 'title:"Vertex OAuth 客户端密钥"',
        'title:"Vertex OAuth scopes"': 'title:"Vertex OAuth 范围"',
        'title:"Vertex AI base URL"': 'title:"Vertex AI 基础 URL"',
        'title:"AWS region"': 'title:"AWS 区域"',
        'title:"AWS bearer token"': 'title:"AWS Bearer 令牌"',
        'title:"Bedrock base URL"': 'title:"Bedrock 基础 URL"',
        'title:"AWS profile name"': 'title:"AWS 配置文件名称"',
        'title:"AWS config directory"': 'title:"AWS 配置目录"',
        'title:"Bedrock service tier"': 'title:"Bedrock 服务层级"',
        'title:"Azure AI Foundry resource name"': 'title:"Azure AI Foundry 资源名称"',
        'title:"Azure AI Foundry API key"': 'title:"Azure AI Foundry API 密钥"',
        'title:"Model list"': 'title:"模型列表"',
        'body:"Auto-populate the model picker from the provider\'s model-list endpoint at launch. Turn off if the endpoint isn\'t reachable from your network, or to use a fixed list. When off, the model list below is required and must use full model IDs (aliases like sonnet/opus are resolved via discovery)."': 'body:"启动时从提供商的模型列表端点自动填充模型选择器。如果你的网络无法访问该端点，或需要使用固定模型列表，请关闭此项。关闭后，下方模型列表为必填，并且必须使用完整模型 ID（sonnet/opus 等别名会通过发现结果解析）。"',
        'title:"Managed MCP servers"': 'title:"托管的 MCP 服务器"',
        'description:\'JSON array of MCP server configs. Each entry: `name` (string, required, unique within array), `url` (https URL, required), `transport` ("http" or "sse", default "http"), `headers` (string→string map, optional, mutually exclusive with `oauth`), `headersHelper` (absolute path to local executable that prints a JSON object of HTTP headers on stdout — for rotating bearers; optional, mutually exclusive with `oauth`; merged over `headers`, helper wins on conflict. The helper runs with the app\'s launch environment, not your shell rc — read credentials from keychain/file or source them explicitly in the script), `headersHelperTtlSec` (positive integer, default 300 — re-runs the helper at most once per TTL across connection attempts), `oauth` (boolean or object, optional — `true` triggers dynamic-registration PKCE; `{"clientId":"<id>"}` skips registration and uses a pre-registered public client (register redirect URI `http://127.0.0.1:53280/callback` on it — Entra/Google accept the portless `http://127.0.0.1/callback`, but providers that match the port exactly need 53280). Optional `tenantId` (Entra Directory ID) pins the authorization server for single-tenant apps; `scope` is required when `tenantId` is set), `toolPolicy` (toolName→"allow"/"ask"/"blocked", optional — locks the per-tool approval state; unset = user controls). Connections are made from a host-side utility process and do not pass through the in-VM allowlist.\'': 'description:\'MCP 服务器配置的 JSON 数组。每项包含：`name`（字符串，必填，数组内唯一）、`url`（https URL，必填）、`transport`（"http" 或 "sse"，默认 "http"）、`headers`（字符串到字符串映射，可选，与 `oauth` 互斥）、`headersHelper`（本地可执行文件绝对路径，会向 stdout 输出 HTTP 请求头 JSON 对象，用于轮换 bearer；可选，与 `oauth` 互斥；会覆盖合并到 `headers`，冲突时辅助脚本优先）、`headersHelperTtlSec`（正整数，默认 300，在 TTL 内连接时最多重新运行一次）、`oauth`（布尔值或对象，可选）、`toolPolicy`（工具名到 "allow"/"ask"/"blocked"，可选，用于锁定每个工具的批准状态；未设置则由用户控制）。连接由主机侧工具进程发起，不经过虚拟机内允许列表。\'',
        'title:"Organization UUID"': 'title:"组织 UUID"',
        'title:"Credential helper script"': 'title:"凭据辅助脚本"',
        'description:"Absolute path to an executable that prints the inference credential to stdout. When set, the static inferenceGatewayApiKey / inferenceFoundryApiKey is optional."': 'description:"可执行文件的绝对路径，该文件会将推理凭据输出到标准输出。设置后，可不填写静态 inferenceGatewayApiKey / inferenceFoundryApiKey。"',
        'hint:"Absolute path to an executable that prints the credential."': 'hint:"输出凭据的可执行文件绝对路径。"',
    'body:\'Claude runs the executable with no arguments and reads **stdout** (trimmed). Exit code must be `0`; any output on **stderr** is logged but ignored. **Stdout must be the credential only** — no banners, prompts, or log lines.\\n\\n**Output format** — either:\\n- a single bare token (the API key / bearer token), or\\n- a JSON object `{"token": "...", "headers": {"Name": "Value", ...}}` when per-request headers are needed (gateway provider only; merged over **Gateway extra headers**, helper wins on conflict)\\n\\nResult is cached for the TTL below. On TTL expiry the helper is re-invoked transparently — no user prompt, no relaunch.\\n\\n**Typical use:** a shell script that pulls from Keychain, 1Password CLI, or an internal secret broker. Example:\\n\\n`security find-generic-password -s anthropic-api -w`\\n\\nIf this field is set, static credential fields (API key, bearer token) are ignored. The helper always wins.\'': 'body:\'Claude 会在不带参数的情况下运行该可执行文件，并读取修剪后的 **标准输出**。退出码必须为 `0`；**标准错误** 的任何输出会被记录但忽略。**标准输出必须只包含凭据**，不能有横幅、提示或日志行。\\n\\n**输出格式**二选一：\\n- 单个纯令牌（API key / bearer token），或\\n- 需要按请求附加请求头时，输出 JSON 对象 `{"token": "...", "headers": {"Name": "Value", ...}}`（仅适用于网关提供商；会与**网关额外请求头**合并，冲突时以辅助脚本为准）。\\n\\n结果会按下方 TTL 缓存。TTL 过期后会自动重新调用辅助脚本，无需用户确认，也无需重启。\\n\\n**常见用法：**通过 shell 脚本从钥匙串、1Password CLI 或内部密钥代理中读取凭据。例如：\\n\\n`security find-generic-password -s anthropic-api -w`\\n\\n设置此字段后，静态凭据字段（API key、bearer token）会被忽略，始终以辅助脚本输出为准。\'',
        'body:\'Claude runs the executable with no arguments and reads **stdout** (trimmed). Exit code must be `0`; any output on **stderr** is logged but ignored. **Stdout must contain only one of the formats below** (no banners, prompts, or log lines).\\n\\n**Output format** is either:\\n- a single bare token (the API key / bearer token), or\\n- a JSON object `{"token": "...", "headers": {"Name": "Value", ...}}` when per-request headers are needed (merged over **Custom inference headers**, helper wins on conflict)\\n\\nResult is cached for the TTL below. On TTL expiry the helper is re-invoked transparently (no user prompt, no relaunch).\\n\\n**Typical use:** a shell script that pulls from Keychain, 1Password CLI, or an internal secret broker. Example:\\n\\n`security find-generic-password -s anthropic-api -w`\\n\\nIf this field is set, static credential fields (API key, bearer token) are ignored. The helper always wins.\'': 'body:\'Claude 会在不带参数的情况下运行该可执行文件，并读取修剪后的 **标准输出**。退出码必须为 `0`；**标准错误** 的任何输出会被记录但忽略。**标准输出必须只包含下列格式之一**，不能有横幅、提示或日志行。\\n\\n**输出格式**二选一：\\n- 单个纯令牌（API key / bearer token），或\\n- 需要按请求附加请求头时，输出 JSON 对象 `{"token": "...", "headers": {"Name": "Value", ...}}`（会与**自定义推理请求头**合并，冲突时辅助脚本优先）。\\n\\n结果会按下方 TTL 缓存。TTL 过期后会自动重新调用辅助脚本，无需用户确认，也无需重启。\\n\\n**常见用法：**通过 shell 脚本从钥匙串、1Password CLI 或内部密钥代理中读取凭据。例如：\\n\\n`security find-generic-password -s anthropic-api -w`\\n\\n设置此字段后，静态凭据字段（API key、bearer token）会被忽略，始终以辅助脚本输出为准。\'',
        'title:"Credential helper TTL"': 'title:"凭据辅助脚本 TTL"',
        'description:"Helper output is cached for this many seconds. Default 3600. Re-runs at the next session start after expiry."': 'description:"辅助脚本输出缓存的秒数。默认 3600。过期后会在下一次会话开始时重新运行。"',
        'defaultMessage:"seconds"': 'defaultMessage:"秒"',
        'defaultMessage:"Helper output is cached for this many seconds. Re-runs at the next session start after expiry."': 'defaultMessage:"辅助脚本输出会缓存指定秒数；过期后会在下一次会话开始时重新运行。"',
        'title:"Allow desktop extensions"': 'title:"允许桌面扩展"',
        'description:"Permit users to install local desktop extensions (.dxt/.mcpb)."': 'description:"允许用户安装本地桌面扩展（.dxt/.mcpb）。"',
        'egressRequirementsLabel:"Desktop extensions (Python runtime)"': 'egressRequirementsLabel:"桌面扩展（Python 运行时）"',
        'title:"Show extension directory"': 'title:"显示扩展目录"',
        'description:"Show the Anthropic extension directory in the connectors UI."': 'description:"在连接器界面显示 Anthropic 扩展目录。"',
        'title:"Require signed extensions"': 'title:"要求扩展已签名"',
        'description:"Reject desktop extensions that are not signed by a trusted publisher."': 'description:"拒绝未由受信任发布者签名的桌面扩展。"',
        'title:"Allow user-added MCP servers"': 'title:"允许用户添加 MCP 服务器"',
        'description:"Permit users to add their own local (stdio) MCP servers via Developer settings. HTTP/SSE servers are managed separately. When false, only servers from the Managed MCP servers list and org-provisioned plugins are available."': 'description:"允许用户通过开发者设置添加自己的本地（stdio）MCP 服务器。HTTP/SSE 服务器会单独管理。关闭后，仅可使用托管 MCP 服务器列表和组织预配插件中的服务器。"',
        'egressRequirementsLabel:"User-added MCP (Python runtime)"': 'egressRequirementsLabel:"用户添加的 MCP（Python 运行时）"',
        'title:"Allow Claude Code tab"': 'title:"允许 Claude Code 标签页"',
        'description:"Show the Code tab (terminal-based coding sessions). Sessions run on the host, not inside the VM."': 'description:"显示 Code 标签页（基于终端的编码会话）。会话在主机上运行，而不是在虚拟机内运行。"',
        'title:"Secure VM features"': 'title:"安全虚拟机功能"',
        'title:"Require full VM sandbox"': 'title:"要求完整虚拟机沙盒"',
        'description:"Forces the agent loop, file/web tools, and plugin-bundled MCPs to run inside the VM, disabling host-loop mode."': 'description:"强制代理循环、文件/网页工具以及插件内置 MCP 在虚拟机内运行，并禁用主机循环模式。"',
        'title:"Allowed egress hosts"': 'title:"允许的出站主机"',
        'description:`Additional hostnames the Cowork sandbox may reach (web fetch, shell commands, package installs). JSON array; supports *.example.com wildcards. The inference provider host is always allowed. Set to ["*"] to disable VM-level egress filtering entirely. Common hosts to add for dependency installs (pip/npm/apt/cargo/git): ${I.join(", ")}.`': 'description:`Cowork 沙盒可访问的额外主机名（网页抓取、Shell 命令、包安装）。JSON 数组；支持 *.example.com 通配符。推理提供商主机始终允许。设置为 ["*"] 可完全禁用虚拟机级出站过滤。依赖安装（pip/npm/apt/cargo/git）常需添加的主机：${I.join(", ")}。`',
        'egressRequirementsLabel:"Tool egress (VM sandbox)"': 'egressRequirementsLabel:"工具出站（虚拟机沙盒）"',
        'banner:"Prompts, completions, and your data are never sent to Anthropic — telemetry covers crash and usage signals only."': 'banner:"提示词、补全和你的数据绝不会发送给 Anthropic；遥测只包含崩溃和使用信号。"',
        'group:"OpenTelemetry"': 'group:"开放遥测"',
        'group:"Updates"': 'group:"更新"',
        'title:"OpenTelemetry collector endpoint"': 'title:"OpenTelemetry 收集器端点"',
        'title:"OpenTelemetry resource attributes"': 'title:"OpenTelemetry 资源属性"',
        'description:"Base URL of an OpenTelemetry collector. When set, Cowork sessions export logs and metrics (prompts, tool calls, token counts) to this endpoint."': 'description:"OpenTelemetry 收集器的基础 URL。设置后，Cowork 会话会将日志和指标（提示词、工具调用、令牌计数）导出到此端点。"',
        'description:"Extra OTEL resource attributes as comma-separated key=value pairs (the standard OTEL_RESOURCE_ATTRIBUTES format). Appended to the app\'s built-in attributes; keys that collide with built-ins (e.g. service.name) are dropped. Scoped for bootstrap so per-user values can be returned at sign-in."': 'description:"额外的 OTEL 资源属性，以逗号分隔的 key=value 对填写（标准 OTEL_RESOURCE_ATTRIBUTES 格式）。会追加到应用内置属性；与内置属性冲突的键（如 service.name）会被丢弃。用于 bootstrap 时可在登录时返回按用户设置的值。"',
        'title:"Block essential telemetry"': 'title:"阻止基础遥测"',
        'description:"Blocks crash and error reports (stack traces, app state at failure, device/OS info) and performance timing data sent to Anthropic. Used to investigate bugs and monitor responsiveness."': 'description:"阻止发送给 Anthropic 的崩溃和错误报告（堆栈跟踪、故障时应用状态、设备/系统信息）以及性能计时数据。这些数据用于调查错误并监控响应性。"',
        'title:"Block nonessential telemetry"': 'title:"阻止非必要遥测"',
        'description:"Blocks product-usage analytics sent to Anthropic — feature usage, navigation patterns, UI actions."': 'description:"阻止发送给 Anthropic 的产品使用分析，包括功能使用、导航模式和界面操作。"',
        'title:"Block nonessential services"': 'title:"阻止非必要服务"',
        'description:"Blocks connector favicons (fetched from a third-party favicon service — leaks MCP hostnames) and the artifact-preview sandbox iframe. Connectors fall back to letter icons; artifacts do not render."': 'description:"阻止连接器网站图标（从第三方图标服务获取，可能泄露 MCP 主机名）和 artifact 预览沙盒 iframe。连接器会回退为字母图标，artifact 将无法渲染。"',
        'title:"Auto-update enforcement window"': 'title:"自动更新强制窗口"',
        'description:"When set, forces a pending update to install after this many hours regardless of user activity. When unset, the app uses a 72-hour window but defers installation while the user is active."': 'description:"设置后，无论用户是否正在使用，待处理更新都会在指定小时后强制安装。未设置时，应用使用 72 小时窗口，但会在用户活跃时延后安装。"',
        'title:"Block auto-updates"': 'title:"阻止自动更新"',
        'description:"Blocks the app from checking for and downloading updates from Anthropic. The app will stay on its installed version until updated by other means."': 'description:"阻止应用检查并下载来自 Anthropic 的更新。应用会保持当前已安装版本，直到通过其他方式更新。"',
        'suffix:"hours"': 'suffix:"小时"',
        'title:"Disable essential telemetry"': 'title:"禁用基础遥测"',
        'description:"Disable essential crash and performance telemetry."': 'description:"禁用基础崩溃和性能遥测。"',
        'title:"Disable auto updates"': 'title:"禁用自动更新"',
        'description:"Prevent Claude Desktop from checking for updates automatically."': 'description:"阻止 Claude Desktop 自动检查更新。"',
        'title:"Daily message limit"': 'title:"每日消息限制"',
        'description:"Maximum number of messages a user can send per day."': 'description:"用户每天可发送的最大消息数。"',
        'title:"Max tokens per window"': 'title:"每窗口最大令牌数"',
        'description:"Total input+output tokens permitted per window before further messages are refused. Unset = no cap."': 'description:"每个窗口允许的输入和输出令牌总数；超过后将拒绝继续发送消息。未设置表示不限制。"',
        'title:"Token cap window"': 'title:"令牌限制窗口"',
        'description:"Tumbling window length for the token cap. Max 720 hours (30 days). The counter resets at the end of each window."': 'description:"令牌限制的滚动窗口长度。最大 720 小时（30 天）。每个窗口结束时计数器会重置。"',
        'hint:"Crash and performance reports to Anthropic."': 'hint:"将崩溃和性能报告发送给 Anthropic。"',
        'hint:"Product-usage analytics and diagnostic-report uploads. No message content."': 'hint:"产品使用分析和诊断报告上传。不包含消息内容。"',
        'hint:"Favicon fetch and the artifact-preview iframe origin. Artifacts will not render."': 'hint:"网站图标获取和 artifact 预览 iframe 的来源。Artifacts 将无法渲染。"',
        'hint:"Stop Cowork from fetching updates. You\'ll need to push new versions yourself."': 'hint:"阻止 Cowork 获取更新。后续新版本需要由你自行推送。"',
        'hint:"Hours before a downloaded update force-installs. Blank = 72-hour default."': 'hint:"已下载更新会在多少小时后强制安装。留空则使用默认的 72 小时。"',
        'hint:"Where Cowork sends OpenTelemetry logs and metrics. Leave blank to disable."': 'hint:"Cowork 会将 OpenTelemetry 日志和指标发送到哪里。留空表示禁用。"',
        'hint:"grpc or http/protobuf."': 'hint:"支持 grpc 或 http/protobuf。"',
        'hint:"Optional auth headers for the collector."': 'hint:"发送给收集器的可选认证请求头。"',
        'hint:"Extra resource attributes to attach to every span/metric, e.g. enduser.id=alice@example.com."': 'hint:"附加到每个 span/metric 的额外资源属性，例如 enduser.id=alice@example.com。"',
        'hint:"Per-user soft cap, counted client-side over the duration below. Not a server-enforced quota."': 'hint:"按用户设置的软限制，在下方时长范围内由客户端统计。不是服务器强制执行的配额。"',
        'reason:"Security and compatibility fixes will not install automatically. Make sure IT has another distribution path."': 'reason:"安全和兼容性修复不会自动安装。请确保 IT 有其他分发路径。"',
        'reason:"Usage analytics help us prioritize improvements for third-party inference. Diagnostic-report uploads will also be blocked. No message content is included in either."': 'reason:"使用分析可帮助我们优先改进第三方推理。诊断报告上传也会被阻止。两者都不包含消息内容。"',
        'reason:"This disables artifact previews and connector icons. Artifacts will not render in conversations."': 'reason:"这会禁用 artifact 预览和连接器图标。Artifact 将不会在对话中渲染。"',
        'body:"\\"Essential\\" means the signals Anthropic needs to keep your deployment working: **crash stacks**, **startup failure reasons**, and **version/OS metadata**. No prompts, completions, file contents, or identifiers beyond a random install ID.\\n\\n**What you lose when this is on:** when a Cowork build hits a bug that only reproduces on your OS version or locale, Anthropic can\'t see it unless a user manually reports. Fixes ship slower.\\n\\n**Why this is discouraged, not blocked:** some air-gapped environments require zero outbound telemetry as a matter of policy. The switch exists for them — if you don\'t have that constraint, leave it off."': 'body:"\\"基础\\"是指 Anthropic 为保持你的部署正常运行所需的信号：**崩溃堆栈**、**启动失败原因**以及**版本/系统元数据**。不包含提示词、补全、文件内容，也不包含随机安装 ID 之外的标识符。\\n\\n**开启后会失去什么：**当 Cowork 构建遇到只在你的系统版本或区域设置上复现的问题时，除非用户手动报告，否则 Anthropic 无法看到，修复发布会更慢。\\n\\n**为什么这是不推荐而不是禁止：**某些隔离网络环境因策略要求零出站遥测。此开关就是为这些环境准备的；如果你没有这类约束，请保持关闭。"',
        'body:\'"Nonessential" covers two things: **product-usage analytics** (which features get used, navigation patterns — no prompts or completions) and the **Send** action in Help → Generate Diagnostic Report. Turning this on stops both.\\n\\nDestination for both: `claude.ai`. Already listed under Egress Requirements → Nonessential telemetry.\'': 'body:\'"非必要"包括两类内容：**产品使用分析**（使用了哪些功能、导航模式；不包含提示词或补全）以及「帮助 → 生成诊断报告」中的**发送**操作。开启后会同时停止两者。\\n\\n两者的目标地址都是 `claude.ai`，已列在「出站要求 → 非必要遥测」下。\'',
        'title:"Disabled built-in tools"': 'title:"禁用内置工具"',
        'description:\'JSON array of tool names to remove from the agent tool list (e.g. ["WebSearch"]).\'': 'description:\'要从代理工具列表中移除的工具名称 JSON 数组（例如 ["WebSearch"]）。\'',
        'title:"Allowed workspace folders"': 'title:"允许的工作区文件夹"',
        'description:"JSON array of absolute paths the user may attach as workspace folders. A leading ~ expands to the per-user home directory. Unset means unrestricted."': 'description:"用户可附加为工作区文件夹的绝对路径 JSON 数组。开头的 ~ 会展开为对应用户的主目录。未设置表示不限制。"',
        'hint:"Domains Cowork\'s tools may reach during a turn. Also surfaced under Egress Requirements."': 'hint:"Cowork 工具在一次回合中可访问的域名。也会显示在出站要求中。"',
        'body:"Only affects **tool calls** — inference and MCP traffic are covered by their own allowlists elsewhere.\\n\\nAccepts exact hostnames (`api.github.com`), wildcards (`*.corp.com` matches one subdomain level), and `*` to allow all.\\n\\nWildcards don\'t cross schemes. `*.corp.com` matches `docs.corp.com` but not `corp.com` itself — add both if you need the apex.\\n\\nIP literals and localhost always resolve regardless of this list; this is a public-egress filter, not a sandbox.\\n\\nHosts you add here also need to be open on your network firewall — see **Egress Requirements** for the full allowlist."': 'body:"仅影响**工具调用**；推理和 MCP 流量由其他位置各自的允许列表控制。\\n\\n支持精确主机名（`api.github.com`）、通配符（`*.corp.com` 匹配一级子域）以及用于允许全部的 `*`。\\n\\n通配符不会跨层级匹配。`*.corp.com` 会匹配 `docs.corp.com`，但不匹配 `corp.com` 本身；如需顶级域，请同时添加两者。\\n\\n无论此列表如何设置，IP 字面量和 localhost 始终可解析；这是公共出站过滤器，不是沙盒。\\n\\n你在此处添加的主机也需要在网络防火墙中放行；完整允许列表请参见**出站要求**。"',
        'hint:"Folders users may attach as a workspace. Leave unset for unrestricted access."': 'hint:"用户可附加为工作区的文件夹。留空表示不限制访问。"',
        'hint:"Built-in tools removed from Cowork."': 'hint:"从 Cowork 中移除的内置工具。"',
        'group:"Extensions"': 'group:"扩展"',
        'group:"MCP servers"': 'group:"MCP 服务器"',
    'group:"Anthropic telemetry"': 'group:"Anthropic 遥测"',
    'defaultMessage:"Allow Claude Code tab"': 'defaultMessage:"允许 Claude Code 标签页"',
    'defaultMessage:"Show the Code tab (terminal-based coding sessions). Sessions run on the host, not inside the VM."': 'defaultMessage:"显示 Code 标签页（基于终端的编码会话）。会话在主机上运行，而不是在虚拟机内运行。"',
    'defaultMessage:"Allowed egress hosts"': 'defaultMessage:"允许的出站主机"',
    "defaultMessage:\"Domains Cowork's tools may reach during a turn. Also surfaced under Egress Requirements.\"": 'defaultMessage:"Cowork 工具在一次回合中可访问的域名，也会显示在出站要求中。"',
    'defaultMessage:"Allowed workspace folders"': 'defaultMessage:"允许的工作区文件夹"',
    'defaultMessage:"Folders users may attach as a workspace. Leave unset for unrestricted access."': 'defaultMessage:"用户可附加为工作区的文件夹。留空表示不限制访问。"',
    'defaultMessage:"Disabled built-in tools"': 'defaultMessage:"禁用内置工具"',
    'defaultMessage:"Built-in tools removed from Cowork."': 'defaultMessage:"从 Cowork 中移除的内置工具。"',
    'defaultMessage:"Built-in tool policy"': 'defaultMessage:"内置工具策略"',
    'defaultMessage:"Per-tool approval policy: “ask” requires user approval before each call; “allow” is the default. Use Disabled built-in tools to remove a tool entirely."': 'defaultMessage:"按工具设置审批策略：“ask” 表示每次调用前都需要用户批准；“allow” 为默认值。使用“禁用的内置工具”可完全移除某个工具。"',
    "defaultMessage:'Per-tool approval policy. \"ask\" requires user approval before each call; \"allow\" is the default. Use Disabled built-in tools to remove a tool entirely.'": "defaultMessage:'按工具设置审批策略。“ask” 表示每次调用前都需要用户批准；“allow” 为默认值。使用“禁用的内置工具”可完全移除某个工具。'",
    'defaultMessage:"Add policy"': 'defaultMessage:"添加策略"',
    'defaultMessage:"Disable Claude.ai sign-in"': 'defaultMessage:"禁用 Claude.ai 登录"',
    'defaultMessage:"Disable claude:// deep-link handling"': 'defaultMessage:"禁用 claude:// 深度链接处理"',
    'defaultMessage:"Stop external apps and websites from opening Cowork via claude:// links."': 'defaultMessage:"阻止外部应用和网站通过 claude:// 链接打开 Cowork。"',
    'defaultMessage:"Prompts, completions, and your data are never sent to Anthropic. Telemetry covers crash and usage signals only."': 'defaultMessage:"提示词、补全和你的数据绝不会发送给 Anthropic。遥测仅包含崩溃和使用信号。"',
    'defaultMessage:"Anthropic telemetry"': 'defaultMessage:"Anthropic 遥测"',
    'defaultMessage:"Organization UUID"': 'defaultMessage:"组织 UUID"',
    "defaultMessage:\"Tags telemetry events with your organization's UUID so Anthropic support can find them. Not used for auth.\"": 'defaultMessage:"用你的组织 UUID 标记遥测事件，方便 Anthropic 支持团队定位问题。不会用于认证。"',
    'defaultMessage:"Block essential telemetry"': 'defaultMessage:"阻止基础遥测"',
    'defaultMessage:"Crash and performance reports to Anthropic."': 'defaultMessage:"发送给 Anthropic 的崩溃和性能报告。"',
    'defaultMessage:"Block nonessential telemetry"': 'defaultMessage:"阻止非必要遥测"',
    'defaultMessage:"Product-usage analytics and diagnostic-report uploads. No message content."': 'defaultMessage:"产品使用分析和诊断报告上传。不包含消息内容。"',
    'defaultMessage:"Block nonessential services"': 'defaultMessage:"阻止非必要服务"',
    'defaultMessage:"Favicon fetch and the artifact-preview iframe origin. Artifacts will not render."': 'defaultMessage:"网站图标获取和 Artifact 预览 iframe 来源。Artifact 将无法渲染。"',
    'defaultMessage:"OpenTelemetry"': 'defaultMessage:"开放遥测"',
    'defaultMessage:"OpenTelemetry collector endpoint"': 'defaultMessage:"OpenTelemetry 收集器端点"',
    'defaultMessage:"Where Cowork sends OpenTelemetry logs and metrics. Leave blank to disable."': 'defaultMessage:"Cowork 会将 OpenTelemetry 日志和指标发送到哪里。留空表示禁用。"',
    'defaultMessage:"Updates"': 'defaultMessage:"更新"',
    'defaultMessage:"Block auto-updates"': 'defaultMessage:"阻止自动更新"',
    'defaultMessage:"Stop Cowork from fetching updates. You’ll need to push new versions yourself."': 'defaultMessage:"阻止 Cowork 获取更新。后续新版本需要由你自行推送。"',
    'defaultMessage:"Security and compatibility fixes will not install automatically. Make sure IT has another distribution path."': 'defaultMessage:"安全和兼容性修复不会自动安装。请确保 IT 有其他分发路径。"',
    'defaultMessage:"Auto-update enforcement window"': 'defaultMessage:"自动更新强制窗口"',
    'defaultMessage:"Hours before a downloaded update force-installs. Blank = 72-hour default."': 'defaultMessage:"已下载更新会在多少小时后强制安装。留空则使用默认的 72 小时。"',
    'defaultMessage:"Max tokens per window"': 'defaultMessage:"每窗口最大令牌数"',
    'defaultMessage:"Per-user soft cap, counted client-side over the duration below. Not a server-enforced quota."': 'defaultMessage:"按用户设置的软限制，在下方时长范围内由客户端统计。不是服务器强制执行的配额。"',
    'defaultMessage:"tokens"': 'defaultMessage:"令牌"',
    'defaultMessage:"Hosts your network firewall must allow, derived from your current settings. This list is read-only and updates as you make changes. Traffic is HTTPS on port 443 unless a custom port is specified (OTLP, gateway, or MCP server URLs)."': 'defaultMessage:"你的网络防火墙必须允许的主机，会根据当前设置生成。此列表为只读，并会随设置变化自动更新。除非配置了自定义端口（OTLP、网关或 MCP 服务器 URL），否则流量均为 443 端口上的 HTTPS。"',
    'defaultMessage:"Firewall allowlist (.txt)"': 'defaultMessage:"防火墙允许列表（.txt）"',
    'defaultMessage:"Copy hostnames"': 'defaultMessage:"复制主机名"',
    'defaultMessage:"Download .txt"': 'defaultMessage:"下载 .txt"',
    'hint:".dxt and .mcpb installs."': 'hint:".dxt 和 .mcpb 安装。"',
        'hint:"The in-app catalogue of installable extensions. Hide to allow sideload only."': 'hint:"应用内可安装扩展目录。隐藏后仅允许侧载。"',
        'hint:"Local stdio servers added via the Developer settings. Remote servers come from the managed list above, or plugins mounted to a user\'s computer by an organization admin."': 'hint:"通过开发者设置添加的本地 stdio 服务器。远程服务器来自上方托管列表，或来自组织管理员挂载到用户电脑的插件。"',
        'hint:"Org-pushed remote MCP servers. May embed bearer tokens."': 'hint:"组织推送的远程 MCP 服务器。可能嵌入 Bearer 令牌。"',
        'label:"Name"': 'label:"名称"',
        'label:"Transport"': 'label:"传输方式"',
        'label:"Headers"': 'label:"请求头"',
        'label:"Headers helper script"': 'label:"请求头辅助脚本"',
        'label:"Helper cache TTL (sec)"': 'label:"辅助缓存 TTL（秒）"',
        'placeholder:"Absolute path"': 'placeholder:"绝对路径"',
        '["active","Active"]': '["active","活跃"]',
        '["archived","Archived"]': '["archived","已归档"]',
        '["all","All"]': '["all","全部"]',
        'jl="Local"': 'jl="本地"',
        'Cl="Cloud"': 'Cl="云端"',
        'Ml="Remote Control"': 'Ml="远程控制"',
        'Il="All"': 'Il="全部"',
        '["alpha","Alphabetically"]': '["alpha","按字母"]',
        '["created","Created time"]': '["created","创建时间"]',
        '["recency","Recency"]': '["recency","最近使用"]',
        '["1","1d"]': '["1","1天"]',
        '["3","3d"]': '["3","3天"]',
        '["7","7d"]': '["7","7天"]',
        '["30","30d"]': '["30","30天"]',
        '["0","All"]': '["0","全部"]',
        '["date","Date"]': '["date","日期"]',
        '["project","Project"]': '["project","项目"]',
        '["state","State"]': '["state","状态"]',
        '["environment","Environment"]': '["environment","环境"]',
        '["none","None"]': '["none","无"]',
        'label:"Status"': 'label:"状态"',
        'label:"Environment"': 'label:"环境"',
        'label:"Last activity"': 'label:"上次活动"',
        'label:"Group by"': 'label:"分组方式"',
        'label:"Sort by"': 'label:"排序方式"',
        'children:"Project"': 'children:"项目"',
        'children:"All projects"': 'children:"全部项目"',
        'children:"Clear filters"': 'children:"清除筛选"',
        '0===e.length?"All":': '0===e.length?"全部":',
        '`${e.length} selected`': '`${e.length} 项已选`',
        'children:"Batch archive…"': 'children:"批量归档…"',
        'children:"Batch delete…"': 'children:"批量删除…"',
        'children:"Sign out"': 'children:"退出登录"',
        'defaultMessage:"Theme"': 'defaultMessage:"主题"',
        'defaultMessage:"Match system"': 'defaultMessage:"跟随系统"',
        'defaultMessage:"Font"': 'defaultMessage:"字体"',
        'defaultMessage:"Anthropic Sans"': 'defaultMessage:"Anthropic 无衬线体"',
        'defaultMessage:"Effort"': 'defaultMessage:"强度"',
        'defaultMessage:"Transcript view"': 'defaultMessage:"思考模式"',
        'defaultMessage:"Transcript view mode"': 'defaultMessage:"思考模式"',
        'defaultMessage:"Connectors have moved to <link>Customize</link>."': 'defaultMessage:"连接器已移至<link>自定义</link>。"',
        'defaultMessage:"Skills have moved to <link>Customize</link>."': 'defaultMessage:"技能已移至<link>自定义</link>。"',
        'defaultMessage:"Generate code, documents, and designs in a dedicated window alongside your conversation."': 'defaultMessage:"在对话旁的专用窗口中生成代码、文档和设计。"',
        'defaultMessage:"Get notified when Claude has finished a response. Useful for long-running tasks."': 'defaultMessage:"Claude 完成响应后通知你，适合长时间运行的任务。"',
        'defaultMessage:"Artifacts"': 'defaultMessage:"创作物"',
        'defaultMessage:"Settings default model not recognized"': 'defaultMessage:"设置中的默认模型无法识别"',
        'Ld("cc-landing-draft-permission-mode","acceptEdits")': 'Ld("cc-landing-draft-permission-mode-cn","bypassPermissions")',
        'Mi("cc-landing-draft-permission-mode","acceptEdits",!1)': 'Mi("cc-landing-draft-permission-mode-cn","bypassPermissions",!1)',
        'fc("cc-landing-draft-permission-mode","acceptEdits")': 'fc("cc-landing-draft-permission-mode-cn","bypassPermissions")',
        'Ks("cc-landing-draft-permission-mode","acceptEdits",!1)': 'Ks("cc-landing-draft-permission-mode-cn","bypassPermissions",!1)',
        'Ld("epitaxy-folder-permission-mode",Kp,{scope:"account"})': 'Ld("epitaxy-folder-permission-mode-cn",Kp,{scope:"account"})',
        'fc("epitaxy-folder-permission-mode",Rm,{scope:"account"})': 'fc("epitaxy-folder-permission-mode-cn",Rm,{scope:"account"})',
        'yc("baku_model","model","claude-sonnet-4-6",l())': 'yc("baku_model","model","opus[1m]",l())',
        'c=yc(e,"model","claude-sonnet-4-5-20250929",l()),': 'c=(e=>e==="kimi-for-coding"?"opus[1m]":e)(yc(e,"model","opus[1m]",l())),',
        'i=yc("baku_model","model","opus[1m]",l()),o=$u(()=>null,null)||i;': 'i=yc("baku_model","model","opus[1m]",l()),o=(e=>e==="kimi-for-coding"?"opus[1m]":e)($u(()=>null,null)||i);',
        'R=N,O=I': 'R=(e=>e==="kimi-for-coding"?"opus[1m]":e)(N),O=I',
        'F=z?.sessionData?.session_context?.model??null,': 'F=(e=>e==="kimi-for-coding"?"opus[1m]":e)(z?.sessionData?.session_context?.model??null),',
        'return t.sessionModel??t.sessionData?.session_context?.model})??null': 'return(e=>e==="kimi-for-coding"?"opus[1m]":e)(t.sessionModel??t.sessionData?.session_context?.model)})??null',
        'const n=s.find(t=>t.model===e),r=(n?.thinking_modes??[]).map': 'const n=s.find(t=>t.model===e)??(("opus"===e||"opus[1m]"===e)?s.find(e=>"opus[1m]"===e.model)??s.find(e=>/opus/i.test(e.model)&&/\\[1m\\]/i.test(e.model))??s.find(e=>e.thinking_modes?.length):void 0),r=(n?.thinking_modes??[]).map',
        'W||(W=F.find(e=>e.model===L)??sgt);': 'W||(W=F.find(e=>e.model===L)??(("opus"===V||"opus[1m]"===V||"opus"===L||"opus[1m]"===L)?{model:"opus[1m]",name:"Opus 4.71M",inactive:!1,overflow:!1}:sgt));',
        'W||(W=F.find(e=>e.model===L)??(("opus"===V||"opus[1m]"===V||"opus"===L||"opus[1m]"===L)?{model:"opus[1m]",name:"Opus 4.71M",inactive:!1,overflow:!1}:sgt));const G=': 'W||(W=F.find(e=>e.model===L)??(("opus"===V||"opus[1m]"===V||"opus"===L||"opus[1m]"===L)?{model:"opus[1m]",name:"Opus 4.71M",inactive:!1,overflow:!1}:sgt));(\"\"===Vft(W)||\"opus\"===V||\"opus[1m]\"===V||\"opus\"===L||\"opus[1m]\"===L)&&(W={...W,model:\"opus[1m]\",name:\"Opus 4.71M\",inactive:!1,overflow:!1});const G=',
        '""===Vft(W)&&(W={model:"opus[1m]",name:"Opus 4.7 1M",inactive:!1,overflow:!1});const G=': '(""===Vft(W)||"opus"===V||"opus[1m]"===V||"opus"===L||"opus[1m]"===L)&&(W={...W,model:"opus[1m]",name:"Opus 4.71M",inactive:!1,overflow:!1});const G=',
        'z=r??A,{allModelOptions:F,mainModels:U,overflowModels:q}=R': 'z=(e=>e==="kimi-for-coding"?"opus[1m]":e)(r??A),{allModelOptions:F,mainModels:U,overflowModels:q}=R',
        '{activeMode:te}=Gft(z,Z),se=O?void 0:te?.label,{toggleConversationSetting:ne}=O6({source:"modelSelector"})': '{activeMode:te}=Gft(z,Z),[me,he]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),fe=n.useMemo(()=>_??{current:me,options:[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],onSelect:e=>{he(e);try{localStorage.setItem("cowork_effort_level_cn",e),window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}}},[_,me]),se=O?{low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}[me]:te?.label,{toggleConversationSetting:ne}=O6({source:"modelSelector"})',
        '_&&a.jsxs(a.Fragment,{children:[a.jsx(ol,{className:IR}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),a.jsx(igt,{section:_,compactMenu:j})]})': 'fe&&a.jsxs(a.Fragment,{children:[a.jsx(ol,{className:IR}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),a.jsx(igt,{section:fe,compactMenu:j})]})',
        'fe&&a.jsxs(a.Fragment,{children:[a.jsx(ol,{className:IR}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),a.jsx(igt,{section:fe,compactMenu:j})]})': 'a.jsxs(a.Fragment,{children:[a.jsx(ol,{className:IR}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),a.jsx(igt,{section:{current:me,options:[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],onSelect:e=>{he(e);try{localStorage.setItem("cowork_effort_level_cn",e),window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}}},compactMenu:j})]})',
        'const Wft=({model:e,compact:t=!1,thinkingLabel:s})=>{const n=Vft(e,{mutedSuffix:!0});return': 'const Wft=({model:e,compact:t=!1,thinkingLabel:s})=>{let n=Vft(e,{mutedSuffix:!0});""===n&&(n="Opus 4.71M");return',
        'function Vft(e,t={}){const s=e.model?Z9(e.model):null;': 'function Vft(e,t={}){if("opus[1m]"===e?.model||"opus"===e?.model)return"Opus 4.71M";const s=e.model?Z9(e.model):null;',
        'j=pc("cowork_effort_level","medium",Wu),M=pc("cowork_model",vJt,yJt),': 'j=(()=>{const e=pc("cowork_effort_level_cn","max",Wu),[t,s]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||e}catch{return e}});return n.useEffect(()=>{const e=()=>{try{s(localStorage.getItem("cowork_effort_level_cn")||"max")}catch{s("max")}};if("undefined"==typeof window)return;e();return window.addEventListener("cowork-effort-change",e),()=>window.removeEventListener("cowork-effort-change",e)},[]),t})(),M=pc("cowork_model",vJt,yJt),',
        '"Scheduled"': '"定时任务"',
        '"Pinned"': '"已固定"',
        '"What’s up next?"': '"接下来做什么？"',
        '"Let\'s knock something off your list"': '"先把清单上的一件事做完"',
        'label:"Projects"': 'label:"项目"',
        'label:"Scheduled"': 'label:"计划任务"',
        'label:"Customize"': 'label:"自定义"',
    }
    replacements.update(CUSTOM3P_SETUP_REPLACEMENTS)
    patched_files = 0
    patched_strings = 0

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched = text
        count = 0
        for source, target in replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        health_guard_source = (
            'y=function(e){if(!e)return"";try{return new URL(e).host}catch{return e}}'
            '(d?.endpoint)||s.formatMessage({defaultMessage:"the provider endpoint",id:"lI6dUVuWZk"}),v=n.useCallback'
        )
        health_guard_target = (
            'y=function(e){if(!e)return"";try{return new URL(e).host}catch{return e}}'
            '(d?.endpoint)||s.formatMessage({defaultMessage:"the provider endpoint",id:"lI6dUVuWZk"});'
            'if(!g&&("gateway"===String(x).toLowerCase()||/api\\.kimi\\.com(?:\\/coding)?/i.test(String(d?.endpoint??d?.requestUrl??"")))'
            '&&(d?.state===eG.InvalidConfig||d?.state===eG.Unreachable))return null;'
            'v=n.useCallback'
        )
        occurrences = patched.count(health_guard_source)
        if occurrences:
            patched = patched.replace(health_guard_source, health_guard_target)
            count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    model_alias_re = re.compile(
        r'function eee\(e,t\)\{if\(!e\)return null;if\(t\.includes\(e\)\)return e;'
        r'(?:if\(\(e==="opus"\|\|e==="opus\[1m\]"\)&&(?:t\.includes\("kimi-for-coding"\)|t\.length>0)\)return\s*(?:"kimi-for-coding"|e);)*'
    )
    model_alias_target = (
        'function eee(e,t){if(!e)return null;if(t.includes(e))return e;'
        'if((e==="opus"||e==="opus[1m]")&&t.length>0)return e;'
    )
    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched, count = model_alias_re.subn(model_alias_target, text)
        if count and patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    pinned_opus_re = re.compile(
        r'(let e=\(e=>\{if\(0===d\.length\)return e;const t=new Set\(e\.map\(e=>e\.model\)\),s=\[\];'
        r'for\(const n of e\)\{if\(s\.push\(n\),!d\.includes\(n\.model\)\)continue;'
        r'const e=`\$\{n\.model\}\[1m\]`;t\.has\(e\)\|\|s\.push\(\{\.\.\.n,model:e,'
        r'name:i\.formatMessage\(\{defaultMessage:"\{modelName\} \(1M context\)",id:"4jU30\+bnSv"\},'
        r'\{modelName:n\.name\}\),name_i18n_key:void 0\}\)\}return s\}\)\(s\)\.filter\(e=>o\.includes\(e\.model\)\)'
        r'\.map\(e=>e\.inactive\?\{\.\.\.e,inactive:!1\}:e\);)'
        r'.*?const n=e\.some\(e=>e\.model===c\);',
        re.DOTALL,
    )
    pinned_opus_target = (
        'if(s.length>0&&!e.some(e=>"opus[1m]"===e.model)){'
        'const l=s.find(e=>"opus[1m]"===e.model)??s.find(e=>/opus/i.test(e.model)&&/\\[1m\\]/i.test(e.model))??'
        's.find(e=>e.thinking_modes?.length)??s[0];'
        'e=[{...l,model:"opus[1m]",name:"Opus 4.71M",name_i18n_key:void 0,inactive:!1,overflow:!1},...e]}'
        'if(c&&!e.some(e=>e.model===c)){const m=s.find(e=>e.model===c)??s.find(e=>e.thinking_modes?.length)??s[0];'
        'e=[{...m,model:c,name:m?.name??tee(c),name_i18n_key:void 0,inactive:!1,overflow:!1},...e]}'
        'const n=e.some(e=>e.model===c);'
    )
    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched, count = pinned_opus_re.subn(lambda match: match.group(1) + pinned_opus_target, text)
        if count and patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    baku_opus_re = re.compile(
        r'(let c=e\.filter\(e=>a\.includes\(e\.model\)\)\.map\(e=>e\.inactive\?\{\.\.\.e,inactive:!1\}:e\);)'
        r'.*?const d=c\.some\(e=>e\.model===o\);',
        re.DOTALL,
    )
    baku_opus_target = (
        'if(e.length>0&&!c.some(e=>"opus[1m]"===e.model)){'
        'const n=e.find(e=>"opus[1m]"===e.model)??e.find(e=>/opus/i.test(e.model)&&/\\[1m\\]/i.test(e.model))??'
        'e.find(e=>e.thinking_modes?.length)??e[0];'
        'c=[{...n,model:"opus[1m]",name:"Opus 4.71M",name_i18n_key:void 0,inactive:!1,overflow:!1},...c]}'
        'if(o&&!c.some(e=>e.model===o)){const d=e.find(e=>e.model===o)??e.find(e=>e.thinking_modes?.length)??e[0];'
        'c=[{...d,model:o,name:d?.name??tee(o),name_i18n_key:void 0,inactive:!1,overflow:!1},...c]}'
        'const d=c.some(e=>e.model===o);'
    )
    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched, count = baku_opus_re.subn(lambda match: match.group(1) + baku_opus_target, text)
        if count and patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    epitaxy_files, epitaxy_count = patch_epitaxy_model_menu(assets_dir, runtime_model)
    patched_files += epitaxy_files
    patched_strings += epitaxy_count
    cowork_files, cowork_count = patch_cowork_model_menu(assets_dir, runtime_model)
    patched_files += cowork_files
    patched_strings += cowork_count
    cache_files, cache_count = patch_epitaxy_cache_bust(app / "Contents/Resources/ion-dist", assets_dir)
    patched_files += cache_files
    patched_strings += cache_count
    health_files, health_count = patch_kimi_gateway_health_banner(assets_dir)
    patched_files += health_files
    patched_strings += health_count
    permission_files, permission_count = patch_permission_defaults(assets_dir)
    patched_files += permission_files
    patched_strings += permission_count
    safe_opus_files, safe_opus_count = patch_safe_opus_context(assets_dir)
    patched_files += safe_opus_files
    patched_strings += safe_opus_count
    context_usage_files, context_usage_count = patch_context_usage_percent(assets_dir, context_window)
    patched_files += context_usage_files
    patched_strings += context_usage_count

    print(f"Patched hardcoded frontend strings: {patched_strings} replacements in {patched_files} files")


def patch_permission_defaults(assets_dir: Path) -> tuple[int, int]:
    """把 Code 新建会话权限默认值固定为绕过权限，并隔离旧 localStorage。"""
    regex_replacements: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r'\b(?P<fn>Ld|fc|Ic|Rp)\("cc-landing-draft-permission-mode","acceptEdits"\)'),
            r'\g<fn>("cc-landing-draft-permission-mode-cn","bypassPermissions")',
        ),
        (
            re.compile(r'\b(?P<fn>Mi|Ks|Ws|Rp)\("cc-landing-draft-permission-mode","acceptEdits",!1\)'),
            r'\g<fn>("cc-landing-draft-permission-mode-cn","bypassPermissions",!1)',
        ),
        (
            re.compile(
                r'\b(?P<fn>Ld|fc|Ic|Rp)\("epitaxy-folder-permission-mode",'
                r'(?P<default>[A-Za-z_$][\w$]*),\{scope:"account"\}\)'
            ),
            r'\g<fn>("epitaxy-folder-permission-mode-cn",\g<default>,{scope:"account"})',
        ),
    ]
    literal_replacements = {
        'const e=en??Zs??Gs??$s;return sn?wt(e,Os):e': (
            'const e=en??Zs??$s??Gs??"bypassPermissions";return sn?wt(e,Os):e'
        ),
        'const e=en??Zs??$s??Gs??"bypassPermissions";return sn?wt(e,Os):e': (
            'const e=en??Zs??$s??Gs??"bypassPermissions";return sn?wt(e,Os):e'
        ),
        'const e=dn??cn??nn??Qs;return fn?jt(e,Gs):e': (
            'const e=dn??cn??Qs??nn??"bypassPermissions";return fn?jt(e,Gs):e'
        ),
        'const e=dn??cn??Qs??nn??"bypassPermissions";return fn?jt(e,Gs):e': (
            'const e=dn??cn??Qs??nn??"bypassPermissions";return fn?jt(e,Gs):e'
        ),
        'const jn=e.useMemo(()=>{if(s)return xn??yn??nn??"default";const e=xn??gn??cn??nn;return bn?jt(e,en):e}': (
            'const jn=e.useMemo(()=>{if(s)return xn??yn??nn??"bypassPermissions";const e=xn??gn??cn??nn??"bypassPermissions";return bn?jt(e,en):e}'
        ),
        'const jn=e.useMemo(()=>{if(s)return xn??yn??nn??"bypassPermissions";const e=xn??gn??cn??nn??"bypassPermissions";return bn?jt(e,en):e}': (
            'const jn=e.useMemo(()=>{if(s)return xn??yn??nn??"bypassPermissions";const e=xn??gn??cn??nn??"bypassPermissions";return bn?jt(e,en):e}'
        ),
    }
    patched_files = 0
    patched_strings = 0

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "cc-landing-draft-permission-mode" not in text and "epitaxy-folder-permission-mode" not in text:
            continue
        patched = text
        count = 0
        for pattern, target in regex_replacements:
            patched, n = pattern.subn(target, patched)
            count += n
        for source, target in literal_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    return patched_files, patched_strings


def patch_safe_opus_context(assets_dir: Path) -> tuple[int, int]:
    """保留 Opus 显示入口，但避免把 1M 名称当成固定真实上下文能力。"""
    replacements = {
        'z="opus[1m]",': 'z="opus",',
        'z="opus[1m]",{allModelOptions:F}=R,': 'z="opus",{allModelOptions:F}=R,',
        'return["opus[1m]",{...s,allModelOptions:[r,l]': 'return["opus",{...s,allModelOptions:[r,l]',
        'model:"opus[1m]",name:"Opus 4.71M"': 'model:"opus",name:"Opus 4.71M"',
        'model:"opus[1m]",name:"Opus 4.7 1M"': 'model:"opus",name:"Opus 4.71M"',
        'yc("baku_model","model","opus[1m]"': 'yc("baku_model","model","opus"',
        'jc("baku_model","model","opus[1m]"': 'jc("baku_model","model","opus"',
        'pc("baku_model","model","opus[1m]"': 'pc("baku_model","model","opus"',
        'if(!e)return"opus[1m]";': 'if(!e)return"opus";',
        'if("opus"===t||"opus[1m]"===t)return"opus[1m]";': (
            'if("opus"===t||"opus[1m]"===t)return"opus";'
        ),
        'return s?s.model:"opus[1m]"': 'return s?s.model:"opus"',
        '?s.model:"opus[1m]"': '?s.model:"opus"',
        ':"opus[1m]")': ':"opus")',
        '})(H??"opus[1m]"),': '})(H??"opus"),',
        '})(U??"opus[1m]"),': '})(U??"opus"),',
        'onSelect:()=>ue.current("opus[1m]")': 'onSelect:()=>ue.current("opus")',
        'onSelect:()=>N.current("opus[1m]")': 'onSelect:()=>N.current("opus")',
        'onSelect:()=>Re.current("opus[1m]")': 'onSelect:()=>Re.current("opus")',
        'ue.current("opus[1m]")': 'ue.current("opus")',
        'N.current("opus[1m]")': 'N.current("opus")',
        'Re.current("opus[1m]")': 'Re.current("opus")',
    }
    patched_files = 0
    patched_strings = 0

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "opus[1m]" not in text or OPUS_DISPLAY_NAME not in text:
            continue
        patched = text
        count = 0
        for source, target in replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    return patched_files, patched_strings


def patch_context_usage_percent(assets_dir: Path, context_window: int | None = None) -> tuple[int, int]:
    """Context Usage 文本和实时面板都按 provider 真实窗口重算。"""
    patched_files = 0
    patched_strings = 0
    window_literal = str(context_window) if context_window else "0"
    replacements = {
        'const n=ege(s[2]);if(0===n)return{text:e,usage:null};': (
            f'const n0=ege(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        'const n=M1e(s[2]);if(0===n)return{text:e,usage:null};': (
            f'const n0=M1e(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        'const n0=ege(s[2]),n=262144||n0;if(0===n)return{text:e,usage:null};': (
            f'const n0=ege(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        'const n0=ege(s[2]),n=200000||n0;if(0===n)return{text:e,usage:null};': (
            f'const n0=ege(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        'const n0=ege(s[2]),n=1000000||n0;if(0===n)return{text:e,usage:null};': (
            f'const n0=ege(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        'const n0=M1e(s[2]),n=262144||n0;if(0===n)return{text:e,usage:null};': (
            f'const n0=M1e(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        'const n0=M1e(s[2]),n=200000||n0;if(0===n)return{text:e,usage:null};': (
            f'const n0=M1e(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        'const n0=M1e(s[2]),n=1000000||n0;if(0===n)return{text:e,usage:null};': (
            f'const n0=M1e(s[2]),n={window_literal}||n0;if(0===n)return{{text:e,usage:null}};'
        ),
        (
            'return{text:e,usage:{model:t,totalTokens:ege(s[1]),rawMaxTokens:n,'
            'percentage:Number(s[3]),categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ): (
            'const l=ege(s[1]),c=Math.round(l/n*1e3)/10;'
            'return{text:e,usage:{model:t,totalTokens:l,rawMaxTokens:n,'
            'percentage:c,categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ),
        (
            'return{text:e,usage:{model:t,totalTokens:ege(s[1]),rawMaxTokens:n,'
            'percentage:Math.round(ege(s[1])/n*1e3)/10,categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ): (
            'const l=ege(s[1]),c=Math.round(l/n*1e3)/10;'
            'return{text:e,usage:{model:t,totalTokens:l,rawMaxTokens:n,'
            'percentage:c,categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ),
        (
            'return{text:e,usage:{model:t,totalTokens:M1e(s[1]),rawMaxTokens:n,'
            'percentage:Number(s[3]),categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ): (
            'const l=M1e(s[1]),c=Math.round(l/n*1e3)/10;'
            'return{text:e,usage:{model:t,totalTokens:l,rawMaxTokens:n,'
            'percentage:c,categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ),
        (
            'return{text:e,usage:{model:t,totalTokens:M1e(s[1]),rawMaxTokens:n,'
            'percentage:Math.round(M1e(s[1])/n*1e3)/10,categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ): (
            'const l=M1e(s[1]),c=Math.round(l/n*1e3)/10;'
            'return{text:e,usage:{model:t,totalTokens:l,rawMaxTokens:n,'
            'percentage:c,categories:a,mcpTools:r,memoryFiles:i,agents:o}}'
        ),
    }
    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "rawMaxTokens" not in text:
            continue
        patched = text
        count = 0
        if "## Context Usage" in patched:
            for source, target in replacements.items():
                occurrences = patched.count(source)
                if occurrences:
                    patched = patched.replace(source, target)
                    count += occurrences
        if context_window:
            live_replacements = {
                'M=n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,Math.min(1,k/M))):null,': (
                    f'M=n?{window_literal}:n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,k/M)):null,'
                ),
                'M=n?262144:n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,k/M)):null,': (
                    f'M=n?{window_literal}:n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,k/M)):null,'
                ),
                'M=n?200000:n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,k/M)):null,': (
                    f'M=n?{window_literal}:n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,k/M)):null,'
                ),
                'M=n?1000000:n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,k/M)):null,': (
                    f'M=n?{window_literal}:n?.rawMaxTokens??null,C=null!==M?Math.round(100*Math.max(0,k/M)):null,'
                ),
                'N=b?C??0:d.peak??0,': 'N=b?Math.min(100,C??0):d.peak??0,',
                'contextUsage:n??null': (
                    f'contextUsage:n?{{...n,rawMaxTokens:{window_literal},'
                    f'percentage:Math.round(100*Math.max(0,n.totalTokens/{window_literal}))}}:null'
                ),
            }
            for source, target in live_replacements.items():
                occurrences = patched.count(source)
                if occurrences:
                    patched = patched.replace(source, target)
                    count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count
    return patched_files, patched_strings


def patch_kimi_gateway_health_banner(assets_dir: Path) -> tuple[int, int]:
    """隐藏 Kimi 网关可连通时仍被旧健康状态标成 Unreachable 的 Cowork 横幅。"""
    replacements = {
        (
            'if(d||!l||!w)return null;'
            'const k=l.state===xV.InvalidConfig||l.state===xV.AuthFailed||l.state===xV.BootstrapError'
        ): (
            'if(d||!l||!w||l.state===xV.Unreachable&&'
            '/api\\.kimi\\.com(?:\\/coding)?/i.test(String(l.endpoint??l.requestUrl??"")))return null;'
            'const k=l.state===xV.InvalidConfig||l.state===xV.AuthFailed||l.state===xV.BootstrapError'
        ),
        (
            'if(d||!l||!w)return null;'
            'const k=l.state===yW.InvalidConfig||l.state===yW.AuthFailed||l.state===yW.BootstrapError'
        ): (
            'if(d||!l||!w||l.state===yW.Unreachable&&'
            '/api\\.kimi\\.com(?:\\/coding)?/i.test(String(l.endpoint??l.requestUrl??"")))return null;'
            'const k=l.state===yW.InvalidConfig||l.state===yW.AuthFailed||l.state===yW.BootstrapError'
        ),
        (
            'if(d||!l||!w)return null;'
            'const k=l.state===wz.InvalidConfig||l.state===wz.AuthFailed||l.state===wz.BootstrapError'
        ): (
            'if(d||!l||!w||l.state===wz.Unreachable&&'
            '/api\\.kimi\\.com(?:\\/coding)?/i.test(String(x??l.endpoint??l.requestUrl??"")))return null;'
            'const k=l.state===wz.InvalidConfig||l.state===wz.AuthFailed||l.state===wz.BootstrapError'
        ),
        'case eG.Unreachable:return t?{title:a.jsx(c,{defaultMessage:"Can\'t reach {host}",id:"Uj5zPEHmrp",values:{host:r}})': (
            'case eG.Unreachable:if(/api\\.kimi\\.com(?:\\/coding)?/i.test(String(r)))return null;return t?{title:a.jsx(c,{defaultMessage:"Can\'t reach {host}",id:"Uj5zPEHmrp",values:{host:r}})'
        ),
    }
    patched_files = 0
    patched_strings = 0

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched = text
        count = 0
        for source, target in replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    return patched_files, patched_strings


def patch_epitaxy_cache_bust(ion_dist_dir: Path, assets_dir: Path) -> tuple[int, int]:
    """给 Code 和第三方设置页相关资源加版本参数，避免命中旧缓存。"""
    patched_files = 0
    patched_strings = 0
    code_chunk = next(
        (
            path
            for path in sorted(assets_dir.glob("*.js"))
            if (
                "function em(t){const s=i()" in path.read_text(encoding="utf-8", errors="ignore")
                and "modelExtraSections:gs" in path.read_text(encoding="utf-8", errors="ignore")
            )
            or (
                'const um="ccd-effort-level' in path.read_text(encoding="utf-8", errors="ignore")
                and "modelExtraSections:xs" in path.read_text(encoding="utf-8", errors="ignore")
            )
            or (
                'const zm="ccd-effort-level' in path.read_text(encoding="utf-8", errors="ignore")
                and "modelExtraSections:Ss" in path.read_text(encoding="utf-8", errors="ignore")
            )
            or (
                "ccd-effort-level" in path.read_text(encoding="utf-8", errors="ignore")
                and "modelExtraSections" in path.read_text(encoding="utf-8", errors="ignore")
            )
        ),
        None,
    )
    if not code_chunk:
        return 0, 0

    version_source = Path(__file__).read_bytes()
    version = "zhcn-" + hashlib.sha256(version_source).hexdigest()[:12]
    query_re = re.compile(r"\?v=zhcn-[0-9a-f]{12}")

    def with_version(value: str) -> str:
        return query_re.sub("", value) + f"?v={version}"

    names = {code_chunk.name}
    custom3p_targets = [target for target in CUSTOM3P_SETUP_REPLACEMENTS.values()]
    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if code_chunk.name in text and not path.name.startswith("index-"):
            names.add(path.name)
        if (
            not path.name.startswith("index-")
            and (
                "setup-desktop-3p" in text
                or "inferenceGatewayBaseUrl" in text
                or any(target in text for target in custom3p_targets)
            )
        ):
            names.add(path.name)

    index_html = ion_dist_dir / "index.html"
    if index_html.exists():
        text = index_html.read_text(encoding="utf-8")
        patched = re.sub(
            r'(?P<prefix><script type="module" crossorigin src="/assets/v1/)(?P<name>index-[^"?]+\.js)(?:\?v=zhcn-[0-9a-f]{12})?(?P<suffix>"></script>)',
            lambda match: f"{match.group('prefix')}{with_version(match.group('name'))}{match.group('suffix')}",
            text,
            count=1,
        )
        if patched != text:
            index_html.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += 1

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched = text
        count = 0
        for name in sorted(names, key=len, reverse=True):
            patterns = [
                (f'./{name}', f'./{with_version(name)}'),
                (f'assets/v1/{name}', f'assets/v1/{with_version(name)}'),
                (f'/assets/v1/{name}', f'/assets/v1/{with_version(name)}'),
            ]
            for source, target in patterns:
                source_re = re.compile(re.escape(source) + r"(?:\?v=zhcn-[0-9a-f]{12})?")
                patched, n = source_re.subn(target, patched)
                count += n
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    return patched_files, patched_strings


def patch_cowork_model_menu(assets_dir: Path, runtime_model: str | None = None) -> tuple[int, int]:
    """把 Cowork 模型菜单固定为 Opus 伪装入口、Kimi 真实入口和完整强度。"""
    patched_files = 0
    patched_strings = 0
    provider_runtime_model = safe_runtime_model_id(runtime_model)
    provider_runtime_model_js = json.dumps(provider_runtime_model, ensure_ascii=False)
    cowork_runtime_mapper = (
        f'((e)=>{{const t=String(e??"").toLowerCase();return"opus"===t||"opus[1m]"===t?{provider_runtime_model_js}:'
        f'("kimi-k2.6"===t?"kimi-for-coding":(e??{provider_runtime_model_js}))}})'
    )

    # Claude 1.6608+：Cowork 与普通入口共用 Jbt 模型选择器。
    # 这里直接把共享选择器的候选项重建为固定两项，避免 Missing/Legacy fallback。
    jbt_model_re = re.compile(
        r'z=(?:r\?\?A|\(e=>e==="kimi-for-coding"\?"opus\[1m\]":e\)\(r\?\?A\)),'
        r'\{allModelOptions:F,mainModels:U,overflowModels:q\}=R,'
        r'B=ud\("sticky_model_selector"\),\[\$,V\]=n\.useState\(null\),H=!B&&\$\?\$:z;'
        r'let W=F\.find\(e=>e\.model===H\);W\|\|\(W=F\.find\(e=>e\.model===L\)\?\?Kbt\);'
        r'const G=n\.useRef\(null\),K=S7\("paprika_mode"\);Dbt\(z\);'
        r'const Y=Abt\(\),Z=!h&&!O,Q=Z\?\[W\]:U,X=Z\?U\.filter\(e=>e\.model!==H\):\[\],'
        r'J=Z\?q\.filter\(e=>e\.model!==H\):q,',
        re.DOTALL,
    )
    jbt_model_target = (
        'z="opus[1m]",'
        '{allModelOptions:F}=R,U=[],q=[],B=ud("sticky_model_selector"),[$,V]=n.useState(null),H=$??z,'
        'rr={...(F.find(e=>"opus[1m]"===e.model)??F.find(e=>/opus/i.test(e.model)&&/\\[1m\\]/i.test(e.model))??F.find(e=>e.thinking_modes?.length)??{}),model:"opus[1m]",name:"Opus 4.71M",name_i18n_key:void 0,inactive:!1,overflow:!1},'
        'oo=F.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-k2.6"===t||"kimi-k2.6"===s||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)}),'
        'll=oo?.model??"kimi-for-coding",'
        'cc={...(oo??F.find(e=>e.thinking_modes?.length)??{}),model:ll,name:"Kimi-k2.6",name_i18n_key:void 0,inactive:!1,overflow:!1};'
        'let W="kimi-for-coding"===String(H).toLowerCase()||/kimi/i.test(String(H))&&/k2\\.6/i.test(String(H))?cc:rr;'
        'const G=n.useRef(null),K=S7("paprika_mode");Dbt(W.model);'
        'const Y=Abt(),Z=!h&&!O,Q=[rr,cc],X=[],J=[],'
    )
    jbt_handler_re = re.compile(
        r'const de=e=>\{if\(e\.model===H\)return;if\(ne\(e\.model\)\)return;'
        r'if\(ae\|\|!Ybt\(e\.model,!1,!re,L,le\)\)\{',
        re.DOTALL,
    )
    jbt_handler_target = (
        'const de=e=>{const t=String(e.model??"").toLowerCase(),s="opus"===t||"opus[1m]"===t||'
        '"kimi-for-coding"===t||/kimi/i.test(String(e.model))&&/k2\\.6/i.test(String(e.model));'
        'if(e.model===H)return;if(!s&&ne(e.model))return;if(s||ae||!Ybt(e.model,!1,!re,L,le)){'
    )
    jbt_effort_re = re.compile(
        r'\{activeMode:ee\}=Fbt\(z,K\),'
        r'(?:\[cw,Sw\]=n\.useState\(\(\)=>\{try\{return localStorage\.getItem\("cowork_effort_level(?:_cn)?"\)\|\|"(?:high|max)"\}catch\{return"(?:high|max)"\}\}\),'
        r'Fw=n\.useMemo\(\(\)=>_\?\?(?:\("cowork"===I\?)?\{current:cw,options:\[\{value:"low",label:"低"\},\{value:"medium",label:"中"\},\{value:"high",label:"高"\},\{value:"xhigh",label:"超高"\},\{value:"max",label:"最大"\}\],onSelect:e=>\{Sw\(e\);try\{localStorage\.setItem\("cowork_effort_level(?:_cn)?",e\),window\.dispatchEvent\(new CustomEvent\("cowork-effort-change",\{detail:e\}\)\)\}catch\{\}\}\}(?::void 0\))?,\[_,cw(?:,I)?\]\),)?'
        r'te=.*?,\{toggleConversationSetting:se\}=E7\(\{source:"modelSelector"\}\)',
        re.DOTALL,
    )
    jbt_effort_target = (
        '{activeMode:ee}=Fbt(z,K),'
        '[cw,Sw]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),'
        'Fw=n.useMemo(()=>_??{current:cw,options:[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],'
        'onSelect:e=>{Sw(e);try{localStorage.setItem("cowork_effort_level_cn",e),window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}}},[_,cw]),'
        'te=Fw?{low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}[Fw.current]??ee?.label:O?void 0:ee?.label,'
        '{toggleConversationSetting:se}=E7({source:"modelSelector"})'
    )
    jbt_effort_render_re = re.compile(
        r'(?:_&&|Fw&&)a\.jsxs\(a\.Fragment,\{children:\[a\.jsx\(tl,\{className:_de\}\),'
        r'a\.jsx\("div",\{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a\.jsx\(c,\{defaultMessage:"强度",id:"VKZ/U8vAsk"\}\)\}\),'
        r'a\.jsx\(Xbt,\{section:(?:_|Fw),compactMenu:j\}\)\]\}\)',
        re.DOTALL,
    )
    jbt_effort_render_target = (
        'Fw&&a.jsxs(a.Fragment,{children:[a.jsx(tl,{className:_de}),'
        'a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),'
        'a.jsx(Xbt,{section:Fw,compactMenu:j})]})'
    )
    jbt_state_replacements = {
        'Y(e.model)||se("compass_mode",null),B||V(e.model),D(e.model),i?.(e)}': (
            'Y(e.model)||se("compass_mode",null),V(e.model),D(e.model),i?.(e)}'
        ),
        'Pbt(W),te].filter(Boolean).join(" ")': (
            'Pbt(W),te].filter(Boolean).join(" ")'
        ),
    }

    pte_replacements = {
        'R=(e=>e==="kimi-for-coding"?"opus[1m]":e)(N),O=I': 'R=N,O=I',
        'F=(e=>e==="kimi-for-coding"?"opus[1m]":e)(z?.sessionData?.session_context?.model??null),': (
            'F=z?.sessionData?.session_context?.model??null,'
        ),
        'return(e=>e==="kimi-for-coding"?"opus[1m]":e)(t.sessionModel??t.sessionData?.session_context?.model)})??null': (
            'return t.sessionModel??t.sessionData?.session_context?.model})??null'
        ),
        '_=yc("cowork_effort_level","medium",Mp),j=yc("cowork_model",T0t,I0t),': (
            '_=(()=>{const e=yc("cowork_effort_level_cn","max",Mp),[t,s]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||e}catch{return e}});return n.useEffect(()=>{const e=()=>{try{s(localStorage.getItem("cowork_effort_level_cn")||"max")}catch{s("max")}};if("undefined"==typeof window)return;e();return window.addEventListener("cowork-effort-change",e),()=>window.removeEventListener("cowork-effort-change",e)},[]),t})(),j=yc("cowork_model",T0t,I0t),'
        ),
        '_=(()=>{const e=yc("cowork_effort_level","high",Mp),[t,s]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level")||e}catch{return e}});return n.useEffect(()=>{const e=()=>{try{s(localStorage.getItem("cowork_effort_level")||"high")}catch{s("high")}};if("undefined"==typeof window)return;e();return window.addEventListener("cowork-effort-change",e),()=>window.removeEventListener("cowork-effort-change",e)},[]),t})(),j=yc("cowork_model",T0t,I0t),': (
            '_=(()=>{const e=yc("cowork_effort_level_cn","max",Mp),[t,s]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||e}catch{return e}});return n.useEffect(()=>{const e=()=>{try{s(localStorage.getItem("cowork_effort_level_cn")||"max")}catch{s("max")}};if("undefined"==typeof window)return;e();return window.addEventListener("cowork-effort-change",e),()=>window.removeEventListener("cowork-effort-change",e)},[]),t})(),j=yc("cowork_model",T0t,I0t),'
        ),
        '_=(()=>{const e=yc("cowork_effort_level_cn","max",Mp),[t,s]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||e}catch{return e}});return n.useEffect(()=>{const e=()=>{try{s(localStorage.getItem("cowork_effort_level_cn")||"max")}catch{s("max")}};if("undefined"==typeof window)return;e();return window.addEventListener("cowork-effort-change",e),()=>window.removeEventListener("cowork-effort-change",e)},[]),t})(),j=yc("cowork_model",T0t,I0t),': (
            '_=(()=>{const e=yc("cowork_effort_level_cn","max",Mp),[t,s]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||e}catch{return e}});return n.useEffect(()=>{const e=()=>{try{s(localStorage.getItem("cowork_effort_level_cn")||"max")}catch{s("max")}};if("undefined"==typeof window)return;e();return window.addEventListener("cowork-effort-change",e),()=>window.removeEventListener("cowork-effort-change",e)},[]),t})(),j=yc("cowork_model",T0t,I0t),'
        ),
    }
    cowork_runtime_replacements = {
        'session_context:{sources:[],...t.sessionModel&&{model:t.sessionModel}}': (
            f'session_context:{{sources:[],...t.sessionModel&&{{model:{cowork_runtime_mapper}(t.sessionModel)}}}}'
        ),
        'if(r&&r!==wt.current&&t.setModel)try{await t.setModel(e,r),a({event_key:"claudeai.cowork.model_switched",session_id:e,previous_model:wt.current??"unknown",new_model:r,session_type:uY(e)?"remote":"local"}),wt.current=r}catch(g){Ac.error(cc.LOCAL_SESSION,"Failed to set model",{error:g,sessionId:e,model:r})}': (
            f'const zhCoworkRuntimeModel={cowork_runtime_mapper}(r);'
            'if(zhCoworkRuntimeModel&&zhCoworkRuntimeModel!==wt.current&&t.setModel)try{await t.setModel(e,zhCoworkRuntimeModel),a({event_key:"claudeai.cowork.model_switched",session_id:e,previous_model:wt.current??"unknown",new_model:zhCoworkRuntimeModel,session_type:uY(e)?"remote":"local"}),wt.current=zhCoworkRuntimeModel}catch(g){Ac.error(cc.LOCAL_SESSION,"Failed to set model",{error:g,sessionId:e,model:zhCoworkRuntimeModel})}'
        ),
    }

    # Claude 1.6608.2：Jbt 变成独立共享模型列表组件，外层配置仍在同一 bundle。
    # 这里按新版变量名补丁，确保升级后 Cowork 不会退回 Legacy Model 或丢失强度。
    jbt_v2_model_re = re.compile(
        r'z=(?:r\?\?A|\(e=>e==="kimi-for-coding"\?"opus\[1m\]":e\)\(r\?\?A\)),'
        r'\{allModelOptions:F,mainModels:U,overflowModels:q\}=R,'
        r'B=ud\("sticky_model_selector"\),\[\$,V\]=n\.useState\(null\),H=!B&&\$\?\$:z;'
        r'let W=F\.find\(e=>e\.model===H\);W\|\|\(W=F\.find\(e=>e\.model===L\)\?\?Zbt\);'
        r'const G=n\.useRef\(null\),K=S7\("paprika_mode"\);zbt\(z\);'
        r'const Y=Rbt\(\),Z=!h&&!O,Q=Z\?\[W\]:U,X=Z\?U\.filter\(e=>e\.model!==H\):\[\],'
        r'J=Z\?q\.filter\(e=>e\.model!==H\):q,',
        re.DOTALL,
    )
    jbt_v2_already_model_re = re.compile(
        r'z=\(e=>\{const t=String\(e\?\?""\)\.toLowerCase\(\);'
        r'.*?return"opus\[1m\]"\}\)\(r\?\?A\),'
        r'\{allModelOptions:F\}=R,U=\[\],q=\[\],'
        r'B=ud\("sticky_model_selector"\),\[\$,V\]=n\.useState\(null\),H=\$\?\?z,'
        r'rr=.*?const Y=Rbt\(\),Z=!h&&!O,Q=\[rr,cc\],X=\[\],J=\[\],',
        re.DOTALL,
    )
    jbt_v2_model_target = (
        'z="opus[1m]",'
        '{allModelOptions:F}=R,U=[],q=[],B=ud("sticky_model_selector"),[$,V]=n.useState(null),H=$??z,'
        'rr={...(F.find(e=>"opus[1m]"===e.model)??F.find(e=>/opus/i.test(e.model)&&/\\[1m\\]/i.test(e.model))??F.find(e=>e.thinking_modes?.length)??{}),model:"opus[1m]",name:"Opus 4.71M",name_i18n_key:void 0,inactive:!1,overflow:!1},'
        'oo=F.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-k2.6"===t||"kimi-k2.6"===s||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)}),'
        'll=oo?.model??"kimi-for-coding",'
        'cc={...(oo??F.find(e=>e.thinking_modes?.length)??{}),model:ll,name:"Kimi-k2.6",name_i18n_key:void 0,inactive:!1,overflow:!1};'
        'let W="kimi-for-coding"===String(H).toLowerCase()||/kimi/i.test(String(H))&&/k2\\.6/i.test(String(H))?cc:rr;'
        'const G=n.useRef(null),K=S7("paprika_mode");zbt(W.model);'
        'const Y=Rbt(),Z=!h&&!O,Q=[rr,cc],X=[],J=[],'
    )
    jbt_v2_effort_re = re.compile(
        r'\{activeMode:ee\}=qbt\(z,K\),'
        r'(?:\[cw,Sw\]=n\.useState\(\(\)=>\{try\{return localStorage\.getItem\("cowork_effort_level(?:_cn)?"\)\|\|"(?:high|max)"\}catch\{return"(?:high|max)"\}\}\),'
        r'Fw=n\.useMemo\(\(\)=>_\?\?\{current:cw,options:\[\{value:"low",label:"低"\},\{value:"medium",label:"中"\},\{value:"high",label:"高"\},\{value:"xhigh",label:"超高"\},\{value:"max",label:"最大"\}\],onSelect:e=>\{Sw\(e\);try\{localStorage\.setItem\("cowork_effort_level(?:_cn)?",e\),window\.dispatchEvent\(new CustomEvent\("cowork-effort-change",\{detail:e\}\)\)\}catch\{\}\}\},\[_,cw\]\),)?'
        r'te=.*?,'
        r'\{toggleConversationSetting:se\}=E7\(\{source:"modelSelector"\}\)',
        re.DOTALL,
    )
    jbt_v2_effort_target = (
        '{activeMode:ee}=qbt(z,K),'
        '[cw,Sw]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),'
        'Fw=n.useMemo(()=>_??{current:cw,options:[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],'
        'onSelect:e=>{Sw(e);try{localStorage.setItem("cowork_effort_level_cn",e),window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}}},[_,cw]),'
        'te=Fw?{low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}[Fw.current]??ee?.label:O?void 0:ee?.label,'
        '{toggleConversationSetting:se}=E7({source:"modelSelector"})'
    )
    jbt_v2_handler_re = re.compile(
        r'const de=e=>\{if\(e\.model===H\)return;if\(ne\(e\.model\)\)return;'
        r'if\(ae\|\|!Qbt\(e\.model,!1,!re,L,le\)\)\{',
        re.DOTALL,
    )
    jbt_v2_handler_target = (
        'const de=e=>{const t=String(e.model??"").toLowerCase(),s="opus"===t||"opus[1m]"===t||'
        '"kimi-for-coding"===t||/kimi/i.test(String(e.model))&&/k2\\.6/i.test(String(e.model));'
        'if(e.model===H)return;if(!s&&ne(e.model))return;if(s||ae||!Qbt(e.model,!1,!re,L,le)){'
    )
    jbt_v2_render_re = re.compile(
        r'_&&a\.jsxs\(a\.Fragment,\{children:\[a\.jsx\(tl,\{className:Mde\}\),'
        r'a\.jsx\("div",\{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a\.jsx\(c,\{defaultMessage:"(?:Effort|强度)",id:"VKZ/U8vAsk"\}\)\}\),'
        r'a\.jsx\(eyt,\{section:_,compactMenu:j\}\)\]\}\)',
        re.DOTALL,
    )
    jbt_v2_render_target = (
        'Fw&&a.jsxs(a.Fragment,{children:[a.jsx(tl,{className:Mde}),'
        'a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),'
        'a.jsx(eyt,{section:Fw,compactMenu:j})]})'
    )
    jbt_v2_state_source = 'Y(e.model)||se("compass_mode",null),B||V(e.model),D(e.model),i?.(e)}'
    jbt_v2_state_target = 'Y(e.model)||se("compass_mode",null),V(e.model),D(e.model),i?.(e)}'
    cowork_effort_config_re = re.compile(
        r'_=yc\("cowork_effort_level(?:_cn)?","(?:medium|high|max)",([A-Za-z0-9_$]+)\),'
        r'j=yc\("cowork_model",([^)]*)\),',
        re.DOTALL,
    )
    cowork_effort_wrapper_re = re.compile(
        r'_=\(\(\)=>\{const e=yc\("cowork_effort_level(?:_cn)?","(?:high|max)",([A-Za-z0-9_$]+)\),'
        r'\[t,s\]=n\.useState\(\(\)=>\{try\{return localStorage\.getItem\("cowork_effort_level(?:_cn)?"\)\|\|e\}catch\{return e\}\}\);'
        r'return n\.useEffect\(\(\)=>\{const e=\(\)=>\{try\{s\(localStorage\.getItem\("cowork_effort_level(?:_cn)?"\)\|\|"(?:high|max)"\)\}catch\{s\("(?:high|max)"\)\}\};'
        r'if\("undefined"==typeof window\)return;e\(\);return window\.addEventListener\("cowork-effort-change",e\),'
        r'\(\)=>window\.removeEventListener\("cowork-effort-change",e\)\},\[\]\),t\}\)\(\),'
        r'j=yc\("cowork_model",([^)]*)\),',
        re.DOTALL,
    )

    def cowork_effort_config_target(match: re.Match[str]) -> str:
        validator = match.group(1)
        cowork_model_args = match.group(2)
        return (
            f'_=(()=>{{const e=yc("cowork_effort_level_cn","max",{validator}),'
            '[t,s]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||e}catch{return e}});'
            'return n.useEffect(()=>{const e=()=>{try{s(localStorage.getItem("cowork_effort_level_cn")||"max")}catch{s("max")}};'
            'if("undefined"==typeof window)return;e();return window.addEventListener("cowork-effort-change",e),'
            '()=>window.removeEventListener("cowork-effort-change",e)},[]),t})(),'
            f'j=yc("cowork_model",{cowork_model_args}),'
        )

    # Claude 1.8089+：Cowork 入口改为 Xae/Wmt，共享选择器仍在主 index bundle。
    xae_return_source = (
        'return{model:e&&"local_session"!==i?.type?b:U?A:"local_session"===i?.type&&B?P:$?F:s||w,'
        'defaultModel:w,modelsConfig:C,hideThinkingMenu:y,stickyModelPreference:U?A:null,setStickyModelPreference:O,isSettled:L}'
    )
    xae_return_target = (
        'const zhCoworkKimiOption=C.allModelOptions.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase(),n=String(e.label_override??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-for-coding"===n||"kimi-k2.6"===t||"kimi-k2.6"===s||"kimi-k2.6"===n||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)||/kimi.*k2\\.6/i.test(n)}),'
        'zhCoworkKimiId=zhCoworkKimiOption?.model??"kimi-for-coding",'
        'zhCoworkOpus={...(C.allModelOptions.find(e=>"opus"===e.model)||C.allModelOptions.find(e=>"opus[1m]"===e.model)||C.allModelOptions.find(e=>e.thinking_modes?.length)||{}),model:"opus",name:"Opus 4.71M",label_override:"Opus 4.71M",name_i18n_key:void 0,inactive:!1,overflow:!1},'
        'zhCoworkKimi={...(zhCoworkKimiOption??C.allModelOptions.find(e=>e.thinking_modes?.length)??{}),model:zhCoworkKimiId,name:"Kimi-k2.6",label_override:"Kimi-k2.6",name_i18n_key:void 0,inactive:!1,overflow:!1},'
        'zhCoworkConfig={...C,allModelOptions:[zhCoworkOpus,zhCoworkKimi],mainModels:[zhCoworkOpus,zhCoworkKimi],overflowModels:[],legacyModelIds:[],syntheticAllowedModels:C.syntheticAllowedModels??{}},'
        'zhCoworkModel=(e=>{const t=String(e??"").toLowerCase();return" kimi-for-coding"===t.trim()||"kimi-k2.6"===t||/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e))?zhCoworkKimiId:"opus"})(A);'
        'return{model:c?zhCoworkModel:e&&"local_session"!==i?.type?b:U?A:"local_session"===i?.type&&B?P:$?F:s||w,'
        'defaultModel:c?"opus":w,modelsConfig:c?zhCoworkConfig:C,hideThinkingMenu:y,stickyModelPreference:c?zhCoworkModel:U?A:null,setStickyModelPreference:O,isSettled:L}'
    )
    wmt_effort_source = (
        '{activeMode:ee}=Omt(z,K),te=O?void 0:ee?.label,{toggleConversationSetting:se}=bz({source:"modelSelector"})'
    )
    wmt_effort_already_source = (
        '{activeMode:ee}=Omt(z,K),'
        '[cw,Sw]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),'
        'Fw=n.useMemo(()=>({current:cw,options:[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],'
        'onSelect:e=>{Sw(e);try{localStorage.setItem("cowork_effort_level_cn",e),window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}}}),[cw]),'
        'te=O?{low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}[cw]:ee?.label,{toggleConversationSetting:se}=bz({source:"modelSelector"})'
    )
    wmt_effort_target = (
        '{activeMode:ee}=Omt(z,K),'
        'zhCoworkEffortOptions=[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],'
        '[cw,Sw]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),'
        'Fw=n.useMemo(()=>({current:cw,options:zhCoworkEffortOptions,'
        'onSelect:e=>{Sw(e);try{localStorage.setItem("cowork_effort_level_cn",e),window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}}}),[cw]),'
        'te=O?{low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}[cw]:ee?.label,{toggleConversationSetting:se}=bz({source:"modelSelector"})'
    )
    wmt_handler_source = (
        'const de=e=>{if(e.model===H)return;if(ne(e.model))return;if(ae||!qmt(e.model,!1,!re,T,le)){'
    )
    wmt_handler_target = (
        'const de=e=>{const zhIsFixed=("opus"===String(e.model).toLowerCase()||"opus[1m]"===String(e.model).toLowerCase()||"kimi-for-coding"===String(e.model).toLowerCase()||/kimi/i.test(String(e.model))&&/k2\\.6/i.test(String(e.model)));'
        'if(e.model===H)return;if(!zhIsFixed&&ne(e.model))return;if(zhIsFixed||ae||!qmt(e.model,!1,!re,T,le)){'
    )
    wmt_current_source = '$=Uc("sticky_model_selector"),[q,V]=n.useState(null),H=!$&&q?q:z;'
    wmt_current_target = '$=Uc("sticky_model_selector"),[q,V]=n.useState(null),H=q??z;'
    wmt_state_source = 'Y(e.model)||se("compass_mode",null),$||V(e.model),D(e.model),i?.(e)}'
    wmt_state_target = 'Y(e.model)||se("compass_mode",null),V(e.model),D(e.model),i?.(e)}'
    wmt_render_source = (
        'ue=a.jsxs(a.Fragment,{children:[a.jsx(Hmt,{models:Q,currentModelOption:W,defaultModel:T,opensInNewChat:N,handleModelSelect:de,hasMultiModalContent:l,checkCapabilityConflicts:E,compactMenu:_}),(!O||k)&&a.jsx(Pmt,{currentModel:z,currentMode:K,coworkExtendedThinkingToggle:k}),'
    )
    wmt_effort_fragment_target = (
        'O&&a.jsxs(a.Fragment,{children:[a.jsx(Ho,{className:vme}),'
        'a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:"强度"}),'
        'Fw.options.map(e=>a.jsx(Nk,{closeOnClick:!1,onClick:()=>Fw.onSelect(e.value),className:fc(xme,"group pr-1 cursor-pointer"),'
        'children:a.jsxs(a.Fragment,{children:[a.jsx("div",{className:fc("flex-1",_?"text-xs":"text-sm"),children:e.label}),'
        'Fw.current===e.value&&a.jsx(Re,{className:"text-accent-100 mb-1 mr-1.5",size:16,weight:"bold","aria-hidden":"true"})]})},e.value))]})'
    )
    wmt_render_target = (
        'ue=a.jsxs(a.Fragment,{children:[a.jsx(Hmt,{models:Q,currentModelOption:W,defaultModel:T,opensInNewChat:N,handleModelSelect:de,hasMultiModalContent:l,checkCapabilityConflicts:E,compactMenu:_}),'
        f'{wmt_effort_fragment_target},(!O||k)&&a.jsx(Pmt,{{currentModel:z,currentMode:K,coworkExtendedThinkingToggle:k}}),'
    )
    wmt_broken_effort_fragment_source = (
        'O&&a.jsxs(a.Fragment,{children:[a.jsx(Ho,{className:vme}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:"强度"}),a.jsx(_mt,{section:Fw,compactMenu:_})]})'
    )

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "function Xae({conversationUuid" not in text or "Wmt=({conversationUuid" not in text:
            continue
        patched = text
        count = 0
        for source, target in {
            xae_return_source: xae_return_target,
            wmt_effort_source: wmt_effort_target,
            wmt_effort_already_source: wmt_effort_target,
            wmt_handler_source: wmt_handler_target,
            wmt_current_source: wmt_current_target,
            wmt_state_source: wmt_state_target,
            wmt_render_source: wmt_render_target,
            wmt_broken_effort_fragment_source: wmt_effort_fragment_target,
            **cowork_runtime_replacements,
        }.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    # Claude 1.7196+：共享模型配置改为 k5()，菜单组件改为 Fht/Pht。
    # 这一版没有 Jbt，必须直接固定 k5 的候选项，并给 Fht 增加 Cowork fallback 强度区。
    k5_return_re = re.compile(
        r'return n\.useEffect\(\(\)=>\{g\|\|a\(\{event_key:"claudeai\.code\.composer\.default_model_missing_from_config",default_model:c\}\)\},\[g,c,a\]\),f',
        re.DOTALL,
    )
    k5_return_target = (
        'return n.useEffect(()=>{g||a({event_key:"claudeai.code.composer.default_model_missing_from_config",default_model:c})},[g,c,a]),'
        '((t)=>{if("ccr_model"!==e&&"cowork_model"!==e)return t;'
        'const s=t[1]??{},a=s.allModelOptions??[],r={...(a.find(e=>"opus[1m]"===e.model)??a.find(e=>/opus/i.test(String(e.model))&&/\\[1m\\]/i.test(String(e.model)))??a.find(e=>e.thinking_modes?.length)??{}),model:"opus[1m]",name:"Opus 4.71M",label_override:"Opus 4.71M",name_i18n_key:void 0,inactive:!1,overflow:!1},'
        'i=a.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase(),n=String(e.label_override??"").toLowerCase();return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-for-coding"===n||"kimi-k2.6"===t||"kimi-k2.6"===s||"kimi-k2.6"===n||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)||/kimi.*k2\\.6/i.test(n)}),'
        'o=i?.model??"kimi-for-coding",l={...(i??a.find(e=>e.thinking_modes?.length)??{}),model:o,name:"Kimi-k2.6",label_override:"Kimi-k2.6",name_i18n_key:void 0,inactive:!1,overflow:!1};'
        'return["opus[1m]",{...s,allModelOptions:[r,l],mainModels:[r,l],overflowModels:[],legacyModelIds:[],syntheticAllowedModels:s.syntheticAllowedModels??{}}]})(f)'
    )
    fht_state_source = 'U=ld("sticky_model_selector"),[q,B]=n.useState(null),$=!U&&q?q:D;'
    fht_state_target = 'U=ld("sticky_model_selector"),[q,B]=n.useState(null),$=q??D;'
    fht_effort_source = (
        '{activeMode:X}=jht(D,W),J=L?void 0:X?.label,{toggleConversationSetting:ee}=X0({source:"modelSelector"})'
    )
    fht_effort_target = (
        '{activeMode:X}=jht(D,W),'
        '[cw,Sw]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),'
        'Fw=n.useMemo(()=>_??{current:cw,options:[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],'
        'onSelect:e=>{Sw(e);try{localStorage.setItem("cowork_effort_level_cn",e),window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}}},[_,cw]),'
        'J=L?void 0:_?X?.label:{low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}[Fw.current]??X?.label,'
        '{toggleConversationSetting:ee}=X0({source:"modelSelector"})'
    )
    fht_effort_render_source = (
        '_&&a.jsxs(a.Fragment,{children:[a.jsx(sl,{className:Ete}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"Effort",id:"VKZ/U8vAsk"})}),a.jsx(zht,{section:_,compactMenu:j})]})'
    )
    fht_effort_render_source_zh = (
        '_&&a.jsxs(a.Fragment,{children:[a.jsx(sl,{className:Ete}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),a.jsx(zht,{section:_,compactMenu:j})]})'
    )
    fht_effort_render_target = (
        'Fw&&a.jsxs(a.Fragment,{children:[a.jsx(sl,{className:Ete}),a.jsx("div",{className:"text-xs text-text-500 pt-2 pb-1 px-2",children:a.jsx(c,{defaultMessage:"强度",id:"VKZ/U8vAsk"})}),a.jsx(zht,{section:Fw,compactMenu:j})]})'
    )
    fht_select_source = 'G(e.model)||ee("compass_mode",null),U||B(e.model),R(e.model),i?.(e)}'
    fht_select_target = 'G(e.model)||ee("compass_mode",null),B(e.model),R(e.model),i?.(e)}'
    yukon_source = (
        'const L=w.autoDownloadInBackground&&!0===v.considerEnabledForNonUI;'
        'n.useEffect(()=>{uA?.setYukonSilverConfig?.({...w,effortByModel:k.effort_by_model,'
    )
    yukon_target = (
        'const[zhEffort,setZhEffort]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),'
        'L=w.autoDownloadInBackground&&!0===v.considerEnabledForNonUI;'
        'n.useEffect(()=>{const e=()=>{try{setZhEffort(localStorage.getItem("cowork_effort_level_cn")||"max")}catch{setZhEffort("max")}};'
        'return window.addEventListener("cowork-effort-change",e),()=>window.removeEventListener("cowork-effort-change",e)},[]),'
        'n.useEffect(()=>{uA?.setYukonSilverConfig?.({...w,effort:zhEffort,effortByModel:k.effort_by_model,'
    )
    yukon_deps_source = '},[w,k,_,L,j,M]);'
    yukon_deps_target = '},[w,k,_,L,j,M,zhEffort]);'

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "k5=(e=\"ccr_model\"" not in text or "Pht=({models:e,currentModelOption" not in text:
            continue
        patched = text
        count = 0
        patched, n = k5_return_re.subn(k5_return_target, patched, count=1)
        count += n
        for source, target in {
            fht_state_source: fht_state_target,
            fht_effort_source: fht_effort_target,
            fht_effort_render_source: fht_effort_render_target,
            fht_effort_render_source_zh: fht_effort_render_target,
            fht_select_source: fht_select_target,
            yukon_source: yukon_target,
            yukon_deps_source: yukon_deps_target,
            **cowork_runtime_replacements,
        }.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "Jbt=({models:e,currentModelOption" not in text:
            continue
        patched = text
        count = 0
        patched, n = jbt_v2_model_re.subn(jbt_v2_model_target, patched, count=1)
        count += n
        if not n:
            patched, n = jbt_v2_already_model_re.subn(jbt_v2_model_target, patched, count=1)
            count += n
        patched, n = jbt_v2_effort_re.subn(jbt_v2_effort_target, patched, count=1)
        count += n
        patched, n = jbt_v2_handler_re.subn(jbt_v2_handler_target, patched, count=1)
        count += n
        patched, n = jbt_v2_render_re.subn(jbt_v2_render_target, patched, count=1)
        count += n
        occurrences = patched.count(jbt_v2_state_source)
        if occurrences:
            patched = patched.replace(jbt_v2_state_source, jbt_v2_state_target)
            count += occurrences
        for source, target in pte_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        for source, target in cowork_runtime_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        patched, n = cowork_effort_wrapper_re.subn(cowork_effort_config_target, patched, count=1)
        count += n
        patched, n = cowork_effort_config_re.subn(cowork_effort_config_target, patched, count=1)
        count += n
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "const Jbt=({conversationUuid" not in text:
            continue
        patched = text
        count = 0
        patched, n = jbt_model_re.subn(jbt_model_target, patched, count=1)
        count += n
        patched, n = jbt_handler_re.subn(jbt_handler_target, patched, count=1)
        count += n
        patched, n = jbt_effort_re.subn(jbt_effort_target, patched, count=1)
        count += n
        patched, n = jbt_effort_render_re.subn(jbt_effort_render_target, patched, count=1)
        count += n
        for source, target in jbt_state_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        for source, target in pte_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        for source, target in cowork_runtime_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        patched, n = cowork_effort_wrapper_re.subn(cowork_effort_config_target, patched, count=1)
        count += n
        patched, n = cowork_effort_config_re.subn(cowork_effort_config_target, patched, count=1)
        count += n
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    vft_re = re.compile(
        r'function Vft\(e,t=\{\}\)\{(?:if\("opus\[1m\]"===e\?\.model\|\|"opus"===e\?\.model\)'
        r'return"Opus 4\.7(?:1)? ?1?M";)?const s=e\.model\?Z9\(e\.model\):null;',
        re.DOTALL,
    )
    vft_target = (
        'function Vft(e,t={}){const r=String(e?.model??e?.name??"");'
        'if("opus[1m]"===e?.model||"opus"===e?.model)return"Opus 4.71M";'
        'if("kimi-for-coding"===r.toLowerCase()||/kimi/i.test(r)&&/k2\\.6/i.test(r))return"Kimi-k2.6";'
        'const s=e.model?Z9(e.model):null;'
    )

    wft_patterns = {
        '""===n&&(n="Opus 4.7 1M");return': '""===n&&(n="Opus 4.71M");return',
        '""===n&&(n="Opus 4.71M");return': '""===n&&(n="Opus 4.71M");return',
    }

    ogt_model_res = [
        re.compile(
            r'z=\(e=>\{const t=String\(e\?\?""\).*?'
            r'const Y=Uft\(\),Q=!0,X=\[Ne,Re\],J=\[\],ee=\[\],',
            re.DOTALL,
        ),
        re.compile(
            r'z=.*?,\{allModelOptions:F,mainModels:U,overflowModels:q\}=R,'
            r'B=Xc\("sticky_model_selector"\),\[\$,H\]=n\.useState\(null\),V=.*?'
            r'const G=n\.useRef\(null\),Z=L6\("paprika_mode"\);Hft\(z\);'
            r'const Y=Uft\(\),Q=.*?,X=.*?,J=.*?,ee=.*?,',
            re.DOTALL,
        ),
    ]
    ogt_model_target = (
        'z="opus[1m]",{allModelOptions:F}=R,'
        'B=Xc("sticky_model_selector"),[$,H]=n.useState(null),'
        'ke=e=>{const t=String(e??"").toLowerCase();return"kimi-for-coding"===t||/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e))?"kimi-for-coding":"opus[1m]"},'
        'Ce=F.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-k2.6"===t||"kimi-k2.6"===s||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)}),'
        'Se=Ce?.model??"kimi-for-coding",'
        'Ne={...(F.find(e=>"opus[1m]"===e.model)??F.find(e=>/opus/i.test(e.model)&&/\\[1m\\]/i.test(e.model))??F.find(e=>e.thinking_modes?.length)??{}),model:"opus[1m]",name:"Opus 4.71M",name_i18n_key:void 0,inactive:!1,overflow:!1},'
        'Re={...(Ce??F.find(e=>e.thinking_modes?.length)??{}),model:Se,name:"Kimi-k2.6",name_i18n_key:void 0,inactive:!1,overflow:!1},'
        'V=$??z,W="kimi-for-coding"===ke(V)?Re:Ne,'
        'G=n.useRef(null),Z=L6("paprika_mode");Hft(z);'
        'const Y=Uft(),Q=!0,X=[Ne,Re],J=[],ee=[],'
    )

    effort_re = re.compile(
        r'\{activeMode:te\}=Gft\(z,Z\),.*?,se=O\?.*?:te\?\.label,'
        r'\{toggleConversationSetting:ne\}=O6\(\{source:"modelSelector"\}\)',
        re.DOTALL,
    )
    effort_target = (
        '{activeMode:te}=Gft(z,Z),'
        '[me,he]=n.useState(()=>{try{return localStorage.getItem("cowork_effort_level_cn")||"max"}catch{return"max"}}),'
        'fe=n.useMemo(()=>{const e=e=>{he(e);try{localStorage.setItem("cowork_effort_level_cn",e),'
        'window.dispatchEvent(new CustomEvent("cowork-effort-change",{detail:e}))}catch{}},'
        't=_?.current??me,s=_?.onSelect??e;return{current:t,'
        'options:[{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"},{value:"xhigh",label:"超高"},{value:"max",label:"最大"}],'
        'onSelect:e=>{s(e);_?.onSelect||he(e)}}},[_,me]),'
        'se=O?{low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}[fe.current]:te?.label,'
        '{toggleConversationSetting:ne}=O6({source:"modelSelector"})'
    )

    handler_re = re.compile(
        r'const ue=e=>\{if\(e\.model===V\)return;if\(ae\(e\.model\)\)return;'
        r'if\(re\|\|!ngt\(e\.model,!1,!ie,L,ce\)\)\{',
        re.DOTALL,
    )
    handler_target = (
        'const ue=e=>{const t=String(e.model??"").toLowerCase(),s="opus"===t||"opus[1m]"===t||'
        '"kimi-for-coding"===t||/kimi/i.test(String(e.model))&&/k2\\.6/i.test(String(e.model));'
        'if(e.model===V)return;if(!s&&ae(e.model))return;if(s||re||!ngt(e.model,!1,!ie,L,ce)){'
    )
    handler_state_patterns = {
        'Y(e.model)||ne("compass_mode",null),B||H(e.model),D(e.model),i?.(e)}': (
            'Y(e.model)||ne("compass_mode",null),H(e.model),D(e.model),i?.(e)}'
        ),
        'Y(e.model)||ne("compass_mode",null),H(e.model),D(e.model),i?.(e)}': (
            'Y(e.model)||ne("compass_mode",null),H(e.model),D(e.model),i?.(e)}'
        ),
    }

    menu_effort_re = re.compile(
        r'a\.jsx\(igt,\{section:\{current:me,options:\[\{value:"low",label:"低"\},'
        r'\{value:"medium",label:"中"\},\{value:"high",label:"高"\}(?:,\{value:"xhigh",label:"超高"\},\{value:"max",label:"最大"\})?\],'
        r'onSelect:e=>\{he\(e\);try\{localStorage\.setItem\("cowork_effort_level",e\),'
        r'window\.dispatchEvent\(new CustomEvent\("cowork-effort-change",\{detail:e\}\)\)\}catch\{\}\}\},compactMenu:j\}\)',
        re.DOTALL,
    )
    menu_effort_target = 'a.jsx(igt,{section:fe,compactMenu:j})'

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "cowork_model" not in text or "const ogt=({conversationUuid" not in text:
            continue
        patched = text
        count = 0
        patched, n = vft_re.subn(vft_target, patched, count=1)
        count += n
        for source, target in wft_patterns.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        ogt_start = patched.find("ogt=({conversationUuid")
        if ogt_start >= 0:
            ogt_end = min(len(patched), ogt_start + 30000)
            ogt_chunk = patched[ogt_start:ogt_end]
            ogt_count = 0
            for ogt_model_re in ogt_model_res:
                ogt_chunk, n = ogt_model_re.subn(ogt_model_target, ogt_chunk, count=1)
                ogt_count += n
                if n:
                    break
            ogt_chunk, n = effort_re.subn(effort_target, ogt_chunk, count=1)
            ogt_count += n
            ogt_chunk, n = handler_re.subn(handler_target, ogt_chunk, count=1)
            ogt_count += n
            for source, target in handler_state_patterns.items():
                occurrences = ogt_chunk.count(source)
                if occurrences:
                    ogt_chunk = ogt_chunk.replace(source, target)
                    ogt_count += occurrences
            ogt_chunk, n = menu_effort_re.subn(menu_effort_target, ogt_chunk, count=1)
            ogt_count += n
            if ogt_count:
                patched = patched[:ogt_start] + ogt_chunk + patched[ogt_end:]
                count += ogt_count
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    return patched_files, patched_strings


def patch_epitaxy_model_menu(assets_dir: Path, runtime_model: str | None = None) -> tuple[int, int]:
    """把 Claude Code 模型菜单固定为 Opus 伪装入口、Kimi 真实入口和完整强度。"""
    patched_files = 0
    patched_strings = 0
    provider_runtime_model = safe_runtime_model_id(runtime_model)
    provider_runtime_model_js = json.dumps(provider_runtime_model, ensure_ascii=False)
    runtime_mapping_js = (
        f'zhProviderModel={provider_runtime_model_js},'
        'zhKimiModel=(M.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase(),n=String(e.label_override??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-for-coding"===n||"kimi-k2.6"===t||"kimi-k2.6"===s||"kimi-k2.6"===n||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)||/kimi.*k2\\.6/i.test(n)})?.model??("kimi-for-coding"===zhProviderModel?"kimi-for-coding":zhProviderModel)),'
        'zhRuntimeModelFor=e=>{const t=String(e??"").toLowerCase();'
        'return"opus"===t||"opus[1m]"===t?zhProviderModel:("kimi-for-coding"===t||"kimi-k2.6"===t||/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e))?zhKimiModel:(e??zhProviderModel))},'
        'zhRuntimeModel=zhRuntimeModelFor(W),'
    )

    def ensure_runtime_mapping(text: str) -> tuple[str, int]:
        """兼容已经被旧脚本半补丁过的 Code chunk：有 zhRuntimeModel 使用时补回定义。"""
        if "model:zhRuntimeModel," not in text or "zhRuntimeModelFor=" in text:
            return text, 0
        anchors = [",[V,G,W]),X=et()", ",[V,G,W]),X=nt()", ",[V,G,W]),Q=Xe()"]
        for anchor in anchors:
            if anchor in text:
                return text.replace(anchor, anchor.replace("),", f"),{runtime_mapping_js}", 1), 1), 1
        return text, 0

    # Claude 1.8089+：Code 页变量改为 ch/dh/fh/Ih/Ts。
    code_18089_current_re = re.compile(
        r'const K=e\.useCallback\(e=>null!==e&&M\.some\(t=>t\.model===e\),\[M\]\)\(S\)\?S:null,'
        r'W=B\?\?O\?\?K\?\?L\?\?k,V=M\.find\(e=>e\.model===W\),G=V\?null:Ze\(W\),'
        r'Q=e\.useMemo\(\(\)=>V\?Eh\(V\):G,\[V,G\]\),X=et\(\)',
        re.DOTALL,
    )
    code_18089_current_target = (
        'const K=e.useCallback(e=>null!==e&&M.some(t=>t.model===e),[M])(S)?S:null,'
        'W=(e=>{const t=String(e??"").toLowerCase();'
        'if(!e)return"opus";'
        'if("opus"===t||"opus[1m]"===t)return"opus";'
        'if("kimi-for-coding"===t||"kimi-k2.6"===t||/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e)))return"kimi-for-coding";'
        'const s=M.find(e=>String(e.model??"").toLowerCase()===t||String(e.name??"").toLowerCase()===t);'
        'return s?s.model:"opus"'
        '})(B??"opus"),'
        'V=M.find(e=>e.model===W),'
        'G=V?null:("opus"===W||"opus[1m]"===W?"Opus 4.71M":("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W))?"Kimi-k2.6":Ze(W))),'
        'Q=e.useMemo(()=>("opus"===W||"opus[1m]"===W)?"Opus 4.71M":("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?"Kimi-k2.6":V?Eh(V):G,[V,G,W]),'
        f'{runtime_mapping_js}X=et()'
    )
    code_18089_items_re = re.compile(
        r'fe=e\.useMemo\(\(\)=>\{const e=M\.map\(e=>\{const t=C\.includes\(e\.model\);return\{label:t\?.*?'
        r'\},\[M,C,W,ue,re,G,n\]\),pe=e\.useMemo\(\(\)=>\{if\(!de\)return fe;'
        r'const\[e,\.\.\.t\]=fe;return e\?\[de,\{...e,separatorBefore:!0\},\.\.\.t\]:\[de\]\},\[de,fe\]\)',
        re.DOTALL,
    )
    code_18089_items_target = (
        'fe=e.useMemo(()=>{'
        'const e={label:"Opus 4.71M",checked:"opus"===W||"opus[1m]"===W,onSelect:()=>ue.current("opus"),disabled:ie},'
        't=M.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase(),n=String(e.label_override??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-for-coding"===n||"kimi-k2.6"===t||"kimi-k2.6"===s||"kimi-k2.6"===n||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)||/kimi.*k2\\.6/i.test(n)}),'
        's=t?.model??"kimi-for-coding",'
        'n={label:"Kimi-k2.6",checked:String(W).toLowerCase()===String(s).toLowerCase()||"kimi-for-coding"===String(W).toLowerCase()||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)),onSelect:()=>ue.current(s),disabled:ie};'
        'return[e,n]},[M,W,ue,ie]),pe=fe'
    )
    code_18089_xs_re = re.compile(
        r'Ts=e\.useMemo\(\(\)=>\{const e=\[\];if\(Es\)\{const t=dh\.filter\(e=>\("max"!==e\|\|At\)&&\("xhigh"!==e\|\|Dt\)\);'
        r'e\.push\(\{key:"effort",header:n\.formatMessage\(Mh\.effortHeader\),items:t\.map\(e=>\(\{label:n\.formatMessage\(kh\[e\]\),checked:e===Is,onSelect:\(\)=>Ps\(e\)\}\)\)\}\)\}'
        r'if\(ks\)\{const t=null!==Ms;e\.push\(\{key:"fastMode",header:n\.formatMessage\(Mh\.fastModeHeader\),items:\[\{label:n\.formatMessage\(Mh\.fastModeToggleLabel\),keepOpen:!0,disabled:t,onSelect:t\?void 0:\(\)=>De\(!Ae\),tooltip:Ms\?\?n\.formatMessage\(Mh\.fastModeToggleHint\),tooltipSide:"left",tooltipMultiline:!0,trailing:l\.jsx\(Iu,\{checked:!t&&Ae,disabled:t,onCheckedChange:De,"aria-hidden":!0,tabIndex:-1\}\)\}\]\}\)\}return e\},\[Es,At,Dt,Is,Ps,ks,Ms,Ae,De,n\]\)',
        re.DOTALL,
    )
    code_18089_xs_target = (
        'Ts=e.useMemo(()=>{const e=[],t=dh;'
        'e.push({key:"effort",header:n.formatMessage(Mh.effortHeader),items:t.map(e=>({label:n.formatMessage(kh[e]),checked:e===Is,onSelect:()=>Ps(e)}))});'
        'if(ks){const t=null!==Ms;e.push({key:"fastMode",header:n.formatMessage(Mh.fastModeHeader),items:[{label:n.formatMessage(Mh.fastModeToggleLabel),keepOpen:!0,disabled:t,onSelect:t?void 0:()=>De(!Ae),tooltip:Ms??n.formatMessage(Mh.fastModeToggleHint),tooltipSide:"left",tooltipMultiline:!0,trailing:l.jsx(Iu,{checked:!t&&Ae,disabled:t,onCheckedChange:De,"aria-hidden":!0,tabIndex:-1})}]})}return e},[Is,Ps,ks,Ms,Ae,De,n])'
    )
    code_18089_replacements = {
        'const ch="ccd-effort-level",dh=': 'const ch="ccd-effort-level-cn",dh=',
        'const ch="ccd-effort-level-cn",dh=': 'const ch="ccd-effort-level-cn",dh=',
        'kh=c({low:{defaultMessage:"Low",id:"477I0ggSYe"},medium:{defaultMessage:"Medium",id:"ovJ26CKo4Q"},high:{defaultMessage:"High",id:"AxMhQrcUDC"},xhigh:{defaultMessage:"Extra high",id:"kDEj60CmLq"},max:{defaultMessage:"Max",id:"kkjl2vQekD"}})': (
            'kh=c({low:{defaultMessage:"低",id:"477I0ggSYe"},medium:{defaultMessage:"中",id:"ovJ26CKo4Q"},high:{defaultMessage:"高",id:"AxMhQrcUDC"},xhigh:{defaultMessage:"超高",id:"kDEj60CmLq"},max:{defaultMessage:"最大",id:"kkjl2vQekD"}})'
        ),
        'h=p??c??f??function(e){return e.toLowerCase().includes("opus-4-7")?lh()?"xhigh":"high":"medium"}(t),g="max"===h&&!r||"xhigh"===h&&!o?"high":h;return{effortLevel:g,spawnEffortLevel:u&&null===p&&null===f?void 0:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(ch,e),m(e)},[]),modelSupportsEffort:i,modelSupportsMaxEffort:r,modelSupportsXhighEffort:o}': (
            'h=p??f??"max",g=h;return{effortLevel:g,spawnEffortLevel:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(ch,e),m(e)},[]),modelSupportsEffort:!0,modelSupportsMaxEffort:!0,modelSupportsXhighEffort:!0}'
        ),
        'effort:Ne?We:void 0,repoInfo': 'effort:We,repoInfo',
        'model:W,': 'model:zhRuntimeModel,',
        'await(Z(te)?.setModel?.(te.id,e)),ne(te,{model:e})': (
            'await(Z(te)?.setModel?.(te.id,zhRuntimeModelFor(e))),ne(te,{model:zhRuntimeModelFor(e)})'
        ),
        'Promise.resolve(Y(J,e)).then(()=>{ne({id:J,type:"local"},{model:e})})': (
            'Promise.resolve(Y(J,zhRuntimeModelFor(e))).then(()=>{ne({id:J,type:"local"},{model:zhRuntimeModelFor(e)})})'
        ),
    }

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if 'const ch="ccd-effort-level' not in text or "modelExtraSections:Ts" not in text:
            continue
        patched = text
        count = 0
        patched, n = code_18089_current_re.subn(code_18089_current_target, patched, count=1)
        count += n
        patched, n = code_18089_items_re.subn(code_18089_items_target, patched, count=1)
        count += n
        patched, n = code_18089_xs_re.subn(code_18089_xs_target, patched, count=1)
        count += n
        for source, target in code_18089_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        patched, n = ensure_runtime_mapping(patched)
        count += n
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    # Claude 1.7196+：Code 页变量改为 zm/Um/Hm，模型菜单项在 fe/pe 中生成。
    code_17196_current_re = re.compile(
        r'const K=e\.useCallback\(e=>null!==e&&M\.some\(t=>t\.model===e\),\[M\]\)\(S\)\?S:null,'
        r'W=H\?\?O\?\?L\?\?K\?\?k,V=M\.find\(e=>e\.model===W\),G=V\?null:Ze\(W\),'
        r'Q=e\.useMemo\(\(\)=>V\?ah\(V\):G,\[V,G\]\),X=et\(\)',
        re.DOTALL,
    )
    code_17196_current_target = (
        'const K=e.useCallback(e=>null!==e&&M.some(t=>t.model===e),[M])(S)?S:null,'
        'W=(e=>{const t=String(e??"").toLowerCase();'
        'if(!e)return"opus[1m]";'
        'if("opus"===t||"opus[1m]"===t)return"opus[1m]";'
        'if("kimi-for-coding"===t||"kimi-k2.6"===t||/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e)))return"kimi-for-coding";'
        'const s=M.find(e=>String(e.model??"").toLowerCase()===t||String(e.name??"").toLowerCase()===t);'
        'return s?s.model:"opus[1m]"'
        '})(H??"opus[1m]"),'
        'V=M.find(e=>e.model===W),'
        'G=V?null:("opus"===W||"opus[1m]"===W?"Opus 4.71M":("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W))?"Kimi-k2.6":Ze(W))),'
        'Q=e.useMemo(()=>("opus"===W||"opus[1m]"===W)?"Opus 4.71M":("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?"Kimi-k2.6":V?ah(V):G,[V,G,W]),'
        f'{runtime_mapping_js}X=et()'
    )
    code_17196_items_re = re.compile(
        r'fe=e\.useMemo\(\(\)=>\{const e=M\.map\(e=>\{const t=C\.includes\(e\.model\);return\{label:t\?.*?'
        r'\},\[M,C,W,ue,re,G,n\]\),pe=e\.useMemo\(\(\)=>\{if\(!de\)return fe;'
        r'const\[e,\.\.\.t\]=fe;return e\?\[de,\{...e,separatorBefore:!0\},\.\.\.t\]:\[de\]\},\[de,fe\]\)',
        re.DOTALL,
    )
    code_17196_items_target = (
        'fe=e.useMemo(()=>{'
        'const e={label:"Opus 4.71M",checked:"opus"===W||"opus[1m]"===W,onSelect:()=>ue.current("opus[1m]"),disabled:ie},'
        't=M.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase(),n=String(e.label_override??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-for-coding"===n||"kimi-k2.6"===t||"kimi-k2.6"===s||"kimi-k2.6"===n||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)||/kimi.*k2\\.6/i.test(n)}),'
        's=t?.model??"kimi-for-coding",'
        'n={label:"Kimi-k2.6",checked:String(W).toLowerCase()===String(s).toLowerCase()||"kimi-for-coding"===String(W).toLowerCase()||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)),onSelect:()=>ue.current(s),disabled:ie};'
        'return[e,n]},[M,W,ue,ie]),pe=fe'
    )
    code_17196_gm_replacements = {
        'const zm="ccd-effort-level",Lm=["low","medium","high","xhigh","max"],Om={low:"Low",medium:"Medium",high:"High",xhigh:"Extra high",max:"Max"}': (
            'const zm="ccd-effort-level-cn",Lm=["low","medium","high","xhigh","max"],Om={low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}'
        ),
        'h=p??c??f??function(e){return e.toLowerCase().includes("opus-4-7")?Fm()?"xhigh":"high":"medium"}(t),g="max"===h&&!r||"xhigh"===h&&!o?"high":h;return{effortLevel:g,spawnEffortLevel:u&&null===p&&null===f?void 0:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(zm,e),m(e)},[]),modelSupportsEffort:i,modelSupportsMaxEffort:r,modelSupportsXhighEffort:o}': (
            'h=p??f??"max",g=h;return{effortLevel:g,spawnEffortLevel:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(zm,e),m(e)},[]),modelSupportsEffort:!0,modelSupportsMaxEffort:!0,modelSupportsXhighEffort:!0}'
        ),
        'x=g.success?"max"===g.data&&!p||"xhigh"===g.data&&!m?"high":g.data:void 0,v=h.current!==n&&void 0!==x?x:c,b=f&&(void 0!==n?!!l&&!!n:s);return{section:e.useMemo(()=>{if(!b)return;const e=Lm.filter(e=>("max"!==e||p)&&("xhigh"!==e||m));return{current:v,options:e.map(e=>({value:e,label:Om[e]})),onSelect:e=>{': (
            'x=g.success?g.data:void 0,v=h.current!==n&&void 0!==x?x:c,b=!0;return{section:e.useMemo(()=>{const e=Lm;return{current:v,options:e.map(e=>({value:e,label:Om[e]})),onSelect:e=>{'
        ),
        'spawnEffort:b?d:void 0': 'spawnEffort:d',
        'effort:Ae?Te:void 0,repoInfo': 'effort:Te,repoInfo',
        'model:W,': 'model:zhRuntimeModel,',
        'await(Z(te)?.setModel?.(te.id,e)),ne(te,{model:e})': (
            'await(Z(te)?.setModel?.(te.id,zhRuntimeModelFor(e))),ne(te,{model:zhRuntimeModelFor(e)})'
        ),
        'Promise.resolve(Y(J,e)).then(()=>{ne({id:J,type:"local"},{model:e})})': (
            'Promise.resolve(Y(J,zhRuntimeModelFor(e))).then(()=>{ne({id:J,type:"local"},{model:zhRuntimeModelFor(e)})})'
        ),
    }

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if 'const zm="ccd-effort-level' not in text or "modelExtraSections:Ss" not in text:
            continue
        patched = text
        count = 0
        patched, n = code_17196_current_re.subn(code_17196_current_target, patched, count=1)
        count += n
        patched, n = code_17196_items_re.subn(code_17196_items_target, patched, count=1)
        count += n
        for source, target in code_17196_gm_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        patched, n = ensure_runtime_mapping(patched)
        count += n
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    # Claude 1.6608+：Code 页模型菜单在 zm() 内部生成，强度来自 hm()/gm() 与 xs。
    # 旧版 ps/Od(W) 类补丁无法覆盖这里，所以单独处理新版结构。
    code_current_re = re.compile(
        r'const K=e\.useCallback\(e=>null!==e&&M\.some\(t=>t\.model===e\),\[M\]\)\(S\)\?S:null,'
        r'W=H\?\?O\?\?L\?\?K\?\?k,V=M\.find\(e=>e\.model===W\),G=V\?null:st\(W\),'
        r'Q=e\.useMemo\(\(\)=>V\?Fm\(V\):G,\[V,G\]\),X=nt\(\)',
        re.DOTALL,
    )
    code_current_target = (
        'const K=e.useCallback(e=>null!==e&&M.some(t=>t.model===e),[M])(S)?S:null,'
        'W=(e=>{const t=String(e??"").toLowerCase();'
        'if(!e)return"opus[1m]";'
        'if("kimi-for-coding"===t||"kimi-k2.6"===t)return"kimi-for-coding";'
        'if("opus"===t||"opus[1m]"===t)return"opus[1m]";'
        'const s=M.find(e=>String(e.model??"").toLowerCase()===t||String(e.name??"").toLowerCase()===t);'
        'return s?s.model:(/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e))?"kimi-for-coding":"opus[1m]")'
        '})(H??"opus[1m]"),'
        'V=M.find(e=>e.model===W),'
        'G=V?null:("opus"===W||"opus[1m]"===W?"Opus 4.71M":'
        '("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W))?"Kimi-k2.6":st(W))),'
        'Q=e.useMemo(()=>("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?'
        '"Kimi-k2.6":V?Fm(V):G,[V,G,W]),'
        f'{runtime_mapping_js}X=nt()'
    )
    code_items_re = re.compile(
        r'pe=e\.useMemo\(\(\)=>\{const e=M\.map\(e=>\{const t=C\.includes\(e\.model\);return\{label:t\?.*?'
        r'\},\[M,C,W,ue,ie,oe,G,s\]\),me=e\.useMemo\(\(\)=>\{if\(!de\)return pe;'
        r'const\[e,\.\.\.t\]=pe;return e\?\[de,\{...e,separatorBefore:!0\},\.\.\.t\]:\[de\]\},\[de,pe\]\)',
        re.DOTALL,
    )
    code_items_target = (
        'pe=e.useMemo(()=>{'
        'const i=e=>"kimi-for-coding"===String(e).toLowerCase()||/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e)),'
        'o=M.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-k2.6"===t||"kimi-k2.6"===s||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)}),'
        'l=o?.model??"kimi-for-coding";'
        'return['
        '{label:"Opus 4.71M",checked:"opus"===W||"opus[1m]"===W,onSelect:()=>ue.current("opus[1m]"),disabled:ie},'
        '{label:"Kimi-k2.6",checked:String(W).toLowerCase()===String(l).toLowerCase()||i(W),onSelect:()=>ue.current(l),disabled:ie}'
        ']},[M,W,ue,ie]),me=pe'
    )
    code_xs_re = re.compile(
        r'xs=e\.useMemo\(\(\)=>\{const e=\[\];if\(ms\)\{const t=fm\.filter\(e=>\("max"!==e\|\|Fe\)&&\("xhigh"!==e\|\|Oe\)\);'
        r'e\.push\(\{key:"effort",header:s\.formatMessage\(Pm\.effortHeader\),items:t\.map\(e=>\(\{label:s\.formatMessage\(Em\[e\]\),checked:e===hs,onSelect:\(\)=>gs\(e\)\}\)\)\}\)\}'
        r'if\(ls\)\{const t=null!==cs;e\.push\(\{key:"fastMode",header:s\.formatMessage\(Pm\.fastModeHeader\),items:\[\{label:s\.formatMessage\(Pm\.fastModeToggleLabel\),'
        r'keepOpen:!0,disabled:t,onSelect:t\?void 0:\(\)=>Pe\(!Ee\),tooltip:cs\?\?s\.formatMessage\(Pm\.fastModeToggleHint\),tooltipSide:"left",tooltipMultiline:!0,'
        r'trailing:c\.jsx\(Tu,\{checked:!t&&Ee,disabled:t,"aria-hidden":!0,tabIndex:-1\}\)\}\]\}\)\}return e\},\[ms,Fe,Oe,hs,gs,ls,cs,Ee,Pe,s\]\)',
        re.DOTALL,
    )
    code_xs_target = (
        'xs=e.useMemo(()=>{const e=[],t=fm;'
        'e.push({key:"effort",header:s.formatMessage(Pm.effortHeader),items:t.map(e=>({label:s.formatMessage(Em[e]),checked:e===hs,onSelect:()=>gs(e)}))});'
        'if(ls){const t=null!==cs;e.push({key:"fastMode",header:s.formatMessage(Pm.fastModeHeader),items:[{label:s.formatMessage(Pm.fastModeToggleLabel),'
        'keepOpen:!0,disabled:t,onSelect:t?void 0:()=>Pe(!Ee),tooltip:cs??s.formatMessage(Pm.fastModeToggleHint),tooltipSide:"left",tooltipMultiline:!0,'
        'trailing:c.jsx(Tu,{checked:!t&&Ee,disabled:t,"aria-hidden":!0,tabIndex:-1})}]})}return e},[hs,gs,ls,cs,Ee,Pe,s])'
    )
    code_new_replacements = {
        'const um="ccd-effort-level",fm=': 'const um="ccd-effort-level-cn",fm=',
        'const um="ccd-effort-level-cn",fm=': 'const um="ccd-effort-level-cn",fm=',
        'h=p??c??f??function(e){return e.toLowerCase().includes("opus-4-7")?dm()?"xhigh":"high":"medium"}(t),g=h;return{effortLevel:g,spawnEffortLevel:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(um,e),m(e)},[]),modelSupportsEffort:!0,modelSupportsMaxEffort:!0,modelSupportsXhighEffort:!0}': (
            'h=p??f??"max",g=h;return{effortLevel:g,spawnEffortLevel:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(um,e),m(e)},[]),modelSupportsEffort:!0,modelSupportsMaxEffort:!0,modelSupportsXhighEffort:!0}'
        ),
        'x=g.success?g.data:void 0,v=h.current!==n&&void 0!==x?x:c,b=!0;return{section:e.useMemo(()=>{const e=fm;return{current:v,options:e.map(e=>({value:e,label:pm[e]})),onSelect:e=>{': (
            'x=void 0,v=c,b=!0;return{section:e.useMemo(()=>{const e=fm;return{current:v,options:e.map(e=>({value:e,label:pm[e]})),onSelect:e=>{'
        ),
        '})(H??O??L??K??k),V=M.find(e=>e.model===W),': '})(H??"opus[1m]"),V=M.find(e=>e.model===W),',
        '})(H??"opus[1m]"),V=M.find(e=>e.model===W),': '})(H??"opus[1m]"),V=M.find(e=>e.model===W),',
        'pm={low:"Low",medium:"Medium",high:"High",xhigh:"Extra high",max:"Max"}': (
            'pm={low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}'
        ),
        'g="max"===h&&!r||"xhigh"===h&&!o?"high":h;return{effortLevel:g,spawnEffortLevel:u&&null===p&&null===f?void 0:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(um,e),m(e)},[]),modelSupportsEffort:i,modelSupportsMaxEffort:r,modelSupportsXhighEffort:o}': (
            'g=h;return{effortLevel:g,spawnEffortLevel:g,setEffortLevel:e.useCallback(e=>{localStorage.setItem(um,e),m(e)},[]),modelSupportsEffort:!0,modelSupportsMaxEffort:!0,modelSupportsXhighEffort:!0}'
        ),
        'x=g.success?"max"===g.data&&!p||"xhigh"===g.data&&!m?"high":g.data:void 0,v=h.current!==n&&void 0!==x?x:c,b=f&&(void 0!==n?!!l&&!!n:s);return{section:e.useMemo(()=>{if(!b)return;const e=fm.filter(e=>("max"!==e||p)&&("xhigh"!==e||m));return{current:v,options:e.map(e=>({value:e,label:pm[e]})),onSelect:e=>{': (
            'x=g.success?g.data:void 0,v=h.current!==n&&void 0!==x?x:c,b=!0;return{section:e.useMemo(()=>{const e=fm;return{current:v,options:e.map(e=>({value:e,label:pm[e]})),onSelect:e=>{'
        ),
        'spawnEffort:b?d:void 0}': 'spawnEffort:d}',
        'ms=De&&(t?!!ps:"bridge"!==rs),hs=': 'ms=!0,hs=',
        'He=Ue.success?"max"===Ue.data&&!Fe||"xhigh"===Ue.data&&!Oe?"high":Ue.data:void 0': (
            'He=Ue.success?Ue.data:void 0'
        ),
        'effort:De?_e:void 0,repoInfo': 'effort:_e,repoInfo',
        'model:W,': 'model:zhRuntimeModel,',
        'await(Z(te)?.setModel?.(te.id,e)),ne(te,{model:e})': (
            'await(Z(te)?.setModel?.(te.id,zhRuntimeModelFor(e))),ne(te,{model:zhRuntimeModelFor(e)})'
        ),
        'Promise.resolve(Y(J,e)).then(()=>{ne({id:J,type:"local"},{model:e})})': (
            'Promise.resolve(Y(J,zhRuntimeModelFor(e))).then(()=>{ne({id:J,type:"local"},{model:zhRuntimeModelFor(e)})})'
        ),
    }

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if 'const um="ccd-effort-level' not in text or "modelExtraSections:xs" not in text:
            continue
        patched = text
        count = 0
        patched, n = code_current_re.subn(code_current_target, patched, count=1)
        count += n
        patched, n = code_items_re.subn(code_items_target, patched, count=1)
        count += n
        patched, n = code_xs_re.subn(code_xs_target, patched, count=1)
        count += n
        for source, target in code_new_replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        patched, n = ensure_runtime_mapping(patched)
        count += n
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count
    kimi_match = r'("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\.6/i.test(String(W)))'
    custom_effort_support = f'("opus"===W||"opus[1m]"===W||{kimi_match})'
    effort_support = f'(_e||"opus"===W||"opus[1m]"===W||{kimi_match})'
    effort_menu_support = f'({custom_effort_support}||(_e&&(t?!!fs:"bridge"!==is)))'

    z_name_re = re.compile(
        r'function Zp\(e\)\{(?:if\("opus\[1m\]"===e\?\.model\|\|"opus"===e\?\.model\)'
        r'return"Opus 4\.7(?: ?1)?M";)?const t=Ct\(e\.model\);'
    )
    z_name_target = (
        'function Zp(e){if("opus[1m]"===e?.model||"opus"===e?.model)'
        'return"Opus 4.71M";const t=Ct(e.model);'
    )

    current_model_re = re.compile(
        r'const K=e\.useCallback\(e=>null!==e&&M\.some\(t=>t\.model===e\),\[M\]\)\(S\)\?S:null,'
        r'W=.*?,V=M\.find\(e=>e\.model===W\),G=.*?,'
        r'X=e\.useMemo\(\(\)=>V\?Zp\(V\):G,\[V,G\]\),Q=Xe\(\)',
        re.DOTALL,
    )
    current_model_target = (
        'const K=e.useCallback(e=>null!==e&&M.some(t=>t.model===e),[M])(S)?S:null,'
        'W=(e=>{const t=String(e??"").toLowerCase();'
        'if(!e)return"opus[1m]";'
        'if("kimi-for-coding"===t||"kimi-k2.6"===t)return"kimi-for-coding";'
        'if("opus"===t||"opus[1m]"===t)return"opus[1m]";'
        'const s=M.find(e=>String(e.model??"").toLowerCase()===t||String(e.name??"").toLowerCase()===t);'
        'return s?s.model:(/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e))?'
        '(t==="kimi-k2.6"?"kimi-for-coding":e):"opus[1m]")'
        '})(U??"opus[1m]"),'
        'V=M.find(e=>e.model===W),'
        'G=V?null:("opus"===W||"opus[1m]"===W?"Opus 4.71M":'
        '("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W))?"Kimi-k2.6":Ge(W))),'
        'X=e.useMemo(()=>("kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?'
        '"Kimi-k2.6":V?Zp(V):G,[V,G,W]),'
        f'{runtime_mapping_js}Q=Xe()'
    )

    model_items_res = [
        re.compile(
            r'pe=e\.useMemo\(\(\)=>\{const e=\{label:"Opus 4\.71M".*?return\[e,a\]\},\[M,W,ue,ie\]\)',
            re.DOTALL,
        ),
        re.compile(
            r'pe=e\.useMemo\(\(\)=>\{.*?\},\[M,C,W,ue,ie,oe,G,s\]\)',
            re.DOTALL,
        ),
    ]
    model_items_target = (
        'pe=e.useMemo(()=>{'
        'const i=e=>"kimi-for-coding"===String(e).toLowerCase()||/kimi/i.test(String(e))&&/k2\\.6/i.test(String(e)),'
        'o=e=>{const t=String(e??"").toLowerCase();return"kimi-k2.6"===t?'
        '"kimi-for-coding":(i(e)?e:"kimi-for-coding")},'
        'e={label:"Opus 4.71M",checked:"opus"===W||"opus[1m]"===W,'
        'onSelect:()=>ue.current("opus[1m]"),disabled:ie},'
        't=M.find(e=>{const t=String(e.model??"").toLowerCase(),s=String(e.name??"").toLowerCase();'
        'return"kimi-for-coding"===t||"kimi-for-coding"===s||"kimi-k2.6"===t||"kimi-k2.6"===s||/kimi.*k2\\.6/i.test(t)||/kimi.*k2\\.6/i.test(s)}),'
        'n=t?.model??o(W),'
        'a={label:"Kimi-k2.6",checked:String(W).toLowerCase()===String(n).toLowerCase()||i(W)&&i(n),'
        'onSelect:()=>ue.current(n),disabled:ie};'
        'return[e,a]},[M,W,ue,ie])'
    )

    effort_state_res = [
        re.compile(
            r'ms=Ct\.current!==fs&&void 0!==It\?It:Ee,hs=e\.useCallback\(e=>\{.*?\},'
            r'\[ms,Ae,fs,cs,us,Z,ne,a,s\]\),',
            re.DOTALL,
        ),
        re.compile(
            r'\[codeEffort,setCodeEffort\]=e\.useState\(\(\)=>\{try\{return localStorage\.getItem\("epitaxy_effort_level"\)\|\|null\}catch\{return null\}\}\),'
            r'ms=.*?hs=e\.useCallback\(t=>\{.*?\},\[ms,Ae,fs,cs,us,Z,ne,a,s\]\),',
            re.DOTALL,
        ),
    ]
    effort_state_target = (
        '[codeEffort,setCodeEffort]=e.useState(()=>{try{return localStorage.getItem("epitaxy_effort_level_cn")||"max"}catch{return"max"}}),'
        'ms=codeEffort??"max",'
        'hs=e.useCallback(t=>{if(t===ms)return;Ct.current=fs,setCodeEffort(t);'
        'try{localStorage.setItem("epitaxy_effort_level_cn",t)}catch{}'
        'Ae(t);const e=e=>{a(s.formatMessage({defaultMessage:"Effort change couldn\'t be applied. You can try again.",id:"NiIv1JQ3Vw"}),'
        '{error:e,errorContext:{tags:{source:"epitaxy_set_effort"}},messageForLogging:"Effort change couldn\'t be applied. You can try again."})};'
        'us?Z(us)?.setEffort?.(us.id,t).then(()=>ne(us,{effort:t})).catch(e):'
        'fs&&cs&&Promise.resolve(cs(fs,t)).then(()=>ne({id:fs,type:"local"},{effort:t})).catch(e)},'
        '[ms,Ae,fs,cs,us,Z,ne,a,s]),'
    )
    effort_section_target = (
        'gs=e.useMemo(()=>{const e=[],t=["low","medium","high","xhigh","max"];'
        'e.push({key:"effort",header:s.formatMessage(Gp.effortHeader),'
        'items:t.map(e=>({label:s.formatMessage(Vp[e]),checked:e===ms,onSelect:()=>hs(e)}))});'
        'if(os){const t=null!==ls;e.push({key:"fastMode",header:s.formatMessage(Gp.fastModeHeader),'
        'items:[{label:s.formatMessage(Gp.fastModeToggleLabel),keepOpen:!0,disabled:t,'
        'onSelect:t?void 0:()=>Pe(!Ie),tooltip:ls??s.formatMessage(Gp.fastModeToggleHint),'
        'tooltipSide:"left",tooltipMultiline:!0,'
        'trailing:r.jsx(Id,{checked:!t&&Ie,disabled:t,"aria-hidden":!0,tabIndex:-1})}]})}'
        'return e},[ms,hs,os,ls,Ie,Pe,s])'
    )

    effort_patterns = {
        'ps=_e&&(t?!!fs:"bridge"!==is),ms=': f'ps={effort_menu_support},ms=',
        'ps=(_e||"opus"===W||"opus[1m]"===W)&&(t?!!fs:"bridge"!==is),ms=': f'ps={effort_menu_support},ms=',
        'ps=(_e||"opus"===W||"opus[1m]"===W||"Kimi-k2.6"===W)&&(t?!!fs:"bridge"!==is),ms=': f'ps={effort_menu_support},ms=',
        'ps=(_e||"opus"===W||"opus[1m]"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))&&(t?!!fs:"bridge"!==is),ms=': f'ps={effort_menu_support},ms=',
        'ps=(_e||"opus"===W||"opus[1m]"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))&&(t?!!fs:"bridge"!==is),[codeEffort': f'ps={effort_menu_support},[codeEffort',
        '})(U??O??L??K??k),V=M.find(e=>e.model===W),': '})(U??"opus[1m]"),V=M.find(e=>e.model===W),',
        '})(U??"opus[1m]"),V=M.find(e=>e.model===W),': '})(U??"opus[1m]"),V=M.find(e=>e.model===W),',
        f'ps={effort_support}&&(t?!!fs:"bridge"!==is),ms=': f'ps={effort_menu_support},ms=',
        f'ps={effort_support}&&(t?!!fs:"bridge"!==is),[codeEffort': f'ps={effort_menu_support},[codeEffort',
        f'ps={effort_menu_support},ms=': f'ps={effort_menu_support},ms=',
        f'ps={effort_menu_support},[codeEffort': f'ps={effort_menu_support},[codeEffort',
        'effort:_e?Te:void 0,repoInfo': f'effort:{effort_support}?ms:void 0,repoInfo',
        'effort:(_e||"opus"===W||"opus[1m]"===W)?Te:void 0,repoInfo': f'effort:{effort_support}?ms:void 0,repoInfo',
        'effort:(_e||"opus"===W||"opus[1m]"===W||"Kimi-k2.6"===W)?Te:void 0,repoInfo': f'effort:{effort_support}?ms:void 0,repoInfo',
        'effort:(_e||"opus"===W||"opus[1m]"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?Te:void 0,repoInfo': f'effort:{effort_support}?ms:void 0,repoInfo',
        f'effort:{effort_support}?Te:void 0,repoInfo': f'effort:{effort_support}?ms:void 0,repoInfo',
        'const t=Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));': (
            'const t=("opus"===W||"opus[1m]"===W||"kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))'
            '?["low","medium","high","xhigh","max"]:Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));'
        ),
        'const t=("opus"===W||"opus[1m]"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?Ud:Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));': (
            'const t=("opus"===W||"opus[1m]"===W||"kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))'
            '?["low","medium","high","xhigh","max"]:Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));'
        ),
        'const t=("opus"===W||"opus[1m]"===W||"kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?["low","medium","high","xhigh","max"]:Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));': (
            'const t=("opus"===W||"opus[1m]"===W||"kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))'
            '?["low","medium","high","xhigh","max"]:Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));'
        ),
        'gs=e.useMemo(()=>{const e=[];if(ps){const t=Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));e.push({key:"effort",header:s.formatMessage(Gp.effortHeader),items:t.map(e=>({label:s.formatMessage(Vp[e]),checked:e===ms,onSelect:()=>hs(e)}))})}if(os){const t=null!==ls;e.push({key:"fastMode",header:s.formatMessage(Gp.fastModeHeader),items:[{label:s.formatMessage(Gp.fastModeToggleLabel),keepOpen:!0,disabled:t,onSelect:t?void 0:()=>Pe(!Ie),tooltip:ls??s.formatMessage(Gp.fastModeToggleHint),tooltipSide:"left",tooltipMultiline:!0,trailing:r.jsx(Id,{checked:!t&&Ie,disabled:t,"aria-hidden":!0,tabIndex:-1})}]})}return e},[ps,De,ze,ms,hs,os,ls,Ie,Pe,s])': effort_section_target,
        'gs=e.useMemo(()=>{const e=[];if(ps){const t=("opus"===W||"opus[1m]"===W||"kimi-for-coding"===W||/kimi/i.test(String(W))&&/k2\\.6/i.test(String(W)))?["low","medium","high","xhigh","max"]:Ud.filter(e=>("max"!==e||De)&&("xhigh"!==e||ze));e.push({key:"effort",header:s.formatMessage(Gp.effortHeader),items:t.map(e=>({label:s.formatMessage(Vp[e]),checked:e===ms,onSelect:()=>hs(e)}))})}if(os){const t=null!==ls;e.push({key:"fastMode",header:s.formatMessage(Gp.fastModeHeader),items:[{label:s.formatMessage(Gp.fastModeToggleLabel),keepOpen:!0,disabled:t,onSelect:t?void 0:()=>Pe(!Ie),tooltip:ls??s.formatMessage(Gp.fastModeToggleHint),tooltipSide:"left",tooltipMultiline:!0,trailing:r.jsx(Id,{checked:!t&&Ie,disabled:t,"aria-hidden":!0,tabIndex:-1})}]})}return e},[ps,De,ze,ms,hs,os,ls,Ie,Pe,s,W])': effort_section_target,
        effort_section_target: effort_section_target,
        '[ps,De,ze,ms,hs,os,ls,Ie,Pe,s])': '[ps,De,ze,ms,hs,os,ls,Ie,Pe,s,W])',
        'model:W,': 'model:zhRuntimeModel,',
        'await(Z(te)?.setModel?.(te.id,e)),ne(te,{model:e})': (
            'await(Z(te)?.setModel?.(te.id,zhRuntimeModelFor(e))),ne(te,{model:zhRuntimeModelFor(e)})'
        ),
        'Promise.resolve(Y(J,e)).then(()=>{ne({id:J,type:"local"},{model:e})})': (
            'Promise.resolve(Y(J,zhRuntimeModelFor(e))).then(()=>{ne({id:J,type:"local"},{model:zhRuntimeModelFor(e)})})'
        ),
    }

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "function em(t){const s=i()" not in text or "modelExtraSections:gs" not in text:
            continue
        patched = text
        count = 0
        patched, n = z_name_re.subn(z_name_target, patched, count=1)
        count += n
        patched, n = current_model_re.subn(current_model_target, patched, count=1)
        count += n
        for model_items_re in model_items_res:
            patched, n = model_items_re.subn(model_items_target, patched, count=1)
            count += n
            if n:
                break
        for effort_state_re in effort_state_res:
            patched, n = effort_state_re.subn(effort_state_target, patched, count=1)
            count += n
            if n:
                break
        for source, target in effort_patterns.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        patched, n = ensure_runtime_mapping(patched)
        count += n
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    return patched_files, patched_strings


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
        '&&(i.CLAUDE_CODE_OAUTH_TOKEN="",i.CLAUDE_CODE_ENTRYPOINT="claude-desktop-3p"),i'
        '}catch{return{}}}'
    )
    try:
        content = read_asar_text(app, ASAR_PATCH_TARGET)
    except Exception as exc:
        raise SystemExit(f"读取 app.asar 失败：{exc}") from exc

    helper_present = "function zhClaudeCodeGatewayEnv()" in content
    spread_present = "...t.sessionEnvVars(),...zhClaudeCodeGatewayEnv()}}" in content
    if helper_present and spread_present:
        print("Claude Code gateway env injection already patched in app.asar")
        return True

    replacements: dict[str, str] = {}
    if not helper_present:
        source = "function lj(e){"
        if source not in content:
            print("Claude Code gateway env injection target function not found in app.asar")
            return False
        replacements[source] = helper + source
    if not spread_present:
        source = "...t.sessionEnvVars()}}"
        if source not in content:
            print("Claude Code gateway env injection spread point not found in app.asar")
            return False
        replacements[source] = "...t.sessionEnvVars(),...zhClaudeCodeGatewayEnv()}}"

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
    spread_count = content.count("...t.sessionEnvVars(),...zhClaudeCodeGatewayEnv()}}")
    settings_path_count = content.count('tA.join(Bi.homedir(),".claude","settings.json")')
    ok = helper_count > 0 and spread_count > 0 and settings_path_count > 0
    return (
        ok,
        (
            f"helper_count={helper_count}; spread_count={spread_count}; "
            f"settings_path_count={settings_path_count}; "
            "static_marker_only=true; runtime_env_must_be_checked_by_pre_repair_active_code_env"
        ),
        helper_count + spread_count + settings_path_count,
    )


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


def patch_native_menu_role_labels(app: Path) -> None:
    replacements = {
        '{role:"services"}': '{label:"服务",role:"services"}',
        '{role:"hide"}': '{label:"隐藏 Claude",role:"hide"}',
        '{role:"hideOthers"}': '{label:"隐藏其他",role:"hideOthers"}',
        '{role:"unhide"}': '{label:"全部显示",role:"unhide"}',
        '{role:"minimize"}': '{label:"最小化",role:"minimize"}',
        '{role:"front"}': '{label:"全部置于前面",role:"front"}',
    }
    replacements.update(DEV_MENU_LABEL_REPLACEMENTS)
    replacements.update(CUSTOM3P_SETUP_REPLACEMENTS)
    count = patch_asar_file_with_replacements(app, ASAR_PATCH_TARGET, replacements)
    print(f"Patched native menu role labels in app.asar: {count} replacements")


def merge_frontend_locale(app: Path) -> tuple[int, int, int]:
    source = app / FRONTEND_I18N_REL / "en-US.json"
    target = app / FRONTEND_I18N_REL / "zh-CN.json"
    require_file(source)
    require_file(FRONTEND_TRANSLATION)

    en = load_json(source)
    zh_pack = load_json(FRONTEND_TRANSLATION)
    if not isinstance(en, dict) or not isinstance(zh_pack, dict):
        raise SystemExit("Unsupported frontend i18n JSON shape.")

    merged: dict[str, Any] = {}
    translated = 0
    fallback = 0
    for key, value in en.items():
        if key in zh_pack:
            merged[key] = zh_pack[key]
            if zh_pack[key] != value:
                translated += 1
        else:
            merged[key] = value
            fallback += 1

    save_json(target, merged)
    extra = len(set(zh_pack) - set(en))
    print(f"Installed frontend zh-CN: {translated} translated, {fallback} fallback, {extra} extra old keys ignored")
    return translated, fallback, extra


def install_desktop_locale(app: Path) -> None:
    resources_dir = app / DESKTOP_RESOURCES_REL
    require_file(DESKTOP_TRANSLATION)
    require_file(LOCALIZABLE_STRINGS)

    shutil.copy2(DESKTOP_TRANSLATION, resources_dir / "zh-CN.json")
    for folder in ["zh-CN.lproj", "zh_CN.lproj"]:
        out_dir = resources_dir / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LOCALIZABLE_STRINGS, out_dir / "Localizable.strings")
    print("Installed desktop shell zh-CN resources")


def install_statsig_locale(app: Path) -> None:
    statsig_dir = app / FRONTEND_I18N_REL / "statsig"
    if not statsig_dir.exists():
        return
    target = statsig_dir / "zh-CN.json"
    bundled = RESOURCES / "statsig-zh-CN.json"
    if bundled.exists():
        shutil.copy2(bundled, target)
    elif (statsig_dir / "en-US.json").exists():
        shutil.copy2(statsig_dir / "en-US.json", target)
    print("Installed statsig zh-CN resource")


def sign_path(path: Path, entitlements_dir: Path) -> None:
    entitlements = load_entitlements(path)
    if entitlements:
        # Ad-hoc signatures cannot legitimately claim Anthropic's Team ID.
        # Newer macOS builds can kill the app at launch when these restricted
        # identifiers remain after local re-signing.
        for restricted_key in [
            "com.apple.application-identifier",
            "com.apple.developer.team-identifier",
            "keychain-access-groups",
        ]:
            entitlements.pop(restricted_key, None)
        # Ad-hoc signatures do not have a real Team ID. Under hardened runtime,
        # Electron's main process otherwise fails library validation when it loads
        # bundled frameworks, even when the whole bundle is signed consistently.
        entitlements["com.apple.security.cs.disable-library-validation"] = True

    cmd = [
        "codesign",
        "--force",
        "--sign",
        "-",
        "--options",
        "runtime",
        "--preserve-metadata=identifier,flags",
    ]
    if entitlements:
        entitlement_path = entitlements_dir / f"{abs(hash(path.as_posix()))}.plist"
        entitlement_path.write_bytes(plistlib.dumps(entitlements, fmt=plistlib.FMT_XML))
        cmd.extend(["--entitlements", str(entitlement_path)])
    cmd.append(str(path))

    result = run(cmd, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        raise SystemExit(f"Failed to re-sign: {path}")


def is_signable_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if path.suffix in {".dylib", ".node", ".so"}:
        return True
    return os.access(path, os.X_OK)


def resign_app(app: Path) -> None:
    print("Re-signing patched app with local ad-hoc signature, preserving entitlements")
    contents = app / "Contents"
    entitlements_dir = Path(tempfile.mkdtemp(prefix="claude-zh-cn-entitlements."))
    bundle_targets: list[Path] = []
    file_targets: list[Path] = []

    for root, dirs, files in os.walk(contents):
        root_path = Path(root)
        for dirname in dirs:
            path = root_path / dirname
            if path.suffix in {".app", ".framework"}:
                bundle_targets.append(path)
        for filename in files:
            path = root_path / filename
            if is_signable_file(path):
                file_targets.append(path)

    # Sign nested Mach-O files first, then their containing bundles, then the outer app.
    for path in sorted(file_targets, key=lambda p: len(p.parts), reverse=True):
        sign_path(path, entitlements_dir)
    for path in sorted(bundle_targets, key=lambda p: len(p.parts), reverse=True):
        sign_path(path, entitlements_dir)
    sign_path(app, entitlements_dir)


def clear_quarantine(app: Path) -> None:
    cleared: list[str] = []
    for attr in ["com.apple.quarantine", "com.apple.provenance"]:
        result = run(["xattr", "-dr", attr, str(app)], check=False)
        if result.returncode == 0:
            cleared.append(attr)
    if cleared:
        print(f"Cleared Gatekeeper attributes: {', '.join(cleared)}")


def set_locale_config(config: Path) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if config.exists():
        try:
            data = load_json(config)
        except Exception:
            backup = config.with_suffix(".json.bak-invalid")
            shutil.copy2(config, backup)
            print(f"Existing config was not valid JSON; backed up to {backup}")
    data["locale"] = LANG_CODE
    save_json(config, data)

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        os.chown(config, int(sudo_uid), int(sudo_gid))
    print(f"Set Claude config locale: {config}")


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


def set_user_locale(user_home: Path) -> None:
    for support_dir in ["Claude", "Claude-3p"]:
        set_locale_config(user_home / f"Library/Application Support/{support_dir}/config.json")
    set_app_language_defaults(user_home)


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
                meta = load_json(meta_path)
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
            data = load_json(path)
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
    """读取用户手动配置的模型列表。第一项通常代表 provider 默认值。"""
    candidates = [
        user_home / "Library/Application Support/Claude-3p/config.json",
        user_home / "Library/Application Support/Claude-3p/claude_desktop_config.json",
    ]
    config_dir = user_home / "Library/Application Support/Claude-3p/configLibrary"
    if config_dir.exists():
        candidates.extend(sorted(config_dir.glob("*.json")))

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
            walk(load_json(path))
        except Exception:
            continue
    return models


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
    configured_all = configured_model_list(user_home)
    ignored_opus_aliases = [model for model in configured_all if is_opus_display_alias(model)]
    configured = [model for model in configured_all if not is_opus_display_alias(model)]
    gateway_models, gateway_errors = gateway_model_probe(user_home)
    if configured:
        metadata: dict[str, Any] = {
            "source": "configured_model_list",
            "model_count": len(configured),
            "configured_model_count": len(configured_all),
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
        data = load_json(settings)
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
            loaded = load_json(settings)
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
        save_json(settings, data)
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
    which = run(["zsh", "-lc", "command -v claude || true"], check=False).stdout.strip()
    if not which:
        return "missing", f"cli_path=missing; shared_env_status={env_status}; {env_message}"
    version = run(["zsh", "-lc", "claude --version 2>&1 || true"], check=False).stdout.strip()
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
                package = load_json(package_json)
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


def read_claude_code_context_window(user_home: Path) -> tuple[int | None, str]:
    config_path = user_home / ".claude.json"
    if not config_path.exists():
        return None, "missing"
    try:
        data = load_json(config_path)
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
        save_json(config_path, {CLAUDE_CODE_CONTEXT_WINDOW_KEY: context_window})
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            os.chown(config_path, int(sudo_uid), int(sudo_gid))
        print(f"Created Claude Code runtime JSON with context window: {context_window}")
        return context_window, "root_created", True
    try:
        data = load_json(config_path)
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
        save_json(config_path, data)
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
            loaded = load_json(settings)
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
    save_json(settings, data)
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        os.chown(settings, int(sudo_uid), int(sudo_gid))
    print(f"Set Claude Code defaults: model={data.get('model')}, effort=max")
    return data.get("model"), metadata


def migrate_saved_session_dynamic_model(user_home: Path, preferred_model: str | None) -> int:
    """把旧 Opus 伪装会话迁移到真实 provider 默认模型，避免上下文能力继续按伪装模型计算。"""
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
                data = load_json(path)
            except Exception:
                continue
            dirty = False
            if data.get("model") in {SAFE_OPUS_MODEL_ID, LEGACY_1M_OPUS_MODEL_ID}:
                data["model"] = preferred_model
                dirty = True
            session_data = data.get("sessionData")
            if isinstance(session_data, dict):
                session_context = session_data.get("session_context")
                if isinstance(session_context, dict) and session_context.get("model") in {
                    SAFE_OPUS_MODEL_ID,
                    LEGACY_1M_OPUS_MODEL_ID,
                }:
                    session_context["model"] = preferred_model
                    dirty = True
            if dirty:
                save_json(path, data)
                changed += 1
    if changed:
        print(f"Migrated saved Claude Code sessions from Opus alias to provider model {preferred_model}: {changed}")
    return changed


def find_claude_code_transcript(user_home: Path, cli_session_id: str) -> Path | None:
    """根据 Claude Code session id 找到磁盘上的 jsonl 历史。"""
    projects = user_home / ".claude/projects"
    if not projects.exists():
        return None
    direct = list(projects.glob(f"*/{cli_session_id}.jsonl"))
    if direct:
        return direct[0]
    try:
        matches = run(
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
                data = load_json(path)
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
            data = load_json(path)
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
            return [{"type": "text", "text": "[历史思考内容已瘦身移除，以避免超过当前真实模型上下文上限。]"}]
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
                return {"type": "text", "text": "[历史截图已瘦身移除，以避免超过当前真实模型上下文上限。]"}
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "signature" and isinstance(item, str) and len(item) > 256:
                stats["signatures_removed"] = stats.get("signatures_removed", 0) + 1
                continue
            if key in {"base64", "data"} and isinstance(item, str) and len(item) > SESSION_LONG_FIELD_KEEP_CHARS:
                stats["binary_fields_removed"] = stats.get("binary_fields_removed", 0) + 1
                result[key] = "[历史二进制内容已瘦身移除，以避免超过当前真实模型上下文上限。]"
                continue
            if key in {"stdout", "stderr"} and isinstance(item, str) and len(item) > SESSION_LONG_FIELD_KEEP_CHARS:
                stats["streams_truncated"] = stats.get("streams_truncated", 0) + 1
                result[key] = item[:SESSION_LONG_FIELD_KEEP_CHARS] + "\n...[历史命令输出已截断]"
                continue
            if key == "snippet" and isinstance(item, str) and len(item) > SESSION_SNIPPET_KEEP_CHARS:
                stats["snippets_truncated"] = stats.get("snippets_truncated", 0) + 1
                result[key] = item[:SESSION_SNIPPET_KEEP_CHARS] + "\n...[历史代码片段已截断]"
                continue
            if key == "content" and isinstance(item, str) and len(item) > SESSION_TEXT_KEEP_CHARS:
                stats["text_truncated"] = stats.get("text_truncated", 0) + 1
                result[key] = item[:SESSION_TEXT_KEEP_CHARS] + "\n...[历史工具输出已截断]"
                continue
            result[key] = shrink_session_value(item, stats)
        return result

    return value


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


def sanitize_active_oversized_sessions(user_home: Path) -> tuple[int, list[dict[str, Any]]]:
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
            {"limit": int(match.group("limit")), "requested": int(match.group("requested"))}
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
        save_json(
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


def clear_frontend_cache(user_home: Path, dry_run: bool) -> None:
    cache_names = ["Cache", "Code Cache", "GPUCache", "Service Worker", "DawnCache", "ShaderCache"]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    moved = 0
    for support_dir in ["Claude", "Claude-3p"]:
        base = user_home / f"Library/Application Support/{support_dir}"
        trash_dir = user_home / ".Trash" / f"{support_dir}-frontend-cache-{stamp}"
        for name in cache_names:
            path = base / name
            if not path.exists():
                continue
            target = trash_dir / name
            if dry_run:
                print(f"[dry-run] Would move frontend cache to trash: {path} -> {target}")
                moved += 1
                continue
            trash_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved += 1
    if moved:
        print(f"Cleared {moved} frontend cache folder(s)")


def check_frontend_invariants(app: Path, report: PatchReport, *, require: bool = True) -> bool:
    bundles = find_frontend_bundles(app)
    index = bundles["index"]
    code = bundles["code"]

    if index is None:
        report.add("frontend.index_bundle", "missing", "找不到包含模型选择器的主前端 bundle", required=require)
    else:
        text = index.read_text(encoding="utf-8", errors="ignore")
        checks = {
            "cowork.two_models": (
                (
                    'Q=[rr,cc],X=[],J=[]' in text
                    or 'allModelOptions:[r,l],mainModels:[r,l],overflowModels:[]' in text
                    or 'zhCoworkConfig={...C,allModelOptions:[zhCoworkOpus,zhCoworkKimi]' in text
                )
                and 'Opus 4.71M' in text
                and 'Kimi-k2.6' in text
            ),
            "cowork.default_opus": (
                'z="opus","' in text
                or 'z="opus",{allModelOptions:F}=R' in text
                or 'return["opus",{...s,allModelOptions:[r,l]' in text
                or 'defaultModel:c?"opus":w' in text
            ),
            "cowork.opus_alias_not_1m": (
                (
                    'return["opus",{...s,allModelOptions:[r,l]' in text
                    or 'zhCoworkOpus={...(C.allModelOptions.find(e=>"opus"===e.model)' in text
                )
                and 'return["opus[1m]",{...s,allModelOptions:[r,l]' not in text
            ),
            "cowork.fallback_effort": (
                'cowork_effort_level_cn")||"max"' in text
                and (
                    'Fw=n.useMemo(()=>_??{current:cw' in text
                    or 'Fw=n.useMemo(()=>_??{current:cw,options:' in text
                    or 'Fw=n.useMemo(()=>({current:cw,options:' in text
                )
                and '"cowork"===I?{current:cw' not in text
                and 'value:"xhigh",label:"超高"' in text
                and 'value:"max",label:"最大"' in text
            ),
            "cowork.effort_render_inline": (
                'Wmt=({conversationUuid' not in text
                or (
                    'zhCoworkEffortOptions=[{value:"low",label:"低"}' in text
                    and 'Fw.options.map(e=>a.jsx(Nk' in text
                    and 'a.jsx(_mt,{section:Fw' not in text
                )
            ),
            "cowork.current_selection_local": (
                'Wmt=({conversationUuid' not in text
                or '$=Uc("sticky_model_selector"),[q,V]=n.useState(null),H=q??z' in text
            ),
            "cowork.default_max_effort": (
                (
                    'yc("cowork_effort_level_cn","max"' in text
                    or 'localStorage.getItem("cowork_effort_level_cn")||"max"' in text
                )
                and 'localStorage.getItem("cowork_effort_level_cn")||"max"' in text
            ),
            "cowork.effort_sync": (
                (
                    'window.addEventListener("cowork-effort-change"' in text
                    and (
                        'setYukonSilverConfig?.({...w,effort:zhEffort' in text
                        or 'NT?.setYukonSilverConfig?.({...k,effort:_' in text
                    )
                )
                or (
                    'Fw=n.useMemo(()=>({current:cw,options:' in text
                    and 'window.dispatchEvent(new CustomEvent("cowork-effort-change"' in text
                )
                or (
                    'setYukonSilverConfig?.({...w,effort:zhEffort' in text
                    or 'NT?.setYukonSilverConfig?.({...k,effort:_' in text
                )
            ),
            "cowork.runtime_model_mapping": (
                (
                    "zhCoworkRuntimeModel" in text
                    and "setModel(e,zhCoworkRuntimeModel)" in text
                    and "model:zhCoworkRuntimeModel" in text
                    and "session_context:{sources:[],...t.sessionModel&&{model:((e)=>" in text
                )
                or (
                    "zhCoworkKimiId" in text
                    and "zhCoworkModel" in text
                    and "session_context:{sources:[],...t.sessionModel&&{model:((e)=>" in text
                )
            ),
            "cowork.kimi_health_hidden": (
                (
                    'function tGt({initialHealth:e})' in text
                    and 'l.state===wz.Unreachable&&/api\\.kimi\\.com' in text
                )
                or (
                    'function tGt({initialHealth:e})' not in text
                    and (
                        'l.state===yW.Unreachable&&/api\\.kimi\\.com' in text
                        or 'l.state===xV.Unreachable&&/api\\.kimi\\.com' in text
                        or 'case eG.Unreachable:if(/api\\.kimi\\.com' in text
                        or 'd?.state===eG.InvalidConfig||d?.state===eG.Unreachable' in text
                    )
                )
            ),
        }
        for name, ok in checks.items():
            report.add(name, "passed" if ok else "missing", file=index, required=require)
        ok, message = check_js_syntax(index)
        report.add("syntax.index_bundle", "passed" if ok else "failed", message, file=index, required=require)

    if code is None:
        report.add("frontend.code_bundle", "missing", "找不到包含 Code 模型菜单的 bundle", required=require)
    else:
        text = code.read_text(encoding="utf-8", errors="ignore")
        checks = {
            "code.two_models": (
                (
                    'return[{label:"Opus 4.71M"' in text
                    or 'return[e,n]},[M,W,ue,ie]),pe=fe' in text
                )
                and 'Kimi-k2.6' in text
            ),
            "code.default_opus": (
                '})(H??"opus"),' in text
                or '})(U??"opus"),' in text
                or '})(B??"opus"),' in text
            ),
            "code.opus_alias_not_1m": (
                'onSelect:()=>ue.current("opus")' in text
                and 'onSelect:()=>ue.current("opus[1m]")' not in text
            ),
            "code.full_effort": (
                (
                    'xs=e.useMemo(()=>{const e=[],t=fm;' in text
                    or 'const e=Lm;return{current:v,options:e.map' in text
                    or 'Ts=e.useMemo(()=>{const e=[],t=dh;' in text
                )
                and 'modelSupportsEffort:!0,modelSupportsMaxEffort:!0,modelSupportsXhighEffort:!0' in text
                and (
                    'pm={low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}' in text
                    or 'Om={low:"低",medium:"中",high:"高",xhigh:"超高",max:"最大"}' in text
                    or 'kh=c({low:{defaultMessage:"低"' in text
                )
            ),
            "code.default_max_effort": (
                (
                    'const um="ccd-effort-level-cn"' in text
                    or 'const zm="ccd-effort-level-cn"' in text
                    or 'const ch="ccd-effort-level-cn"' in text
                )
                and ('h=p??f??"max"' in text or 'ms=codeEffort??"max"' in text)
            ),
            "code.runtime_model_mapping": (
                "zhRuntimeModelFor=" in text
                and "zhRuntimeModel=zhRuntimeModelFor(W)" in text
                and "zhProviderModel=" in text
            ),
            "code.spawn_model_not_display_model": (
                "model:zhRuntimeModel," in text
                and "model:W," not in text
                and "setModel?.(te.id,zhRuntimeModelFor(e))" in text
            ),
        }
        for name, ok in checks.items():
            report.add(name, "passed" if ok else "missing", file=code, required=require)
        ok, message = check_js_syntax(code)
        report.add("syntax.code_bundle", "passed" if ok else "failed", message, file=code, required=require)

    context_usage_file: Path | None = None
    context_usage_ok = False
    live_context_usage_ok = False
    assets_dir_for_context = app / FRONTEND_ASSETS_REL
    if assets_dir_for_context.exists():
        for path in sorted(assets_dir_for_context.glob("*.js")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "rawMaxTokens" not in text:
                continue
            if "## Context Usage" in text:
                context_usage_file = context_usage_file or path
                context_usage_ok = (
                    ("n0=ege(s[2]),n=" in text or "n0=M1e(s[2]),n=" in text)
                    and "rawMaxTokens:n" in text
                    and "percentage:c" in text
                )
            if "contextUsage:n?{...n,rawMaxTokens:" in text and "N=b?Math.min(100,C??0):d.peak??0" in text:
                context_usage_file = context_usage_file or path
                live_context_usage_ok = True
            if context_usage_ok and live_context_usage_ok:
                break
    report.add(
        "code.context_usage_window_override",
        "passed" if context_usage_ok else "missing",
        "Context Usage 解析器必须用 provider 真实窗口覆盖文本分母并重算百分比",
        file=context_usage_file,
        required=require,
    )
    report.add(
        "code.live_context_usage_window_override",
        "passed" if live_context_usage_ok else "missing",
        "Code 实时上下文窗口组件必须用 provider 真实窗口覆盖 rawMaxTokens",
        file=context_usage_file,
        required=require,
    )

    assets_dir = app / FRONTEND_ASSETS_REL
    permission_files: list[Path] = []
    bad_permission_files: list[str] = []
    has_draft_default = False
    has_folder_key = False
    has_bypass_priority = False
    if assets_dir.exists():
        for path in sorted(assets_dir.glob("*.js")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "cc-landing-draft-permission-mode" not in text and "epitaxy-folder-permission-mode" not in text:
                continue
            permission_files.append(path)
            if '"cc-landing-draft-permission-mode","acceptEdits"' in text:
                bad_permission_files.append(path.name)
            if '"epitaxy-folder-permission-mode",' in text:
                bad_permission_files.append(path.name)
            has_draft_default = has_draft_default or '"cc-landing-draft-permission-mode-cn","bypassPermissions"' in text
            has_folder_key = has_folder_key or '"epitaxy-folder-permission-mode-cn"' in text
            has_bypass_priority = (
                has_bypass_priority
                or 'en??Zs??$s??Gs??"bypassPermissions"' in text
                or 'dn??cn??Qs??nn??"bypassPermissions"' in text
                or 'xn??gn??cn??nn??"bypassPermissions"' in text
            )
    permission_ok = has_draft_default and has_folder_key and has_bypass_priority and not bad_permission_files
    report.add(
        "code.permission_default_bypass",
        "passed" if permission_ok else "missing",
        (
            ""
            if permission_ok
            else f"permission_files={[path.name for path in permission_files]}, bad={sorted(set(bad_permission_files))}"
        ),
        required=require,
    )

    i18n_ok, i18n_message, i18n_count = check_known_frontend_i18n(app)
    report.add(
        "i18n.known_missing_strings",
        "passed" if i18n_ok else "missing",
        i18n_message,
        count=i18n_count,
        required=require,
    )

    dev_menu_ok, dev_menu_message, dev_menu_count = check_developer_menu_i18n(app)
    report.add(
        "i18n.developer_menu_labels",
        "passed" if dev_menu_ok else "missing",
        dev_menu_message,
        count=dev_menu_count,
        required=require,
    )

    custom3p_setup_ok, custom3p_setup_message, custom3p_setup_count = check_custom3p_setup_i18n(app)
    report.add(
        "i18n.custom3p_setup_labels",
        "passed" if custom3p_setup_ok else "missing",
        custom3p_setup_message,
        count=custom3p_setup_count,
        required=require,
    )

    custom3p_ok = check_custom3p_validation_patched(app)
    report.add("asar.custom3p_validation", "passed" if custom3p_ok else "missing", required=require)
    gateway_env_injection_ok, gateway_env_injection_message, gateway_env_injection_count = (
        claude_code_gateway_env_injection_status(app)
    )
    report.add(
        "asar.code_gateway_env_injection",
        "passed" if gateway_env_injection_ok else "missing",
        gateway_env_injection_message,
        count=gateway_env_injection_count,
        required=require,
    )

    signature = run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=False)
    report.add(
        "codesign.verify",
        "passed" if signature.returncode == 0 else "failed",
        signature.stdout.strip(),
        required=require,
    )

    return not report.has_required_failures()


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
    preferred_model, model_metadata = preferred_gateway_model_id(args.user_home)
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
    if not check_frontend_invariants(patched_app, report, require=True):
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
