"""策略层单元测试（Layer 3 stock_screener.py）—— 全部离线构造数据，不访问网络

覆盖范围：价格区间、行业白名单、超跌底部形态（60 日回撤 + 距低点反弹）、
趋势确认（MA 多头排列）、涨停股性验证、波动率区间、RSI 区间、PE 分位条件，
以及空 K 线 / 缺估值 / 非白名单行业等未通过路径。
"""

from decimal import Decimal

import pandas as pd

from src.stock_screener import (
    _limit_up_threshold,
    evaluate_stock,
    evaluate_financial_stock,
)


def make_kline(closes, lows=None, highs=None, opens=None, volumes=None):
    """构造个股 K 线：日期用工作日序列，open/high/low 缺省由 close 派生"""
    closes_d = [c if isinstance(c, Decimal) else Decimal(str(c)) for c in closes]
    dates = [d.strftime("%Y-%m-%d")
             for d in pd.date_range("2025-09-01", periods=len(closes_d), freq="B")]
    df = pd.DataFrame({
        "trade_date": dates,
        "open": closes_d if opens is None else opens,
        "high": [c + Decimal("0.1") for c in closes_d] if highs is None else highs,
        "low": [c - Decimal("0.1") for c in closes_d] if lows is None else lows,
        "close": closes_d,
        "volume": [1000.0] * len(closes_d) if volumes is None else volumes,
    })
    return df


def make_val_df(pe_values):
    """构造估值历史 DataFrame（trade_date 升序 + pe 列）"""
    dates = [d.strftime("%Y-%m-%d")
             for d in pd.date_range("2024-01-01", periods=len(pe_values))]
    return pd.DataFrame({
        "trade_date": dates,
        "pe": [v if isinstance(v, Decimal) else Decimal(str(v)) for v in pe_values],
    })


def make_pb_val_df(pb_values):
    """构造估值历史 DataFrame（trade_date 升序 + pb 列，金融股筛选用）"""
    dates = [d.strftime("%Y-%m-%d")
             for d in pd.date_range("2024-01-01", periods=len(pb_values))]
    return pd.DataFrame({
        "trade_date": dates,
        "pb": [v if isinstance(v, Decimal) else Decimal(str(v)) for v in pb_values],
    })


def make_full_pass_kline():
    """构造满足 PRD 2.1 全部条件的 K 线（数值经手工演算，见下）

    结构（130 个工作日）：
      - day 0~74   横盘 10 元
      - day 75     涨停 +10% -> 11.0（近 3 个月内股性验证）
      - day 76~105 11.0 线性跌至 7.0（60 日窗口内回撤 36.4% >= 30%）
      - day 106~129 +3.5% / -3% 交替 12 轮（距低点反弹约 4.9% <= 15%，
        同时使 MA5>MA10>MA20、RSI14 约 54 落入 30~55、年化波动率约 28%）
    """
    closes = [Decimal("10.0")] * 75
    closes.append(Decimal("11.0"))
    for i in range(30):
        closes.append(Decimal("11.0") - Decimal(i + 1) * Decimal("4") / Decimal("30"))
    cur = closes[-1]
    for _ in range(12):
        cur = cur * Decimal("1.035")
        closes.append(cur)
        cur = cur * Decimal("0.97")
        closes.append(cur)
    return make_kline(closes)


def make_low_pe_val():
    """构造末值处于历史低分位的 PE 序列（20 递减至 8.3，末值分位为 0）"""
    return make_val_df([Decimal(20) - Decimal(i) * Decimal("0.3") for i in range(40)])


# ---------------------------------------------------------------------------
# 涨停阈值（按板块）
# ---------------------------------------------------------------------------
class TestLimitUpThreshold:
    def test_main_board(self):
        assert _limit_up_threshold("000001") == Decimal("0.095")

    def test_chinext(self):
        assert _limit_up_threshold("300001") == Decimal("0.195")

    def test_star_market(self):
        assert _limit_up_threshold("688001") == Decimal("0.195")


