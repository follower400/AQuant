"""策略层单元测试（Layer 3 position_sizing.py）—— 全部离线构造数据，不访问网络

覆盖范围：科技股三批建仓触发与暂停/加速、底仓初始建仓与分批止盈、
恐慌加仓、三层仓位上限校验，以及非法输入边缘情况。
"""

from decimal import Decimal

import pandas as pd
import pytest

from src.position_sizing import (
    ACTION_BUY_1ST,
    ACTION_BUY_2ND,
    ACTION_BUY_3RD,
    ACTION_BUY_INIT,
    ACTION_BUY_PANIC,
    ACTION_NONE,
    ACTION_PAUSE,
    ACTION_SELL,
    apply_position_limits,
    decide_tech_batch,
    decide_value_position,
    plan_panic_add,
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


def uptrend_kline(n=30, step="0.1", start="10.0"):
    """单边匀速上涨 K 线（站上 MA20，RSI 趋近 100）"""
    return make_kline([Decimal(start) + Decimal(i) * Decimal(step) for i in range(n)])


# ---------------------------------------------------------------------------
# 科技股分批建仓
# ---------------------------------------------------------------------------
class TestDecideTechBatch:
    def test_first_batch_triggered(self):
        """站上 MA20 + 末日量能 >= 5日均量 x 1.2：首笔 15% x 30% = 4.5%"""
        kline = uptrend_kline()
        kline["volume"] = [1000.0] * 29 + [1600.0]
        plan = decide_tech_batch(kline, batch_done=0)
        assert plan.action == ACTION_BUY_1ST
        assert plan.ratio == Decimal("0.045")

    def test_first_batch_not_above_ma(self):
        """横盘收盘等于 MA20：不站上，不触发"""
        kline = make_kline([Decimal("10.0")] * 30)
        kline["volume"] = [1000.0] * 29 + [1600.0]
        plan = decide_tech_batch(kline, batch_done=0)
        assert plan.action == ACTION_NONE
        assert plan.ratio == 0

    def test_first_batch_volume_fail(self):
        """站上 MA20 但量能不达标：不触发"""
        plan = decide_tech_batch(uptrend_kline(), batch_done=0)
        assert plan.action == ACTION_NONE

    def test_second_batch_pullback_ma10(self):
        """盘中回踩 MA10 不破（low <= MA10 <= close）且 MACD 红柱放大：第二笔"""
        closes = [Decimal("10.0") + Decimal(i) * Decimal("0.3") for i in range(25)]
        closes.append(Decimal("17.8"))                        # 末日跳涨，dif 大升红柱放大
        kline = make_kline(closes,
                           lows=[c - Decimal("0.1") for c in closes[:-1]]
                                + [Decimal("15.5")],          # 盘中回踩 MA10
                           highs=[c + Decimal("0.1") for c in closes[:-1]]
                                 + [Decimal("18.0")],
                           opens=closes[:-1] + [Decimal("17.0")])
        plan = decide_tech_batch(kline, batch_done=1)
        assert plan.action == ACTION_BUY_2ND, f"实际: {plan.action} | {plan.reason}"
        assert plan.ratio == Decimal("0.045")

    def test_second_batch_pause_on_drop(self):
        """首笔后下跌超过 5%：暂停加仓"""
        plan = decide_tech_batch(make_kline([Decimal("10.0")] * 30), batch_done=1,
                                 entry_price=Decimal("11.0"))
        assert plan.action == ACTION_PAUSE
        assert plan.ratio == 0

    def test_second_batch_accelerate(self):
        """首笔后上涨超过 8%：跳过常规条件直接执行第二笔"""
        plan = decide_tech_batch(make_kline([Decimal("10.0")] * 30), batch_done=1,
                                 entry_price=Decimal("9.0"))
        assert plan.action == ACTION_BUY_2ND
        assert plan.ratio == Decimal("0.045")

    def test_third_batch_break_high(self):
        """收盘突破前 5 日最高（含 high 极值）且 RSI > 50：第三笔 15% x 40% = 6%"""
        closes = [Decimal("10.0") + Decimal(i) * Decimal("0.1") for i in range(29)]
        closes.append(Decimal("13.05"))                        # 末日跳涨突破前 5 日最高 high
        plan = decide_tech_batch(make_kline(closes), batch_done=2)
        assert plan.action == ACTION_BUY_3RD, f"实际: {plan.action} | {plan.reason}"
        assert plan.ratio == Decimal("0.060")

    def test_third_batch_no_break(self):
        """横盘未突破前高：不触发"""
        plan = decide_tech_batch(make_kline([Decimal("10.0")] * 30), batch_done=2)
        assert plan.action == ACTION_NONE

    def test_third_batch_accelerate(self):
        """首笔后上涨超过 8%：直接执行第三笔"""
        plan = decide_tech_batch(make_kline([Decimal("10.0")] * 30), batch_done=2,
                                 entry_price=Decimal("9.0"))
        assert plan.action == ACTION_BUY_3RD

    def test_all_batches_done(self):
        """三批已建完：不再产生任何动作"""
        plan = decide_tech_batch(uptrend_kline(), batch_done=3)
        assert plan.action == ACTION_NONE

    def test_invalid_inputs(self):
        with pytest.raises(ValueError):
            decide_tech_batch(pd.DataFrame(), batch_done=0)
        with pytest.raises(ValueError):
            decide_tech_batch(uptrend_kline(n=10), batch_done=-1)


# ---------------------------------------------------------------------------
# 金融/ETF 底仓决策
# ---------------------------------------------------------------------------
class TestDecideValuePosition:
    def test_init_stock_buy(self):
        """PB 分位 <= 20%：初始建仓 40% x 60% = 24%"""
        plan = decide_value_position("stock", Decimal("0.10"), Decimal("0.40"))
        assert plan.action == ACTION_BUY_INIT
        assert plan.ratio == Decimal("0.24")

    def test_init_stock_not_triggered(self):
        plan = decide_value_position("stock", Decimal("0.25"), Decimal("0.40"))
        assert plan.action == ACTION_NONE
        assert plan.ratio == 0

    def test_init_stock_percentile_none(self):
        """分位不可用：保守不建仓"""
        plan = decide_value_position("stock", None, Decimal("0.40"))
        assert plan.action == ACTION_NONE

    def test_init_etf_buy(self):
        """ETF PE 分位 <= 30%：初始建仓"""
        plan = decide_value_position("etf", Decimal("0.30"), Decimal("0.40"))
        assert plan.action == ACTION_BUY_INIT
        assert plan.ratio == Decimal("0.24")

    def test_take_profit(self):
        """已建仓且分位 >= 70%：分批止盈 40% x 50% = 20%"""
        plan = decide_value_position("stock", Decimal("0.75"), Decimal("0.40"),
                                     current_ratio=Decimal("0.24"))
        assert plan.action == ACTION_SELL
        assert plan.ratio == Decimal("0.20")

    def test_hold(self):
        """已建仓但分位未到止盈线：继续持有"""
        plan = decide_value_position("stock", Decimal("0.50"), Decimal("0.40"),
                                     current_ratio=Decimal("0.24"))
        assert plan.action == ACTION_NONE

    def test_invalid_kind(self):
        with pytest.raises(ValueError):
            decide_value_position("bond", Decimal("0.10"), Decimal("0.40"))

    def test_invalid_target(self):
        with pytest.raises(ValueError):
            decide_value_position("stock", Decimal("0.10"), Decimal("0"))


# ---------------------------------------------------------------------------
# 恐慌加仓
# ---------------------------------------------------------------------------
class TestPanicAdd:
    def test_ratio(self):
        """恐慌加仓 = 计划仓位 x 10%"""
        plan = plan_panic_add(Decimal("0.40"))
        assert plan.action == ACTION_BUY_PANIC
        assert plan.ratio == Decimal("0.04")


# ---------------------------------------------------------------------------
# 三层仓位上限校验
# ---------------------------------------------------------------------------
class TestApplyPositionLimits:
    def test_no_limit(self):
        allowed, note = apply_position_limits(Decimal("0.05"),
                                              current_single=Decimal("0.05"),
                                              current_tech_total=Decimal("0.30"))
        assert allowed == Decimal("0.05")
        assert "未触发" in note

    def test_single_stock_cap(self):
        """单票已 10%，请求 20%：只能再买 5%（15% 上限）"""
        allowed, note = apply_position_limits(Decimal("0.20"),
                                              current_single=Decimal("0.10"))
        assert allowed == Decimal("0.05")
        assert "单票上限" in note

    def test_tech_total_cap(self):
        """科技板块已 55%，请求 20%：只能再买 5%（60% 上限）"""
        allowed, note = apply_position_limits(Decimal("0.20"),
                                              current_tech_total=Decimal("0.55"))
        assert allowed == Decimal("0.05")
        assert "科技板块上限" in note

    def test_both_caps_take_min(self):
        """单票余 7%、板块余 2%：取更小值 2%"""
        allowed, note = apply_position_limits(Decimal("0.10"),
                                              current_single=Decimal("0.08"),
                                              current_tech_total=Decimal("0.58"))
        assert allowed == Decimal("0.02")
        assert "单票上限" in note and "科技板块上限" in note

    def test_value_total_cap(self):
        """底仓已 35%，请求 30%：只能再买 5%（40% 上限）"""
        allowed, note = apply_position_limits(Decimal("0.30"), is_tech=False,
                                              current_value_total=Decimal("0.35"))
        assert allowed == Decimal("0.05")
        assert "底仓合计上限" in note

    def test_value_no_limit(self):
        allowed, note = apply_position_limits(Decimal("0.02"), is_tech=False,
                                              current_value_total=Decimal("0.10"))
        assert allowed == Decimal("0.02")
        assert "未触发" in note
