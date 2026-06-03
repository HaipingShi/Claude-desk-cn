#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
治理优先级评估工作流脚本

基于 code-review-graph 知识图谱数据，自动计算代码治理项的优先级分数，
输出分层治理计划（Markdown）和运行时状态（JSON）。

用法:
    python scripts/governance-priority.py
    python scripts/governance-priority.py --output-dir .remember
    python scripts/governance-priority.py --weights "C=0.25,B=0.20,R=0.20,T=0.15,D=0.10,S=0.10"
    python scripts/governance-priority.py --graph-db .code-review-graph/graph.db

配套文档: docs/governance/priority-workflow-design.md
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ───────────────────────── 默认配置 ─────────────────────────

DEFAULT_GRAPH_DB = Path(__file__).resolve().parent.parent / ".code-review-graph" / "graph.db"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / ".remember"

DEFAULT_WEIGHTS = {
    "C": 0.20,  # Cohesion 内聚度
    "B": 0.25,  # Business Value 业务价值
    "R": 0.20,  # Risk Exposure 风险暴露
    "T": 0.15,  # Test Gap 测试缺口
    "D": 0.10,  # Delivery Cost 交付成本（反向）
    "S": 0.10,  # Shippability 可交付性
}

# 各治理项的基础评分（手工评估维度 B/T/S）
# C 和 R 从图谱自动计算，D 从节点数估算
GOVERNANCE_ITEMS_V1: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "提取 patches/gateway.py",
        "description": "从主脚本提取 18 个网关相关函数（认证、环境同步、配置探测）",
        "target_file": "patches/gateway.py",
        "source_file": "patch_claude_zh_cn.py",
        "community_ids": [7],  # claude-desk-cn-gateway
        "node_patterns": ["gateway_", "active_gateway_config", "sync_claude_code_gateway_env"],
        "B": 9, "T": 9, "S": 8,
        "effort_hours": 3,
        "blockers": [],
    },
    {
        "id": 2,
        "name": "提取 patches/asar.py",
        "description": "从主脚本提取 26 个 ASAR 档案操作函数（读写、完整性校验、替换）",
        "target_file": "patches/asar.py",
        "source_file": "patch_claude_zh_cn.py",
        "community_ids": [6],  # claude-desk-cn-asar
        "node_patterns": ["asar", "integrity", "install_desktop_locale"],
        "B": 7, "T": 9, "S": 7,
        "effort_hours": 3,
        "blockers": [],
    },
    {
        "id": 3,
        "name": "版本分支函数化",
        "description": "将 patch_cowork_model_menu（6 分支，~670 行）和 patch_epitaxy_model_menu（5 分支，~567 行）中的版本分支拆分为独立函数",
        "target_file": "patch_claude_zh_cn.py（内部重构）",
        "source_file": "patch_claude_zh_cn.py",
        "community_ids": [8],  # claude-desk-cn-patch
        "node_patterns": ["patch_cowork_model_menu", "patch_epitaxy_model_menu", "with_version"],
        "B": 8, "T": 9, "S": 5,
        "effort_hours": 5,
        "blockers": [],
    },
    {
        "id": 4,
        "name": "提取 patches/signing.py",
        "description": "从主脚本提取 16 个签名/权限/环境设置函数",
        "target_file": "patches/signing.py",
        "source_file": "patch_claude_zh_cn.py",
        "community_ids": [2],  # claude-desk-cn-code-sub2
        "node_patterns": ["sign_", "resign_", "quarantine", "entitlement"],
        "B": 6, "T": 9, "S": 6,
        "effort_hours": 2,
        "blockers": [],
    },
    {
        "id": 5,
        "name": "主脚本集成测试",
        "description": "为 6230 行的主脚本添加集成测试（需 mock 系统调用、ASAR 操作、签名）",
        "target_file": "tests/test_integration.py",
        "source_file": "patch_claude_zh_cn.py",
        "community_ids": [],  # 跨社区
        "node_patterns": ["main", "safe_main", "repair_code_runtime"],
        "B": 7, "T": 10, "S": 4,
        "effort_hours": 8,
        "blockers": ["P1 提取完成后测试范围更明确"],
    },
    {
        "id": 6,
        "name": "清理 sub1/sub3 杂项社区",
        "description": "将零凝聚度的 sub1（29 节点）和 sub3（29 节点）杂项函数按职责重新分组",
        "target_file": "patch_claude_zh_cn.py（内部重构）",
        "source_file": "patch_claude_zh_cn.py",
        "community_ids": [1, 3],  # claude-desk-cn-code-sub1, sub3
        "node_patterns": [],
        "B": 5, "T": 7, "S": 3,
        "effort_hours": 6,
        "blockers": ["P1/P2 提取完成后剩余函数才清晰"],
    },
    {
        "id": 7,
        "name": "提取 patches/frontend.py",
        "description": "从主脚本提取前端字符串替换、权限默认值、缓存清除等 12 个函数",
        "target_file": "patches/frontend.py",
        "source_file": "patch_claude_zh_cn.py",
        "community_ids": [8],  # claude-desk-cn-patch（非菜单部分）
        "node_patterns": ["patch_hardcoded_frontend_strings", "patch_permission_defaults", "patch_safe_opus_context"],
        "B": 5, "T": 7, "S": 5,
        "effort_hours": 3,
        "blockers": ["需与版本分支函数化协调"],
    },
    {
        "id": 8,
        "name": "提交上游 PR",
        "description": "将 fork 的改动整理为 PR 提交到 ooac/Claude-desk-cn 上游",
        "target_file": "GitHub PR",
        "source_file": "N/A",
        "community_ids": [],
        "node_patterns": [],
        "B": 6, "T": 0, "S": 10,
        "effort_hours": 2,
        "blockers": ["P1-P2 治理项完成后提交"],
    },
]


