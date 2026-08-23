"""主入口单元测试（main.py）—— 全部离线，mock 各层依赖验证编排逻辑

覆盖范围：股票池参数解析、dry-run / 正式推送两条编排路径、
指数拉取失败告警、行业成分股降级小池、AI 降级格式化分支。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import pandas as pd

import main
from src.data_layer import DataFetchError
from src.market_regime import MarketRegime


# ---------------------------------------------------------------------------
# 辅助：Fake 对象
# ---------------------------------------------------------------------------
@dataclass
class FakeSettings:
    """config.get_settings() 的替身（仅含 main.run 用到的字段）"""
    simulated_capital: Decimal = Decimal("1000000")
    enable_ai_analysis: bool = True
    pushplus_token: str = "test-token"
    ai: object = None

    def __post_init__(self):
        if self.ai is None:
            import config as cfg
            self.ai = cfg._Section({"primary_model": "qwen3.8-max"})


@dataclass
class FakeCandidate:
    code: str = "000063"
    industry: str = "通信"
    passed: bool = True
    error: Optional[str] = None


@dataclass
class FakeRec:
    code: str
    action: str
    confidence: int


@dataclass
class FakeAnalysis:
    stock_recommendations: list = field(default_factory=list)
    market_summary: str = ""
    risk_warnings: List[str] = field(default_factory=list)


@dataclass
class FakeAnalysisResult:
    used_ai: bool = True
    analysis: object = None
    used_model: str = "qwen3.8-max"
    degraded_signal: str = ""
    errors: List[str] = field(default_factory=list)


def patch_dependencies(monkeypatch, *, kline_error=None, used_ai=True,
                       resolved_pool=None):
    """批量 mock main.run 的全部外部依赖，返回调用捕获字典"""
    calls = {
        "screen_pool": None,      # screen_tech_stocks 收到的股票池
        "notifications": [],      # send_notification 的 (title, content)
        "closed": False,          # close_db 是否被调用
    }

    # Layer 0 配置
    import config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())

    # Layer 1 数据（需支持 len() 与 ['close'].iloc[-1]，run 会打印日志）
    if kline_error:
        def fake_get_kline(*args, **kwargs):
            raise DataFetchError(kline_error)
        monkeypatch.setattr(main, "get_kline", fake_get_kline)
    else:
        fake_df = pd.DataFrame({
            "close": [Decimal("4500")],
            "trade_date": ["2026-08-21"],
        })
        monkeypatch.setattr(main, "get_kline", lambda *a, **k: fake_df)

    # Layer 3 宏观判定与风控开关
    monkeypatch.setattr(main, "detect_regime",
                        lambda df: MarketRegime.BEAR)
    monkeypatch.setattr(main, "is_force_reduce_triggered", lambda df: False)
    monkeypatch.setattr(main, "is_half_position_triggered", lambda df: False)
    monkeypatch.setattr(main, "is_forbid_open", lambda df: False)
    monkeypatch.setattr(main, "is_clear_tech_triggered", lambda df: True)
    monkeypatch.setattr(main, "is_panic_add_triggered", lambda df: False)

    # Layer 3 选股池降级链（缺省返回模拟快照池）
    _pool = resolved_pool or ({"000063": "通信", "300750": "电力设备"},
                              "静态快照（2 只，抓取于 2026-08-23T09:00:00）")
    monkeypatch.setattr(main, "resolve_stock_pool", lambda: _pool)

    # Layer 3 选股
    def fake_screen(pool):
        calls["screen_pool"] = pool
        return [FakeCandidate(), FakeCandidate(code="300750",
                                               industry="电力设备")]
    monkeypatch.setattr(main, "screen_tech_stocks", fake_screen)

    # 健康检查与 Layer 4
    monkeypatch.setattr(main, "check_data_health", lambda *a, **k: [])
    if used_ai:
        result = FakeAnalysisResult(
            used_ai=True,
            analysis=FakeAnalysis(
                stock_recommendations=[FakeRec("000063", "回避", 2)]),
        )
    else:
        result = FakeAnalysisResult(used_ai=False, analysis=None,
                                    degraded_signal="纯量化降级信号",
                                    errors=["模型全部失败"])
    monkeypatch.setattr(main, "analyze", lambda **k: result)

    # Layer 5 格式化与推送
    monkeypatch.setattr(main, "format_ai_report", lambda r: "AI-CONTENT")
    monkeypatch.setattr(main, "format_degraded_report",
                        lambda r: "DEGRADED-CONTENT")
    monkeypatch.setattr(main, "format_error_alert", lambda m: f"ALERT:{m}")

    def fake_send(title, content, token=None):
        calls["notifications"].append((title, content))
        return True
    monkeypatch.setattr(main, "send_notification", fake_send)

    # 收尾
    def fake_close():
        calls["closed"] = True
    monkeypatch.setattr(main, "close_db", fake_close)

    return calls


# ---------------------------------------------------------------------------
# 股票池参数解析
# ---------------------------------------------------------------------------
class TestParseStockPool:
    def test_normal(self):
        pool = main._parse_stock_pool("000063:通信,300750:电力设备")
        assert pool == {"000063": "通信", "300750": "电力设备"}

    def test_invalid_items_skipped(self):
        """缺少冒号的非法项应跳过（打印提示）而非抛错"""
        pool = main._parse_stock_pool("000063:通信,bad-item,300750:电力设备")
        assert pool == {"000063": "通信", "300750": "电力设备"}

    def test_whitespace_trimmed(self):
        pool = main._parse_stock_pool(" 000063 : 通信 , 300750:电力设备 ")
        assert pool == {"000063": "通信", "300750": "电力设备"}

    def test_industry_with_colon(self):
        """行业名含冒号时只按首个冒号切分"""
        pool = main._parse_stock_pool("000063:通信:子行业")
        assert pool == {"000063": "通信:子行业"}

    def test_empty(self):
        assert main._parse_stock_pool("") == {}


# ---------------------------------------------------------------------------
# run 编排：dry-run 路径
# ---------------------------------------------------------------------------
class TestRunDryRun:
    def test_manual_pool_and_no_push(self, monkeypatch):
        """手工池传入初筛、dry-run 不推送、正常收尾关闭连接"""
        calls = patch_dependencies(monkeypatch)
        main.run(dry_run=True, stock_pool_str="000063:通信,300750:电力设备")
        assert calls["screen_pool"] == {"000063": "通信",
                                        "300750": "电力设备"}
        assert calls["notifications"] == []   # dry-run 严禁推送
        assert calls["closed"] is True

    def test_risk_flag_appended_to_content(self, monkeypatch):
        """风控触发时内容末尾应追加触发清单（dry-run 路径仅打印，
        改用正式推送路径捕获内容验证）"""
        calls = patch_dependencies(monkeypatch)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        assert len(calls["notifications"]) == 1
        title, content = calls["notifications"][0]
        assert "BEAR" in title
        assert content.startswith("AI-CONTENT")
        assert "风控触发" in content and "科技股清仓" in content


# ---------------------------------------------------------------------------
# run 编排：正式推送路径
# ---------------------------------------------------------------------------
class TestRunPush:
    def test_ai_success_uses_ai_report(self, monkeypatch):
        """AI 成功时标题含 AI 分析且正文用 format_ai_report"""
        calls = patch_dependencies(monkeypatch, used_ai=True)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        title, content = calls["notifications"][0]
        assert "AI 分析" in title
        assert content.startswith("AI-CONTENT")

    def test_ai_degraded_uses_degraded_report(self, monkeypatch):
        """AI 降级时标题含纯量化且正文用 format_degraded_report"""
        calls = patch_dependencies(monkeypatch, used_ai=False)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        title, content = calls["notifications"][0]
        assert "纯量化" in title
        assert content.startswith("DEGRADED-CONTENT")


# ---------------------------------------------------------------------------
# run 编排：异常与降级路径
# ---------------------------------------------------------------------------
class TestRunFailurePaths:
    def test_index_fetch_failure_alerts_and_stops(self, monkeypatch):
        """指数 K 线拉取失败：推送告警后立即返回（不进入选股）"""
        calls = patch_dependencies(monkeypatch, kline_error="网络超时")
        main.run(dry_run=False, stock_pool_str="000063:通信")
        assert calls["screen_pool"] is None      # 未进入选股
        assert len(calls["notifications"]) == 1
        title, content = calls["notifications"][0]
        assert "告警" in title
        assert "网络超时" in content

    def test_pool_from_resolve_chain(self, monkeypatch):
        """未传手工池时应使用降级链（resolve_stock_pool）返回的池"""
        calls = patch_dependencies(
            monkeypatch,
            resolved_pool=({"600519": "电子"}, "手工池（1 只）"))
        main.run(dry_run=True)                   # 不传手工池触发降级链
        assert calls["screen_pool"] == {"600519": "电子"}
