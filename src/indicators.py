"""
indicators.py —— Layer 2 因子计算层：纯数学计算（MA / MACD / RSI / 年化波动率 / 最大回撤 / 估值历史分位）

设计约束（遵循 .coderule 与 PROJECT_PLAN.md Layer 2 规划）：
- 输入必须是 DataFrame，输出以新增列的形式包含在输入 DataFrame 中（不复制）；
- 涉及价格/金额的计算一律使用 Decimal（日线数据量为千行级别，
  纯 Python 循环的精度优先策略性能可接受）；
- 本层严禁调用 AI、严禁访问网络、严禁硬编码参数（周期参数来自 config.get_settings()）；
- 周期参数可显式传入，缺省时自动从 settings.yaml 对应配置读取。

用法示例：
    from data_layer import get_kline, get_valuation_history
    from indicators import add_all_indicators, rsi, ma, valuation_percentile
    df = get_kline("sh000300", is_index=True)
    df = add_all_indicators(df)     # 一次性追加 ma5/ma10/.../macd_*/rsi14/volatility/max_drawdown
    df = rsi(df, period=14)         # 或单独计算某个因子
    val = get_valuation_history("000001")          # 估值历史（trade_date/pe/pb）
    val = valuation_percentile(val, column="pe")   # PE 近 5 年分位（窗口默认读配置）
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

import pandas as pd

# 项目根目录注册到 sys.path：支持直接运行与包内引用（同 data_layer.py）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config

# 年化波动率基准：A 股每年约 252 个交易日
TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Decimal 辅助函数
# ---------------------------------------------------------------------------
def _ensure_decimal(series: pd.Series) -> List[Decimal]:
    """将价格列转为 Decimal 列表（float 自动精确转换；缺失值抛错）"""
    out: List[Decimal] = []
    for v in series:
        if isinstance(v, Decimal):
            out.append(v)
        elif v is None or pd.isna(v):
            raise ValueError("价格列存在缺失值，因子计算前请先清洗数据")
        else:
            out.append(Decimal(str(v)))
    return out


def _ensure_decimal_optional(series: pd.Series) -> List[Optional[Decimal]]:
    """将估值列转为 Decimal 列表（允许缺失值 -> None；非法值抛错）

    P2 新增：估值序列（PE/PB）某日可能缺某个指标，缺失保留为 None，
    由调用方（如估值分位）自行决定剔除或跳过。
    """
    out: List[Optional[Decimal]] = []
    for v in series:
        if isinstance(v, Decimal):
            out.append(v)
        elif v is None or pd.isna(v):
            out.append(None)
        else:
            out.append(Decimal(str(v)))
    return out


def _check_input(df: pd.DataFrame, column: str) -> None:
    """输入校验：必须是 DataFrame 且包含目标列"""
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"输入必须是 pandas DataFrame，实际为 {type(df).__name__}")
    if column not in df.columns:
        raise ValueError(f"DataFrame 缺少必需列: {column}")
    if len(df) < 2:
        raise ValueError("数据行数不足，至少需要 2 行")


def _decimal_std(values: List[Decimal]) -> Decimal:
    """样本标准差（Decimal，n-1 分母）"""
    n = len(values)
    mean = sum(values) / Decimal(n)
    var = sum((v - mean) ** 2 for v in values) / Decimal(n - 1)
    return var.sqrt()


# ---------------------------------------------------------------------------
# 基础均线
# ---------------------------------------------------------------------------
def ma(df: pd.DataFrame, period: Optional[int] = None, column: str = "close") -> pd.DataFrame:
    """简单移动平均，输出列 ma{period}

    :param period: 周期（日），缺省取 settings.market_regime.ma_period
    """
    if period is None:
        period = config.get_settings().market_regime.ma_period
    if period < 1:
        raise ValueError(f"MA 周期必须为正整数，实际为 {period}")
    _check_input(df, column)

    prices = _ensure_decimal(df[column])
    out: List[Optional[Decimal]] = [None] * len(df)
    window_sum = Decimal(0)
    for i, price in enumerate(prices):
        window_sum += price
        if i >= period:
            window_sum -= prices[i - period]
        if i >= period - 1:
            out[i] = window_sum / Decimal(period)
    df[f"ma{period}"] = out
    return df


def ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.DataFrame:
    """指数移动平均（EMA），输出列 ema{period}；递推式 EMA_t = α·P_t + (1-α)·EMA_{t-1}

    初始值取首个价格；α = 2 / (period + 1)，用于 MACD 内部计算。
    """
    if period < 1:
        raise ValueError(f"EMA 周期必须为正整数，实际为 {period}")
    _check_input(df, column)

    prices = _ensure_decimal(df[column])
    alpha = Decimal(2) / Decimal(period + 1)
    out: List[Optional[Decimal]] = [None] * len(df)
    prev: Optional[Decimal] = None
    for i, price in enumerate(prices):
        if prev is None:
            prev = price
        else:
            prev = price * alpha + prev * (Decimal(1) - alpha)
        out[i] = prev
    df[f"ema{period}"] = out
    return df


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
def macd(df: pd.DataFrame, fast: Optional[int] = None,
         slow: Optional[int] = None, signal: Optional[int] = None) -> pd.DataFrame:
    """MACD 指标，输出列 macd_dif / macd_dea / macd_hist

    公式（国内软件惯例）：
      DIF  = EMA(fast) - EMA(slow)
      DEA  = EMA(DIF, signal)
      MACD = (DIF - DEA) * 2   （红柱为正、绿柱为负）
    金叉判定：DIF 上穿 DEA；红柱放大：macd_hist 逐日递增。
    """
    settings = config.get_settings().market_regime
    fast = fast or settings.macd_fast
    slow = slow or settings.macd_slow
    signal = signal or settings.macd_signal
    if not (0 < fast < slow):
        raise ValueError(f"MACD 参数非法: fast={fast} 必须小于 slow={slow}")
    _check_input(df, "close")

    ema(df, fast)
    ema(df, slow)
    dif = [a - b for a, b in zip(df[f"ema{fast}"], df[f"ema{slow}"])]

    # DEA = DIF 的 EMA（复用 ema 的递推逻辑，避免重复建列）
    alpha = Decimal(2) / Decimal(signal + 1)
    dea: List[Optional[Decimal]] = [None] * len(dif)
    prev: Optional[Decimal] = None
    for i, v in enumerate(dif):
        if prev is None:
            prev = v
        else:
            prev = v * alpha + prev * (Decimal(1) - alpha)
        dea[i] = prev

    hist = [(d - e) * Decimal(2) if d is not None and e is not None else None
            for d, e in zip(dif, dea)]
    df["macd_dif"] = dif
    df["macd_dea"] = dea
    df["macd_hist"] = hist
    return df


# ---------------------------------------------------------------------------
# RSI（Wilder 平滑）
# ---------------------------------------------------------------------------
def rsi(df: pd.DataFrame, period: Optional[int] = None, column: str = "close") -> pd.DataFrame:
    """相对强弱指标 RSI，输出列 rsi{period}（0~100）

    Wilder 平滑法：
      初始 avg_gain/avg_loss = 前 period 个涨跌幅的简单平均；
      之后逐日平滑: avg = (avg·(period-1) + 当日值) / period；
      RS = avg_gain / avg_loss；RSI = 100 - 100/(1+RS)；avg_loss=0 时 RSI=100。
    """
    if period is None:
        period = config.get_settings().market_regime.rsi_period
    if period < 1:
        raise ValueError(f"RSI 周期必须为正整数，实际为 {period}")
    _check_input(df, column)

    prices = _ensure_decimal(df[column])
    out: List[Optional[Decimal]] = [None] * len(df)
    gains: List[Decimal] = []
    losses: List[Decimal] = []
    avg_gain: Optional[Decimal] = None
    avg_loss: Optional[Decimal] = None

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gain = change if change > 0 else Decimal(0)
        loss = -change if change < 0 else Decimal(0)
        if i <= period:
            gains.append(gain)
            losses.append(loss)
            if i == period:  # 用前 period 个变化量初始化均值
                avg_gain = sum(gains) / Decimal(period)
                avg_loss = sum(losses) / Decimal(period)
            continue
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
        if avg_loss == 0:
            out[i] = Decimal(100)
        else:
            rs = avg_gain / avg_loss
            out[i] = Decimal(100) - Decimal(100) / (Decimal(1) + rs)
    df[f"rsi{period}"] = out
    return df


# ---------------------------------------------------------------------------
# 年化波动率
# ---------------------------------------------------------------------------
def annualized_volatility(df: pd.DataFrame, window: Optional[int] = None,
                          column: str = "close") -> pd.DataFrame:
    """年化波动率，输出列 volatility（小数，如 0.35 表示 35%）

    逐日收益率 r_t = P_t / P_{t-1} - 1，取窗口内样本标准差（n-1），
    再乘以 sqrt(252) 年化；window=None 表示使用全部样本。
    """
    _check_input(df, column)
    prices = _ensure_decimal(df[column])
    n = len(prices)
    if window is None:
        window = n

    # 逐日收益率（Decimal）
    # 修正记录（P2 冒烟测试）：真实前复权数据含非正收盘价，除以前价为 0 时抛
    # DivisionByZero；现对非正价格直接记为缺失（无收益意义，不参与波动率统计），
    # 影响面：仅 annualized_volatility 对脏数据的容忍度，正常正价数据行为不变。
    returns: List[Optional[Decimal]] = [None] * n
    for i in range(1, n):
        if prices[i] <= 0 or prices[i - 1] <= 0:
            continue  # 非正价格（前复权脏数据）不产生收益率，保守记为缺失
        returns[i] = prices[i] / prices[i - 1] - Decimal(1)

    out: List[Optional[Decimal]] = [None] * n
    annual_factor = Decimal(TRADING_DAYS_PER_YEAR).sqrt()
    for i in range(n):
        start = max(1, i - window + 1)
        chunk = [r for r in returns[start:i + 1] if r is not None]
        if len(chunk) < 2:  # 至少 2 个收益率样本才能计算标准差
            continue
        out[i] = _decimal_std(chunk) * annual_factor
    df["volatility"] = out
    return df


# ---------------------------------------------------------------------------
# 最大回撤
# ---------------------------------------------------------------------------
def max_drawdown(df: pd.DataFrame, window: Optional[int] = None,
                 column: str = "close") -> pd.DataFrame:
    """滚动窗口内最大回撤，输出列 max_drawdown（正数，如 0.30 表示回撤 30%）

    定义：窗口内取峰值（peak）及其后最低点（trough），回撤 = (peak - trough) / peak；
    window=None 表示自上市以来全量计算。
    """
    _check_input(df, column)
    prices = _ensure_decimal(df[column])
    n = len(prices)
    if window is None:
        window = n

    out: List[Optional[Decimal]] = [None] * n
    for i in range(n):
        start = max(0, i - window + 1)
        chunk = prices[start:i + 1]
        if len(chunk) < 2:
            continue
        peak = max(chunk)
        peak_index = chunk.index(peak)  # 首个峰值
        trough = min(chunk[peak_index:])  # 峰值之后的最低点
        out[i] = (peak - trough) / peak
    df["max_drawdown"] = out
    return df


# ---------------------------------------------------------------------------
# 估值历史分位（P2 新增）
# ---------------------------------------------------------------------------
# 分位窗口内最少有效样本数：不足时历史分布无统计意义，返回 None
_MIN_PERCENTILE_SAMPLES = 2


def _sub_years(d: date, years: int) -> date:
    """日期减 N 年；2 月 29 日等边界回退到当月 28 日，避免 replace 抛错"""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def _to_date(d) -> date:
    """各种日期表示（str / date / datetime / Timestamp）统一转为 datetime.date"""
    if isinstance(d, datetime):  # 含 pd.Timestamp（datetime 子类）
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def valuation_percentile(df: pd.DataFrame, column: str = "pe",
                         years: Optional[int] = None,
                         min_samples: int = _MIN_PERCENTILE_SAMPLES) -> pd.DataFrame:
    """估值历史百分位，输出列 {column}_percentile_{years}y（0~1 小数，越低越便宜）

    定义：对每一行（当日估值），统计此前近 years 年内「低于当日估值」的
    正样本占比：
        percentile = count(v < current, v > 0) / count(v > 0)
    0 = 窗口内历史最低，1 = 窗口内历史最高（严格小于口径）。

    处理约定：
      - 仅正样本参与统计：负 PE（亏损）/负 PB（资不抵债）无估值意义一律剔除，
        防止亏损股因负 PE 被误判为「低估」；当日估值为 None 或 <= 0 时分位为 None；
      - 仅统计当日之前的样本（避免未来函数），窗口内有效样本不足
        min_samples 时分位为 None；
      - 输入必须按 trade_date 升序（数据层 get_valuation_history 已保证）。

    性能说明：O(n²) 纯 Python 循环，近 5 年日频约 1200 行时秒级完成，
    与 P1 max_drawdown 同策略（精度优先，千行级数据量可接受）。

    :param column: 估值列名（'pe' / 'pb'），同时决定缺省回看年限：
                   pe -> settings.tech.pe_percentile_years
                   pb -> settings.value.pb_percentile_years
    :param years: 回看年限（如 3 / 5），缺省按 column 从配置读取
    :param min_samples: 窗口内最少有效样本数（低于则返回 None）
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"输入必须是 pandas DataFrame，实际为 {type(df).__name__}")
    for need in (column, "trade_date"):
        if need not in df.columns:
            raise ValueError(f"DataFrame 缺少必需列: {need}")
    if years is None:
        settings = config.get_settings()
        if column == "pe":
            years = settings.tech.pe_percentile_years
        elif column == "pb":
            years = settings.value.pb_percentile_years
        else:
            raise ValueError(
                f"无法从配置推断回看年限，请显式传入 years 参数（column={column}）")
    if years < 1:
        raise ValueError(f"回看年限必须为正整数，实际为 {years}")
    if min_samples < 1:
        raise ValueError(f"最少有效样本数必须为正整数，实际为 {min_samples}")

    values = _ensure_decimal_optional(df[column])
    dates = [_to_date(d) for d in df["trade_date"]]

    n = len(df)
    out: List[Optional[Decimal]] = [None] * n
    for i in range(n):
        current = values[i]
        if current is None or current <= 0:
            out[i] = None  # 当日估值缺失或为负，分位无意义
            continue
        cutoff = _sub_years(dates[i], years)
        cnt = 0
        total = 0
        for j in range(i):  # 仅统计当日之前的样本，避免未来函数
            if dates[j] < cutoff:
                continue  # 超出回看窗口
            v = values[j]
            if v is None or v <= 0:
                continue  # 剔除负值与缺失样本
            total += 1
            if v < current:
                cnt += 1
        if total < min_samples:
            continue  # 窗口内有效样本不足，分位无统计意义
        out[i] = Decimal(cnt) / Decimal(total)
    df[f"{column}_percentile_{years}y"] = out
    return df