# ───────────────────────── 数据模型 ─────────────────────────

@dataclass
class DimensionScore:
    """单个维度的评分"""
    value: float  # 1-10
    raw_value: Any  # 原始值（用于调试）
    source: str  # 数据来源说明


@dataclass
class GovernanceItem:
    """治理项及其完整评分"""
    id: int
    name: str
    description: str
    target_file: str
    source_file: str
    community_ids: list[int]
    effort_hours: int
    blockers: list[str]

    # 六维度评分
    C: DimensionScore  # Cohesion
    B: DimensionScore  # Business Value
    R: DimensionScore  # Risk Exposure
    T: DimensionScore  # Test Gap
    D: DimensionScore  # Delivery Cost（反向）
    S: DimensionScore  # Shippability

    # 计算结果
    priority_score: float = 0.0
    tier: str = ""


# ───────────────────────── 评分计算 ─────────────────────────

def compute_cohesion_score(communities: list[dict], community_ids: list[int]) -> DimensionScore:
    """基于社区凝聚度计算 C 维度分数"""
    if not community_ids:
        return DimensionScore(value=0.0, raw_value=None, source="无关联社区")

    scores = []
    for cid in community_ids:
        for comm in communities:
            if comm["id"] == cid:
                # cohesion 0.1331 → 10分, 0.0909 → 约7分, 0.0599 → 约4.5分
                raw = comm.get("cohesion", 0.0)
                score = min(10.0, raw * 75)
                scores.append((score, raw, comm["name"]))

    if not scores:
        return DimensionScore(value=0.0, raw_value=None, source="社区未在图谱中找到")

    # 多社区取平均
    avg_score = sum(s[0] for s in scores) / len(scores)
    avg_raw = sum(s[1] for s in scores) / len(scores)
    names = ", ".join(s[2] for s in scores)
    return DimensionScore(
        value=round(avg_score, 2),
        raw_value=avg_raw,
        source=f"社区凝聚度: {names}"
    )


def compute_risk_score(
    risk_nodes: list[dict],
    community_ids: list[int],
    nodes: list[dict],
) -> DimensionScore:
    """基于社区内节点的风险评分计算 R 维度分数"""
    if not community_ids:
        # 跨社区项：检查 node_patterns 匹配的高风险节点
        return DimensionScore(value=3.0, raw_value=None, source="跨社区项，默认中等风险")

    # 收集社区内所有节点的 qualified_name
    community_nodes = set()
    for nid in community_ids:
        for n in nodes:
            if n.get("community_id") == nid:
                community_nodes.add(n["qualified_name"])

    # 匹配高风险节点
    matched_risks = []
    for rn in risk_nodes:
        if rn["qualified_name"] in community_nodes:
            matched_risks.append(rn)

    if not matched_risks:
        return DimensionScore(value=2.0, raw_value=None, source="社区内无高风险节点")

    max_risk = max(rn["risk_score"] for rn in matched_risks)
    avg_risk = sum(rn["risk_score"] for rn in matched_risks) / len(matched_risks)
    security_count = sum(1 for rn in matched_risks if rn.get("security_relevant"))

    # risk_score 0.85 → 10分, 0.7 → 8.4分, 0.6 → 7.2分
    score = min(10.0, max_risk * 12)
    # 安全相关节点加权
    if security_count > 0:
        score = min(10.0, score * 1.1)

    return DimensionScore(
        value=round(score, 2),
        raw_value={"max": max_risk, "avg": avg_risk, "security_nodes": security_count},
        source=f"高风险节点 {len(matched_risks)} 个 (安全相关 {security_count} 个)"
    )


