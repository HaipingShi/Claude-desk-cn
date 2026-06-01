# Patch 脚本重构：提取通用清理逻辑与安装后全量验证

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将嵌在 `patch_cowork_model_menu` 中的兜底清理逻辑提取为独立函数，在 Cowork 和 Code 补丁后都被调用；增加安装后全量扫描，确保没有旧版补丁残留；将硬编码版本号提取到集中配置。

**Architecture:** 不改动主流程，只做"提取 + 增强"。新增 `patches/` 包存放提取的函数和常量，`tests/` 存放单元测试。保持 `patch_claude_zh_cn.py` 的向后兼容——主脚本继续直接导入和使用这些模块。

**Tech Stack:** Python 3.12+, `pathlib`, `pytest`（可选，如果没有 pytest 就用标准库 `unittest`）

---

## 文件结构

```
Claude-desk-cn/
├── patch_claude_zh_cn.py          # 主脚本（修改：import + 调用新函数）
├── patches/
│   ├── __init__.py                # 空（或 re-export 常用函数）
│   ├── constants.py               # 集中配置：OPUS_DISPLAY_NAME 等
│   ├── cleanup.py                 # 兜底清理逻辑（提取自 patch_cowork_model_menu）
│   └── verification.py            # 安装后全量扫描
├── tests/
│   ├── __init__.py                # 空
│   ├── test_cleanup.py            # cleanup 单元测试
│   └── test_verification.py       # 全量扫描单元测试
└── docs/superpowers/plans/        # 本计划
```

**设计边界：**
- `patches/constants.py`：只放"可能被多个补丁函数共享且可能频繁变更"的常量（目前只有 `OPUS_DISPLAY_NAME` 和相关衍生字符串）。不迁移所有常量——大部分常量（如 `APP_DEFAULT`、`LANG_CODE`）只在主脚本使用。
- `patches/cleanup.py`：纯函数，只操作传入的 `Path` 和字符串。不依赖 `PatchReport` 或其他主脚本对象。
- `patches/verification.py`：纯函数，返回扫描结果。由主脚本决定是 warning 还是 error。

---

## Task 1: 创建 patches 包和 tests 目录

**Files:**
- Create: `patches/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: 创建 patches/__init__.py**

```python
"""Patch utilities extracted from patch_claude_zh_cn.py."""
```

- [ ] **Step 2: 创建 tests/__init__.py**

```python
"""Unit tests for patch utilities."""
```

- [ ] **Step 3: Commit**

```bash
git add patches/__init__.py tests/__init__.py
git commit -m "chore: create patches/ and tests/ directories"
```

---

## Task 2: 提取版本号常量到集中配置

**Files:**
- Create: `patches/constants.py`
- Modify: `patch_claude_zh_cn.py:55-65`（常量定义区）

**上下文：** `patch_claude_zh_cn.py` 第 58 行定义 `OPUS_DISPLAY_NAME = "Opus 4.8"`，但这个字符串在 40+ 处硬编码。每次升级版本号时，需要搜索所有出现位置。集中配置后，可以从常量动态生成清理字典中的 key-value。

- [ ] **Step 1: 编写 test——验证常量导出正确**

Create `tests/test_constants.py`:

```python
from patches.constants import OPUS_DISPLAY_NAME, STALE_OPUS_VARIANTS


def test_opus_display_name_is_string():
    assert isinstance(OPUS_DISPLAY_NAME, str)
    assert len(OPUS_DISPLAY_NAME) > 0


def test_stale_variants_list_non_empty():
    assert isinstance(STALE_OPUS_VARIANTS, tuple)
    assert len(STALE_OPUS_VARIANTS) > 0
    for v in STALE_OPUS_VARIANTS:
        assert isinstance(v, str)
        assert len(v) > 0


def test_stale_variants_do_not_include_current():
    """旧版本列表不能包含当前版本号，否则清理逻辑会误删当前显示名。"""
    assert OPUS_DISPLAY_NAME not in STALE_OPUS_VARIANTS