# ---------------------------------------------------------------------------
# 一键计算全部指标（按 settings.yaml 参数）
# ---------------------------------------------------------------------------
def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """按 settings.yaml 配置一次性追加全部指标列

    计算清单（周期全部来自 Layer 0 配置）：
      - MA：ma5/ma10/ma20/ma60/ma120/ma250（收集自 market_regime / tech / value / risk）
      - MACD：macd_dif / macd_dea / macd_hist（market_regime.macd_*）
      - RSI：rsi14（market_regime.rsi_period）
      - 年化波动率：volatility（252 日窗口，对应 PRD 近 1 年股性验证）
      - 最大回撤：max_drawdown（60 日窗口，对应 PRD 超跌底部形态判定）
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"输入必须是 pandas DataFrame，实际为 {type(df).__name__}")

    settings = config.get_settings()
    mr, tech, value, risk = settings.market_regime, settings.tech, settings.value, settings.risk

    # MA 周期集合：跨配置去重合并
    ma_periods = sorted({
        mr.ma_period,
        *tech.ma_trend_periods,
        *value.trend_ma_periods,
        tech.limit_up_trail_ma,
        risk.clear_tech_ma,
    })
    for p in ma_periods:
        ma(df, p)

    macd(df, mr.macd_fast, mr.macd_slow, mr.macd_signal)
    rsi(df, mr.rsi_period)
    annualized_volatility(df, window=TRADING_DAYS_PER_YEAR)
    max_drawdown(df, window=tech.drawdown_window)
    return df
