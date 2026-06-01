# Kimi 伪装 Opus 4.8 排障复盘

本文记录一次 Claude Desktop 第三方推理配置排障：目标是在界面继续显示 `Opus 4.8`，但 Code/Cowork 实际请求走 Kimi 的 `kimi-for-coding`。

## 最终结论

这次失败不是 Kimi Key 本身不可用，也不是单纯的模型名映射问题。真实根因是 Claude Desktop 拉起 Code 子进程时仍走 `CLAUDE_CODE_ENTRYPOINT=claude-desktop-3p`，导致请求被第三方网关链路按错误认证方式处理，最终出现 `401`。

修复后的稳定链路是：

- Claude Desktop 第三方推理配置使用 Kimi endpoint：`https://api.kimi.com/coding/`。
- `~/.claude/settings.json` 同步写入 Kimi endpoint、Key 和真实模型 `kimi-for-coding`。
- `Opus 4.8` 只作为 UI 伪装显示入口，运行时模型必须改写为 `kimi-for-coding`。
- Code 子进程入口强制为 `CLAUDE_CODE_ENTRYPOINT=sdk-ts`。
- `/Applications/Claude.app/Contents/Helpers/disclaimer` 安装 wrapper，原始二进制备份为 `disclaimer.real`。

## 当时症状

- 设置页“测试连接”通过，显示 `1-token completion` 成功。
- Cowork/Code 会话里仍报：

```text
Failed to authenticate. API Error: 401
```

- 使用智谱 GLM 时错误信息是中文 `令牌已过期或验证不正确`。
- 换 Kimi Key 后错误变成英文 `The API Key appears to be invalid`，但本质仍是 Code 实际请求链路没有走对。
- 黄色配置提示曾显示 `configured model "glm-5.1" is not an Anthropic model`，说明 3P profile 里的 `inferenceModels` 不能填非 Anthropic 名称，必须用 provider route 或由运行时映射接管。

## 验证过的事实

这次排障不要只看 UI 成功提示，必须同时验证实际请求路径。

- 直接调用 Kimi `/v1/messages`，使用同一个 Key 和 `kimi-for-coding`，返回 `200`。
- 用 Claude Code CLI 测试：
  - `CLAUDE_CODE_ENTRYPOINT=claude-desktop-3p` 会触发 `401`。
  - `CLAUDE_CODE_ENTRYPOINT=sdk-ts` 可以正常返回。
- 最新失败 transcript 里能看到 Code run metadata 仍是 `entrypoint=claude-desktop-3p`。
- `patch_claude_zh_cn.py --diagnose` 可以通过，但这只能证明静态补丁点存在，不等于实际 UI 启动路径已经继承正确环境。

## 修复方案

### 1. 同步用户配置

`~/.claude/settings.json` 至少需要保持这些字段一致：

```json
{
  "model": "kimi-for-coding",
  "effortLevel": "max",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/",
    "ANTHROPIC_MODEL": "kimi-for-coding",
    "ANTHROPIC_SMALL_FAST_MODEL": "kimi-for-coding",
    "CLAUDE_CODE_ENTRYPOINT": "sdk-ts"
  }
}
```

实际文件还会包含 `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY`。文档和日志不能记录 Key 明文。

### 2. 同步 Claude 3P profile

当前启用 profile 位于：

```text
~/Library/Application Support/Claude-3p/configLibrary/*.json
```

关键字段应为：

- `inferenceProvider: gateway`
- `inferenceCredentialKind: static`
- `inferenceGatewayBaseUrl: https://api.kimi.com/coding/`
- `inferenceGatewayAuthScheme: bearer`
- `inferenceModels: ["kimi-for-coding"]`
- `modelDiscoveryEnabled: false`
- `unstableDisableModelVerification: true`

注意：Claude Desktop 的 provider 校验会要求配置的模型 route 看起来像 Anthropic 模型。若直接填 `glm-5.1` 这类非 Anthropic 名称，UI 可能提示 provider 配置需要修复。

### 3. 安装 disclaimer wrapper

最终生效点在：

```text
/Applications/Claude.app/Contents/Helpers/disclaimer
```

安装后结构为：

```text
/Applications/Claude.app/Contents/Helpers/disclaimer       # wrapper 脚本
/Applications/Claude.app/Contents/Helpers/disclaimer.real  # 原始二进制
```

wrapper 做四件事：

- 从 `~/.claude/settings.json` 读取 Kimi 运行时环境。
- 强制导出 `CLAUDE_CODE_ENTRYPOINT=sdk-ts`。
- 清掉 OAuth 相关变量，避免错误走官方账号认证。
- 当命令是 Claude Code CLI 时，把 `--model Opus...`、`--model default`、`--model claude-*` 改写为 `kimi-for-coding`。

wrapper 模板保存在项目根目录：

```text
claude-disclaimer-kimi-env-wrapper
```

`install.command` 会通过 `patch_claude_zh_cn.py` 把它安装进 app bundle 并重新签名。

## 验证清单

修复后不要只看“测试连接”。完整验证应包含：

1. 跑安装诊断：

```bash
python3 patch_claude_zh_cn.py --diagnose
```

2. 确认 Kimi messages 接口真实可用，返回 `200`。

3. 看 wrapper 日志：

```bash
tail -20 ~/.claude/desktop-kimi-wrapper.log
```

应看到类似：

```text
entrypoint=sdk-ts model=kimi-for-coding rewrote_model=true
```

4. 看最新 Code transcript：

```bash
rg '"entrypoint":"sdk-ts"|"model":"kimi-for-coding"|API Error: 401' ~/.claude/projects
```

新会话里应出现 `sdk-ts` 和 `kimi-for-coding`，不应继续出现新的 `401`。

## 经验教训

- “测试连接通过”只证明设置页探测请求通过，不证明 Code 子进程的实际启动链路正确。
- 静态 bundle patch 命中不等于运行时生效。遇到反复 401，要查 transcript 里的 `entrypoint` 和真实 `model`。
- 不要把真实模型名和 UI 伪装名混在同一个责任层。UI 可以显示 `Opus 4.8`，运行时必须使用 provider 真实模型 `kimi-for-coding`。
- 不要盲删 `~/.claude` 或 `Claude-3p`。这些目录包含 CLI、VS Code 插件、hooks、会话和 provider profile，应该先备份再做定向同步。
- 不要继续用旧会话反复测试。修复入口、模型和 env 后，开新会话验证更干净。
- wrapper 日志不能记录完整 argv，因为 MCP 配置、环境参数或项目命令里可能带密钥。只记录 entrypoint、最终模型、是否改写模型和命令 basename。
- 如果以后又出现 `401`，优先对比三层：Kimi 直连、Claude CLI entrypoint、Claude Desktop 实际 transcript。

## 回滚方法

如果要恢复官方 helper：

```bash
sudo cp /Applications/Claude.app/Contents/Helpers/disclaimer.real /Applications/Claude.app/Contents/Helpers/disclaimer
sudo codesign --force --deep --sign - /Applications/Claude.app
```

也可以直接重装官方 Claude Desktop，然后重新运行本项目 `install.command`。

配置回滚使用当时生成的备份：

```text
~/.claude/backups/settings.before-kimi-clean-*.json
~/Library/Application Support/Claude-3p/configLibrary/*.json.before-kimi-clean-*
```

回滚后再次检查 `~/.claude/settings.json`、Claude 3P profile 和最新 Code transcript，确认没有残留旧模型或旧 endpoint。
