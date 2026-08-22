"""
position_sizing.py —— Layer 3 策略信号层：仓位计算与上限校验（PRD 2.2 / 3.3 / 4.3）

职责边界（遵循 .coderule 与 PROJECT_PLAN.md Layer 3 规划）：
- 仅输出建议动作与比例（Decimal），不执行任何交易（本系统不做自动下单）；
- 金额/比例计算一律使用 Decimal，阈值全部来自 settings.yaml（tech / value / risk 节）；
- 输入为个股 K 线 DataFrame（含 Layer 2 指标列），模块内部惰性补齐指标。

核心语义（P2 设计约定，重要）：
- tech.position_1st/2nd/3rd（30%/30%/40%）为「单票目标仓位内的分批比例」，
  单票目标仓位 = risk.position_single_stock_max（15% 总资产），
  因此三批合计恰好等于单票上限（15% x 30% + 15% x 30% + 15% x 40% = 15%），不产生冲突；
- 金融/ETF 底仓：value.init_position_ratio（60%）同样为「该标的计划仓位内的比例」，
  计划仓位由调用方通过 target_ratio 传入，最终受 risk.position_value_total_max（40%）约束；
- 所有返回的 ratio 均表示「占总资产的比例」（金额 = 总资产 x ratio）。

规则摘要：
  - 科技股分批：首笔 站上MA20 且 量能 >= 5日均量x1.2；二笔 回踩MA10不破 且 MACD红柱放大；
    三笔 突破前5日高点 且 RSI>50；首笔后跌>=5%暂停、涨>=8%加速执行下一批；
  - 底仓：初始建仓 金融PB分位<=20% / ETF PE分位<=30%；分位>=70% 分批止盈50%；
    恐慌加仓（指数单日跌超3%或连跌3日）额外 10%（触发判定见 market_regime）；
  - 三层上限：科技单票<=15%、科技合计<=60%、底仓合计<=40%。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

# 项目根目录与 src 目录注册到 sys.path：支持直接运行与包内引用，
# 使同目录模块（indicators 等）可互相引用（修正记录 P2：原模板仅注册根目录，
# 导致 `python src/position_sizing.py` 与 pytest 收集时 ModuleNotFoundError）
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
from indicators import ma, macd, rsi

# 动作枚举（字符串常量，便于 AI 层序列化）
ACTION_PAUSE = "PAUSE"          # 暂停加仓（首笔后跌超阈值）
ACTION_BUY_1ST = "BUY_1ST"      # 科技股首批建仓
ACTION_BUY_2ND = "BUY_2ND"      # 科技股第二笔加仓
ACTION_BUY_3RD = "BUY_3RD"      # 科技股第三笔加仓
ACTION_BUY_INIT = "BUY_INIT"    # 底仓初始建仓
ACTION_BUY_PANIC = "BUY_PANIC"  # 底仓恐慌加仓
ACTION_SELL = "SELL"            # 底仓分批止盈
ACTION_NONE = "NONE"            # 无动作


@dataclass
class PositionPlan:
    """仓位决策结果：动作 + 建议比例（占总资产）+ 理由"""
    action: str
    ratio: Decimal
    reason: str


# ---------------------------------------------------------------------------
# 输入辅助
# ---------------------------------------------------------------------------
def _to_d(v) -> Decimal:
    """数值统一转 Decimal（兼容 float/Decimal/int）"""
    if v is None or pd.isna(v):
        raise ValueError("指标值为空，无法参与仓位计算")
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _prepare_tech(kline_df: pd.DataFrame) -> pd.DataFrame:
    """惰性补齐科技股分批决策所需指标：ma5/ma10/ma20、macd、rsi14（幂等）"""
    settings = config.get_settings()
    mr = settings.market_regime
    for p in (5, 10, 20):
        if f"ma{p}" not in kline_df.columns:
            ma(kline_df, p)
    if "macd_hist" not in kline_df.columns:
        macd(kline_df)
    if f"rsi{mr.rsi_period}" not in kline_df.columns:
        rsi(kline_df, mr.rsi_period)
    return kline_df


def _volume_ma5_ok(kline_df: pd.DataFrame, ratio: Decimal) -> bool:
    """量能确认：当日成交量 >= 近5日均量 x ratio；缺 volume 列保守视为不满足"""
    if "volume" not in kline_df.columns or len(kline_df) < 1:
        return False
    n = min(len(kline_df), 5)
    vols = [_to_d(v) for v in kline_df["volume"].tail(n)]
    avg = sum(vols) / Decimal(len(vols))
    return _to_d(kline_df["volume"].iloc[-1]) >= avg * ratio


# ---------------------------------------------------------------------------
# 科技股分批建仓（PRD 2.2）
# ---------------------------------------------------------------------------
def decide_tech_batch(kline_df: pd.DataFrame, batch_done: int,
                      entry_price: Optional[Decimal] = None) -> PositionPlan:
    """科技股分批建仓决策（30%/30%/40% 三批触发条件）

    :param kline_df: 个股日 K 线（含 Layer 2 指标列，模块内惰性补齐）
    :param batch_done: 已完成批次数（0=未建仓 / 1=已首笔 / 2=已两笔 / >=3=已完成）
    :param entry_price: 首笔建仓成本价（动态控制：跌>=5%暂停、涨>=8%加速）
    :return: PositionPlan；ratio 为该批占总资产比例
             （= position_single_stock_max x position_1st/2nd/3rd）
    """
    if not isinstance(kline_df, pd.DataFrame) or kline_df.empty:
        raise ValueError("K 线数据为空，无法进行仓位决策")
    if batch_done < 0:
        raise ValueError(f"batch_done 必须为非负整数，实际为 {batch_done}")

    settings = config.get_settings()
    s, risk, mr = settings.tech, settings.risk, settings.market_regime
    _prepare_tech(kline_df)
    latest = kline_df.iloc[-1]
    price = _to_d(latest["close"])
    accelerate = False

    # 动态控制：已建仓后，按首笔成本判断暂停/加速
    if batch_done >= 1 and entry_price is not None:
        entry = _to_d(entry_price)
        move = (price - entry) / entry
        if move <= -s.pause_add_drop:
            return PositionPlan(
                ACTION_PAUSE, Decimal(0),
                f"首笔建仓后下跌 {_pct(move)} 超过 {_pct(s.pause_add_drop)}，暂停加仓")
        if move >= s.accelerate_add_rise:
            accelerate = True  # 上涨达标，跳过常规条件直接执行下一批

    if batch_done == 0:
        # 首次建仓（30%）：站上 MA20 且 成交量 >= 5日均量 x first_volume_ratio
        ma20 = latest["ma20"]
        stand_above = ma20 is not None and not pd.isna(ma20) and price > _to_d(ma20)
        volume_ok = _volume_ma5_ok(kline_df, s.first_volume_ratio)
        if stand_above and volume_ok:
            ratio = risk.position_single_stock_max * s.position_1st
            return PositionPlan(ACTION_BUY_1ST, ratio,
                                f"站上 MA20 且量能达到 5 日均量 x {s.first_volume_ratio}，"
                                f"首次建仓 {_pct(s.position_1st)}（占总资产 {_pct(ratio)}）")

    elif batch_done == 1:
        # 第二笔（30%）：回踩 MA10 不破（盘中触及但收盘站回）且 MACD 红柱放大
        ok = _check_second_batch(kline_df, accelerate)
        if ok:
            ratio = risk.position_single_stock_max * s.position_2nd
            tag = "（加速执行）" if accelerate else ""
            return PositionPlan(ACTION_BUY_2ND, ratio,
                                f"回踩 MA10 不破且 MACD 红柱放大{tag}，"
                                f"加仓 {_pct(s.position_2nd)}（占总资产 {_pct(ratio)}）")

    elif batch_done == 2:
        # 第三笔（40%）：突破前 5 日高点 且 RSI > 50
        ok = _check_third_batch(kline_df, accelerate)
        if ok:
            ratio = risk.position_single_stock_max * s.position_3rd
            tag = "（加速执行）" if accelerate else ""
            return PositionPlan(ACTION_BUY_3RD, ratio,
                                f"突破前 5 日高点且 RSI > 50{tag}，"
                                f"加仓 {_pct(s.position_3rd)}（占总资产 {_pct(ratio)}）")

    return PositionPlan(ACTION_NONE, Decimal(0), "当前不满足加仓条件")


def _check_second_batch(kline_df: pd.DataFrame, accelerate: bool) -> bool:
    """第二笔条件：回踩 MA10 不破（当日最低 <= MA10 且收盘 >= MA10）且 MACD 红柱放大"""
    if accelerate:
        return True
    if len(kline_df) < 2 or "low" not in kline_df.columns:
        return False
    latest, prev = kline_df.iloc[-1], kline_df.iloc[-2]
    ma10 = latest["ma10"]
    if ma10 is None or pd.isna(ma10):
        return False
    low, close = _to_d(latest["low"]), _to_d(latest["close"])
    touched = low <= _to_d(ma10) <= close  # 盘中触及 MA10 且收盘站回（不破）
    hist_latest, hist_prev = latest["macd_hist"], prev["macd_hist"]
    hist_growing = (hist_latest is not None and hist_prev is not None
                    and not pd.isna(hist_latest) and not pd.isna(hist_prev)
                    and _to_d(hist_latest) > _to_d(hist_prev))
    return touched and hist_growing


def _check_third_batch(kline_df: pd.DataFrame, accelerate: bool) -> bool:
    """第三笔条件：收盘突破前 5 日最高价 且 RSI > 50"""
    if accelerate:
        return True
    if len(kline_df) < 6 or "high" not in kline_df.columns:
        return False
    mr = config.get_settings().market_regime
    latest = kline_df.iloc[-1]
    prev_high = max(_to_d(v) for v in kline_df["high"].iloc[-6:-1])  # 前 5 日（不含当日）
    break_high = _to_d(latest["close"]) > prev_high
    rsi_val = latest[f"rsi{mr.rsi_period}"]
    rsi_ok = rsi_val is not None and not pd.isna(rsi_val) \
        and _to_d(rsi_val) > _to_d(config.get_settings().market_regime.rsi_bull_threshold)
    return break_high and rsi_ok


# ---------------------------------------------------------------------------
# 金融/ETF 底仓决策（PRD 3.3）
# ---------------------------------------------------------------------------
def decide_value_position(kind: str, percentile: Optional[Decimal],
                          target_ratio: Decimal, current_ratio: Decimal = Decimal(0)) -> PositionPlan:
    """金融股/ETF 底仓决策（初始建仓 / 分批止盈）

    :param kind: 'stock'（金融股，PB 分位）或 'etf'（PE 分位）
    :param percentile: 当前 PB/PE 分位（None 表示不可用，不触发任何买入）
    :param target_ratio: 该标的计划仓位占总资产比例（由调用方规划）
    :param current_ratio: 该标的当前仓位占总资产比例
    :return: PositionPlan
      - 未建仓且分位达标 -> BUY_INIT，ratio = target_ratio x init_position_ratio(60%)
      - 已建仓且分位 >= 止盈线 -> SELL，ratio = target_ratio x take_profit_ratio(50%)
      - 其他 -> NONE
    """
    if kind not in ("stock", "etf"):
        raise ValueError(f"kind 必须为 'stock' 或 'etf'，实际为 {kind}")
    target = _to_d(target_ratio)
    if target <= 0:
        raise ValueError(f"target_ratio 必须为正数，实际为 {target}")
    s = config.get_settings().value
    cur = _to_d(current_ratio)

    if cur <= 0:
        threshold = s.init_pb_percentile if kind == "stock" else s.init_pe_percentile
        metric = "PB" if kind == "stock" else "PE"
        if percentile is not None and percentile <= threshold:
            ratio = target * s.init_position_ratio
            return PositionPlan(ACTION_BUY_INIT, ratio,
                                f"{metric} 分位 {_pct(percentile)} <= {_pct(threshold)}，"
                                f"初始建仓 {_pct(s.init_position_ratio)}"
                                f"（占总资产 {_pct(ratio)}）")
        return PositionPlan(ACTION_NONE, Decimal(0),
                            f"{metric} 分位 {_pct(percentile)} 未达建仓线 {_pct(threshold)}")

    # 已建仓：分批止盈
    if percentile is not None and percentile >= s.take_profit_percentile:
        ratio = target * s.take_profit_ratio
        return PositionPlan(ACTION_SELL, ratio,
                            f"分位 {_pct(percentile)} >= {_pct(s.take_profit_percentile)}，"
                            f"分批止盈 {_pct(s.take_profit_ratio)}"
                            f"（占总资产 {_pct(ratio)}）")
    return PositionPlan(ACTION_NONE, Decimal(0), "估值未到止盈线，继续持有")


def plan_panic_add(target_ratio: Decimal) -> PositionPlan:
    """恐慌加仓计划（PRD 3.3）：指数触发恐慌信号（market_regime.is_panic_add_triggered）后调用

    :param target_ratio: 底仓标的计划仓位占总资产比例
    :return: BUY_PANIC，ratio = target_ratio x panic_add_ratio(10%)
    """
    s = config.get_settings().value
    target = _to_d(target_ratio)
    ratio = target * s.panic_add_ratio
    return PositionPlan(ACTION_BUY_PANIC, ratio,
                        f"恐慌加仓 {_pct(s.panic_add_ratio)}（占总资产 {_pct(ratio)}）")


# ---------------------------------------------------------------------------
# 三层仓位上限校验（PRD 4.3）
# ---------------------------------------------------------------------------
def apply_position_limits(requested_ratio: Decimal, current_single: Decimal = Decimal(0),
                          current_tech_total: Decimal = Decimal(0),
                          current_value_total: Decimal = Decimal(0),
                          is_tech: bool = True) -> Tuple[Decimal, str]:
    """三层仓位上限校验：返回（实际允许买入比例, 约束说明）

    - 科技股：加仓后 单票 <= position_single_stock_max 且 科技合计 <= position_tech_total_max
    - 底仓：加仓后 底仓合计 <= position_value_total_max
    请求超出上限时按剩余额度缩减，绝不超限。
    """
    risk = config.get_settings().risk
    requested = _to_d(requested_ratio)
    single = _to_d(current_single)
    allowed = requested
    notes: list = []

    if is_tech:
        cap_single = max(Decimal(0), _to_d(risk.position_single_stock_max) - single)
        if allowed > cap_single:
            allowed = cap_single
            notes.append(f"单票上限 {_pct(risk.position_single_stock_max)}")
        cap_tech = max(Decimal(0), _to_d(risk.position_tech_total_max)
                       - _to_d(current_tech_total))
        if allowed > cap_tech:
            allowed = cap_tech
            notes.append(f"科技板块上限 {_pct(risk.position_tech_total_max)}")
    else:
        cap_value = max(Decimal(0), _to_d(risk.position_value_total_max)
                        - _to_d(current_value_total))
        if allowed > cap_value:
            allowed = cap_value
            notes.append(f"底仓合计上限 {_pct(risk.position_value_total_max)}")

    reason = f"受上限约束（{'; '.join(notes)}）" if notes else "未触发仓位上限"
    return allowed, reason


def _pct(v: Optional[Decimal]) -> str:
    """比例转百分比字符串（如 0.045 -> 4.50%）；None 显示 -（修正记录 P2：
    percentile 传 None 时旧实现直接乘算报 TypeError，现已保守降级显示）"""
    return "-" if v is None else f"{v * Decimal(100):.2f}%"


# ---------------------------------------------------------------------------
# 命令行自检：python src/position_sizing.py
# ---------------------------------------------------------------------------
def _self_check() -> None:
    """自检入口：用构造行情验证分批与上限逻辑（不访问网络）"""
    print("[OK] 仓位模块自检（构造数据，不访问网络）")
    # 构造单边上涨放量行情：应触发首笔建仓
    # 修正记录（P2）：原自检量能递增幅度不足（末量 < 5日均量 x 1.2）恒为 NONE，
    # 与注释不符，已改为前 n-1 日平稳、末日放量 1600（>= 5日均量 1120 x 1.2）。
    n = 30
    base = [Decimal(10) + Decimal(i) * Decimal("0.1") for i in range(n)]
    kline = pd.DataFrame({
        "trade_date": [f"2026-07-{i + 1:02d}" for i in range(n)],
        "open": base, "high": [c + Decimal("0.2") for c in base],
        "low": [c - Decimal("0.2") for c in base], "close": base,
        "volume": [1000.0] * (n - 1) + [1600.0],
    })
    plan = decide_tech_batch(kline, batch_done=0)
    print(f"[OK] 上涨放量行情分批决策: {plan.action} ratio={plan.ratio} | {plan.reason}")
    allowed, note = apply_position_limits(Decimal("0.20"), current_single=Decimal("0.10"),
                                          current_tech_total=Decimal("0.50"))
    print(f"[OK] 请求20%单票10%科技50% -> 允许 {allowed} | {note}")


if __name__ == "__main__":
    _self_check()
