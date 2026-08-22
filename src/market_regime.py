"""
market_regime.py —— Layer 3 策略信号层：宏观市场景气度判定（PRD 第 1 节 + 4.3 全局风控）

职责边界（遵循 .coderule 与 PROJECT_PLAN.md Layer 3 规划）：
- 输入为沪深300 指数日 K 线 DataFrame（data_layer.get_kline("sh000300", is_index=True)），
  本模块内部惰性补齐所需指标（复用 Layer 2 indicators，不重复造轮子）；
- 输出为市场状态与风控触发标志，不包含任何下单/仓位动作（仓位决策属 position_sizing）；
- 全部阈值来自 settings.yaml（market_regime / risk / value 节），严禁硬编码；
- 可选字段访问一律使用安全方式（如成交量列缺失时的保守降级处理）。

判定规则（PRD v2.0）：
- 景气上行 BULL：收 > MA60 且 MACD 金叉(DIF>DEA) 且 RSI14 > 50 且 量能 >= 5日均量 x 量能倍数
- 低落横盘 BEAR：收 < MA60 且 30 < RSI14 < 50
- 其他：NEUTRAL（震荡/无明确信号）
- 强制减仓：沪深300 连续 5 日收阴 且 期间累计跌幅 >= 3%（双重条件防震荡市误触发）
- 科技股减半：沪深300 连续 5 日下跌（risk.half_position_loss_days）
- 禁止开仓：单日涨超 3% 且 RSI >= 70（过热追高禁止，两条件同时满足）
- 科技股清仓：沪深300 收盘跌破 MA120（risk.clear_tech_ma）
- 恐慌加仓：沪深300 单日跌超 3% 或 连续 3 日下跌（value 节，供底仓加仓引用）

用法示例：
    from data_layer import get_kline
    from market_regime import detect_regime, is_force_reduce_triggered
    index_df = get_kline("sh000300", is_index=True)
    print(detect_regime(index_df))                 # MarketRegime.BULL / BEAR / NEUTRAL
    print(is_force_reduce_triggered(index_df))     # 是否触发强制减仓
"""

from __future__ import annotations

import sys
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import List, Optional

import pandas as pd

# 项目根目录与 src 目录注册到 sys.path：支持直接运行与包内引用，
# 使同目录模块（indicators 等）可互相引用（修正记录 P2：原模板仅注册根目录，
# 导致 `python src/market_regime.py` 与 pytest 收集时 ModuleNotFoundError）
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
from indicators import ma, macd, rsi


class MarketRegime(str, Enum):
    """宏观市场景气状态（继承 str，便于直接序列化/输出）"""
    BULL = "BULL"          # 景气上行
    BEAR = "BEAR"          # 低落横盘
    NEUTRAL = "NEUTRAL"    # 震荡/无明确信号


# ---------------------------------------------------------------------------
# 输入校验与指标补齐
# ---------------------------------------------------------------------------
def _check_index_df(index_df: pd.DataFrame, need: str) -> None:
    """指数输入校验：必须是 DataFrame 且含 close 列"""
    if not isinstance(index_df, pd.DataFrame):
        raise TypeError(f"指数数据必须是 pandas DataFrame，实际为 {type(index_df).__name__}")
    if index_df.empty:
        raise ValueError(f"指数数据为空，无法{need}")
    if "close" not in index_df.columns:
        raise ValueError(f"指数数据缺少必需列: close")


def prepare_index(index_df: pd.DataFrame) -> pd.DataFrame:
    """惰性补齐宏观判定所需全部指标列（幂等：列已存在则跳过）

    所需指标：ma{ma_period}（景气判定）、macd_dif/dea（金叉）、
    rsi{rsi_period}（景气/禁止开仓）、ma{clear_tech_ma}（科技股清仓线）。
    """
    settings = config.get_settings()
    mr, risk = settings.market_regime, settings.risk
    if f"ma{mr.ma_period}" not in index_df.columns:
        ma(index_df, mr.ma_period)
    if "macd_dif" not in index_df.columns or "macd_dea" not in index_df.columns:
        macd(index_df)
    if f"rsi{mr.rsi_period}" not in index_df.columns:
        rsi(index_df, mr.rsi_period)
    if f"ma{risk.clear_tech_ma}" not in index_df.columns:
        ma(index_df, risk.clear_tech_ma)
    return index_df


