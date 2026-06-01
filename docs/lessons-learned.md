# 补丁开发复盘：Opus 显示版本号升级的经验教训

> 记录 2026-06-01 将 `Opus 4.71M` 升级到 `Opus 4.8` 过程中踩过的坑，以及项目优化的方向。

---

## 一、版本号更新的陷阱

### 1. 常量不够

`OPUS_DISPLAY_NAME = "Opus 4.8"` 只是 Python 层的常量。实际 bundle 中有 **40+ 处硬编码**的显示名称，分布在 old_string、new_string、转义引号、单引号等多种格式中。

### 2. 转义引号是不同字符序列

补丁字典中的 old_string 和 new_string 都可能包含转义引号：

```python
# 文件中实际字符：\ 和 " 是两个字符
'name:\"Opus 4.71M\"'

# 普通双引号
'name:"Opus 4.71M"'
```

`replace_all` 搜索 `"Opus 4.71M"` 不会匹配到 `\"Opus 4.71M\"`，必须分别处理。

### 3. 单引号也要单独处理

```python
'Opus 4.71M' in text   # 单引号包裹
```

这与 `"Opus 4.71M"`（双引号）也是不同字符序列。

### 4. old_string 中的版本号

补丁字典是 `old: new` 映射。如果只改 new_string 中的版本号，old_string 里还是旧的，会导致：

- 替换时 old_string 找不到匹配（因为文件中可能已被旧版补丁改成新格式）
- 或者更隐蔽：old_string 和 new_string 同时存在，但 key 重复导致字典覆盖

**正确做法**：用脚本全局搜索所有 `4.71M`、`4.7 1M` 出现位置，逐一确认是 old_string、new_string 还是常量，然后全部同步更新。

---

## 二、旧版补丁残留问题（本次最大坑）

### 现象

安装脚本报告 required failure：

```
cowork.two_models (index-DhPEOQY7.js): missing
```

但 `index-DhPEOQY7.js` 中已有 `zhModelConfig18555` 和 `Kimi-k2.6`，只是没有 `Opus 4.8`。

### 根因

```
第一次运行（旧版脚本）：
  old_string_A 命中 → 写入含 "Opus 4.71M" 的 target_A

Claude Desktop 更新后：
  old_string_A 在 bundle 中消失（代码结构变了）

第二次运行（新版脚本）：
  新版 old_string_B 找不到匹配 → 无法覆盖旧版 target_A 中的 "Opus 4.71M"
  → 残留 → diagnostics 检查 'Opus 4.8' in text 失败 → required failure
```

### 关键洞察

`install.command` 的工作流程是：

1. 从 `/Applications/Claude.app` 复制到临时目录
2. 在临时目录中应用所有补丁
3. 通过诊断后替换原始 app

**陷阱**：如果用户之前运行过旧版补丁，`/Applications/Claude.app` 已被修改过。新版补丁从"已修改的 app"复制，旧版补丁写入的内容仍在 bundle 中，但新版补丁的 old_string 在新版 Claude 代码中可能已不存在，导致无法覆盖。

### 解决方案：兜底清理逻辑

不依赖 old_string 匹配，直接扫描所有 `*.js` 文件中的已知残留模式：

```python
stale_opus_cleanup = {
    'name:"Opus 4.71M"': 'name:"Opus 4.8"',
    'label_override:"Opus 4.71M"': 'label_override:"Opus 4.8"',
    # ... 更多模式
}
```

这个逻辑应该放在**所有模型菜单补丁函数之后**，作为全量兜底。

### 教训

- 补丁脚本不是"无状态"的：它运行在可能已被自己修改过的目标上
- 每个补丁函数都应该有对应的"清理残留"步骤
- 不能假设 old_string 永远存在于 bundle 中

---

## 三、多文件分散问题

Cowork/Code 模型菜单逻辑分散在多个前端 bundle 中：

