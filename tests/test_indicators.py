"""因子计算层单元测试（Layer 2 indicators.py）

覆盖范围：MA / EMA / MACD / RSI / 年化波动率 / 最大回撤 / add_all_indicators。
测试用例均采用可手工验证的已知序列，断言全部基于 Decimal 精确比较。
"""

from decimal import Decimal

import pandas as pd
import pytest

from src.indicators import (
    TRADING_DAYS_PER_YEAR,
    add_all_indicators,
    annualized_volatility,
    ema,
    ma,
    macd,
    max_drawdown,
    rsi,
    valuation_percentile,
)


# ---------------------------------------------------------------------------
# 测试数据辅助
# ---------------------------------------------------------------------------
def make_df(prices):
    """构造价格列全为 Decimal 的 DataFrame"""
    return pd.DataFrame({"close": [Decimal(str(p)) for p in prices]})


def seq(start, stop, step=1):
    """生成 [start, start+step, ..., stop] 的 Decimal 序列（含端点）"""
    return [Decimal(i) for i in range(start, stop + step, step)]


# ---------------------------------------------------------------------------
# MA
# ---------------------------------------------------------------------------
class TestMA:
    def test_ma_basic_values(self):
        """价格 1..10，MA(3) 末 8 个值应为 2..9"""
        df = make_df(seq(1, 10))
        ma(df, period=3)
        assert df["ma3"].tolist()[2:] == [Decimal(i) for i in range(2, 10)]

    def test_ma_leading_none(self):
        """前 period-1 个位置无有效值（应为 None）"""
        df = make_df(seq(1, 10))
        ma(df, period=3)
        assert df["ma3"].tolist()[:2] == [None, None]

    def test_ma_default_period_from_settings(self):
        """不传 period 时读取 settings.yaml（ma_period=60）"""
        df = make_df(seq(1, 80))
        ma(df)
        assert "ma60" in df.columns
        assert df["ma60"].iloc[-1] == Decimal("50.5")  # (21+...+80)/60

    def test_ma_float_input_converted(self):
        """float 输入自动转为 Decimal，且结果精确"""
        df = pd.DataFrame({"close": [1.1, 2.2, 3.3]})
        ma(df, period=2)
        assert df["ma2"].iloc[-1] == Decimal("2.75")

    def test_ma_validation(self):
        """非法输入必须抛错：非 DataFrame / 缺列 / 周期非法"""
        with pytest.raises(TypeError):
            ma([1, 2, 3], period=3)
        with pytest.raises(ValueError):
            ma(make_df(seq(1, 10)), period=0)
        with pytest.raises(ValueError):
            ma(pd.DataFrame({"x": [1, 2]}), period=3)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------
class TestEMA:
    def test_ema_first_value_equals_first_price(self):
        """EMA 初始值取首个价格"""
        df = make_df(seq(1, 10))
        ema(df, period=5)
        assert df["ema5"].iloc[0] == Decimal(1)

    def test_ema_bounded_by_prices(self):
        """EMA 应始终落在价格区间内"""
        df = make_df(seq(1, 30))
        ema(df, period=5)
        values = df["ema5"]
        assert all(Decimal(1) <= v <= Decimal(30) for v in values)

    def test_ema_approaches_price(self):
        """价格趋稳后 EMA 应逼近最新价格（误差 < 1e-6）"""
        prices = [Decimal(10)] * 50 + [Decimal(20)] * 50
        df = pd.DataFrame({"close": prices})
        ema(df, period=5)
        assert abs(df["ema5"].iloc[-1] - Decimal(20)) < Decimal("1e-6")


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
class TestMACD:
    def test_macd_columns(self):
        """输出列齐全且尾部无缺失"""
        df = make_df(seq(1, 40))
        macd(df)
        assert {"macd_dif", "macd_dea", "macd_hist"} <= set(df.columns)
        assert all(v is not None for v in df["macd_hist"][20:])

    def test_macd_hist_positive_in_uptrend(self):
        """单调上涨序列：DIF > DEA，红柱为正"""
        df = make_df(seq(1, 40))
        macd(df)
        assert df["macd_hist"].iloc[-1] > 0

    def test_macd_hist_negative_in_downtrend(self):
        """单调下跌序列：DIF < DEA，绿柱为负"""
        df = make_df(seq(40, 1, -1))
        macd(df)
        assert df["macd_hist"].iloc[-1] < 0

    def test_macd_parameter_validation(self):
        """fast 必须小于 slow"""
        df = make_df(seq(1, 40))
        with pytest.raises(ValueError):
            macd(df, fast=26, slow=12)


