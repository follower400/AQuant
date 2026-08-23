"""AI 解读层单元测试（Layer 4 ai_layer.py）—— 全部离线，不调用真实 API

覆盖范围：Pydantic 输出模型校验、Prompt 构建、降级信号生成、
数据健康检查、analyze 配置开关路径、分层调用失败降级。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

import pandas as pd
import pytest

import src.ai_layer as ai


# ---------------------------------------------------------------------------
# 辅助：模拟 StockCandidate（与 stock_screener.StockCandidate 接口兼容）
# ---------------------------------------------------------------------------
@dataclass
class FakeCandidate:
    code: str
    industry: str
    price: Optional[Decimal]
    pe_percentile: Optional[Decimal]
    passed: bool
    failures: List[str] = None
    error: Optional[str] = None
    # 指标快照字段（P4 新增：与 StockCandidate 保持一致，缺省 None）
    ma5: Optional[Decimal] = None
    ma10: Optional[Decimal] = None
    ma20: Optional[Decimal] = None
    rsi: Optional[Decimal] = None
    macd_dif: Optional[Decimal] = None
    macd_dea: Optional[Decimal] = None
    macd_hist: Optional[Decimal] = None
    max_drawdown: Optional[Decimal] = None
    volatility: Optional[Decimal] = None
    rebound_from_low: Optional[Decimal] = None

    def __post_init__(self):
        if self.failures is None:
            self.failures = []


def _make_candidate(**kwargs) -> FakeCandidate:
    defaults = dict(
        code="000001", industry="电子", price=Decimal("12.50"),
        pe_percentile=Decimal("0.35"), passed=True, failures=[],
    )
    defaults.update(kwargs)
    return FakeCandidate(**defaults)


# ---------------------------------------------------------------------------
# Pydantic 输出模型
# ---------------------------------------------------------------------------
class TestPydanticModels:
    def test_stock_recommendation_valid(self):
        """合法输入应成功构建 StockRecommendation"""
        rec = ai.StockRecommendation(
            code="000001", action="买入", confidence=4,
            reasoning="PE 分位 0.35 处于低位，RSI 42.5 中性偏强",
        )
        assert rec.code == "000001"
        assert rec.action == "买入"
        assert rec.confidence == 4

    def test_confidence_out_of_range(self):
        """confidence 超出 1-5 范围应触发 Pydantic 校验错误"""
        with pytest.raises(Exception):
            ai.StockRecommendation(
                code="000001", action="买入", confidence=6,
                reasoning="test",
            )

    def test_confidence_zero_rejected(self):
        """confidence=0 应被拒绝（ge=1）"""
        with pytest.raises(Exception):
            ai.StockRecommendation(
                code="000001", action="买入", confidence=0,
                reasoning="test",
            )

    def test_stock_analysis_with_empty_warnings(self):
        """risk_warnings 默认为空列表（Field default_factory）"""
        analysis = ai.StockAnalysis(
            stock_recommendations=[],
            market_summary="市场震荡",
        )
        assert analysis.risk_warnings == []

    def test_stock_analysis_full(self):
        """完整构建 StockAnalysis 应保留全部字段"""
        rec = ai.StockRecommendation(
            code="000001", action="观望", confidence=3, reasoning="指标中性",
        )
        analysis = ai.StockAnalysis(
            stock_recommendations=[rec],
            market_summary="震荡市",
            risk_warnings=["RSI 偏高", "量能不足"],
        )
        assert len(analysis.stock_recommendations) == 1
        assert len(analysis.risk_warnings) == 2


# ---------------------------------------------------------------------------
# Prompt 构建
# ---------------------------------------------------------------------------
class TestPromptBuilding:
    def test_build_user_prompt_contains_regime(self):
        """Prompt 应包含市场状态中文描述"""
        prompt = ai._build_user_prompt([], "BULL", {})
        assert "景气上行" in prompt

    def test_build_user_prompt_contains_stock_data(self):
        """Prompt 应包含候选股票的结构化指标"""
        data = [{
            "code": "000001", "industry": "电子",
            "price": "12.50", "pe_percentile": "0.35",
            "rsi": "42.5", "max_drawdown": "0.35",
            "volatility": "0.40",
            "ma5": "12.30", "ma10": "12.10", "ma20": "11.80",
            "macd_dif": "0.15", "macd_dea": "0.10", "macd_hist": "0.05",
            "failures": [],
        }]
        prompt = ai._build_user_prompt(data, "NEUTRAL", {})
        assert "000001" in prompt
        assert "12.50" in prompt
        assert "0.35" in prompt

    def test_build_user_prompt_risk_flags(self):
        """风控信号应在 Prompt 中标注触发状态"""
        prompt = ai._build_user_prompt(
            [], "BEAR",
            {"强制减仓": True, "科技股减半": False},
        )
        assert "已触发" in prompt
        assert "未触发" in prompt

    def test_build_user_prompt_failures_included(self):
        """未通过条件应包含在 Prompt 中"""
        data = [{
            "code": "000002", "industry": "计算机",
            "price": "8.00", "pe_percentile": "0.60",
            "failures": ["PE 分位 0.60 > 0.50", "RSI 不在区间"],
        }]
        prompt = ai._build_user_prompt(data, "NEUTRAL", {})
        assert "PE 分位 0.60 > 0.50" in prompt

    def test_system_prompt_no_hallucination(self):
        """系统提示词应包含去幻觉规则"""
        assert "禁止引入" in ai._SYSTEM_PROMPT
        assert "仅使用提供的数据" in ai._SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Prompt 指标传值（P4 修正：验证候选快照数值真正进入 Prompt）
# ---------------------------------------------------------------------------
class TestIndicatorPrompt:
    def test_prompt_contains_indicator_values(self, monkeypatch):
        """Prompt 应包含候选股指标快照的真实数值（而非全部 N/A）"""
        import config as cfg
        fake_ai = cfg._Section({
            "enable_ai_analysis": True,
            "primary_model": "qwen3.8-max",
            "fallback_models": [],
            "max_retries_per_model": 1,
            "timeout_seconds": 15,
            "max_tokens": 2000,
            "max_batch_size": 5,
            "temperature": 0.3,
        })

        class FakeSettings:
            enable_ai_analysis = True
            ai = fake_ai
            dashscope_api_key = "test-key"
            dashscope_base_url = "https://test.com/v1"

        monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())

        captured = {}

        class CaptureAgent:
            def run_sync(self, prompt, model_settings=None):
                captured["prompt"] = prompt

                class R:
                    output = ai.StockAnalysis(
                        stock_recommendations=[], market_summary="ok")
                return R()

        monkeypatch.setattr(ai, "_build_agents",
                            lambda: [("model-a", CaptureAgent())])

        df = pd.DataFrame({
            "close": [Decimal("100")] * 10,
            "trade_date": [f"2026-01-{i+1:02d}" for i in range(10)],
        })
        c = _make_candidate(ma5=Decimal("12.30"), macd_dif=Decimal("0.15"),
                            rsi=Decimal("42.5"))
        result = ai.analyze([c], "BULL", index_df=df)
        assert result.used_ai is True
        prompt = captured["prompt"]
        assert "12.30" in prompt          # MA5 数值传入
        assert "0.15" in prompt           # MACD DIF 数值传入
        assert "42.5" in prompt           # RSI 数值传入
        assert "N/A" in prompt            # 未提供的字段（如波动率）仍为 N/A，向后兼容


# ---------------------------------------------------------------------------
# 降级信号
# ---------------------------------------------------------------------------
class TestDegradedSignal:
    def test_degraded_signal_empty_candidates(self):
        """无候选股时降级信号应包含「无通过初筛」"""
        signal = ai._build_degraded_signal([], "BEAR", {"强制减仓": True})
        assert "纯量化信号" in signal
        assert "低落横盘" in signal
        assert "无通过初筛" in signal

    def test_degraded_signal_with_passed_candidates(self):
        """有通过初筛的候选股时降级信号应列出"""
        c = _make_candidate(code="000001", price=Decimal("12.50"),
                            pe_percentile=Decimal("0.35"), passed=True)
        signal = ai._build_degraded_signal([c], "BULL", {})
        assert "000001" in signal
        assert "12.50" in signal

    def test_degraded_signal_failed_candidates_not_listed(self):
        """未通过初筛的候选股不应出现在降级信号列表中"""
        c = _make_candidate(code="000002", passed=False)
        signal = ai._build_degraded_signal([c], "NEUTRAL", {})
        assert "无通过初筛" in signal


# ---------------------------------------------------------------------------
# 数据健康检查
# ---------------------------------------------------------------------------
class TestDataHealthCheck:
    def test_none_index_df(self):
        """指数数据为 None 应报告异常"""
        issues = ai.check_data_health(None)
        assert len(issues) == 1
        assert "None" in issues[0]

    def test_empty_index_df(self):
        """指数数据为空 DataFrame 应报告异常"""
        issues = ai.check_data_health(pd.DataFrame())
        assert len(issues) == 1
        assert "为空" in issues[0]

    def test_valid_index_df(self):
        """合法指数数据应无异常"""
        df = pd.DataFrame({
            "close": [Decimal("100")] * 10,
            "trade_date": [f"2026-01-{i+1:02d}" for i in range(10)],
        })
        issues = ai.check_data_health(df)
        assert issues == []

    def test_wrong_type_index_df(self):
        """非 DataFrame 类型应报告异常"""
        issues = ai.check_data_health("not-a-dataframe")
        assert len(issues) == 1
        assert "类型异常" in issues[0]

    def test_empty_candidates_list(self):
        """候选列表为空应报告异常"""
        df = pd.DataFrame({"close": [1], "trade_date": ["2026-01-01"]})
        issues = ai.check_data_health(df, candidates=[])
        assert len(issues) == 1
        assert "为空" in issues[0]

    def test_all_candidates_have_errors(self):
        """全部候选股都有 error 应报告异常"""
        df = pd.DataFrame({"close": [1], "trade_date": ["2026-01-01"]})
        c = _make_candidate(error="K 线拉取失败")
        issues = ai.check_data_health(df, candidates=[c])
        assert len(issues) == 1
        assert "全部" in issues[0]

    def test_some_candidates_have_errors_ok(self):
        """部分候选股有 error 但非全部时不应报告异常"""
        df = pd.DataFrame({"close": [1], "trade_date": ["2026-01-01"]})
        c1 = _make_candidate(error="K 线拉取失败")
        c2 = _make_candidate(code="000002", passed=True)
        issues = ai.check_data_health(df, candidates=[c1, c2])
        assert issues == []


# ---------------------------------------------------------------------------
# analyze 主入口（配置开关 + 降级路径）
# ---------------------------------------------------------------------------
class TestAnalyze:
    def test_ai_disabled_returns_degraded(self, monkeypatch):
        """enable_ai_analysis=false 时应直接返回降级信号，不调用 API"""
        import config as cfg
        # Monkeypatch settings to disable AI
        fake_ai = cfg._Section({
            "enable_ai_analysis": False,
            "primary_model": "qwen3.8-max",
            "fallback_models": [],
            "max_retries_per_model": 2,
            "timeout_seconds": 15,
            "max_tokens": 2000,
            "temperature": 0.3,
        })

        class FakeSettings:
            enable_ai_analysis = False
            ai = fake_ai
            dashscope_api_key = "test-key"
            dashscope_base_url = "https://test.com/v1"

        monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())

        c = _make_candidate()
        result = ai.analyze([c], "BULL")
        assert result.used_ai is False
        assert "AI 分析已关闭" in result.errors[0]
        assert result.degraded_signal  # 降级信号非空

    def test_data_health_failure(self, monkeypatch):
        """数据健康检查失败时应返回降级信号"""
        import config as cfg
        fake_ai = cfg._Section({
            "enable_ai_analysis": True,
            "primary_model": "qwen3.8-max",
            "fallback_models": [],
            "max_retries_per_model": 2,
            "timeout_seconds": 15,
            "max_tokens": 2000,
            "temperature": 0.3,
        })

        class FakeSettings:
            enable_ai_analysis = True
            ai = fake_ai
            dashscope_api_key = "test-key"
            dashscope_base_url = "https://test.com/v1"

        monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())

        # None index_df should trigger health check failure
        result = ai.analyze([], "NEUTRAL", index_df=None)
        assert result.used_ai is False
        assert any("None" in e for e in result.errors)

    def test_no_api_key_returns_degraded(self, monkeypatch):
        """API Key 未配置时应返回降级信号而非崩溃"""
        import config as cfg
        fake_ai = cfg._Section({
            "enable_ai_analysis": True,
            "primary_model": "qwen3.8-max",
            "fallback_models": [],
            "max_retries_per_model": 2,
            "timeout_seconds": 15,
            "max_tokens": 2000,
            "temperature": 0.3,
        })

        class FakeSettings:
            enable_ai_analysis = True
            ai = fake_ai
            dashscope_api_key = None  # 未配置
            dashscope_base_url = "https://test.com/v1"

        monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())

        df = pd.DataFrame({
            "close": [Decimal("100")] * 10,
            "trade_date": [f"2026-01-{i+1:02d}" for i in range(10)],
        })
        c = _make_candidate()
        result = ai.analyze([c], "BULL", index_df=df)
        assert result.used_ai is False
        assert any("DASHSCOPE_API_KEY" in e for e in result.errors)

    def test_all_candidates_have_error_skips_ai(self, monkeypatch):
        """全部候选股数据拉取失败时应跳过 AI 调用"""
        import config as cfg
        fake_ai = cfg._Section({
            "enable_ai_analysis": True,
            "primary_model": "qwen3.8-max",
            "fallback_models": [],
            "max_retries_per_model": 2,
            "timeout_seconds": 15,
            "max_tokens": 2000,
            "temperature": 0.3,
        })

        class FakeSettings:
            enable_ai_analysis = True
            ai = fake_ai
            dashscope_api_key = "test-key"
            dashscope_base_url = "https://test.com/v1"

        monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())

        df = pd.DataFrame({
            "close": [Decimal("100")] * 10,
            "trade_date": [f"2026-01-{i+1:02d}" for i in range(10)],
        })
        c = _make_candidate(error="K 线拉取失败")
        result = ai.analyze([c], "NEUTRAL", index_df=df)
        assert result.used_ai is False
        # 健康检查先于 Prompt 构建拦截：信号含「数据源异常」
        assert "数据源异常" in result.degraded_signal


# ---------------------------------------------------------------------------
# 分层调用（mock agent 验证降级逻辑）
# ---------------------------------------------------------------------------
class TestTieredCall:
    def test_all_agents_fail_returns_none(self):
        """全部 agent 抛异常时应返回 (None, '', errors)"""
        def failing_agent(*args, **kwargs):
            raise RuntimeError("模拟 API 超时")

        class FakeAgent:
            def run_sync(self, prompt, model_settings=None):
                raise RuntimeError("模拟 API 超时")

        agents = [("model-a", FakeAgent()), ("model-b", FakeAgent())]
        result, model, errors = ai._call_tiered(agents, "test prompt", 1, 10)
        assert result is None
        assert model == ""
        assert len(errors) == 2  # 每个模型重试 1 次，共 2 次失败

    def test_first_agent_succeeds(self):
        """第一个 agent 成功时应立即返回结果"""
        expected = ai.StockAnalysis(
            stock_recommendations=[],
            market_summary="测试",
        )

        class SuccessAgent:
            def run_sync(self, prompt, model_settings=None):
                class R:
                    output = expected
                return R()

        agents = [("model-a", SuccessAgent())]
        result, model, errors = ai._call_tiered(agents, "test", 1, 10)
        assert result is expected
        assert model == "model-a"
        assert errors == []

    def test_first_fails_second_succeeds(self):
        """第一个 agent 失败后应切换到第二个"""
        expected = ai.StockAnalysis(
            stock_recommendations=[],
            market_summary="备用模型结果",
        )

        class FailAgent:
            def run_sync(self, prompt, model_settings=None):
                raise RuntimeError("限流")

        class SuccessAgent:
            def run_sync(self, prompt, model_settings=None):
                class R:
                    output = expected
                return R()

        agents = [("model-a", FailAgent()), ("model-b", SuccessAgent())]
        result, model, errors = ai._call_tiered(agents, "test", 1, 10)
        assert result is expected
        assert model == "model-b"
