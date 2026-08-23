"""
stock_screener.py —— Layer 3 策略信号层：科技股选股（PRD 2.1 选股池构建）

职责边界（遵循 .coderule 与 PROJECT_PLAN.md Layer 3 规划）：
- 初筛条件全部为硬编码 If-Else 逻辑，阈值一律引用 settings.yaml tech 节；
- 数据获取复用 Layer 1（data_layer.get_kline / get_valuation_history），
  因子计算复用 Layer 2（indicators.add_all_indicators / valuation_percentile）；
- 本层严禁调用 AI（AI 解读属 Layer 4）；
- 单只股票拉取失败不阻塞全流程（try-except 兜底并记录失败原因）。

初筛条件（PRD 2.1）：
  1. 价格区间：1 ~ 20 元（tech.price_min / price_max）
  2. 行业白名单：电子/计算机/通信/电力设备/传媒（tech.industries）
  3. 超跌底部形态：近60日最大回撤 >= 30% 且 当前距低点反弹 <= 15%
  4. 趋势确认：收 > MA20 且 MA5 > MA10 > MA20
  5. 股性验证：近3个月至少1次涨停（主板9.5%/创业板19.5%）且 近1年年化波动率 20%~60%
  6. 估值指标：30 <= RSI14 <= 55 且 PE 近5年分位 <= 50%

用法示例：
    from stock_screener import screen_tech_stocks
    candidates = screen_tech_stocks({"000001": "电子", "300001": "计算机"})  # 手工小池
    passed = [c for c in candidates if c.passed]   # 初筛通过的候选池
"""

from __future__ import annotations

import calendar
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# 项目根目录与 src 目录注册到 sys.path：支持直接运行与包内引用，
# 使同目录模块（data_layer / indicators）可互相引用（修正记录 P2：原模板仅
# 注册根目录，导致 `python src/stock_screener.py` 与 pytest 收集时 ModuleNotFoundError）
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
from data_layer import DataFetchError, get_kline, get_valuation_history
from indicators import add_all_indicators, valuation_percentile


@dataclass
class StockCandidate:
    """科技股初筛候选结果（PRD 第 5 节 Stock 对象的初筛子集）"""
    code: str                              # 证券代码（如 000001）
    industry: str                          # 行业名称
    price: Optional[Decimal]               # 最新收盘价
    pe_percentile: Optional[Decimal]       # PE 近 N 年分位（None 表示不可用）
    passed: bool                           # 是否通过全部初筛条件
    # 最新指标快照（P4 修正：原仅保留初筛结论，指标用完即弃，导致 Layer 4 Prompt
    # 中均线/RSI/MACD 等字段全部为 N/A；现回填供 AI 解读引用）
    ma5: Optional[Decimal] = None
    ma10: Optional[Decimal] = None
    ma20: Optional[Decimal] = None
    rsi: Optional[Decimal] = None
    macd_dif: Optional[Decimal] = None
    macd_dea: Optional[Decimal] = None
    macd_hist: Optional[Decimal] = None
    max_drawdown: Optional[Decimal] = None
    volatility: Optional[Decimal] = None
    rebound_from_low: Optional[Decimal] = None   # 当前距近60日低点反弹幅度
    failures: List[str] = field(default_factory=list)  # 未通过条件明细
    error: Optional[str] = None            # 数据拉取异常信息（None 表示无异常）


# ---------------------------------------------------------------------------
# 涨停阈值与日期辅助
# ---------------------------------------------------------------------------
def _limit_up_threshold(code: str) -> Decimal:
    """按板块返回涨停幅度阈值：创业板(300/301)/科创板(688) 19.5%，其余主板 9.5%"""
    s = config.get_settings().tech
    if code.startswith(("300", "301", "688")):
        return s.limit_up_chinext
    return s.limit_up_main


def _sub_months(d: date, months: int) -> date:
    """日期减 N 个月（月末日期自动收缩到当月最后一天）"""
    total = d.year * 12 + (d.month - 1) - months
    y, m = divmod(total, 12)
    day = min(d.day, calendar.monthrange(y, m + 1)[1])
    return date(y, m + 1, day)


