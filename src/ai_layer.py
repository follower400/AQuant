"""
ai_layer.py —— Layer 4 AI 解读层：提示词工程 + 分层代理池 + 结构化输出校验

职责边界（遵循 .coderule 与 PROJECT_PLAN.md Layer 4 规划）：
- 仅接收 Layer 3 筛选出的候选池 + 市场状态，调用大模型生成解读；
- 本层严禁直接推送消息（推送属 Layer 5）；
- 全部阈值来自 settings.yaml ai 节，严禁硬编码模型名称或超时参数；
- AI 全部失败时降级为纯量化信号（不阻塞主流程）。

核心设计（P3 六大决策）：
  1. 分层代理池：主力 qwen3.8-max → deepseek-v4-pro-0813 → kimi-k3 → 无 AI 降级
  2. Prompt 去幻觉：仅基于传入的结构化量化指标分析，禁止引入实时市场情绪
  3. 批量调用：5 只股票一次 API 调用（节省 ~47% Token 成本）
  4. Pydantic-AI 校验：output_type=StockAnalysis 自动 JSON 提取 + Schema 校验
  5. 配置开关：enable_ai_analysis=false 时跳过 AI 层（回测友好）
  6. 数据健康检查：空 DataFrame 提前拦截（防止 stock_screener 崩溃）

用法示例：
    from ai_layer import analyze
    result = analyze(candidates, regime, index_df)
    if result.used_ai:
        for s in result.stock_recommendations:
            print(s.code, s.action, s.confidence, s.reasoning)
    else:
        print(result.degraded_signal)  # 纯量化文本
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

# 项目根目录与 src 目录注册到 sys.path（修正记录 P2：原模板仅注册根目录）
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config


# ---------------------------------------------------------------------------
# Pydantic 输出模型（AI 必须按此结构返回，pydantic-ai 自动校验）
# ---------------------------------------------------------------------------
class StockRecommendation(BaseModel):
    """单只股票的 AI 解读结果"""
    code: str = Field(description="证券代码")
    action: str = Field(description="操作建议: 买入 / 观望 / 回避")
    confidence: int = Field(ge=1, le=5, description="信心评分 1-5 星")
    reasoning: str = Field(description="基于量化指标的理由（50-200字，必须引用具体数值）")


class StockAnalysis(BaseModel):
    """AI 批量分析结果（全部候选股票的统一解读）"""
    stock_recommendations: List[StockRecommendation] = Field(
        description="每只候选股票的操作建议")
    market_summary: str = Field(
        description="基于传入指标的市场概况（50-150字）")
    risk_warnings: List[str] = Field(
        default_factory=list,
        description="风险警告列表（每条对应具体触发条件）")


# ---------------------------------------------------------------------------
# Prompt 模板（去幻觉设计：仅基于结构化数据，禁止主观情绪判断）
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
你是一位严谨的 A 股量化分析师。请严格基于以下提供的结构化量化指标数据进行分析。

## 分析规则（必须严格遵守）
1. **仅使用提供的数据**：禁止引入任何未提供的信息（如实时新闻、政策变动、市场情绪、\
北向资金、板块轮动等），你无法获取实时信息。
2. **禁止主观臆测**：不要猜测未来走势，仅基于当前指标状态给出评估。
3. **理由必须引用数据**：每条 reasoning 必须包含具体的指标数值（如 "PE 分位 0.35"、\
"RSI 42.5"），不得使用模糊表述。
4. **操作建议标准**：
   - "买入"：全部核心指标（趋势/估值/量能）均处于有利区间
   - "观望"：部分指标有利但存在明显不确定性（如趋势未确认或估值偏高）
   - "回避"：多项指标处于不利区间（如估值过高、趋势向下、回撤过大）
5. **confidence 评分标准**：
   - 5：全部条件完美匹配，信号极强
   - 4：大部分条件满足，仅 minor 瑕疵
   - 3：条件部分满足，存在不确定性
   - 2：条件勉强满足，风险较大
   - 1：条件几乎不满足，仅为数据完整性保留

## 输出格式
严格按 JSON 结构输出，不要包含任何额外文字、解释或 Markdown 标记。"""