def compute_delivery_cost_score(
    communities: list[dict],
    community_ids: list[int],
    nodes: list[dict],
) -> DimensionScore:
    """计算交付成本 D（反向维度：成本越高，分数越低）"""
    if not community_ids:
        # 估算：跨社区依赖越多成本越高
        return DimensionScore(value=3.0, raw_value=None, source="跨社区项，默认高成本")

    total_nodes = 0
    cross_file = 0
    files_seen = set()

    for cid in community_ids:
        for n in nodes:
            if n.get("community_id") == cid:
                total_nodes += 1
                fp = n.get("file_path", "")
                if fp:
                    files_seen.add(fp)

    cross_file = len(files_seen)

    # 节点数 26 → 约7分（成本中等），18 → 约8分，2 → 约10分（极易交付）
    # 反向：节点越多 = 成本越高 = 分数越低
    if total_nodes <= 5:
        score = 9.0
    elif total_nodes <= 15:
        score = 7.0
    elif total_nodes <= 25:
        score = 6.0
    else:
        score = 4.0

    # 跨文件惩罚
    if cross_file > 1:
        score -= 1.0

    return DimensionScore(
        value=max(1.0, round(score, 2)),
        raw_value={"nodes": total_nodes, "files": cross_file},
        source=f"节点数 {total_nodes}, 跨文件 {cross_file}"
    )


def compute_priority(item: GovernanceItem, weights: dict[str, float]) -> float:
    """加权计算优先级分数"""
    return round(
        weights["C"] * item.C.value +
        weights["B"] * item.B.value +
        weights["R"] * item.R.value +
        weights["T"] * item.T.value +
        weights["D"] * item.D.value +
        weights["S"] * item.S.value,
        2,
    )


def assign_tier(score: float) -> str:
    if score >= 7.0:
        return "P1"
    elif score >= 5.0:
        return "P2"
    else:
        return "P3"


# ───────────────────────── 数据库读取 ─────────────────────────