```

- [ ] **Step 2: 运行测试——预期失败（模块不存在）**

```bash
cd /Users/geesh/projects/Claude-desk-cn
python3 -m pytest tests/test_constants.py -v
```

Expected:
```
ModuleNotFoundError: No module named 'patches.constants'
```

- [ ] **Step 3: 创建 patches/constants.py**

```python
"""Centralized patch constants for version-bump safety.

When upgrading the Opus display version (e.g. 4.8 -> 4.9),
change OPUS_DISPLAY_NAME and append the old name to STALE_OPUS_VARIANTS.
The cleanup module uses these to build residue-scan patterns automatically.
"""

OPUS_DISPLAY_NAME: str = "Opus 4.8"

# Historical display names that may still linger in previously-patched bundles.
# These are used by the stale-residue cleanup logic.
STALE_OPUS_VARIANTS: tuple[str, ...] = (
    "Opus 4.71M",
    "Opus 4.7 1M",
)

# Model IDs used internally (not display names). These rarely change.
SAFE_OPUS_MODEL_ID: str = "opus"
LEGACY_1M_OPUS_MODEL_ID: str = "opus[1m]"
```

- [ ] **Step 4: 修改 patch_claude_zh_cn.py——导入新常量**

Replace lines 56-58:
```python
SAFE_OPUS_MODEL_ID = "opus"
LEGACY_1M_OPUS_MODEL_ID = "opus[1m]"
OPUS_DISPLAY_NAME = "Opus 4.8"
```

With:
```python
from patches.constants import (
    OPUS_DISPLAY_NAME,
    SAFE_OPUS_MODEL_ID,
    LEGACY_1M_OPUS_MODEL_ID,
)
```

- [ ] **Step 5: 运行测试——预期通过**

```bash
python3 -m pytest tests/test_constants.py -v
```

Expected:
```
tests/test_constants.py::test_opus_display_name_is_string PASSED
tests/test_constants.py::test_stale_variants_list_non_empty PASSED
tests/test_constants.py::test_stale_variants_do_not_include_current PASSED
```

- [ ] **Step 6: Commit**

```bash
git add patches/constants.py tests/test_constants.py patch_claude_zh_cn.py
git commit -m "refactor: extract OPUS_DISPLAY_NAME and stale variants to patches/constants.py"
```

---

## Task 3: 提取 cleanup_stale_display_names 为独立函数

**Files:**
- Create: `patches/cleanup.py`
- Modify: `patch_claude_zh_cn.py:2568-2593`（删除内嵌的 stale_opus_cleanup 块）

**上下文：** 当前 `stale_opus_cleanup` 字典（第 2568-2582 行）和遍历逻辑（第 2583-2591 行）嵌在 `patch_cowork_model_menu` 末尾。`patch_epitaxy_model_menu` 没有调用它，导致 Code 页面的 `c5610fbe3-rsWnjbnF.js` 残留未被清理。

提取后：
- `patches/cleanup.py` 提供 `cleanup_stale_display_names(assets_dir)`
- 主脚本在 `patch_cowork_model_menu` 和 `patch_epitaxy_model_menu` 之后都调用

- [ ] **Step 1: 编写 test——验证清理逻辑**

Create `tests/test_cleanup.py`:

```python
import tempfile
from pathlib import Path

from patches.cleanup import cleanup_stale_display_names
from patches.constants import OPUS_DISPLAY_NAME, STALE_OPUS_VARIANTS


def test_cleanup_replaces_stale_names():
    """旧版补丁写入的 Opus 显示名应被替换为当前版本号。"""
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp)
        js_file = assets_dir / "index-test.js"

        # 构造一段包含旧版残留 + 当前版本的模拟 bundle 代码
        stale_name = STALE_OPUS_VARIANTS[0]  # "Opus 4.71M"
        js_file.write_text(
            f'zhOpus={...{model:"opus",name:"{stale_name}",label_override:"{stale_name}"},'
            f'zhReal={...{model:"kimi-for-coding",name:"Kimi-k2.6"},'
            f'const e={...{label:"{OPUS_DISPLAY_NAME}"}'
        )

        patched_files, patched_strings = cleanup_stale_display_names(assets_dir)

        assert patched_files == 1
        assert patched_strings >= 2  # name 和 label_override 各一次

        result = js_file.read_text()
        assert stale_name not in result
        assert OPUS_DISPLAY_NAME in result