def _to_d(v) -> Decimal:
    """数值统一转 Decimal（兼容 float/Decimal/int，规避跨类型比较隐患）"""
    if v is None or pd.isna(v):
        raise ValueError("指标值为空，无法比较")
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _closes(index_df: pd.DataFrame) -> List[Decimal]:
    """close 列转 Decimal 列表（指数 K 线 close 不允许缺失）"""
    return [_to_d(v) for v in index_df["close"]]


# ---------------------------------------------------------------------------
# 核心判定
# ---------------------------------------------------------------------------
def _volume_confirm(index_df: pd.DataFrame) -> bool:
    """量能确认：当日成交量 >= 近 volume_ma_period 日均量 x volume_ratio

    修正记录（P2）：指数接口理论上有成交量列，但按 .coderrules 边缘情况要求，
    缺 volume 列时保守视为「量能不确认」（不判 BULL），严禁直接访问属性抛错。
    """
    mr = config.get_settings().market_regime
    if "volume" not in index_df.columns or index_df["volume"].isna().all():
        return False
    vols = [_to_d(v) for v in index_df["volume"].tail(mr.volume_ma_period)]
    if not vols:
        return False
    avg = sum(vols) / Decimal(len(vols))
    latest_vol = _to_d(index_df["volume"].iloc[-1])
    return latest_vol >= avg * _to_d(mr.volume_ratio)


def _consecutive_down_days(index_df: pd.DataFrame, days: int) -> bool:
    """最近 days 日收盘价逐日下跌（每根均严格低于前一日收盘）"""
    if len(index_df) < days + 1:
        return False  # 样本不足，保守判定为未触发
    closes = _closes(index_df)[-(days + 1):]
    return all(closes[i] < closes[i - 1] for i in range(1, len(closes)))


def detect_regime(index_df: pd.DataFrame) -> MarketRegime:
    """宏观市场景气度判定（PRD 第 1 节）

    BULL：收 > MA60 且 MACD 金叉 且 RSI14 > rsi_bull_threshold 且量能确认
    BEAR：收 < MA60 且 rsi_bear_low < RSI14 < rsi_bear_high
    其余 NEUTRAL；指标值缺失时对应条件视为不满足（保守）。
    """
    _check_index_df(index_df, "判定市场状态")
    settings = config.get_settings()
    mr = settings.market_regime
    prepare_index(index_df)

    latest = index_df.iloc[-1]
    close = _to_d(latest["close"])

    ma_val = latest[f"ma{mr.ma_period}"]
    above_ma = ma_val is not None and not pd.isna(ma_val) and close > _to_d(ma_val)

    dif, dea = latest["macd_dif"], latest["macd_dea"]
    golden_cross = (dif is not None and dea is not None
                    and not pd.isna(dif) and not pd.isna(dea)
                    and _to_d(dif) > _to_d(dea))

    rsi_val = latest[f"rsi{mr.rsi_period}"]
    rsi_ok = rsi_val is not None and not pd.isna(rsi_val)
    rsi_decimal = _to_d(rsi_val) if rsi_ok else None

    # 景气上行：四项条件同时满足
    if (above_ma and golden_cross and rsi_ok
            and rsi_decimal > _to_d(mr.rsi_bull_threshold)
            and _volume_confirm(index_df)):
        return MarketRegime.BULL

    # 低落横盘：两项条件同时满足
    if (not above_ma and rsi_ok
            and _to_d(mr.rsi_bear_low) < rsi_decimal < _to_d(mr.rsi_bear_high)):
        return MarketRegime.BEAR

    return MarketRegime.NEUTRAL


def is_force_reduce_triggered(index_df: pd.DataFrame) -> bool:
    """强制减仓触发（PRD 第 1 节）：连续 N 日收阴 且 期间累计跌幅 >= 阈值

    双重条件同时满足才触发，避免震荡市连阴误判；样本不足时保守不触发。
    """
    _check_index_df(index_df, "判定强制减仓")
    mr = config.get_settings().market_regime
    days = mr.force_reduce_loss_days
    if len(index_df) < days + 1:
        return False
    closes = _closes(index_df)[-(days + 1):]
    consecutive = all(closes[i] < closes[i - 1] for i in range(1, len(closes)))
    # 期间累计跌幅：从首根阴线的前收算到最新收盘
    total_drop = (closes[0] - closes[-1]) / closes[0]
    return consecutive and total_drop >= _to_d(mr.force_reduce_loss_drop)


def is_half_position_triggered(index_df: pd.DataFrame) -> bool:
    """科技股减半触发（PRD 4.3）：沪深300 连续 N 日下跌（risk.half_position_loss_days）"""
    _check_index_df(index_df, "判定科技股减半")
    days = config.get_settings().risk.half_position_loss_days
    return _consecutive_down_days(index_df, days)