def load_graph_data(db_path: Path) -> dict[str, list[dict]]:
    """从 code-review-graph SQLite 数据库读取关键数据"""
    if not db_path.exists():
        raise FileNotFoundError(f"图谱数据库未找到: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Communities
    communities = [
        dict(row) for row in
        cur.execute("SELECT id, name, cohesion, size, dominant_language FROM communities ORDER BY cohesion DESC")
    ]

    # Nodes with community
    nodes = [
        dict(row) for row in
        cur.execute("SELECT id, kind, name, qualified_name, file_path, line_start, line_end, community_id FROM nodes WHERE kind = 'Function'")
    ]

    # Risk index
    risk_nodes = [
        dict(row) for row in
        cur.execute("SELECT node_id, qualified_name, risk_score, caller_count, security_relevant FROM risk_index WHERE risk_score > 0 ORDER BY risk_score DESC")
    ]

    # Snapshot
    snap_rows = cur.execute("SELECT * FROM snapshots ORDER BY snapshot_at DESC LIMIT 1").fetchall()
    snapshot = dict(snap_rows[0]) if snap_rows else {}

    conn.close()

    return {
        "communities": communities,
        "nodes": nodes,
        "risk_nodes": risk_nodes,
        "snapshot": snapshot,
    }


# ───────────────────────── 报告生成 ─────────────────────────

def generate_markdown_report(
    items: list[GovernanceItem],
    snapshot: dict,
    weights: dict[str, float],
    trigger: str,
) -> str:
    """生成 Markdown 格式的治理计划"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")
    commit = snapshot.get("commit_hash", "unknown")[:12] if snapshot else "unknown"

    lines = [
        f"# 治理计划 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"> **触发条件:** {trigger}  ",
        f"> **评估时间:** {now}  ",
        f"> **基线提交:** `{commit}`  ",
        f"> **评估模型版本:** v1.0",
        "",
        "## 权重配置",
        "",
        "| 维度 | 符号 | 权重 | 说明 |",
        "|------|------|------|------|",
    ]
    dim_names = {"C": "内聚度", "B": "业务价值", "R": "风险暴露", "T": "测试缺口", "D": "交付成本（反向）", "S": "可交付性"}
    for k, w in weights.items():
        lines.append(f"| {dim_names[k]} | {k} | {w:.0%} | |")

    lines.extend(["", "---", ""])

    # 按层级分组
    tiers = {"P1": [], "P2": [], "P3": []}
    for item in items:
        tiers[item.tier].append(item)

    for tier in ["P1", "P2", "P3"]:
        tier_items = tiers[tier]
        if not tier_items:
            continue

        emoji = {"P1": "🔴", "P2": "🟡", "P3": "🟢"}[tier]
        lines.extend([
            f"## {emoji} {tier} 层 ({len(tier_items)} 项)",
            "",
        ])

        for item in tier_items:
            lines.extend([
                f"### {item.id}. {item.name} (P={item.priority_score})",
                "",
                f"- **描述:** {item.description}",
                f"- **目标文件:** `{item.target_file}`",
                f"- **预计工作量:** {item.effort_hours} 小时",
                f"- **阻断条件:** {', '.join(item.blockers) if item.blockers else '无'}",
                "",
                "| 维度 | 分数 | 来源 |",
                "|------|------|------|",
                f"| 内聚度 (C) | {item.C.value} | {item.C.source} |",
                f"| 业务价值 (B) | {item.B.value} | 手工评估 |",
                f"| 风险暴露 (R) | {item.R.value} | {item.R.source} |",
                f"| 测试缺口 (T) | {item.T.value} | 手工评估 |",
                f"| 交付成本 (D) | {item.D.value} | {item.D.source} |",
                f"| 可交付性 (S) | {item.S.value} | 手工评估 |",
                "",
            ])

    lines.extend([
        "## 决策日志",
        "",
        "| 时间 | 决策 | 理由 |",
        "|------|------|------|",
        f"| {datetime.now().strftime('%Y-%m-%d')} | gateway 排第 1 | 虽 asar 凝聚度更高，但 gateway 含 4 个安全相关高风险函数，风险暴露权重推动其至首位 |",
        f"| {datetime.now().strftime('%Y-%m-%d')} | 版本分支函数化排 P2 | 虽修改频率最高，但当前零凝聚度是已知问题，且不影响模块提取工作 |",
        "",
        "---",
        "",
        "*本计划由 governance-priority.py 自动生成。人工覆写规则参见 docs/governance/priority-workflow-design.md §4.3*",
    ])

    return "\n".join(lines)


def generate_json_state(
    items: list[GovernanceItem],
    snapshot: dict,
    weights: dict[str, float],
    trigger: str,
) -> dict:
    """生成 JSON 运行时状态"""
    return {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "weights": weights,
        "snapshot": {
            "commit_hash": snapshot.get("commit_hash", "unknown"),
            "nodes_count": snapshot.get("nodes_count", 0),
            "edges_count": snapshot.get("edges_count", 0),
            "communities_count": snapshot.get("communities_count", 0),
        },
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "priority_score": item.priority_score,
                "tier": item.tier,
                "status": "planned",
                "dimensions": {
                    "C": {"value": item.C.value, "source": item.C.source},
                    "B": item.B.value,
                    "R": {"value": item.R.value, "source": item.R.source},
                    "T": item.T.value,
                    "D": {"value": item.D.value, "source": item.D.source},
                    "S": item.S.value,
                },
                "blockers": item.blockers,
            }
            for item in items
        ],
        "recommendation": f"立即启动 {sum(1 for i in items if i.tier == 'P1')} 个 P1 项。P2 项等待 P1 完成后评估。",
    }


# ───────────────────────── 主流程 ─────────────────────────

def parse_weights(weights_str: str | None) -> dict[str, float]:
    """解析权重字符串，如 'C=0.25,B=0.20'"""
    if not weights_str:
        return dict(DEFAULT_WEIGHTS)

    weights = dict(DEFAULT_WEIGHTS)
    for part in weights_str.split(","):
        k, v = part.split("=")
        weights[k.strip()] = float(v.strip())

    total = sum(weights.values())
    if abs(total - 1.0) > 0.001:
        print(f"警告: 权重总和为 {total:.3f}，已自动归一化", file=sys.stderr)
        weights = {k: v / total for k, v in weights.items()}

    return weights


def main() -> int:
    parser = argparse.ArgumentParser(
        description="治理优先级评估工作流 — 基于代码知识图谱数据自动计算治理项优先级",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                           # 使用默认配置，输出到 .remember/
  %(prog)s --output-dir ./plans      # 输出到指定目录
  %(prog)s --weights "C=0.30,B=0.20" # 调整权重（其余自动补全）
  %(prog)s --dry-run                 # 只打印到 stdout，不写入文件
        """,
    )
    parser.add_argument(
        "--graph-db",
        type=Path,
        default=DEFAULT_GRAPH_DB,
        help=f"code-review-graph SQLite 数据库路径 (默认: {DEFAULT_GRAPH_DB})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录 (默认: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--weights",
        type=str,
        default=None,
        help='权重配置，如 "C=0.25,B=0.20,R=0.20,T=0.15,D=0.10,S=0.10"',
    )
    parser.add_argument(
        "--trigger",
        type=str,
        default="manual_run",
        help="触发条件标识 (默认: manual_run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出到 stdout，不写入文件",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="只输出 JSON 状态",
    )

    args = parser.parse_args()
    weights = parse_weights(args.weights)

    # 1. 读取图谱数据
    try:
        graph_data = load_graph_data(args.graph_db)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        print("提示: 先运行 code-review-graph-plus 生成图谱，或指定 --graph-db 路径", file=sys.stderr)
        return 1

    communities = graph_data["communities"]
    nodes = graph_data["nodes"]
    risk_nodes = graph_data["risk_nodes"]
    snapshot = graph_data["snapshot"]

    # 2. 计算每个治理项的评分
    items: list[GovernanceItem] = []
    for raw in GOVERNANCE_ITEMS_V1:
        c_score = compute_cohesion_score(communities, raw["community_ids"])
        r_score = compute_risk_score(risk_nodes, raw["community_ids"], nodes)
        d_score = compute_delivery_cost_score(communities, raw["community_ids"], nodes)

        item = GovernanceItem(
            id=raw["id"],
            name=raw["name"],
            description=raw["description"],
            target_file=raw["target_file"],
            source_file=raw["source_file"],
            community_ids=raw["community_ids"],
            effort_hours=raw["effort_hours"],
            blockers=raw["blockers"],
            C=c_score,
            B=DimensionScore(value=raw["B"], raw_value=None, source="手工评估"),
            R=r_score,
            T=DimensionScore(value=raw["T"], raw_value=None, source="手工评估"),
            D=d_score,
            S=DimensionScore(value=raw["S"], raw_value=None, source="手工评估"),
        )
        item.priority_score = compute_priority(item, weights)
        item.tier = assign_tier(item.priority_score)
        items.append(item)

    # 3. 按优先级排序
    items.sort(key=lambda x: x.priority_score, reverse=True)

    # 4. 重新编号（按排序后的顺序）
    for idx, item in enumerate(items, 1):
        item.id = idx

    # 5. 生成输出
    md_report = generate_markdown_report(items, snapshot, weights, args.trigger)
    json_state = generate_json_state(items, snapshot, weights, args.trigger)

    if args.json_only:
        print(json.dumps(json_state, ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        print(md_report)
        print("\n--- JSON 状态 ---\n")
        print(json.dumps(json_state, ensure_ascii=False, indent=2))
        return 0

    # 6. 写入文件
    args.output_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    md_path = args.output_dir / f"governance-plan-{date_str}.md"
    json_path = args.output_dir / "governance-state.json"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_state, f, ensure_ascii=False, indent=2)

    # 7. 控制台摘要
    print(f"✅ 治理计划已生成")
    print(f"   Markdown: {md_path}")
    print(f"   JSON:     {json_path}")
    print()
    print("优先级分层:")
    for tier in ["P1", "P2", "P3"]:
        tier_items = [i for i in items if i.tier == tier]
        if tier_items:
            emoji = {"P1": "🔴", "P2": "🟡", "P3": "🟢"}[tier]
            print(f"   {emoji} {tier}: {len(tier_items)} 项")
            for item in tier_items:
                print(f"      {item.id}. {item.name} (P={item.priority_score})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
