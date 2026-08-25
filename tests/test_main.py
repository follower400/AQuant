"""主入口单元测试（main.py）—— 全部离线，mock 各层依赖验证编排逻辑

覆盖范围：股票池参数解析、dry-run / 正式推送两条编排路径、
指数拉取失败告警、行业成分股降级小池、纯筛选报告格式化、
推荐池入池/减仓建议分支（P4 策略调整：AI 层停用）。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

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
    value: object = None

    def __post_init__(self):
        import config as cfg
        if self.ai is None:
            self.ai = cfg._Section({"primary_model": "qwen3.8-max"})
        if self.value is None:
            self.value = cfg._Section({"financial_industries": ["银行", "非银金融"]})


@dataclass
class FakeCandidate:
    code: str = "000063"
    industry: str = "通信"
    passed: bool = True
    error: Optional[str] = None


def patch_dependencies(monkeypatch, *, kline_error=None, resolved_pool=None,
                       clear_tech=True, rec_pool=None):
    """批量 mock main.run 的全部外部依赖，返回调用捕获字典"""
    calls = {
        "screen_pool": None,      # screen_tech_stocks 收到的股票池
        "finance_pool": None,     # screen_finance_stocks 收到的金融股池（未切换为 None）
        "notifications": [],      # send_notification 的 (title, content)
        "closed": False,          # close_db 是否被调用
        "pool_updated": None,     # update_recommendation_pool 收到的候选（未调用为 None）
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

    # Layer 3 宏观判定与风控开关（clear_tech 控制科技股清仓是否触发）
    monkeypatch.setattr(main, "detect_regime",
                        lambda df: MarketRegime.BEAR)
    monkeypatch.setattr(main, "is_force_reduce_triggered", lambda df: False)
    monkeypatch.setattr(main, "is_half_position_triggered", lambda df: False)
    monkeypatch.setattr(main, "is_forbid_open", lambda df: False)
    monkeypatch.setattr(main, "is_clear_tech_triggered", lambda df: clear_tech)
    monkeypatch.setattr(main, "is_panic_add_triggered", lambda df: False)

    # Layer 3 选股池降级链（缺省返回模拟快照池）
    _pool = resolved_pool or ({"000063": "通信", "300750": "电力设备"},
                              "静态快照（2 只，抓取于 2026-08-23T09:00:00）")
    monkeypatch.setattr(main, "resolve_stock_pool", lambda: _pool)

    # Layer 3 选股（缺省两只均通过初筛）
    def fake_screen(pool):
        calls["screen_pool"] = pool
        return [FakeCandidate(), FakeCandidate(code="300750",
                                               industry="电力设备")]
    monkeypatch.setattr(main, "screen_tech_stocks", fake_screen)

    # Layer 3 金融股筛选（P4 策略调整：熊市切换分支）
    def fake_finance_screen(pool):
        calls["finance_pool"] = pool
        return [FakeCandidate(code="601398", industry="银行")]
    monkeypatch.setattr(main, "screen_finance_stocks", fake_finance_screen)

    # 健康检查
    monkeypatch.setattr(main, "check_data_health", lambda *a, **k: [])

    # 推荐池（P4 策略调整）
    monkeypatch.setattr(main, "load_recommendation_pool",
                        lambda: dict(rec_pool) if rec_pool else {})

    def fake_update(cands):
        calls["pool_updated"] = cands
        return {}
    monkeypatch.setattr(main, "update_recommendation_pool", fake_update)

    # Layer 5 格式化与推送（AI 层停用：只测筛选报告分支）
    monkeypatch.setattr(main, "format_screening_report",
                        lambda c, r, f: "SCREEN-CONTENT")
    monkeypatch.setattr(main, "format_reduce_suggestions",
                        lambda p: "REDUCE-CONTENT")
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
        calls = patch_dependencies(monkeypatch, clear_tech=False)
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
        assert content.startswith("SCREEN-CONTENT")
        assert "风控触发" in content and "科技股清仓" in content


# ---------------------------------------------------------------------------
# run 编排：正式推送路径（P4 策略调整：AI 层停用，只测筛选报告）
# ---------------------------------------------------------------------------
class TestRunPush:
    def test_screening_report_used(self, monkeypatch):
        """AI 层停用：标题含筛选结果，正文用 format_screening_report"""
        calls = patch_dependencies(monkeypatch)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        title, content = calls["notifications"][0]
        assert "筛选结果" in title
        assert content.startswith("SCREEN-CONTENT")


# ---------------------------------------------------------------------------
# run 编排：推荐池分支（P4 策略调整）
# ---------------------------------------------------------------------------
class TestRecommendationPoolBranch:
    def test_pool_updated_when_not_clear(self, monkeypatch):
        """非清仓：通过初筛的股票写入推荐池，报告无减仓建议"""
        calls = patch_dependencies(monkeypatch, clear_tech=False)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        assert calls["pool_updated"] is not None
        assert len(calls["pool_updated"]) == 2      # 两只 FakeCandidate 均通过
        _, content = calls["notifications"][0]
        assert "REDUCE-CONTENT" not in content

    def test_reduce_suggestions_when_clear_and_pool(self, monkeypatch):
        """清仓触发且推荐池非空：报告追加减仓建议且不入池"""
        pool = {"000063": {"industry": "通信", "price": "35.2",
                           "first_recommended_at": "2026-08-01T09:40:00",
                           "last_recommended_at": "2026-08-01T09:40:00",
                           "recommend_count": 1}}
        calls = patch_dependencies(monkeypatch, clear_tech=True,
                                   rec_pool=pool)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        assert calls["pool_updated"] is None        # 清仓时严禁入池
        _, content = calls["notifications"][0]
        assert "REDUCE-CONTENT" in content

    def test_clear_with_empty_pool_no_suggestions(self, monkeypatch):
        """清仓触发但推荐池为空：无减仓建议也不入池"""
        calls = patch_dependencies(monkeypatch, clear_tech=True)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        assert calls["pool_updated"] is None
        _, content = calls["notifications"][0]
        assert "REDUCE-CONTENT" not in content


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
            monkeypatch, clear_tech=False,
            resolved_pool=({"600519": "电子"}, "手工池（1 只）"))
        main.run(dry_run=True)                   # 不传手工池触发降级链
        assert calls["screen_pool"] == {"600519": "电子"}


# ---------------------------------------------------------------------------
# run 编排：熊市切换金融股筛选（P4 策略调整）
# ---------------------------------------------------------------------------
class TestBearFinanceSwitch:
    def test_clear_switches_to_finance_screening(self, monkeypatch):
        """清仓触发且池含金融股：切换金融股筛选，不再调科技股初筛"""
        calls = patch_dependencies(
            monkeypatch, clear_tech=True,
            resolved_pool=({"601398": "银行", "000063": "通信"}, "手工池（2 只）"))
        main.run(dry_run=False)
        assert calls["screen_pool"] is None       # 科技股初筛未执行
        assert calls["finance_pool"] == {"601398": "银行"}  # 仅金融股入筛
        assert len(calls["notifications"]) == 1

    def test_clear_without_finance_in_pool(self, monkeypatch):
        """清仓触发但池内无金融股：不崩溃，正常输出空筛选报告"""
        calls = patch_dependencies(
            monkeypatch, clear_tech=True,
            resolved_pool=({"000063": "通信"}, "手工池（1 只）"))
        main.run(dry_run=False)
        assert calls["screen_pool"] is None
        assert calls["finance_pool"] is None      # 无金融股可调筛选
        assert len(calls["notifications"]) == 1

    def test_not_clear_keeps_tech_screening(self, monkeypatch):
        """非清仓：保持科技股初筛，不切换金融股"""
        calls = patch_dependencies(monkeypatch, clear_tech=False)
        main.run(dry_run=False, stock_pool_str="000063:通信")
        assert calls["screen_pool"] == {"000063": "通信"}
        assert calls["finance_pool"] is None