def test_cleanup_no_false_positives_on_unrelated_files():
    """没有旧版残留的文件不应被修改。"""
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp)
        js_file = assets_dir / "clean.js"
        js_file.write_text('console.log("hello world")')

        patched_files, patched_strings = cleanup_stale_display_names(assets_dir)

        assert patched_files == 0
        assert patched_strings == 0


def test_cleanup_skips_non_js_files():
    """只处理 .js 文件，不碰 .json / .css 等。"""
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp)
        json_file = assets_dir / "config.json"
        json_file.write_text('{"model":"Opus 4.71M"}')

        patched_files, patched_strings = cleanup_stale_display_names(assets_dir)

        assert patched_files == 0
        assert patched_strings == 0
```

- [ ] **Step 2: 运行测试——预期失败**

```bash
python3 -m pytest tests/test_cleanup.py -v
```

Expected:
```
ModuleNotFoundError: No module named 'patches.cleanup'
```

- [ ] **Step 3: 创建 patches/cleanup.py**

```python
"""Stale patch residue cleanup — removes old display names from previously-patched bundles.

Problem: when Claude Desktop updates, old patch old_strings may no longer exist in the
new bundle. The new patch therefore cannot "overwrite" the old patch's target, leaving
stale display names (e.g. "Opus 4.71M") behind. This module provides a brute-force
post-patch cleanup that scans every *.js file and replaces known stale patterns.
"""

from pathlib import Path

from patches.constants import OPUS_DISPLAY_NAME, STALE_OPUS_VARIANTS


def _build_cleanup_patterns() -> dict[str, str]:
    """Build old -> new replacement patterns from STALE_OPUS_VARIANTS.

    Covers multiple JS literal forms that the patch target historically wrote:
    - name:"..."
    - label_override:"..."
    - return"..."
    - "...",inactive
    - ternary expression: ?"..."
    - useMemo expression: )?"..."
    - object literal: {label:"...",checked:...
    """
    current = OPUS_DISPLAY_NAME
    patterns: dict[str, str] = {}

    for stale in STALE_OPUS_VARIANTS:
        patterns[f'name:"{stale}"'] = f'name:"{current}"'
        patterns[f'label_override:"{stale}"'] = f'label_override:"{current}"'
        patterns[f'return"{stale}"'] = f'return"{current}"'
        patterns[f'"{stale}",inactive'] = f'"{current}",inactive'
        # Code page ternary / useMemo patterns
        patterns[f'?"{stale}"'] = f'?"{current}"'
        patterns[f')?"{stale}"'] = f')?"{current}"'
        patterns[f'{{label:"{stale}",checked:'] = f'{{label:"{current}",checked:'

    return patterns


# Pre-built at import time so callers don't rebuild on every invocation.
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
```

- [ ] **Step 4: 运行测试——预期通过**

```bash
python3 -m pytest tests/test_cleanup.py -v
```

Expected:
```
tests/test_cleanup.py::test_cleanup_replaces_stale_names PASSED
tests/test_cleanup.py::test_cleanup_no_false_positives_on_unrelated_files PASSED
tests/test_cleanup.py::test_cleanup_skips_non_js_files PASSED
```

- [ ] **Step 5: 修改 patch_claude_zh_cn.py——删除内嵌清理块**

Delete lines 2568-2591 (the stale_opus_cleanup dict + loop) from `patch_claude_zh_cn.py`.

同时，在 `patch_cowork_model_menu` 函数末尾（第 2567 行之后）添加导入和调用：

```python
    # 兜底：清理旧版补丁残留的显示版本号
    cf, cs = cleanup_stale_display_names(assets_dir)
    if cf:
        patched_files += cf
        patched_strings += cs

    return patched_files, patched_strings
```

在 `patch_epitaxy_model_menu` 函数末尾（第 3159 行之后）也添加同样的调用：

```python
    # 兜底：清理旧版补丁残留的显示版本号
    cf, cs = cleanup_stale_display_names(assets_dir)
    if cf:
        patched_files += cf
        patched_strings += cs

    return patched_files, patched_strings
```

并在文件顶部添加导入（在 `from patches.constants import ...` 之后）：

```python
from patches.cleanup import cleanup_stale_display_names
```

- [ ] **Step 6: 验证语法**

```bash
python3 -c "import ast; ast.parse(open('patch_claude_zh_cn.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 7: Commit**

