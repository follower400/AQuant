"""策略层单元测试（Layer 3 market_regime.py）—— 全部离线构造数据，不访问网络

覆盖范围：景气/低落/震荡判定、量能缺失降级、强制减仓（连阴+跌幅双重条件）、
科技股减半、禁止开仓、清仓线、恐慌加仓，以及边缘情况（全涨/全跌/样本不足）。
"""

from decimal import Decimal

import pandas as pd
import pytest

from src.market_regime import (
    MarketRegime,
    detect_regime,
    is_clear_tech_triggered,
    is_forbid_open,
    is_force_reduce_triggered,
    is_half_position_triggered,
    is_panic_add_triggered,
    prepare_index,
)


def make_index_df(closes, volumes=None):
    """构造指数 K 线：open/high/low 由 close 派生，日期用 date_range 连续生成"""
    closes_d = [c if isinstance(c, Decimal) else Decimal(str(c)) for c in closes]
    dates = [d.strftime("%Y-%m-%d")
             for d in pd.date_range("2025-01-01", periods=len(closes_d))]
    df = pd.DataFrame({
        "trade_date": dates,
        "open": closes_d,
        "high": [c + Decimal("0.5") for c in closes_d],
        "low": [c - Decimal("0.5") for c in closes_d],
        "close": closes_d,
    })
    if volumes is not None:
        df["volume"] = volumes
    return df


# ---------------------------------------------------------------------------
# 市场状态判定
# ---------------------------------------------------------------------------
class TestDetectRegime:
    def test_bull_uptrend(self):
        """单边上涨 80 日 + 量能递增：满足 BULL 全部四项条件"""
        closes = [Decimal(100) + Decimal(i) for i in range(80)]  # 100 -> 179
        volumes = [float(1000 + i * 10) for i in range(80)]      # 末日均量确认
        df = make_index_df(closes, volumes)
        assert detect_regime(df) == MarketRegime.BULL

    def test_bear_below_ma_with_mid_rsi(self):
        """价格在 MA60 下方 + RSI 落入 30~50 区间：BEAR"""
        # 前 60 日 100 -> 80 温和下跌，后 20 日 跌0.5/涨0.4 交替（RSI 约 44）
        down = [Decimal(100) - Decimal(20) * Decimal(i) / Decimal(59)
                for i in range(60)]
        closes = list(down)
        cur = closes[-1]
        for k in range(20):
            cur = cur - Decimal("0.5") if k % 2 == 0 else cur + Decimal("0.4")
            closes.append(cur)
        assert detect_regime(make_index_df(closes)) == MarketRegime.BEAR

    def test_neutral_mixed(self):
        """价格低于 MA60 但 RSI 高于 50（不满足 BULL 也不满足 BEAR）：NEUTRAL"""
        closes = [Decimal(100) - Decimal(40) * Decimal(i) / Decimal(39)
                  for i in range(40)]                                   # 100 -> 60
        closes += [closes[-1] + Decimal("0.2") * (i + 1)
                   for i in range(30)]                                  # 60 -> 66（全涨 RSI=100）
        assert detect_regime(make_index_df(closes)) == MarketRegime.NEUTRAL

    def test_volume_missing_no_bull(self):
        """成交量列缺失时量能确认保守不通过，即使其余条件满足也不判 BULL"""
        closes = [Decimal(100) + Decimal(i) for i in range(80)]
        df = make_index_df(closes)  # 无 volume 列
        assert detect_regime(df) != MarketRegime.BULL

    def test_prepare_index_idempotent(self):
        """prepare_index 幂等：连续调用两次不报错且指标列一致"""
        df = make_index_df([Decimal(100) + Decimal(i) for i in range(80)])
        cols1 = list(prepare_index(df).columns)
        cols2 = list(prepare_index(df).columns)
        assert cols1 == cols2