# ---------------------------------------------------------------------------
# RSI（Wilder 平滑）
# ---------------------------------------------------------------------------
class TestRSI:
    def test_rsi_all_up_is_100(self):
        """全涨序列 RSI=100（avg_loss=0）"""
        df = make_df(seq(1, 16))
        rsi(df, period=5)
        assert df["rsi5"].iloc[-1] == Decimal(100)

    def test_rsi_all_down_is_0(self):
        """全跌序列 RSI=0（avg_gain=0）"""
        df = make_df(seq(15, 7, -1))
        rsi(df, period=5)
        assert df["rsi5"].iloc[-1] == Decimal(0)

    def test_rsi_manual_case(self):
        """手工用例 [10,12,14,12] period=2：平滑后 avg_gain=avg_loss=1，RSI=50"""
        df = make_df([10, 12, 14, 12])
        rsi(df, period=2)
        assert df["rsi2"].iloc[-1] == Decimal(50)

    def test_rsi_leading_none(self):
        """前 period 个位置无有效值（需 period+1 个变化量）"""
        df = make_df(seq(1, 10))
        rsi(df, period=5)
        assert df["rsi5"].tolist()[:5] == [None] * 5
        assert df["rsi5"].iloc[-1] is not None

    def test_rsi_default_period_from_settings(self):
        """不传 period 时读取 settings.yaml（rsi_period=14）"""
        df = make_df(seq(1, 30))
        rsi(df)
        assert "rsi14" in df.columns


# ---------------------------------------------------------------------------
# 年化波动率
# ---------------------------------------------------------------------------
class TestVolatility:
    def test_constant_series_zero(self):
        """常数序列波动率为 0"""
        df = pd.DataFrame({"close": [Decimal("100")] * 10})
        annualized_volatility(df)
        assert df["volatility"].iloc[-1] == Decimal(0)

    def test_manual_case(self):
        """价格 [100,110,99]：收益率 [0.1,-0.1]，std=sqrt(0.02)，年化乘 sqrt(252)"""
        df = make_df([100, 110, 99])
        annualized_volatility(df)
        expected = Decimal("0.02").sqrt() * Decimal(TRADING_DAYS_PER_YEAR).sqrt()
        assert abs(df["volatility"].iloc[-1] - expected) < Decimal("1e-10")

    def test_window_filtering(self):
        """窗口收窄后波动率只依赖窗口内样本（短窗口值不应为 None）"""
        df = make_df([100, 110, 99, 108, 95])
        annualized_volatility(df, window=3)
        assert df["volatility"].iloc[-1] is not None
        assert df["volatility"].iloc[1] is None  # 窗口内不足 2 个样本

    def test_non_positive_prices_skipped(self):
        """含 0 价/负价脏数据（真实前复权序列）不崩溃，非正价格收益记为缺失

        修正记录（P2 冒烟测试）：000001 新浪前复权早期数据为负/为 0，旧实现
        除零崩溃（DivisionByZero）；现跳过非正价格，窗口内正价收益正常统计。
        """
        df = make_df([0, -3, 5, 10, 12, 11])
        annualized_volatility(df)
        assert df["volatility"].iloc[-1] is not None  # [10->12, 12->11] 两笔收益参与
        assert df["volatility"].iloc[1] is None       # 非正价格处收益缺失