def _build_user_prompt(
    candidates_data: List[Dict],
    regime: str,
    risk_flags: Dict[str, bool],
) -> str:
    """构建用户提示词（批量包含全部候选股票的结构化数据）

    :param candidates_data: 每只股票的指标字典列表
    :param regime: 市场状态（BULL / BEAR / NEUTRAL）
    :param risk_flags: 风控触发标志
    """
    regime_map = {
        "BULL": "景气上行",
        "BEAR": "低落横盘",
        "NEUTRAL": "震荡/无明确信号",
    }
    lines = [
        f"## 当前市场状态: {regime_map.get(regime, regime)}",
        "",
        "## 风控信号",
    ]
    for flag, triggered in risk_flags.items():
        lines.append(f"- {flag}: {'已触发' if triggered else '未触发'}")
    lines.append("")
    lines.append("## 候选股票数据")
    for stock in candidates_data:
        lines.append(f"\n### {stock['code']}（{stock['industry']}）")
        lines.append(f"- 收盘价: {stock['price']}")
        lines.append(f"- PE 近5年分位: {stock.get('pe_percentile', 'N/A')}")
        lines.append(f"- RSI(14): {stock.get('rsi', 'N/A')}")
        lines.append(f"- 近60日最大回撤: {stock.get('max_drawdown', 'N/A')}")
        lines.append(f"- 年化波动率: {stock.get('volatility', 'N/A')}")
        lines.append(f"- MA5: {stock.get('ma5', 'N/A')}")
        lines.append(f"- MA10: {stock.get('ma10', 'N/A')}")
        lines.append(f"- MA20: {stock.get('ma20', 'N/A')}")
        lines.append(f"- MACD DIF: {stock.get('macd_dif', 'N/A')}")
        lines.append(f"- MACD DEA: {stock.get('macd_dea', 'N/A')}")
        lines.append(f"- MACD 柱: {stock.get('macd_hist', 'N/A')}")
        lines.append(f"- 距低点反弹: {stock.get('rebound_from_low', 'N/A')}")
        if stock.get("failures"):
            lines.append(f"- 未通过条件: {'; '.join(stock['failures'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 降级信号（AI 全部失败时的纯量化输出）
# ---------------------------------------------------------------------------
def _build_degraded_signal(
    candidates: list,
    regime: str,
    risk_flags: Dict[str, bool],
) -> str:
    """AI 不可用时，生成格式化纯量化信号（Markdown 文本）"""
    regime_map = {
        "BULL": "景气上行",
        "BEAR": "低落横盘",
        "NEUTRAL": "震荡/无明确信号",
    }
    lines = [
        "# 📊 纯量化信号（AI 降级模式）",
        "",
        f"**市场状态**: {regime_map.get(regime, regime)}",
        "",
        "## 风控信号",
    ]
    for flag, triggered in risk_flags.items():
        status = "⚠️ 已触发" if triggered else "✅ 未触发"
        lines.append(f"- {flag}: {status}")
    lines.append("")
    lines.append("## 候选股票")
    passed = [c for c in candidates if getattr(c, "passed", False)]
    if not passed:
        lines.append("_无通过初筛的股票_")
    for c in passed:
        lines.append(f"\n**{c.code}**（{c.industry}）")
        lines.append(f"- 收盘价: {c.price}")
        lines.append(f"- PE 分位: {c.pe_percentile}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 数据源健康检查（P3 新增：防止空 DataFrame 传播至下游崩溃）
# ---------------------------------------------------------------------------
def check_data_health(
    index_df: Optional[pd.DataFrame],
    candidates: Optional[list] = None,
) -> List[str]:
    """数据源健康检查：返回异常消息列表，空列表表示全部正常

    修正记录（P3）：P2 冒烟测试已修复非正价净化，但实盘推送时若某只股票因
    停牌或数据缺失导致 data_layer 返回空 DataFrame，stock_screener.evaluate_stock
    可能在索引越界处崩溃。本函数在 main.py 顶部调用，提前拦截。
    """
    issues: List[str] = []
    if index_df is None:
        issues.append("指数 K 线数据为 None（get_kline 调用失败）")
    elif not isinstance(index_df, pd.DataFrame):
        issues.append(f"指数数据类型异常: {type(index_df).__name__}")
    elif index_df.empty:
        issues.append("指数 K 线数据为空（可能非交易时段或数据源异常）")
    if candidates is not None:
        if len(candidates) == 0:
            issues.append("候选股票列表为空（行业成分股拉取失败或池为空）")
        else:
            error_count = sum(
                1 for c in candidates
                if getattr(c, "error", None) is not None
            )
            if error_count == len(candidates):
                issues.append(
                    f"全部 {error_count} 只候选股数据拉取失败，无法进行有效分析"
                )
    return issues


# ---------------------------------------------------------------------------
# 分层代理池构建
# ---------------------------------------------------------------------------
def _build_agents() -> list:
    """按配置构建分层代理池：[(model_name, Agent), ...]

    使用 pydantic-ai v2 的 OpenAIProvider + OpenAIChatModel，
    通过 settings.yaml 的 dashscope_base_url（DASHSCOPE_BASE_URL）连接百炼平台。
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    settings = config.get_settings()
    ai_cfg = settings.ai
    base_url = settings.dashscope_base_url
    api_key = settings.dashscope_api_key

    if not api_key:
        raise RuntimeError(
            "DASHSCOPE_API_KEY 未配置，无法调用 AI 模型。"
            "请在 .env 中设置 DASHSCOPE_API_KEY，或在 settings.yaml 中将 "
            "ai.enable_ai_analysis 设为 false 以跳过 AI 层。"
        )

    # 构建 provider（OpenAI 兼容接口）
    provider_kwargs: Dict = {"api_key": api_key}
    if base_url:
        provider_kwargs["base_url"] = base_url
    provider = OpenAIProvider(**provider_kwargs)

    # 模型列表：主力 + 备用
    model_names = [ai_cfg.primary_model] + list(ai_cfg.fallback_models)
    agents = []
    for model_name in model_names:
        model = OpenAIChatModel(
            model_name=model_name,
            provider=provider,
        )
        agent = Agent(
            model=model,
            output_type=StockAnalysis,
            system_prompt=_SYSTEM_PROMPT,
        )
        agents.append((model_name, agent))
    return agents


# ---------------------------------------------------------------------------
# 分层调用（主力 → 备用 → 降级）
# ---------------------------------------------------------------------------
def _call_tiered(
    agents: list,
    prompt: str,
    max_retries: int,
    timeout: int,
) -> tuple:
    """分层调用代理池：逐模型尝试，每个模型最多重试 max_retries 次

    :return: (StockAnalysis | None, used_model: str, errors: list)
             成功时返回 (result, model_name, [])；全部失败时返回 (None, "", errors)
    """
    errors: List[str] = []
    for model_name, agent in agents:
        for attempt in range(1, max_retries + 1):
            try:
                result = agent.run_sync(
                    prompt,
                    # 修正记录（P4）：qwen3.8-max 等思考模型在 thinking mode 下不支持
                    # pydantic-ai 的 tool_choice=required（结构化输出依赖），需显式禁用。
                    model_settings={
                        "timeout": timeout,
                        "extra_body": {"enable_thinking": False},
                    },
                )
                return result.output, model_name, []
            except Exception as e:
                err_msg = f"[{model_name}] 第 {attempt}/{max_retries} 次失败: {e}"
                errors.append(err_msg)
                print(f"[ai_layer] {err_msg}")
        print(f"[ai_layer] 模型 {model_name} 全部重试失败，切换下一个备用模型")
    return None, "", errors


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
@dataclass
class AnalysisResult:
    """AI 分析结果（含降级标记）"""
    analysis: Optional[StockAnalysis]   # AI 成功时为 StockAnalysis，否则为 None
    used_ai: bool                       # 是否成功使用了 AI
    used_model: str                     # 实际使用的模型名称
    degraded_signal: str                # 降级时的纯量化文本
    candidates: list = field(default_factory=list)
    regime: str = ""
    errors: List[str] = field(default_factory=list)


def analyze(
    candidates: list,
    regime: str,
    index_df: Optional[pd.DataFrame] = None,
    risk_flags: Optional[Dict[str, bool]] = None,
) -> AnalysisResult:
    """AI 解读主入口：构建 Prompt → 分层调用 → 校验 → 降级

    :param candidates: Layer 3 输出的 StockCandidate 列表
    :param regime: 市场状态字符串（BULL / BEAR / NEUTRAL）
    :param index_df: 指数 K 线（用于健康检查，可为 None）
    :param risk_flags: 风控触发标志字典
    :return: AnalysisResult（含 AI 结果或降级信号）
    """
    settings = config.get_settings()

    # 1) 配置开关：AI 关闭时直接返回降级信号
    if not settings.enable_ai_analysis:
        return AnalysisResult(
            analysis=None, used_ai=False, used_model="",
            degraded_signal=_build_degraded_signal(
                candidates, regime, risk_flags or {}),
            candidates=candidates, regime=regime,
            errors=["AI 分析已关闭（enable_ai_analysis=false）"],
        )

    # 2) 数据健康检查
    if risk_flags is None:
        risk_flags = {}
    issues = check_data_health(index_df, candidates)
    if issues:
        return AnalysisResult(
            analysis=None, used_ai=False, used_model="",
            degraded_signal=(
                "数据源异常，跳过 AI 分析:\n" + "\n".join(f"- {i}" for i in issues)
            ),
            candidates=candidates, regime=regime,
            errors=issues,
        )

    # 3) 构建候选数据（仅包含通过初筛且有数据的候选股）
    # 修正记录（P4）：原 stock_dict 仅含 code/industry/price/pe_percentile，
    # Prompt 模板期望的均线/RSI/MACD 等字段全部输出 N/A，导致 AI 误报「指标缺失」。
    # 现从 StockCandidate 指标快照（evaluate_stock 回填）取真实数值。
    # 策略调整（P4）：原实现把全部候选（含未通过初筛者）发给 AI，熊市 0 只通过时
    # 仍对 1400+ 只股票批量调用（数百批 × 多模型 × 重试）纯属浪费 token；
    # 现仅送 passed=True 的股票，0 只通过时直接短路返回（0 次 API 调用）。
    def _fmt_field(cand, name: str) -> str:
        """指标字段格式化：非空转字符串，缺失/属性不存在时输出 N/A"""
        val = getattr(cand, name, None)
        return "N/A" if val is None else str(val)

    candidates_data = []
    for c in candidates:
        if getattr(c, "error", None) is not None:
            continue  # 跳过数据拉取失败的股票
        if not getattr(c, "passed", False):
            continue  # 跳过未通过初筛的股票（策略调整：AI 只评价通过筛选的股票）
        stock_dict: Dict = {
            "code": c.code,
            "industry": c.industry,
            "price": _fmt_field(c, "price"),
            "pe_percentile": _fmt_field(c, "pe_percentile"),
            "rsi": _fmt_field(c, "rsi"),
            "max_drawdown": _fmt_field(c, "max_drawdown"),
            "volatility": _fmt_field(c, "volatility"),
            "ma5": _fmt_field(c, "ma5"),
            "ma10": _fmt_field(c, "ma10"),
            "ma20": _fmt_field(c, "ma20"),
            "macd_dif": _fmt_field(c, "macd_dif"),
            "macd_dea": _fmt_field(c, "macd_dea"),
            "macd_hist": _fmt_field(c, "macd_hist"),
            "rebound_from_low": _fmt_field(c, "rebound_from_low"),
            "failures": getattr(c, "failures", []),
        }
        candidates_data.append(stock_dict)

    if not candidates_data:
        # 策略调整（P4）：0 只通过初筛时直接降级返回，不构建代理池、不调 API
        return AnalysisResult(
            analysis=None, used_ai=False, used_model="",
            degraded_signal=_build_degraded_signal(
                candidates, regime, risk_flags),
            candidates=candidates, regime=regime,
            errors=["无通过初筛的候选股票，跳过 AI 分析（节省 token）"],
        )

    # 4) 分层代理池构建（一次性构建，分批复用）
    try:
        agents = _build_agents()
    except RuntimeError as e:
        return AnalysisResult(
            analysis=None, used_ai=False, used_model="",
            degraded_signal=_build_degraded_signal(
                candidates, regime, risk_flags),
            candidates=candidates, regime=regime,
            errors=[str(e)],
        )

    # 5) 分批调用（P4 新增：max_batch_size 控制每批股票数，避免单次调用超时）
    #    修正记录（P4）：原实现将全部候选股一次性发给 AI，12 只股票时 Prompt 过长，
    #    qwen3.8-max 15s 超时无法完成。现按 max_batch_size 分批，逐批调用后合并结果。
    ai_cfg = settings.ai
    batch_size = ai_cfg.max_batch_size
    batches = [candidates_data[i:i + batch_size]
               for i in range(0, len(candidates_data), batch_size)]

    all_recommendations: List = []
    all_risk_warnings: List[str] = []
    market_summaries: List[str] = []
    used_models: set = set()
    all_errors: List[str] = []
    any_success = False

    for batch_idx, batch in enumerate(batches):
        user_prompt = _build_user_prompt(batch, regime, risk_flags)
        result, used_model, errors = _call_tiered(
            agents, user_prompt,
            max_retries=ai_cfg.max_retries_per_model,
            timeout=ai_cfg.timeout_seconds,
        )
        if result is not None:
            any_success = True
            used_models.add(used_model)
            all_recommendations.extend(result.stock_recommendations)
            all_risk_warnings.extend(result.risk_warnings)
            market_summaries.append(result.market_summary)
            print(f"[ai_layer] 批次 {batch_idx + 1}/{len(batches)} 成功"
                  f"（模型: {used_model}，{len(result.stock_recommendations)} 只股票）")
        else:
            all_errors.extend(errors)
            print(f"[ai_layer] 批次 {batch_idx + 1}/{len(batches)} 全部模型失败")

    # 6) 合并结果：任一批次成功即返回 AI 结果（部分失败时仅包含成功批次的推荐）
    if any_success:
        merged = StockAnalysis(
            stock_recommendations=all_recommendations,
            market_summary=" | ".join(market_summaries),
            risk_warnings=list(dict.fromkeys(all_risk_warnings)),  # 去重保序
        )
        return AnalysisResult(
            analysis=merged, used_ai=True,
            used_model=", ".join(sorted(used_models)),
            degraded_signal="",
            candidates=candidates, regime=regime,
            errors=all_errors,  # 保留失败批次的错误信息（供日志参考）
        )

    # 7) 全部批次失败 → 降级为纯量化信号
    print(f"[ai_layer] 全部批次失败，降级为纯量化信号。错误: {all_errors}")
    return AnalysisResult(
        analysis=None, used_ai=False, used_model="",
        degraded_signal=_build_degraded_signal(
            candidates, regime, risk_flags),
        candidates=candidates, regime=regime,
        errors=all_errors,
    )


# ---------------------------------------------------------------------------
# 命令行自检：python src/ai_layer.py
# ---------------------------------------------------------------------------
def _self_check() -> None:
    """自检入口：验证 Prompt 构建与降级逻辑（不调用真实 API）"""
    print("[OK] AI 层自检（构造数据，不调用 API）")

    # 构造候选数据
    candidates_data = [
        {
            "code": "000001", "industry": "电子",
            "price": "12.50", "pe_percentile": "0.35",
            "rsi": "42.5", "max_drawdown": "0.35",
            "volatility": "0.40",
            "ma5": "12.30", "ma10": "12.10", "ma20": "11.80",
            "macd_dif": "0.15", "macd_dea": "0.10", "macd_hist": "0.05",
            "failures": [],
        },
    ]
    prompt = _build_user_prompt(
        candidates_data, "NEUTRAL",
        {"强制减仓": False, "科技股减半": False},
    )
    print(f"[OK] Prompt 构建成功（{len(prompt)} 字符）")
    print(f"--- Prompt 预览 ---\n{prompt[:500]}...")

    # 降级信号
    signal = _build_degraded_signal([], "BEAR", {"强制减仓": True})
    print(f"\n[OK] 降级信号:\n{signal}")


if __name__ == "__main__":
    _self_check()