```bash
git add patches/cleanup.py tests/test_cleanup.py patch_claude_zh_cn.py
git commit -m "refactor: extract stale_opus_cleanup to patches/cleanup.py and call from both menu patchers"
```

---

## Task 4: 添加安装后全量扫描验证

**Files:**
- Create: `patches/verification.py`
- Modify: `patch_claude_zh_cn.py`（安装流程末尾调用）

**上下文：** 当前 `check_frontend_invariants` 只检查离散标记（如 `'Opus 4.8' in text`），如果残留出现在未覆盖的文件中（如 `c5610fbe3-rsWnjbnF.js`），invariants 无法发现。全量扫描会遍历所有 `*.js` 文件，报告任何残留的 stale 显示名。

- [ ] **Step 1: 编写 test——验证全量扫描**

Create `tests/test_verification.py`:

```python
import tempfile
from pathlib import Path

from patches.verification import scan_for_stale_display_names
from patches.constants import OPUS_DISPLAY_NAME, STALE_OPUS_VARIANTS


def test_scan_finds_stale_residue():
    """在包含旧版残留的文件中应能发现残留。"""
    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp)
        js_dir = app_dir / "Contents/Resources/ion-dist/assets/v1"
        js_dir.mkdir(parents=True)
        (js_dir / "index.js").write_text(
            f'zhOpus={...{name:"{STALE_OPUS_VARIANTS[0]}"}}'
        )

        findings = scan_for_stale_display_names(app_dir)

        assert len(findings) == 1
        assert findings[0]["file"].endswith("index.js")
        assert STALE_OPUS_VARIANTS[0] in findings[0]["matches"]


def test_scan_returns_empty_when_clean():
    """干净的安装不应报告残留。"""
    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp)
        js_dir = app_dir / "Contents/Resources/ion-dist/assets/v1"
        js_dir.mkdir(parents=True)
        (js_dir / "index.js").write_text(
            f'zhOpus={...{name:"{OPUS_DISPLAY_NAME}"}}'
        )

        findings = scan_for_stale_display_names(app_dir)

        assert findings == []


def test_scan_skips_non_js_files():
    """不应扫描 .json / .css 等文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp)
        css_dir = app_dir / "Contents/Resources/ion-dist/assets/v1"
        css_dir.mkdir(parents=True)
        (css_dir / "style.css").write_text(
            f'.model::before{{content:"{STALE_OPUS_VARIANTS[0]}"}}'
        )

        findings = scan_for_stale_display_names(app_dir)

        assert findings == []
```

- [ ] **Step 2: 运行测试——预期失败**

```bash
python3 -m pytest tests/test_verification.py -v
```

Expected:
```
ModuleNotFoundError: No module named 'patches.verification'
```

- [ ] **Step 3: 创建 patches/verification.py**

```python
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
```

- [ ] **Step 4: 运行测试——预期通过**

```bash
python3 -m pytest tests/test_verification.py -v
```

Expected:
```
tests/test_verification.py::test_scan_finds_stale_residue PASSED
tests/test_verification.py::test_scan_returns_empty_when_clean PASSED
tests/test_verification.py::test_scan_skips_non_js_files PASSED
```

- [ ] **Step 5: 修改 patch_claude_zh_cn.py——集成全量扫描到安装流程**

找到 `main()` 函数中安装流程的末尾（在 `check_frontend_invariants` 和 `check_runtime_invariants` 之后，打印报告之前）。

在文件顶部添加导入：
```python
from patches.verification import scan_for_stale_display_names
```

找到第 5943-5949 行的 diagnose 分支（或安装流程中调用 check 的位置），在安装流程的 `check_frontend_invariants` 之后添加全量扫描。

当前安装流程大致在 `main()` 的第 5980+ 行（在 `report = PatchReport(...)` 和 `check_frontend_invariants` 之后）。找到以下模式：

```python
check_frontend_invariants(temp_app, report, require=True)
```

在其后添加：

```python
    # 全量扫描：检查是否有旧版补丁残留
    stale_findings = scan_for_stale_display_names(temp_app)
    if stale_findings:
        for finding in stale_findings:
            report.add(
                f"residue.{finding['file'].replace('/', '_')}",
                "warning",
                f"发现旧版补丁残留: {finding['matches']} in {finding['file']}",
            )
```