| 文件 | 负责的功能 | 处理函数 |
|------|-----------|---------|
| `index-DhPEOQY7.js` | 主 bundle，Cowork 配置 | `patch_cowork_model_menu` |
| `c5610fbe3-rsWnjbnF.js` | Code 页面模型选择器 | `patch_epitaxy_model_menu` |
| `app.asar` (`.vite/build/index.js`) | 共享模型逻辑 | `patch_hardcoded_frontend_strings` |

每个文件由不同补丁函数处理，检测条件各不相同：

- `patch_cowork_model_menu`：检查 `Qte=(e="ccr_model"` 或 `Xae`/`Wmt`
- `patch_epitaxy_model_menu`：检查 `const hm="ccd-effort-level"` + `modelExtraSections:Lt`

**陷阱**：残留可能出现在任何分支未覆盖的文件中。本次 `c5610fbe3-rsWnjbnF.js` 就是因为不符合 `patch_epitaxy_model_menu` 的检测条件而被跳过，但文件中又有旧版补丁写入的 `Opus 4.71M`。

### 教训

- 不能依赖"检测条件 + 分支处理"来覆盖所有文件
- 必须有全量兜底扫描，不依赖任何版本标记

---

## 四、诊断系统的盲区

当前的 `cowork.two_models` 诊断检查：

```python
(
    'Q=[rr,cc],X=[],J=[]' in text
    or 'allModelOptions:[r,l],mainModels:[r,l],overflowModels:[]' in text
    or 'zhCoworkConfig=...' in text
    or 'zhModelConfig18555' in text
)
and 'Opus 4.8' in text
and 'Kimi-k2.6' in text
```

**盲区**：`'Opus 4.8' in text` 为 False 时，只知道"没有新显示名"，但不知道是：
- 补丁根本没应用到该文件？
- 还是旧版残留未被覆盖？
- 还是新版 Claude 使用了完全不同的代码结构？

改进方向：诊断应该报告更精确的信息，比如"发现 2 处旧版残留：文件 X 偏移 Y"。

---

## 五、项目优化方向汇总

### 5.1 立即能做的

- [ ] 将 `stale_opus_cleanup` 提取为独立函数，在 `patch_cowork_model_menu` 和 `patch_epitaxy_model_menu` 之后都调用
- [ ] 安装后增加全量扫描：`rg 'Opus 4\.7[0-9]' /Applications/Claude.app/Contents/Resources`
- [ ] 写一个 `bump_opus_display_name.py` 工具脚本，自动同步所有 old_string / new_string / 常量

### 5.2 中期改进

- [ ] 模块化拆分 `patch_claude_zh_cn.py`（已超 2500 行）
- [ ] 引入"补丁沙盒 + 两阶段安装"：沙盒中打完补丁后先全量验证，通过才替换原始 app
- [ ] 结构化诊断报告：增加 `residue_scan` 字段，记录所有文件中的残留检查结果

### 5.3 长期目标

- [ ] 回归测试脚本：每次 Claude Desktop 更新后自动验证模型菜单、强度选项、残留检查
- [ ] 版本号集中配置：通过一个常量驱动所有 old_string / new_string 的生成

---

## 六、快速排查清单

下次升级显示版本号时，按这个顺序检查：

1. **搜索所有出现位置**：`grep -n 'Opus 4\.[0-9]' patch_claude_zh_cn.py`
2. **检查转义引号**：`grep -n '\\"Opus 4\.[0-9]' patch_claude_zh_cn.py`
3. **检查单引号**：`grep -n "'Opus 4\.[0-9]'" patch_claude_zh_cn.py`
4. **检查 old_string 中是否遗漏**：有些 old_string 本身包含旧版本号
5. **运行安装后全量扫描**：
   ```bash
   find /Applications/Claude.app/Contents/Resources -name '*.js' -exec grep -H 'Opus 4\.7' {} +
   ```
6. **验证诊断通过**：`python3 patch_claude_zh_cn.py --diagnose`

---

*文档生成时间：2026-06-01*