def _has_limit_up_recent(kline_df: pd.DataFrame, months: int, threshold: Decimal) -> bool:
    """近 months 个月（自然月）内是否出现至少 1 次涨停（单日涨幅 >= threshold）

    遍历窗口内所有交易日的前后收盘对比；样本不足 2 日时保守返回 False。
    """
    if len(kline_df) < 2:
        return False
    try:
        latest_date = datetime.strptime(str(kline_df["trade_date"].iloc[-1])[:10],
                                        "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    cutoff = _sub_months(latest_date, months)
    prev: Optional[Decimal] = None
    for row in kline_df.itertuples(index=False):
        try:
            d = datetime.strptime(str(row.trade_date)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if d < cutoff:
            prev = _as_decimal(getattr(row, "close", None))
            continue
        close = _as_decimal(getattr(row, "close", None))
        if prev is not None and close is not None and prev > 0:
            rise = (close - prev) / prev
            if rise >= threshold:
                return True
        prev = close
    return False


def _as_decimal(v) -> Optional[Decimal]:
    """任意数值/None 转 Decimal；None/NaN 返回 None"""
    if v is None or pd.isna(v):
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


# ---------------------------------------------------------------------------
# 单票评估（全部初筛条件，纯逻辑，便于单测）
# ---------------------------------------------------------------------------
def evaluate_stock(kline_df: Optional[pd.DataFrame], code: str, industry: str,
                   valuation_df: Optional[pd.DataFrame] = None) -> StockCandidate:
    """对单只股票应用 PRD 2.1 全部初筛条件，返回候选结果（含未通过明细）

    :param kline_df: 个股日 K 线（data_layer.get_kline 标准格式，可为 None）
    :param code: 证券代码
    :param industry: 行业名称（用于白名单校验）
    :param valuation_df: 估值历史（data_layer.get_valuation_history，可为 None，
                         此时 PE 分位条件记为不通过）
    """
    s = config.get_settings().tech
    failures: List[str] = []
    if kline_df is None or kline_df.empty:
        return StockCandidate(code=code, industry=industry, price=None,
                              pe_percentile=None, passed=False,
                              failures=["K 线数据为空"])
    try:
        add_all_indicators(kline_df)  # 补齐 ma/rsi/macd/volatility/max_drawdown 全部指标
    except Exception as e:
        return StockCandidate(code=code, industry=industry, price=None,
                              pe_percentile=None, passed=False,
                              failures=[], error=f"指标计算失败: {e}")

    latest = kline_df.iloc[-1]
    price = _as_decimal(latest["close"])

    # 1) 价格区间
    if price is None or not (s.price_min <= price <= s.price_max):
        failures.append(f"价格 {price} 不在 [{s.price_min}, {s.price_max}] 区间")

    # 2) 行业白名单
    if industry not in s.industries:
        failures.append(f"行业 {industry} 不在白名单 {s.industries}")

    # 3) 超跌底部形态：近 drawdown_window 日最大回撤 >= 30% 且 当前距低点反弹 <= 15%
    mdd = _as_decimal(latest["max_drawdown"])
    if mdd is None or mdd < s.max_drawdown_required:
        failures.append(f"近{s.drawdown_window}日最大回撤 {_fmt(mdd)} "
                        f"< {s.max_drawdown_required}")
    rebound_from_low: Optional[Decimal] = None
    if price is not None:
        window_closes = [_as_decimal(v) for v in
                         kline_df["close"].tail(s.drawdown_window)]
        valid = [v for v in window_closes if v is not None and v > 0]
        if valid:
            low60 = min(valid)
            rebound_from_low = (price - low60) / low60
            if rebound_from_low > s.rebound_from_low_max:
                failures.append(f"当前距低点反弹 {_fmt(rebound_from_low)} > {s.rebound_from_low_max}")

    # 4) 趋势确认：收 > MA20 且 MA5 > MA10 > MA20
    ma5, ma10, ma20 = (_as_decimal(latest["ma5"]), _as_decimal(latest["ma10"]),
                       _as_decimal(latest["ma20"]))
    if not (price is not None and ma20 is not None and price > ma20
            and ma5 is not None and ma10 is not None and ma5 > ma10 > ma20):
        failures.append("趋势未确认（需 收>MA20 且 MA5>MA10>MA20）")

    # 5) 股性验证：近3个月至少1次涨停 + 年化波动率 20%~60%
    if not _has_limit_up_recent(kline_df, s.limit_up_months, _limit_up_threshold(code)):
        failures.append(f"近{s.limit_up_months}个月无涨停（阈值 {_limit_up_threshold(code)}）")
    vol = _as_decimal(latest["volatility"])
    if vol is None or not (s.volatility_min <= vol <= s.volatility_max):
        failures.append(f"年化波动率 {_fmt(vol)} 不在 [{s.volatility_min}, {s.volatility_max}]")

    # 6) 估值指标：30 <= RSI14 <= 55 且 PE 近5年分位 <= 50%
    mr = config.get_settings().market_regime
    rsi_val = _as_decimal(latest[f"rsi{mr.rsi_period}"])
    if rsi_val is None or not (s.rsi_min <= rsi_val <= s.rsi_max):
        failures.append(f"RSI {_fmt(rsi_val)} 不在 [{s.rsi_min}, {s.rsi_max}]")

    # PE 分位计算（P4 修正：增加降级窗口 5→3→2 年，提高覆盖率）
    # 修正记录（P4）：百度股市通估值接口对部分中小盘股或次新股覆盖不足，
    # 5 年窗口内有效样本不够时返回 None。现按 5→3→2 年逐级降级尝试，
    # 并在日志中记录实际使用的窗口或缺失原因。
    pe_percentile: Optional[Decimal] = None
    pe_window_used: Optional[int] = None
    pe_missing_reason: str = ""
    if valuation_df is not None and not valuation_df.empty:
        # 降级窗口序列：配置年限 → 3 年 → 2 年（去重且降序）
        fallback_windows = sorted(set([s.pe_percentile_years, 3, 2]), reverse=True)
        for years in fallback_windows:
            try:
                valuation_percentile(valuation_df, column="pe", years=years)
                col = f"pe_percentile_{years}y"
                val = _as_decimal(valuation_df[col].iloc[-1])
                if val is not None:
                    pe_percentile = val
                    pe_window_used = years
                    break
            except Exception:
                continue
        if pe_percentile is None:
            pe_missing_reason = f"估值数据仅 {len(valuation_df)} 行，全部窗口均无法计算"
            print(f"[stock_screener] {code} PE 分位缺失: {pe_missing_reason}")
    else:
        pe_missing_reason = "估值数据为空（百度接口无数据或拉取失败）"
        print(f"[stock_screener] {code} PE 分位缺失: {pe_missing_reason}")

    if pe_percentile is None or pe_percentile > s.pe_percentile_max:
        window_info = f"（{pe_window_used}年窗口）" if pe_window_used else f"（{pe_missing_reason}）"
        failures.append(f"PE 近{s.pe_percentile_years}年分位 {_fmt(pe_percentile)} "
                        f"> {s.pe_percentile_max} {window_info}")

    return StockCandidate(code=code, industry=industry, price=price,
                          pe_percentile=pe_percentile,
                          passed=not failures, failures=failures,
                          # 指标快照回填（P4 修正：供 Layer 4 AI 解读引用）
                          ma5=ma5, ma10=ma10, ma20=ma20,
                          rsi=rsi_val,
                          macd_dif=_as_decimal(latest["macd_dif"]),
                          macd_dea=_as_decimal(latest["macd_dea"]),
                          macd_hist=_as_decimal(latest["macd_hist"]),
                          max_drawdown=mdd, volatility=vol,
                          rebound_from_low=rebound_from_low)


def _fmt(v: Optional[Decimal]) -> str:
    """Decimal/None 格式化输出（四舍五入 4 位小数，None 显示 -）"""
    return "-" if v is None else f"{v.quantize(Decimal('0.0001'))}"


# ---------------------------------------------------------------------------
# 选股池构建与主流程
# ---------------------------------------------------------------------------
def fetch_industry_pool() -> Dict[str, str]:
    """按行业白名单拉取成分股：返回 {code: industry}（东财行业板块接口）

    修正记录（P2）：东财接口当前存在反爬风险，失败时打印告警并跳过该行业；
    全部行业失败则抛 RuntimeError 提示改用手工股票池（screen_tech_stocks(stock_pool=...)）。
    """
    try:
        import akshare as ak
    except ImportError:
        raise RuntimeError("AKShare 未安装，无法拉取行业成分股，请手工传入 stock_pool")
    s = config.get_settings().tech
    pool: Dict[str, str] = {}
    for industry in s.industries:
        try:
            cons = ak.stock_board_industry_cons_em(symbol=industry)
        except Exception as e:
            print(f"[stock_screener] 行业 {industry} 成分股拉取失败: {e}")
            continue
        if "代码" not in cons.columns:
            print(f"[stock_screener] 行业 {industry} 返回缺少「代码」列，跳过")
            continue
        for code in cons["代码"]:
            pool[str(code)] = industry
    if not pool:
        raise RuntimeError(
            "行业成分股全部拉取失败（东财接口反爬或网络异常），"
            "请稍后重试或手工传入 stock_pool")
    return pool


def screen_tech_stocks(stock_pool: Optional[Dict[str, str]] = None) -> List[StockCandidate]:
    """科技股初筛主流程：逐只拉取 K 线与估值并应用全部条件

    :param stock_pool: {code: industry} 候选池，缺省按行业白名单自动拉取
    :return: 全部候选结果（含未通过项与失败原因），调用方按 passed 过滤
    """
    if stock_pool is None:
        stock_pool = fetch_industry_pool()
    results: List[StockCandidate] = []
    for code, industry in stock_pool.items():
        # K 线拉取失败直接记录（data_layer 已含重试与缓存降级）
        try:
            kline = get_kline(code)
        except DataFetchError as e:
            results.append(StockCandidate(code=code, industry=industry, price=None,
                                          pe_percentile=None, passed=False,
                                          failures=[], error=f"K 线拉取失败: {e}"))
            continue
        # 估值拉取失败不阻塞初筛（PE 条件记为不通过）
        valuation: Optional[pd.DataFrame] = None
        try:
            valuation = get_valuation_history(code)
        except DataFetchError as e:
            print(f"[stock_screener] {code} 估值历史拉取失败（PE 条件视为不通过）: {e}")
        results.append(evaluate_stock(kline, code, industry, valuation))
    return results


# ---------------------------------------------------------------------------
# 命令行自检：python src/stock_screener.py
# ---------------------------------------------------------------------------
def _self_check() -> None:
    """自检入口：用构造行情验证评估逻辑（不访问网络）"""
    print("[OK] 选股模块自检（构造数据，不访问网络）")
    # 构造一只「温和上涨、无大回撤无涨停」的股票，预期多项初筛条件不通过
    # 修正记录（P2）：原注释仅提超跌与 PE 两项，实际失败项含回撤/涨停/波动率/RSI/PE
    # 共 5 项（涨速过慢不涨停、波动率过低、单边上涨 RSI=100），注释已修正为如实描述。
    n = 90
    base = [Decimal("4.5") + Decimal(i) * Decimal("0.01") for i in range(n)]
    kline = pd.DataFrame({
        "trade_date": [f"2026-05-{i % 28 + 1:02d}" for i in range(n)],
        "open": base, "high": [c + Decimal("0.1") for c in base],
        "low": [c - Decimal("0.1") for c in base], "close": base,
        "volume": [1000.0] * n,
    })
    cand = evaluate_stock(kline, "000001", "电子")
    print(f"[OK] 构造股评估: passed={cand.passed}, failures={cand.failures}")
    print(f"[OK] 真实筛选请调用: screen_tech_stocks()")


if __name__ == "__main__":
    _self_check()