# ---------------------------------------------------------------------------
# 强制减仓（连阴 + 累计跌幅双重条件）
# ---------------------------------------------------------------------------
class TestForceReduce:
    def test_triggered_consecutive_and_drop(self):
        """连续 5 日收阴且累计跌 6%：触发"""
        df = make_index_df([100, 99, 98, 97, 96, 94])
        assert is_force_reduce_triggered(df)

    def test_not_triggered_by_drop_only(self):
        """连阴但累计跌幅不足 3%：不触发"""
        df = make_index_df([100, 99.9, 99.8, 99.7, 99.6, 99.5])
        assert not is_force_reduce_triggered(df)

    def test_not_triggered_by_consecutive_only(self):
        """跌幅 6% 但中间有反弹（非连阴）：不触发"""
        df = make_index_df([100, 99, 100.5, 98, 97, 96, 94])
        assert not is_force_reduce_triggered(df)

    def test_not_triggered_insufficient_samples(self):
        """样本不足（少于 6 行）：保守不触发"""
        df = make_index_df([100, 99, 98])
        assert not is_force_reduce_triggered(df)


# ---------------------------------------------------------------------------
# 科技股减半（连续 N 日下跌）
# ---------------------------------------------------------------------------
class TestHalfPosition:
    def test_triggered_five_days_down(self):
        """连续 5 日下跌：触发"""
        df = make_index_df([100, 99, 98, 97, 96, 95])
        assert is_half_position_triggered(df)

    def test_not_triggered_with_rebound(self):
        """中间有反弹：不触发"""
        df = make_index_df([100, 99, 100.5, 98, 97, 96])
        assert not is_half_position_triggered(df)

    def test_not_triggered_insufficient(self):
        """样本不足：不触发"""
        df = make_index_df([100, 99, 98])
        assert not is_half_position_triggered(df)


# ---------------------------------------------------------------------------
# 禁止开仓（单日涨超 3% 且 RSI >= 70）
# ---------------------------------------------------------------------------
class TestForbidOpen:
    def test_triggered_rush_up(self):
        """单日涨 4% 且全涨行情 RSI=100：触发"""
        closes = [Decimal(100) + Decimal(i) for i in range(19)]      # 100 -> 118
        closes.append(closes[-1] * Decimal("1.04"))                  # 单日 +4%
        assert is_forbid_open(make_index_df(closes))

    def test_not_triggered_by_rise_only(self):
        """单日涨 4% 但前期下跌 RSI 低：不触发（两条件必须同时满足）"""
        closes = [Decimal(100) - Decimal(i) for i in range(14)]      # 下跌段
        closes.append(closes[-1] * Decimal("1.04"))                  # 单日 +4%
        assert not is_forbid_open(make_index_df(closes))

    def test_not_triggered_by_rsi_only(self):
        """RSI=100 但单日仅涨 1%：不触发"""
        closes = [Decimal(100) + Decimal(i) for i in range(20)]      # 每天 +1%
        assert not is_forbid_open(make_index_df(closes))


# ---------------------------------------------------------------------------
# 科技股清仓（跌破 MA120）
# ---------------------------------------------------------------------------
class TestClearTech:
    def test_triggered_break_below_ma120(self):
        """前 120 日横盘 100，后 10 日跌破至 90：触发"""
        closes = [Decimal(100)] * 120 + [Decimal(100) - Decimal(i + 1)
                                         for i in range(10)]
        assert is_clear_tech_triggered(make_index_df(closes))

    def test_not_triggered_above_ma120(self):
        """全横盘 100：不触发"""
        closes = [Decimal(100)] * 130
        assert not is_clear_tech_triggered(make_index_df(closes))


# ---------------------------------------------------------------------------
# 恐慌加仓（单日跌超 3% 或连续 3 日下跌）
# ---------------------------------------------------------------------------
class TestPanicAdd:
    def test_triggered_by_one_day_crash(self):
        """单日跌 3.5%：触发"""
        closes = [Decimal(100)] * 15 + [Decimal("96.5")]
        assert is_panic_add_triggered(make_index_df(closes))

    def test_triggered_by_three_days_down(self):
        """连续 3 日下跌（单日不足 3%）：触发"""
        closes = [Decimal(100)] * 10 + [Decimal("99.9"), Decimal("99.8"), Decimal("99.7")]
        assert is_panic_add_triggered(make_index_df(closes))

    def test_not_triggered_normal(self):
        """横盘行情：不触发"""
        closes = [Decimal(100)] * 15
        assert not is_panic_add_triggered(make_index_df(closes))


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
class TestValidation:
    def test_invalid_inputs(self):
        with pytest.raises(TypeError):
            detect_regime([1, 2, 3])
        with pytest.raises(ValueError):
            detect_regime(pd.DataFrame())
        with pytest.raises(ValueError):
            is_force_reduce_triggered(pd.DataFrame({"x": [1, 2]}))