注意：`temp_app` 是安装流程中复制到临时目录的 app 路径。如果变量名不同，请使用实际变量名。

同时，在 `--diagnose` 分支中也添加同样的扫描（第 5945-5949 行）：

```python
if args.diagnose:
    report = PatchReport(str(args.app), get_claude_version(args.app), "diagnose")
    check_frontend_invariants(args.app, report, require=True)
    check_runtime_invariants(args.user_home, report, require=False, project_paths=args.project)
    # 全量扫描
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
```

- [ ] **Step 6: 验证语法**

```bash
python3 -c "import ast; ast.parse(open('patch_claude_zh_cn.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 7: Commit**

```bash
git add patches/verification.py tests/test_verification.py patch_claude_zh_cn.py
git commit -m "feat: add post-install full-disk scan for stale patch residue"
```

---

## Task 5: 运行完整回归测试

**Files:**
- No new files
- Run: `tests/` 全部测试
- Run: `patch_claude_zh_cn.py --diagnose`

- [ ] **Step 1: 运行所有单元测试**

```bash
cd /Users/geesh/projects/Claude-desk-cn
python3 -m pytest tests/ -v
```

Expected: 所有 9 个测试通过（3 个 constants + 3 个 cleanup + 3 个 verification）。

- [ ] **Step 2: 运行诊断——验证主脚本仍正常工作**

```bash
python3 patch_claude_zh_cn.py --diagnose
```

Expected:
- 不报错
- 输出 Patch diagnostics summary
- 如果有 `residue.*` 警告，说明还有真实残留（正常，因为还没重新安装）

- [ ] **Step 3: 验证 install.command 可以 dry-run 通过**

```bash
./install.command --dry-run
```

或如果 install.command 不支持 `--dry-run`，则在终端运行：

```bash
sudo python3 patch_claude_zh_cn.py --user-home "$HOME" --dry-run
```

Expected: 脚本运行到末尾，不替换 `/Applications/Claude.app`。

- [ ] **Step 4: Commit（如果测试通过）**

```bash
git add -A
git commit -m "test: verify all extracted modules work with main script"
```

---

## Task 6: 清理旧版残留——真正运行 install.command

**Files:**
- No code changes
- Run: `./install.command`

- [ ] **Step 1: 运行安装**

```bash
./install.command
```

Expected:
- Patch diagnostics summary: passed >= 50
- 没有 required failures
- `cowork.two_models` 通过
- 如果有 `residue.*` warnings，记录具体文件，作为下一轮 cleanup pattern 的补充

- [ ] **Step 2: 验证右下角显示**

打开 Claude Desktop，检查 Cowork / Code 页面的右下角模型选择器，确认显示为 `Opus 4.8` 而不是 `4.7 1M`。

- [ ] **Step 3: Commit（如有新发现则记录到 docs/lessons-learned.md）**

如果安装成功且没有新的残留问题：

```bash
git add -A
git commit -m "chore: verify install passes after refactor"
```

如果仍有残留，不要提交代码，把残留模式记录到 `docs/lessons-learned.md`，然后回到 Task 3 补充 `patches/cleanup.py` 中的 pattern。

---

## 自我审查

### Spec coverage

| 需求 | 对应 Task |
|------|----------|
| 提取通用清理逻辑 | Task 3 |
| 在 Cowork 和 Code 补丁后都调用 | Task 3 Step 5 |
| 安装后全量扫描 | Task 4 |
| 版本号集中配置 | Task 2 |
| 向后兼容 | 所有 Task 都不删除原有函数签名，只在末尾添加调用 |
| 测试驱动 | 每个 Task 都有 Step 1（写测试）和 Step 2（运行失败） |

### Placeholder scan

- 没有 "TBD"、"TODO"、"implement later"
- 每个步骤都有完整代码块
- 每个步骤都有命令和期望输出
- 没有 "Similar to Task N"

### 类型一致性

- `cleanup_stale_display_names(assets_dir: Path) -> tuple[int, int]` — 返回值类型与原函数一致
- `scan_for_stale_display_names(app: Path) -> list[dict[str, str]]` — 新函数，不影响现有代码
- `OPUS_DISPLAY_NAME: str` — 与旧常量类型一致

---

## 执行选择

**Plan complete and saved to `docs/superpowers/plans/2026-06-01-refactor-patches.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