# ---------------------------------------------------------------------------
# 单票评估
# ---------------------------------------------------------------------------
class TestEvaluateStock:
    def test_full_pass(self):
        """满足全部初筛条件的构造股：passed=True 且无失败项"""
        kline = make_full_pass_kline()
        cand = evaluate_stock(kline, "000001", "电子", make_low_pe_val())
        assert cand.passed, f"预期通过，实际失败项: {cand.failures}"
        assert cand.price == kline["close"].iloc[-1]
        assert cand.pe_percentile is not None and cand.pe_percentile <= Decimal("0.5")

    def test_kline_none(self):
        cand = evaluate_stock(None, "000001", "电子")
        assert not cand.passed
        assert "K 线数据为空" in cand.failures

    def test_empty_kline(self):
        cand = evaluate_stock(pd.DataFrame(), "000001", "电子")
        assert not cand.passed
        assert "K 线数据为空" in cand.failures

    def test_price_out_of_range(self):
        kline = make_kline([Decimal("25.0")] * 40)
        cand = evaluate_stock(kline, "000001", "电子")
        assert any("价格" in f for f in cand.failures)

    def test_industry_not_whitelisted(self):
        cand = evaluate_stock(make_full_pass_kline(), "000001", "银行",
                              make_low_pe_val())
        assert not cand.passed
        assert any("白名单" in f for f in cand.failures)

    def test_pe_missing_passes_when_optional(self):
        """P4 策略调整：可选模式（settings.yaml 默认）下估值数据缺失时跳过 PE 条件，
        其余条件全满足则通过（修正记录：原"缺失即不通过"导致熊市初筛全军覆没）"""
        cand = evaluate_stock(make_full_pass_kline(), "000001", "电子", None)
        assert cand.passed, f"预期通过，实际失败项: {cand.failures}"
        assert not any("PE" in f for f in cand.failures)

    def test_pe_missing_passes_when_optional_empty_df(self):
        """估值 DataFrame 为空时同样按可选模式跳过 PE 条件"""
        cand = evaluate_stock(make_full_pass_kline(), "000001", "电子",
                              pd.DataFrame())
        assert cand.passed, f"预期通过，实际失败项: {cand.failures}"

    def test_pe_missing_fails_when_not_optional(self, monkeypatch):
        """可选开关关闭时，估值数据缺失仍判不通过（保留原行为的回归验证）"""
        import config as cfg
        monkeypatch.setattr(cfg.get_settings().tech,
                            "pe_percentile_optional", False)
        cand = evaluate_stock(make_full_pass_kline(), "000001", "电子", None)
        assert not cand.passed
        assert any("PE" in f for f in cand.failures)

    def test_drawdown_condition_fails(self):
        """单边温和上涨无回撤：超跌条件不通过"""
        kline = make_kline([Decimal("5.0") + Decimal(i) * Decimal("0.05")
                            for i in range(80)])
        cand = evaluate_stock(kline, "000001", "电子", make_low_pe_val())
        assert any("回撤" in f for f in cand.failures)

    def test_trend_condition_fails(self):
        """长期下跌无多头排列：趋势条件不通过"""
        kline = make_kline([Decimal("15.0") - Decimal(i) * Decimal("0.1")
                            for i in range(40)])
        cand = evaluate_stock(kline, "000001", "电子", make_low_pe_val())
        assert any("趋势" in f for f in cand.failures)

    def test_pe_condition_fails_high_percentile(self):
        """PE 末值处于历史高分位：PE 分位条件不通过"""
        val = make_val_df([Decimal("5.0") + Decimal(i) * Decimal("0.3")
                           for i in range(40)])  # 5 -> 16.7 递增，末值分位为 1
        cand = evaluate_stock(make_full_pass_kline(), "000001", "电子", val)
        assert any("PE" in f for f in cand.failures)

    def test_indicator_snapshot_filled(self):
        """指标快照应回填真实数值（P4 修正：供 Layer 4 AI 解读，严禁全部为 None）"""
        kline = make_full_pass_kline()
        cand = evaluate_stock(kline, "000001", "电子", make_low_pe_val())
        for field_name in ("ma5", "ma10", "ma20", "rsi",
                           "macd_dif", "macd_dea", "macd_hist",
                           "max_drawdown", "volatility", "rebound_from_low"):
            assert getattr(cand, field_name) is not None, f"{field_name} 未回填"
        # MA 多头排列与初筛结论一致（full pass 场景）
        assert cand.ma5 > cand.ma10 > cand.ma20


# ---------------------------------------------------------------------------
# 金融股简化版评估（P4 策略调整：熊市切换筛选）
# ---------------------------------------------------------------------------
class TestEvaluateFinancialStock:
    def test_low_pb_passes(self):
        """PB 末值处历史低分位：通过初筛且回填 pb_percentile"""
        kline = make_kline([Decimal("6.0")] * 40)
        pb_val = make_pb_val_df(
            [Decimal("2.0") - Decimal(i) * Decimal("0.03") for i in range(40)])
        cand = evaluate_financial_stock(kline, "601398", "银行", pb_val)
        assert cand.passed, f"预期通过，实际失败项: {cand.failures}"
        assert cand.pb_percentile is not None
        assert cand.pb_percentile <= Decimal("0.30")
        assert cand.price == Decimal("6.0")

    def test_high_pb_fails(self):
        """PB 末值处历史高分位：不通过"""
        kline = make_kline([Decimal("6.0")] * 40)
        pb_val = make_pb_val_df(
            [Decimal("0.5") + Decimal(i) * Decimal("0.05") for i in range(40)])
        cand = evaluate_financial_stock(kline, "601398", "银行", pb_val)
        assert not cand.passed
        assert any("PB" in f for f in cand.failures)

    def test_pb_missing_passes_when_optional(self):
        """可选模式（默认）：估值缺失时跳过 PB 条件"""
        kline = make_kline([Decimal("6.0")] * 40)
        cand = evaluate_financial_stock(kline, "601398", "银行", None)
        assert cand.passed, f"预期通过，实际失败项: {cand.failures}"
        assert cand.pb_percentile is None

    def test_pb_missing_fails_when_not_optional(self, monkeypatch):
        """可选开关关闭：估值缺失仍判不通过"""
        import config as cfg
        monkeypatch.setattr(cfg.get_settings().value,
                            "pb_percentile_optional", False)
        kline = make_kline([Decimal("6.0")] * 40)
        cand = evaluate_financial_stock(kline, "601398", "银行", None)
        assert not cand.passed
        assert any("PB" in f for f in cand.failures)

    def test_kline_none(self):
        cand = evaluate_financial_stock(None, "601398", "银行")
        assert not cand.passed
        assert "K 线数据为空" in cand.failures

    def test_indicator_snapshot_defaults_none(self):
        """K 线为空时指标快照应为 None（不抛异常）"""
        cand = evaluate_stock(None, "000001", "电子")
        assert cand.ma5 is None
        assert cand.rsi is None
        assert cand.rebound_from_low is None