# ---------------------------------------------------------------------------
# 最大回撤
# ---------------------------------------------------------------------------
class TestMaxDrawdown:
    def test_full_series_known_value(self):
        """[1,2,3,2,1] 全量回撤 = (3-1)/3"""
        df = make_df([1, 2, 3, 2, 1])
        max_drawdown(df)
        assert df["max_drawdown"].iloc[-1] == Decimal(2) / Decimal(3)

    def test_window_limited(self):
        """[1,2,3,2,1] window=2：最后窗口 [2,1] 回撤 = 0.5"""
        df = make_df([1, 2, 3, 2, 1])
        max_drawdown(df, window=2)
        assert df["max_drawdown"].iloc[-1] == Decimal("0.5")

    def test_monotonic_uptrend_zero_drawdown(self):
        """单调上涨序列回撤为 0"""
        df = make_df(seq(1, 30))
        max_drawdown(df)
        assert df["max_drawdown"].iloc[-1] == Decimal(0)

    def test_leading_none(self):
        """窗口内不足 2 个样本时为 None"""
        df = make_df(seq(1, 10))
        max_drawdown(df, window=5)
        assert df["max_drawdown"].iloc[0] is None


# ---------------------------------------------------------------------------
# add_all_indicators（一键全量，参数来自 settings.yaml）
# ---------------------------------------------------------------------------
class TestAddAllIndicators:
    def test_all_columns_present(self):
        """settings 配置的 12 个指标列应全部生成"""
        df = make_df(seq(1, 300))
        add_all_indicators(df)
        expected = {
            "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
            "macd_dif", "macd_dea", "macd_hist", "rsi14",
            "volatility", "max_drawdown",
        }
        assert expected <= set(df.columns)

    def test_output_is_decimal(self):
        """价格类指标列必须为 Decimal 对象（精度规范）"""
        df = make_df(seq(1, 300))
        add_all_indicators(df)
        assert isinstance(df["ma20"].iloc[-1], Decimal)
        assert isinstance(df["macd_hist"].iloc[-1], Decimal)
        assert isinstance(df["rsi14"].iloc[-1], Decimal)

    def test_returns_same_dataframe(self):
        """输出应包含在输入 DataFrame 中（同一对象，不复制）"""
        df = make_df(seq(1, 300))
        result = add_all_indicators(df)
        assert result is df

    def test_input_validation(self):
        """非 DataFrame 输入必须抛 TypeError"""
        with pytest.raises(TypeError):
            add_all_indicators([1, 2, 3])


# ---------------------------------------------------------------------------
# P2 新增：估值历史分位 valuation_percentile
# ---------------------------------------------------------------------------
def make_val_df(pairs):
    """构造估值 DataFrame：pairs 为 (日期, pe值) 列表，值 None 表示缺失"""
    return pd.DataFrame({
        "trade_date": [d for d, _ in pairs],
        "pe": [Decimal(str(v)) if v is not None else None for _, v in pairs],
    })