def is_forbid_open(index_df: pd.DataFrame) -> bool:
    """禁止开仓触发（PRD 4.3）：单日涨超 3% 且 RSI >= 70（两条件同时满足）

    解读说明：PRD 原文「单日涨超3%但 RSI ≥ 70时」，工程上取 AND 语义——
    单日大涨叠加 RSI 过热时才禁止追高开仓；若需 OR 语义可调整。
    """
    _check_index_df(index_df, "判定禁止开仓")
    settings = config.get_settings()
    risk, mr = settings.risk, settings.market_regime
    if len(index_df) < 2:
        return False
    prepare_index(index_df)
    closes = _closes(index_df)
    rise = (closes[-1] - closes[-2]) / closes[-2]
    rsi_val = index_df[f"rsi{mr.rsi_period}"].iloc[-1]
    return (rise > _to_d(risk.forbid_open_rise)
            and rsi_val is not None and not pd.isna(rsi_val)
            and _to_d(rsi_val) >= _to_d(risk.forbid_open_rsi))


def is_clear_tech_triggered(index_df: pd.DataFrame) -> bool:
    """科技股清仓触发（PRD 4.3）：沪深300 收盘跌破 MA120（risk.clear_tech_ma）"""
    _check_index_df(index_df, "判定科技股清仓")
    risk = config.get_settings().risk
    prepare_index(index_df)
    ma_col = f"ma{risk.clear_tech_ma}"
    ma_val = index_df[ma_col].iloc[-1]
    if ma_val is None or pd.isna(ma_val):
        return False
    return _closes(index_df)[-1] < _to_d(ma_val)


def is_panic_add_triggered(index_df: pd.DataFrame) -> bool:
    """恐慌加仓触发（PRD 3.3）：沪深300 单日跌超 3% 或 连续 3 日下跌（供底仓加仓引用）"""
    _check_index_df(index_df, "判定恐慌加仓")
    value = config.get_settings().value
    if len(index_df) < 2:
        return False
    closes = _closes(index_df)
    one_day_crash = (closes[-2] - closes[-1]) / closes[-2] > _to_d(value.panic_drop_1d)
    return one_day_crash or _consecutive_down_days(index_df, value.panic_drop_days)


# ---------------------------------------------------------------------------
# 命令行自检：python src/market_regime.py
# ---------------------------------------------------------------------------
def _self_check() -> None:
    """自检入口：用构造的行情片段验证各判定函数（不访问网络）"""
    print("[OK] 宏观判定模块自检（构造数据，不访问网络）")
    # 构造一段 80 日上涨行情（含成交量），验证 BULL 判定与清仓线
    # 修正记录（P2）：原自检仅 10 日数据，MA60 无法计算恒为 NEUTRAL，与注释不符，
    # 已扩充至 80 日（MA60 周期内）单边上涨 + 量能递增，验证真实 BULL 路径。
    n = 80
    closes = [Decimal(100) + Decimal(i) * Decimal(2) for i in range(n)]  # 100 -> 258 单边上涨
    volumes = [float(1000 + i * 10) for i in range(n)]
    df = pd.DataFrame({
        "trade_date": [f"2026-08-{i + 10:02d}" for i in range(n)],
        "open": [c - Decimal("0.5") for c in closes],
        "high": [c + Decimal("0.5") for c in closes],
        "low": [c - Decimal("1") for c in closes],
        "close": closes,
        "volume": volumes,
    })
    regime = detect_regime(df)
    print(f"[OK] 单边上涨行情判定: {regime.value}")
    # 构造 6 日连续收阴且累计跌超 3%，验证强制减仓
    down = [Decimal(100) - Decimal(i) * Decimal("1.2") for i in range(6)]  # 100 -> 94，跌 6%
    df2 = pd.DataFrame({
        "trade_date": [f"2026-08-{i + 10:02d}" for i in range(6)],
        "open": [c + Decimal("0.3") for c in down],
        "high": [c + Decimal("0.5") for c in down],
        "low": [c - Decimal("0.5") for c in down],
        "close": down,
        "volume": [1000.0] * 6,
    })
    print(f"[OK] 连续6日收阴跌6% -> 强制减仓触发: {is_force_reduce_triggered(df2)}")
    print(f"[OK] 连续6日收阴 -> 科技股减半触发: {is_half_position_triggered(df2)}")
    print(f"[OK] 连续6日收阴跌6% -> 恐慌加仓触发: {is_panic_add_triggered(df2)}")


if __name__ == "__main__":
    _self_check()
