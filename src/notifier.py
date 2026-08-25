"""
notifier.py —— Layer 5 通知层：PushPlus 微信推送 + 控制台降级

职责边界（遵循 .coderule 与 PROJECT_PLAN.md Layer 5 规划）：
- 仅负责消息格式化与推送，严禁包含任何策略逻辑（策略属 Layer 3/4）；
- PushPlus Token 来自 settings.pushplus_token，未配置时降级为控制台输出；
- 推送失败不阻塞主流程（try-except 兜底，失败信息打印到控制台）；
- 消息格式为 Markdown（PushPlus 支持 markdown 模板）。

PushPlus API（https://www.pushplus.plus/doc/）：
  POST https://www.pushplus.plus/send
  Content-Type: application/json
  Body: {"token": "xxx", "title": "标题", "content": "Markdown内容", "template": "markdown"}

用法示例：
    from notifier import send_notification, format_ai_report, format_degraded_report
    send_notification("📊 AQuant 信号", "# 景气上行\\n...")
    send_notification("📊 纯量化信号", format_degraded_signal(analysis_result))

    # P4 策略调整（AI 层停用期间）：纯筛选报告 + 推荐池减仓建议
    from notifier import format_screening_report, format_reduce_suggestions
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

import requests

# 项目根目录与 src 目录注册到 sys.path
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config


# ---------------------------------------------------------------------------
# PushPlus API 推送
# ---------------------------------------------------------------------------
PUSHPLUS_API_URL = "https://www.pushplus.plus/send"


def send_notification(title: str, content: str, token: Optional[str] = None) -> bool:
    """推送通知：PushPlus 微信推送，Token 未配置时降级为控制台输出

    :param title: 消息标题（微信通知栏显示）
    :param content: 消息正文（Markdown 格式）
    :param token: PushPlus Token（缺省从 settings.pushplus_token 读取）
    :return: True=推送成功，False=推送失败或降级为控制台
    """
    if token is None:
        token = config.get_settings().pushplus_token

    if not token:
        _console_fallback(title, content)
        return False

    try:
        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": "markdown",
        }
        resp = requests.post(
            PUSHPLUS_API_URL,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 200:
            print(f"[notifier] PushPlus 推送成功: {title}")
            return True
        else:
            print(f"[notifier] PushPlus 返回异常: code={result.get('code')}, "
                  f"msg={result.get('msg', '未知错误')}")
            _console_fallback(title, content)
            return False
    except requests.RequestException as e:
        print(f"[notifier] PushPlus 推送失败（网络异常）: {e}")
        _console_fallback(title, content)
        return False
    except (ValueError, KeyError) as e:
        # 修正记录（P4）：resp.json() 解析失败时 requests 实际抛
        # RequestJSONDecodeError（继承 ValueError），仅捕获 json.JSONDecodeError
        # 会漏接导致推送崩溃；现统一按 ValueError 兜底降级控制台。
        print(f"[notifier] PushPlus 响应解析失败: {e}")
        _console_fallback(title, content)
        return False


def _console_fallback(title: str, content: str) -> None:
    """控制台降级输出（PushPlus 不可用时的兜底）"""
    print(f"\n{'=' * 60}")
    print(f"📢 {title}")
    print(f"{'=' * 60}")
    print(content)
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# 消息格式化：AI 分析报告
# ---------------------------------------------------------------------------
def format_ai_report(analysis_result) -> str:
    """将 AI 分析结果格式化为 Markdown 消息

    :param analysis_result: ai_layer.AnalysisResult（used_ai=True 时调用）
    :return: Markdown 格式的消息正文
    """
    analysis = analysis_result.analysis
    lines = [
        "# 📊 AQuant AI 分析报告",
        "",
        f"**AI 模型**: {analysis_result.used_model}",
        "",
        "## 市场概况",
        analysis.market_summary if analysis else "无数据",
        "",
        "## 候选股票建议",
    ]
    if analysis and analysis.stock_recommendations:
        for rec in analysis.stock_recommendations:
            action_emoji = {"买入": "🟢", "观望": "🟡", "回避": "🔴"}.get(rec.action, "⚪")
            lines.append(f"\n### {rec.code} — {action_emoji} {rec.action}")
            lines.append(f"- **信心**: {'⭐' * rec.confidence}")
            lines.append(f"- **理由**: {rec.reasoning}")
    else:
        lines.append("_无候选股票推荐_")

    if analysis and analysis.risk_warnings:
        lines.append("\n## ⚠️ 风险提示")
        for warning in analysis.risk_warnings:
            lines.append(f"- {warning}")

    lines.append("\n---\n_由 AQuant 量化系统生成_")
    return "\n".join(lines)


def format_degraded_report(analysis_result) -> str:
    """将降级信号格式化为 Markdown 消息

    :param analysis_result: ai_layer.AnalysisResult（used_ai=False 时调用）
    :return: Markdown 格式的消息正文
    """
    lines = [
        "# 📊 AQuant 纯量化信号（AI 降级模式）",
        "",
        analysis_result.degraded_signal or "无降级信号数据",
        "",
        "---",
        "_AI 层不可用，仅展示量化指标信号_",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 消息格式化：纯筛选报告（P4 策略调整：AI 层停用期间替代 AI 报告）
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    """数值格式化：None 显示 N/A，Decimal 去除冗余精度"""
    if v is None:
        return "N/A"
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def _fmt_pct(v) -> str:
    """比率格式化为百分比：0.35 → 35.0%，None 显示 N/A"""
    if v is None:
        return "N/A"
    return f"{float(v) * 100:.1f}%"


def format_screening_report(candidates, regime: str,
                            risk_flags: Dict[str, bool]) -> str:
    """将初筛结果格式化为 Markdown 消息（AI 层停用期间的推送正文）

    :param candidates: stock_screener.screen_tech_stocks 返回的全部候选列表
    :param regime: 市场状态中文名（MarketRegime.value）
    :param risk_flags: 风控五开关 {名称: 是否触发}
    :return: Markdown 格式的消息正文
    """
    passed = [c for c in candidates if c.passed]
    error_count = sum(1 for c in candidates if getattr(c, "error", None))
    triggered = [name for name, on in risk_flags.items() if on]

    lines = [
        "# 📊 AQuant 筛选结果报告",
        "",
        f"**市场状态**: {regime}",
        "",
        "## 筛选统计",
        f"- 评估: {len(candidates)} 只",
        f"- 通过初筛: {len(passed)} 只",
        f"- 数据异常: {error_count} 只",
        "",
        "## 风控信号",
        (", ".join(triggered) if triggered else "全部未触发"),
    ]

    lines.append("")
    lines.append("## 通过初筛的股票")
    if passed:
        for c in passed:
            lines.append(f"\n### {c.code}（{c.industry}）")
            lines.append(f"- **收盘价**: {_fmt(c.price)}")
            # P4 策略调整：指标明细按有值才显示——科技股显示 PE/技术面，
            # 金融股显示 PB 分位，避免满屏 N/A。
            if c.pe_percentile is not None:
                lines.append(f"- **PE 分位**: {_fmt_pct(c.pe_percentile)}")
            if getattr(c, "pb_percentile", None) is not None:
                lines.append(f"- **PB 分位**: {_fmt_pct(c.pb_percentile)}")
            if c.rsi is not None:
                lines.append(f"- **RSI14**: {_fmt(c.rsi)}")
            if c.max_drawdown is not None:
                lines.append(f"- **近60日最大回撤**: {_fmt_pct(c.max_drawdown)}")
            if c.rebound_from_low is not None:
                lines.append(f"- **距低点反弹**: {_fmt_pct(c.rebound_from_low)}")
    else:
        lines.append("_本次无股票通过初筛_")

    lines.append("\n---\n_由 AQuant 量化系统生成（AI 解读已临时停用）_")
    return "\n".join(lines)


def format_reduce_suggestions(pool: Dict[str, dict]) -> str:
    """将推荐池持仓格式化为减仓建议区块（熊市科技股清仓信号触发时追加）

    :param pool: recommendation_pool 载入的推荐池 {code: 入池记录}
    :return: Markdown 格式的减仓建议区块（调用方自行追加到报告末尾）
    """
    lines = [
        "## 📉 推荐池减仓建议（熊市 · 科技股清仓信号已触发）",
        "",
        "以下为此前初筛推荐过的股票，建议逢反弹分批减仓或清仓：",
    ]
    for code, info in pool.items():
        first_at = str(info.get("first_recommended_at", ""))[:10] or "未知"
        count = info.get("recommend_count", 1)
        price = info.get("price", "") or "N/A"
        lines.append(
            f"- **{code}**（{info.get('industry', '')}）"
            f"推荐价 {price} | 首次入池 {first_at} | 累计入池 {count} 次")
    return "\n".join(lines)


def format_error_alert(error_msg: str) -> str:
    """将异常告警格式化为 Markdown 消息

    :param error_msg: 异常信息
    :return: Markdown 格式的告警正文
    """
    lines = [
        "# ⚠️ AQuant 运行告警",
        "",
        "程序运行过程中出现异常，请检查：",
        "",
        "```",
        error_msg,
        "```",
        "",
        "---",
        "_由 AQuant 量化系统自动生成_",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令行自检：python src/notifier.py
# ---------------------------------------------------------------------------
def _self_check() -> None:
    """自检入口：验证消息格式化（不调用 PushPlus API）"""
    print("[OK] 通知层自检（验证格式化，不调用 API）")

    # 模拟 AI 结果
    from dataclasses import dataclass
    from typing import List

    @dataclass
    class FakeRec:
        code: str
        action: str
        confidence: int
        reasoning: str

    @dataclass
    class FakeAnalysis:
        stock_recommendations: list
        market_summary: str
        risk_warnings: List[str]

    @dataclass
    class FakeResult:
        analysis: object
        used_ai: bool
        used_model: str
        degraded_signal: str

    fake_analysis = FakeAnalysis(
        stock_recommendations=[
            FakeRec("000063", "买入", 4, "PE 分位 0.35，RSI 42.5，趋势确认"),
        ],
        market_summary="沪深300 震荡，MACD 金叉，RSI 52",
        risk_warnings=["强制减仓未触发", "注意回撤风险"],
    )
    fake_result = FakeResult(
        analysis=fake_analysis, used_ai=True,
        used_model="qwen3.8-max", degraded_signal="",
    )
    report = format_ai_report(fake_result)
    print(f"[OK] AI 报告格式化成功（{len(report)} 字符）")
    print(report[:300])

    # 降级信号
    fake_degraded = FakeResult(
        analysis=None, used_ai=False, used_model="",
        degraded_signal="# 纯量化信号\n**市场状态**: 低落横盘",
    )
    degraded = format_degraded_report(fake_degraded)
    print(f"\n[OK] 降级报告格式化成功（{len(degraded)} 字符）")


if __name__ == "__main__":
    _self_check()