class TestValuationPercentile:
    def test_basic_percentile(self):
        """[10,20,30,40,25] 当前 25：此前样本中低于 25 的有 2 个 -> 2/4 = 0.5"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", 30),
            ("2025-01-04", 40), ("2025-01-05", 25),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] == Decimal("0.5")

    def test_min_value_is_zero(self):
        """当前为窗口最低值 -> 分位 0"""
        df = make_val_df([
            ("2025-01-01", 50), ("2025-01-02", 40), ("2025-01-03", 30),
            ("2025-01-04", 20), ("2025-01-05", 10),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] == Decimal(0)

    def test_max_value_is_one(self):
        """当前为窗口最高值 -> 分位 1（全部历史样本低于当前）"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", 30),
            ("2025-01-04", 40), ("2025-01-05", 50),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] == Decimal(1)

    def test_tie_not_counted_as_below(self):
        """等值样本不计入「低于」：当前 20，此前 [10,20,20,20] -> 1/4 = 0.25"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", 20),
            ("2025-01-04", 20), ("2025-01-05", 20),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] == Decimal("0.25")

    def test_negative_samples_excluded(self):
        """负 PE 样本剔除：当前 15，正样本 [10,20,30] 中低于的 [10] -> 1/3"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", -5), ("2025-01-03", 20),
            ("2025-01-04", 30), ("2025-01-05", 15),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] == Decimal(1) / Decimal(3)

    def test_current_negative_none(self):
        """当日 PE 为负（亏损）时分位为 None"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", -5),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] is None

    def test_current_missing_none(self):
        """当日 PE 缺失时分位为 None"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", None),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] is None

    def test_window_filter_by_years(self):
        """years=1 只统计近 1 年：窗口 [2024-08-01, 2025-08-01) 内仅 [10, 20] -> 0.5"""
        df = make_val_df([
            ("2024-01-01", 100), ("2024-06-01", 100),  # 超出窗口，应被过滤
            ("2025-06-01", 10), ("2025-07-01", 20),
            ("2025-08-01", 15),
        ])
        valuation_percentile(df, column="pe", years=1)
        assert df["pe_percentile_1y"].iloc[-1] == Decimal("0.5")

    def test_insufficient_samples_none(self):
        """窗口内有效样本不足 min_samples（默认 2）时分位为 None"""
        df = make_val_df([
            ("2024-01-01", 100),   # 超出 1 年窗口
            ("2025-06-01", 10),    # 窗口内唯一样本
            ("2025-08-01", 15),
        ])
        valuation_percentile(df, column="pe", years=1)
        assert df["pe_percentile_1y"].iloc[-1] is None

    def test_leading_row_none(self):
        """首行无此前样本，分位为 None"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", 30),
        ])
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[0] is None
        assert df["pe_percentile_5y"].iloc[-1] is not None

    def test_decimal_exact_fraction(self):
        """结果必须为 Decimal 精确分数（2/7 不得有浮点误差）"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", 30),
            ("2025-01-04", 40), ("2025-01-05", 50), ("2025-01-06", 60),
            ("2025-01-07", 70), ("2025-01-08", 25),
        ])
        valuation_percentile(df, column="pe", years=5)
        value = df["pe_percentile_5y"].iloc[-1]
        assert value == Decimal(2) / Decimal(7)
        assert isinstance(value, Decimal)

    def test_default_years_from_settings_pe(self):
        """column='pe' 不传 years 时应读 tech.pe_percentile_years（=5）"""
        df = make_val_df([
            ("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", 15),
        ])
        valuation_percentile(df, column="pe")
        assert "pe_percentile_5y" in df.columns
        assert df["pe_percentile_5y"].iloc[-1] == Decimal("0.5")

    def test_default_years_from_settings_pb(self):
        """column='pb' 不传 years 时应读 value.pb_percentile_years（=5）"""
        df = pd.DataFrame({
            "trade_date": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "pb": [Decimal("1.0"), Decimal("2.0"), Decimal("1.5")],
        })
        valuation_percentile(df, column="pb")
        assert "pb_percentile_5y" in df.columns
        assert df["pb_percentile_5y"].iloc[-1] == Decimal("0.5")

    def test_unknown_column_requires_years(self):
        """非 pe/pb 列且未显式传 years 时应抛 ValueError"""
        df = pd.DataFrame({
            "trade_date": ["2025-01-01", "2025-01-02"],
            "ps": [Decimal("1"), Decimal("2")],
        })
        with pytest.raises(ValueError):
            valuation_percentile(df, column="ps")

    def test_validation(self):
        """非法输入必须抛错：非 DataFrame / 缺列 / 年限非法"""
        df = make_val_df([("2025-01-01", 10), ("2025-01-02", 20)])
        with pytest.raises(TypeError):
            valuation_percentile([1, 2])
        with pytest.raises(ValueError):
            valuation_percentile(df, column="pe", years=0)
        with pytest.raises(ValueError):
            valuation_percentile(pd.DataFrame({"pe": [1, 2]}), column="pe", years=3)

    def test_date_object_input(self):
        """trade_date 传 date 对象也能正确处理"""
        from datetime import date as date_cls
        df = pd.DataFrame({
            "trade_date": [date_cls(2025, 1, 1), date_cls(2025, 1, 2), date_cls(2025, 1, 3)],
            "pe": [Decimal("10"), Decimal("20"), Decimal("15")],
        })
        valuation_percentile(df, column="pe", years=5)
        assert df["pe_percentile_5y"].iloc[-1] == Decimal("0.5")

    def test_output_in_same_dataframe(self):
        """输出应包含在输入 DataFrame 中（同一对象，不复制）"""
        df = make_val_df([("2025-01-01", 10), ("2025-01-02", 20), ("2025-01-03", 15)])
        result = valuation_percentile(df, column="pe", years=5)
        assert result is df
