# 治理工作流脚本

> 配套文档: [docs/governance/priority-workflow-design.md](../docs/governance/priority-workflow-design.md)

## governance-priority.py

基于 `code-review-graph` 知识图谱数据，自动计算代码治理项的优先级分数，输出分层治理计划。

### 依赖

- Python 3.10+
- 无需第三方包（仅使用标准库 `sqlite3`、`json`、`argparse`、`dataclasses`）
- **前提条件**: 项目根目录已运行 `code-review-graph-plus` 生成 `.code-review-graph/graph.db`

### 用法

```bash
# 基本用法：读取默认图谱，输出到 .remember/
python scripts/governance-priority.py

# 指定输出目录
python scripts/governance-priority.py --output-dir ./plans

# 调整权重（安全审计场景：提高风险权重）
python scripts/governance-priority.py --weights "C=0.20,B=0.20,R=0.30,T=0.15,D=0.10,S=0.05"

# 只打印到控制台，不写入文件
python scripts/governance-priority.py --dry-run

# 只输出 JSON 状态
python scripts/governance-priority.py --json-only

# 指定触发条件（用于状态记录）
python scripts/governance-priority.py --trigger "graph_rebuild"
```

### 输出文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 治理计划 | `.remember/governance-plan-YYYY-MM-DD.md` | 人可读的 Markdown，按 P1/P2/P3 分层 |
| 运行时状态 | `.remember/governance-state.json` | 机器可读的 JSON，包含完整评分明细 |

### 评估模型

六维度加权评分（详见设计文档 §3.1）：

| 维度 | 权重 | 数据来源 |
|------|------|----------|
| 内聚度 (C) | 20% | 图谱社区 `cohesiveness` |
| 业务价值 (B) | 25% | 手工评估 |
| 风险暴露 (R) | 20% | 图谱 `risk_index` |
| 测试缺口 (T) | 15% | 手工评估 |
| 交付成本 (D) | 10% | 图谱节点数/跨文件数（反向） |
| 可交付性 (S) | 10% | 手工评估 |

### 当前治理项列表

1. **提取 patches/gateway.py** — 网关认证/环境函数
2. **提取 patches/asar.py** — ASAR 档案操作函数
3. **版本分支函数化** — 拆分 model_menu 的版本分支
4. **提取 patches/signing.py** — 签名/权限函数
5. **主脚本集成测试** — 6230 行主脚本测试覆盖
6. **清理 sub1/sub3 杂项** — 零凝聚度社区重组
7. **提取 patches/frontend.py** — 前端字符串替换函数
8. **提交上游 PR** — GitHub PR 创建

### 演进路线

| 版本 | 目标 | 状态 |
|------|------|------|
| v1.0 | 基于内置数据 + 图谱自动读取 C/R/D 维度 | ✅ 当前 |
| v1.5 | 直接从图谱 `community_summaries` 读取更多元数据 | 🚧 规划中 |
| v2.0 | 接入 git diff，评估"最近修改区域"紧迫性 | 📋 待排期 |
| v2.5 | 接入 CI 测试覆盖率数据，T 维度自动量化 | 📋 待排期 |
