# 第三方网关 Code 401 排障指南

Claude Desktop 使用第三方网关时，Cowork/Code 模式可能遇到 `401 API Key invalid`。
本文记录通用排障方法论，不绑定任何具体 provider（Kimi/DeepSeek/GLM 等）。

## 根因模型

核心问题不是 Key 本身无效，而是 **Claude Desktop 拉起 Code 子进程时的认证链路不正确**。

设置页"测试连接"通过只证明浏览器端请求成功。Code 子进程使用完全不同的启动路径，
可能没有继承相同的环境变量和认证方式。

## 通用排障步骤

### 第一步：确认网关本身可用

直接调用网关的 messages 接口，排除 Key 和 endpoint 问题：

```bash
curl -s <gateway-endpoint>/v1/messages \
  -H "Authorization: Bearer <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"<gateway-model>","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
```

返回 `200` 说明 Key 和 endpoint 正常。返回 `401`/`403` 先检查 Key 是否过期。

### 第二步：检查 entrypoint

Claude Desktop Code 子进程最关键的环境变量是 `CLAUDE_CODE_ENTRYPOINT`：

| 值 | 含义 |
|----|------|
| `claude-desktop-3p` | 旧版/错误路径，走官方 OAuth 认证，第三方 Key 不会生效 |
| `sdk-ts` | 正确路径，使用 SDK 方式，继承用户环境的认证变量 |

验证当前 entrypoint：

```bash
# 查看最新 Code transcript 中的 entrypoint
rg '"entrypoint"' ~/.claude/projects | tail -5
```

如果出现 `claude-desktop-3p`，说明 entrypoint 未正确注入。

### 第三步：验证环境继承

Code 子进程必须拿到三个关键环境变量：

- `ANTHROPIC_BASE_URL` — 网关地址
- `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` — 认证凭据
- `ANTHROPIC_MODEL` — 真实请求模型

运行诊断确认：

```bash
python3 patch_claude_zh_cn.py --diagnose
```

查看 `Logs/latest.json` 中的：
- `runtime.claude_code_gateway_env`：是否同步了网关环境
- `runtime.pre_repair_active_code_env`：活动子进程是否继承了这些环境

如果探测正常但活动子进程环境为空，说明问题在启动层。

### 第四步：检查启动层

Claude Desktop 通过 `disclaimer` helper 拉起 Code 子进程。
如果启动层仍未注入正确环境，需要安装 wrapper（由 `install.command` 自动完成）。

wrapper 做三件事：
1. 从 `~/.claude/settings.json > env` 读取用户配置的网关环境
2. 强制设置 `CLAUDE_CODE_ENTRYPOINT=sdk-ts`
3. 清除 OAuth 相关变量，避免错误走官方账号认证

验证 wrapper 生效：

```bash
tail -20 ~/.claude/desktop-kimi-wrapper.log
```

应看到正确的 `entrypoint=sdk-ts`。

### 第五步：确认用户配置正确

`~/.claude/settings.json` 至少应包含：

```json
{
  "model": "<gateway-model>",
  "env": {
    "ANTHROPIC_BASE_URL": "<gateway-endpoint>",
    "ANTHROPIC_MODEL": "<gateway-model>",
    "ANTHROPIC_SMALL_FAST_MODEL": "<gateway-model>",
    "CLAUDE_CODE_ENTRYPOINT": "sdk-ts"
  }
}
```

Claude Desktop 的第三方推理 profile 配置位于：

```text
~/Library/Application Support/Claude-3p/configLibrary/*.json
```

关键字段：
- `inferenceProvider: gateway`
- `inferenceGatewayBaseUrl: <gateway-endpoint>`
- `inferenceGatewayAuthScheme: bearer`（或 `x-api-key`，取决于 provider）
- `inferenceModels: ["<gateway-model>"]`
- `modelDiscoveryEnabled: false`

### 第六步：验证三层一致性

每次排障必须对比三层：

| 层 | 验证方法 |
|----|---------|
| 网关直连 | `curl` 请求 `/v1/messages`，确认 `200` |
| CLI entrypoint | `CLAUDE_CODE_ENTRYPOINT=sdk-ts claude` 测试，确认正常返回 |
| Desktop transcript | 查看最新 transcript 中的 `entrypoint` 和 `model` |

三层都正确 → Code 应该正常。只有第一层正确、后两层不对 → entrypoint 或环境继承问题。
第一层就 `401` → Key 本身有问题，先换 Key 或检查网关权限。

## 常见误区

- **"测试连接通过" ≠ Code 正常**：测试连接只验证浏览器端，Code 走独立进程。
- **静态补丁点通过 ≠ 运行时生效**：诊断看到补丁点 `passed` 只说明 bundle 被修改过，不等于子进程继承了正确环境。
- **删 `~/.claude`**：不要删。这个目录包含 CLI、VS Code 插件、hooks 和会话记录。先备份，再定向同步。
- **继续用旧会话测试**：修复后应开新会话验证，旧会话可能缓存了错误的模型和 entrypoint。

## 回滚方法

恢复官方 helper（如果 wrapper 引起问题）：

```bash
sudo cp /Applications/Claude.app/Contents/Helpers/disclaimer.real \
        /Applications/Claude.app/Contents/Helpers/disclaimer
sudo codesign --force --deep --sign - /Applications/Claude.app
```

配置回滚使用 `repair_code_runtime.command` 生成的备份：

```text
~/.claude/backups/settings.before-kimi-clean-*.json
~/Library/Application Support/Claude-3p/configLibrary/*.json.before-kimi-clean-*
```

## 验证清单

修复后的完整验证：

1. 运行诊断：`python3 patch_claude_zh_cn.py --diagnose`
2. 确认消息接口可用（curl 返回 `200`）
3. 查看 wrapper 日志：`tail -20 ~/.claude/desktop-kimi-wrapper.log`
4. 查看最新 transcript 不出现新 `401`，且 `entrypoint=sdk-ts`
5. 开新会话测试 Cowork 和 Code 都能正常回复
