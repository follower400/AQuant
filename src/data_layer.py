"""
data_layer.py —— Layer 1 数据层：AKShare 行情获取 + SQLite 本地缓存

职责边界（遵循 .coderule 与 PROJECT_PLAN.md Layer 1 规划）：
- 仅负责数据获取、清洗与缓存，不包含任何策略判断（策略逻辑属于 Layer 3）；
- 缓存策略：优先读 SQLite 缓存 -> 缓存过期或无数据时调用 AKShare 并更新缓存；
  拉取失败时降级读取过期缓存并告警，无缓存可读则抛 DataFetchError；
- 价格/成交额字段以 Decimal 字符串（TEXT）落库，规避浮点精度丢失；
- 本层严禁调用 AI，严禁写死策略阈值（参数一律来自 config.get_settings()）。

数据表 Schema（SQLite）：
  1. kline      —— 日 K 线主表，(code, trade_date) 联合主键
       code        TEXT  证券代码（如 sh000300 / 000001）
       trade_date  TEXT  交易日（YYYY-MM-DD）
       open/high/low/close  TEXT  价格（Decimal 字符串）
       volume      REAL  成交量（股）
       amount      TEXT  成交额（元，Decimal 字符串，可为 NULL）
  2. valuation  —— 个股估值历史表，(code, trade_date) 联合主键（P2 新增）
       code        TEXT  证券代码（如 000001）
       trade_date  TEXT  日期（YYYY-MM-DD，自然日序列）
       pe          TEXT  市盈率(TTM)，Decimal 字符串（亏损为负，缺失为 NULL）
       pb          TEXT  市净率，Decimal 字符串（可为负，缺失为 NULL）
  3. cache_meta —— 缓存元信息表，(code, data_type) 联合主键（P2 由 code 单主键迁移）
       code        TEXT  证券代码
       data_type   TEXT  数据类型（'kline' / 'valuation'）
       updated_at  TEXT  最近刷新时间（ISO8601，用于过期判断）

用法示例：
    from data_layer import get_kline, get_valuation_history
    df = get_kline("sh000300", is_index=True)      # 沪深300 日线（优先缓存）
    df = get_kline("000001", refresh=True)         # 强制刷新个股日线
    df = get_kline("sh000300", start_date="2026-01-01", end_date="2026-08-21")
    val = get_valuation_history("000001")          # 个股 PE(TTM)/PB 历史（优先缓存）
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

# 项目根目录注册到 sys.path：支持 `python src/data_layer.py` 与 `python -m src.data_layer`
# 两种运行方式都能找到根目录下的 config 模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config

try:
    import akshare as ak
except ImportError:  # AKShare 未安装时允许模块导入，拉取函数调用时抛出明确错误
    ak = None


class DataFetchError(RuntimeError):
    """数据获取失败：AKShare 拉取失败且无缓存可降级时抛出"""


# ---------------------------------------------------------------------------
# 缓存库路径与 Schema
# ---------------------------------------------------------------------------
DB_PATH: Path = config.PROJECT_ROOT / "data" / "market_cache.db"

# 建表语句：K 线主表 + 估值历史表 + 缓存元信息表 + 索引
# 修正记录（P2）：cache_meta 主键由 code 单主键调整为 (code, data_type) 复合主键，
# 以区分 kline 与 valuation 两类缓存的新鲜度；P1 旧库由 _migrate_cache_meta 无损迁移，
# 影响面：仅 data_layer 内部缓存新鲜度判断与元信息写入。
_CACHE_META_TABLE_SQL: str = """
    CREATE TABLE IF NOT EXISTS cache_meta (
        code       TEXT NOT NULL,
        data_type  TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (code, data_type)
    )
    """

_SCHEMA_SQL: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS kline (
        code       TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open       TEXT NOT NULL,
        high       TEXT NOT NULL,
        low        TEXT NOT NULL,
        close      TEXT NOT NULL,
        volume     REAL NOT NULL,
        amount     TEXT,
        PRIMARY KEY (code, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kline_code_date ON kline (code, trade_date)",
    """
    CREATE TABLE IF NOT EXISTS valuation (
        code       TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        pe         TEXT,
        pb         TEXT,
        PRIMARY KEY (code, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_valuation_code_date ON valuation (code, trade_date)",
    _CACHE_META_TABLE_SQL,
)

# AKShare 不同接口的列名 -> 内部标准列名 映射表
_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "trade_date": ("date", "日期"),
    "open": ("open", "开盘"),
    "high": ("high", "最高"),
    "low": ("low", "最低"),
    "close": ("close", "收盘"),
    "volume": ("volume", "成交量"),
    "amount": ("amount", "成交额"),
}

# 价格类列（必须为 Decimal 字符串）
_PRICE_COLUMNS: Tuple[str, ...] = ("open", "high", "low", "close", "amount")

# 估值类列（必须为 Decimal 字符串；负值合法，缺失为 NULL）
_VALUATION_COLUMNS: Tuple[str, ...] = ("pe", "pb")

# 百度股市通估值指标名 -> 内部字段名（PE 采用 TTM 口径，与 PRD 分位计算约定一致）
_VALUATION_INDICATORS: Dict[str, str] = {
    "pe": "市盈率(TTM)",
    "pb": "市净率",
}


# ---------------------------------------------------------------------------
# SQLite 连接管理
# ---------------------------------------------------------------------------
_conn: Optional[sqlite3.Connection] = None


def _get_connection() -> sqlite3.Connection:
    """获取全局 SQLite 连接（首次调用时建目录、建表、执行旧库迁移）"""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH))
        _conn.row_factory = sqlite3.Row
        for sql in _SCHEMA_SQL:
            _conn.execute(sql)
        _conn.commit()
        _migrate_cache_meta(_conn)  # P2 新增：兼容 P1 旧库的 cache_meta 结构
    return _conn


def _migrate_cache_meta(conn: sqlite3.Connection) -> None:
    """迁移 cache_meta 旧结构（code 单主键）到复合主键 (code, data_type)

    修正记录（P2）：P1 版本的 cache_meta 主键只有 code，无法区分 kline 与
    valuation 两类缓存；迁移步骤为 旧表改名 -> 建新表 -> 旧数据以
    data_type='kline' 迁入 -> 删除旧表，全程单事务保证原子性。
    影响面：仅 data_layer 内部的缓存新鲜度判断与元信息写入。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cache_meta'").fetchone()
    if row is None or "PRIMARY KEY (code, data_type)" in (row["sql"] or ""):
        return  # 无表或已是新结构，无需迁移
    with conn:
        conn.execute("ALTER TABLE cache_meta RENAME TO cache_meta_old")
        conn.execute(_CACHE_META_TABLE_SQL)
        conn.execute(
            "INSERT INTO cache_meta (code, data_type, updated_at) "
            "SELECT code, 'kline', updated_at FROM cache_meta_old")
        conn.execute("DROP TABLE cache_meta_old")


def close() -> None:
    """关闭全局 SQLite 连接（程序退出时调用）"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# 数据标准化
# ---------------------------------------------------------------------------
def _to_decimal_str(x: Any) -> Optional[str]:
    """数值转 Decimal 字符串（保留精度）；缺失值返回 None"""
    if x is None or pd.isna(x):
        return None
    return str(Decimal(str(x)))


def _normalize_kline(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """将 AKShare 不同接口（指数/个股）返回的 DataFrame 统一为标准列名与格式

    标准化规则：列名映射、交易日统一 YYYY-MM-DD、价格转 Decimal 字符串、
    按日期升序去重。
    """
    # 1) 列名映射到内部标准名
    rename_map = {}
    for std_name, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in df.columns:
                rename_map[alias] = std_name
                break
    df = df.rename(columns=rename_map)

    # 2) 必需列校验
    required = ("trade_date", "open", "high", "low", "close", "volume")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DataFetchError(f"AKShare 返回数据缺少必需列: {missing}（code={code}）")

    # 3) 仅保留标准列并统一类型
    keep = list(required) + (["amount"] if "amount" in df.columns else [])
    df = df[keep].copy()
    df["trade_date"] = df["trade_date"].map(lambda d: str(d)[:10])
    for col in _PRICE_COLUMNS:
        if col in df.columns:
            df[col] = df[col].map(_to_decimal_str)
    df["volume"] = df["volume"].astype(float)
    df = df.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
    df.reset_index(drop=True, inplace=True)

    # 修正记录（P2 冒烟测试）：新浪前复权序列早期存在非正收盘价（实测 000001
    # 1991 年复权价为 -3.08 且含 0 值行），非正价格无收益意义，且会导致因子层
    # 收益率计算除零（DivisionByZero）崩溃；落库前过滤。
    # 影响面：仅 data_layer 数据清洗路径，正常正价数据不受影响。
    valid = df["close"].map(lambda s: s is not None and Decimal(s) > 0)
    if not valid.all():
        print(f"[data_layer] 清洗非正收盘价 {int((~valid).sum())} 行（code={code}）")
        df = df[valid].reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# AKShare 拉取（带重试）
# ---------------------------------------------------------------------------
def _fetch_index_daily(index_code: str) -> pd.DataFrame:
    """调用 AKShare 获取指数日线（如 sh000300 沪深300）"""
    if ak is None:
        raise DataFetchError("AKShare 未安装，请先执行: pip install akshare")
    raw = ak.stock_zh_index_daily(symbol=index_code)
    return _normalize_kline(raw, index_code)


def _fetch_stock_daily(stock_code: str, adjust: str = "qfq") -> pd.DataFrame:
    """调用 AKShare 获取个股日线（默认前复权）

    修正记录（P2）：东财接口 stock_zh_a_hist 存在反爬风险（实测会抛
    RemoteDisconnected），失败时自动降级新浪备用源 stock_zh_a_daily；
    影响面：仅个股 K 线拉取路径，指数路径与缓存逻辑不变。
    """
    if ak is None:
        raise DataFetchError("AKShare 未安装，请先执行: pip install akshare")
    try:
        raw = ak.stock_zh_a_hist(
            symbol=stock_code, period="daily",
            start_date="19900101", end_date="20500101", adjust=adjust,
        )
        return _normalize_kline(raw, stock_code)
    except Exception as e:
        # print(f"[data_layer] 东财个股接口失败，降级新浪备用源: {e}")
        return _fetch_stock_daily_sina(stock_code, adjust)


def _sina_symbol(stock_code: str) -> str:
    """纯数字代码转新浪格式（带交易所前缀）：60/68->sh，00/30->sz，其余北交所->bj"""
    if stock_code.startswith(("60", "68")):
        return f"sh{stock_code}"
    if stock_code.startswith(("00", "30")):
        return f"sz{stock_code}"
    return f"bj{stock_code}"


def _fetch_stock_daily_sina(stock_code: str, adjust: str = "qfq") -> pd.DataFrame:
    """新浪备用源获取个股日线（stock_zh_a_daily，需 sz/sh/bj 前缀）

    修正记录（P2）：东财接口反爬时的降级方案；接口缺失时抛明确升级提示。
    """
    if ak is None:
        raise DataFetchError("AKShare 未安装，请先执行: pip install akshare")
    fetch_fn = getattr(ak, "stock_zh_a_daily", None)
    if fetch_fn is None:
        raise DataFetchError(
            "当前 AKShare 版本不支持 stock_zh_a_daily 备用源，"
            "请升级: pip install -U akshare")
    raw = fetch_fn(symbol=_sina_symbol(stock_code),
                   start_date="19900101", end_date="20500101", adjust=adjust)
    return _normalize_kline(raw, stock_code)


def _fetch_with_retry(fetch_fn, retry_times: int) -> pd.DataFrame:
    """带重试的 AKShare 拉取；全部失败时抛 DataFetchError"""
    last_err: Optional[Exception] = None
    for attempt in range(1, retry_times + 1):
        try:
            return fetch_fn()
        except DataFetchError:
            raise  # 参数/列缺失类错误无需重试
        except Exception as e:  # 网络波动类错误按配置重试
            last_err = e
            print(f"[data_layer] AKShare 拉取失败（第 {attempt}/{retry_times} 次）: {e}")
    raise DataFetchError(f"AKShare 拉取失败，重试 {retry_times} 次仍失败: {last_err}")


# ---------------------------------------------------------------------------
# SQLite 缓存读写
# ---------------------------------------------------------------------------
def _is_cache_fresh(conn: sqlite3.Connection, code: str, expire_days: int,
                    data_type: str = "kline") -> bool:
    """缓存是否新鲜：cache_meta.updated_at 距今未超过 expire_days 天

    :param data_type: 数据类型（'kline' / 'valuation'），P2 起 K 线与估值缓存独立判新鲜
    """
    row = conn.execute(
        "SELECT updated_at FROM cache_meta WHERE code = ? AND data_type = ?",
        (code, data_type)).fetchone()
    if row is None:
        return False
    updated = datetime.fromisoformat(row["updated_at"])
    return datetime.now() - updated < timedelta(days=expire_days)


def _read_kline_cache(conn: sqlite3.Connection, code: str,
                      start_date: Optional[str] = None,
                      end_date: Optional[str] = None) -> pd.DataFrame:
    """从 SQLite 读取缓存 K 线（可按日期范围过滤）；价格列还原为 Decimal 对象"""
    sql = ("SELECT trade_date, open, high, low, close, volume, amount "
           "FROM kline WHERE code = ?")
    params: list = [code]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date"
    df = pd.read_sql_query(sql, conn, params=params)
    for col in _PRICE_COLUMNS:
        df[col] = df[col].map(lambda s: Decimal(s) if s is not None else None)
    # 修正记录（P2 冒烟测试）：P1 旧缓存可能含非正收盘价脏数据（新浪前复权早期
    # 复权价为负/为 0），读取时过滤，与 _normalize_kline 写侧过滤互为兜底；
    # 影响面：读取路径返回的数据恒为正价，样本量略减不影响 MA250 等窗口指标。
    df = df[df["close"].map(lambda v: v is not None and v > 0)].reset_index(drop=True)
    return df


def _write_kline_cache(conn: sqlite3.Connection, code: str, df: pd.DataFrame) -> None:
    """全量覆盖写入指定标的的 K 线缓存，并刷新缓存元信息（事务保证原子性）"""
    rows = [
        # amount 为可选列（指数接口可能缺失），缺失时存 NULL
        (code, row.trade_date, str(row.open), str(row.high), str(row.low), str(row.close),
         float(row.volume),
         str(getattr(row, "amount", None)) if getattr(row, "amount", None) is not None else None)
        for row in df.itertuples(index=False)
    ]
    with conn:  # 进入事务：删除旧数据 -> 写入新数据 -> 更新时间戳
        conn.execute("DELETE FROM kline WHERE code = ?", (code,))
        conn.executemany(
            "INSERT INTO kline (code, trade_date, open, high, low, close, volume, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (code, data_type, updated_at) "
            "VALUES (?, 'kline', ?)",
            (code, datetime.now().isoformat(timespec="seconds")),
        )


# ---------------------------------------------------------------------------
# 估值历史（P2 新增）：AKShare 拉取 + SQLite 缓存
# ---------------------------------------------------------------------------
def _normalize_valuation(pe_df: pd.DataFrame, pb_df: pd.DataFrame,
                         code: str) -> pd.DataFrame:
    """将百度股市通 PE/PB 两个时间序列合并为标准估值表

    合并规则：两个接口调用返回的日期集合可能不完全一致，按日期外连接
    （缺失侧为 NULL）；值转 Decimal 字符串；按日期升序去重。
    """
    pe_s = pe_df.rename(columns={"date": "trade_date", "value": "pe"})
    pb_s = pb_df.rename(columns={"date": "trade_date", "value": "pb"})
    # merge 前统一把日期转字符串，避免 datetime.date 与 str 混合导致对齐失败
    pe_s["trade_date"] = pe_s["trade_date"].map(lambda d: str(d)[:10])
    pb_s["trade_date"] = pb_s["trade_date"].map(lambda d: str(d)[:10])
    merged = pd.merge(pe_s, pb_s, on="trade_date", how="outer")
    for col in _VALUATION_COLUMNS:
        merged[col] = merged[col].map(_to_decimal_str)
    merged = merged.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
    merged.reset_index(drop=True, inplace=True)
    return merged


def _fetch_valuation_history(code: str, period: str) -> pd.DataFrame:
    """调用 AKShare 获取个股 PE(TTM)/PB 历史序列（百度股市通口径）

    一次调用只返回一个指标序列（date/value 两列），此处分别拉取 PE 与 PB
    后按日期合并；接口为 AKShare 1.18.x 提供的 stock_zh_valuation_baidu，
    若缺失则抛明确错误提示升级。
    """
    if ak is None:
        raise DataFetchError("AKShare 未安装，请先执行: pip install akshare")
    fetch_fn = getattr(ak, "stock_zh_valuation_baidu", None)
    if fetch_fn is None:
        raise DataFetchError(
            "当前 AKShare 版本不支持 stock_zh_valuation_baidu 估值接口，"
            "请升级: pip install -U akshare")
    pe_df = fetch_fn(symbol=code, indicator=_VALUATION_INDICATORS["pe"], period=period)
    pb_df = fetch_fn(symbol=code, indicator=_VALUATION_INDICATORS["pb"], period=period)
    return _normalize_valuation(pe_df, pb_df, code)


def _read_valuation_cache(conn: sqlite3.Connection, code: str,
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None) -> pd.DataFrame:
    """从 SQLite 读取缓存估值序列（可按日期范围过滤）；pe/pb 还原为 Decimal"""
    sql = "SELECT trade_date, pe, pb FROM valuation WHERE code = ?"
    params: list = [code]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date"
    df = pd.read_sql_query(sql, conn, params=params)
    for col in _VALUATION_COLUMNS:
        df[col] = df[col].map(lambda s: Decimal(s) if s is not None else None)
    return df


def _write_valuation_cache(conn: sqlite3.Connection, code: str,
                           df: pd.DataFrame) -> None:
    """全量覆盖写入指定标的的估值缓存，并刷新缓存元信息（data_type='valuation'）"""
    rows = [
        # pe/pb 为可选列（某日可能缺某个指标），缺失时存 NULL；负值合法
        (code, str(row.trade_date)[:10],
         str(getattr(row, "pe", None)) if getattr(row, "pe", None) is not None else None,
         str(getattr(row, "pb", None)) if getattr(row, "pb", None) is not None else None)
        for row in df.itertuples(index=False)
    ]
    with conn:  # 事务：删除旧数据 -> 写入新数据 -> 更新时间戳
        conn.execute("DELETE FROM valuation WHERE code = ?", (code,))
        conn.executemany(
            "INSERT INTO valuation (code, trade_date, pe, pb) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (code, data_type, updated_at) "
            "VALUES (?, 'valuation', ?)",
            (code, datetime.now().isoformat(timespec="seconds")),
        )


def get_valuation_history(code: str, start_date: Optional[str] = None,
                          end_date: Optional[str] = None,
                          refresh: bool = False) -> pd.DataFrame:
    """获取个股估值历史 PE(TTM)/PB（优先读缓存，过期或无数据时调用 AKShare 并更新缓存）

    :param code: 证券代码（如 000001）
    :param start_date: 起始日期（YYYY-MM-DD，可选）
    :param end_date: 截止日期（YYYY-MM-DD，可选）
    :param refresh: True 时忽略缓存新鲜度，强制从 AKShare 拉取并更新缓存
    :return: DataFrame，列为 trade_date/pe/pb，值为 Decimal（缺失为 None）
    :raises DataFetchError: 拉取失败且无任何缓存可降级时抛出
    """
    settings = config.get_settings()
    expire_days = settings.operation.cache_expire_days
    retry_times = settings.operation.data_retry_times
    period = settings.operation.valuation_history_period
    conn = _get_connection()

    # 1) 缓存命中且新鲜 -> 直接返回（按需过滤日期范围）
    if not refresh and _is_cache_fresh(conn, code, expire_days, data_type="valuation"):
        cached = _read_valuation_cache(conn, code, start_date, end_date)
        if not cached.empty:
            return cached

    # 2) 缓存过期或无数据 -> 调用 AKShare（带重试）并更新缓存
    try:
        fresh = _fetch_with_retry(lambda: _fetch_valuation_history(code, period), retry_times)
    except DataFetchError:
        # 3) 降级：拉取失败但存在历史缓存（即使已过期）-> 返回并告警
        cached = _read_valuation_cache(conn, code, start_date, end_date)
        if not cached.empty:
            print(f"[data_layer] 警告：估值数据拉取失败，已降级返回 {code} 的过期缓存")
            return cached
        raise

    if fresh.empty:
        raise DataFetchError(f"AKShare 返回空估值数据: {code}")
    _write_valuation_cache(conn, code, fresh)
    return _read_valuation_cache(conn, code, start_date, end_date)


# ---------------------------------------------------------------------------
# 对外主接口
# ---------------------------------------------------------------------------
def get_kline(code: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
              refresh: bool = False, is_index: bool = False) -> pd.DataFrame:
    """获取日 K 线（优先读缓存，过期或无数据时调用 AKShare 并更新缓存）

    :param code: 证券代码，指数如 sh000300，个股如 000001
    :param start_date: 起始日期（YYYY-MM-DD，可选）
    :param end_date: 截止日期（YYYY-MM-DD，可选）
    :param refresh: True 时忽略缓存新鲜度，强制从 AKShare 拉取并更新缓存
    :param is_index: True 走指数接口，否则走个股接口
    :return: DataFrame，列为 trade_date/open/high/low/close/volume/amount，
             其中价格列为 Decimal 对象（金额/价格遵循 Decimal 规范）
    :raises DataFetchError: 拉取失败且无任何缓存可降级时抛出
    """
    settings = config.get_settings()
    expire_days = settings.operation.cache_expire_days
    retry_times = settings.operation.data_retry_times
    conn = _get_connection()

    # 1) 缓存命中且新鲜 -> 直接返回（按需过滤日期范围）
    if not refresh and _is_cache_fresh(conn, code, expire_days):
        cached = _read_kline_cache(conn, code, start_date, end_date)
        if not cached.empty:
            return cached

    # 2) 缓存过期或无数据 -> 调用 AKShare（带重试）并更新缓存
    fetch_fn = (lambda: _fetch_index_daily(code)) if is_index \
        else (lambda: _fetch_stock_daily(code))
    try:
        fresh = _fetch_with_retry(fetch_fn, retry_times)
    except DataFetchError:
        # 3) 降级：拉取失败但存在历史缓存（即使已过期）-> 返回并告警
        cached = _read_kline_cache(conn, code, start_date, end_date)
        if not cached.empty:
            print(f"[data_layer] 警告：AKShare 拉取失败，已降级返回 {code} 的过期缓存")
            return cached
        raise

    if fresh.empty:
        raise DataFetchError(f"AKShare 返回空数据: {code}")
    _write_kline_cache(conn, code, fresh)
    return _read_kline_cache(conn, code, start_date, end_date)


# ---------------------------------------------------------------------------
# 命令行自检：python src/data_layer.py
# ---------------------------------------------------------------------------
def _self_check() -> None:
    """自检入口：验证数据库初始化、缓存读写链路（不访问网络）"""
    conn = _get_connection()
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"[OK] SQLite 缓存库: {DB_PATH}")
    print(f"[OK] 数据表: {tables}")
    # 构造一条假数据验证 写入 -> 读取 闭环（价格 Decimal 精度保持）
    demo = pd.DataFrame([{
        "trade_date": "2026-08-21", "open": "10.10", "high": "10.50",
        "low": "10.00", "close": "10.35", "volume": 123456.0, "amount": "1277126.50",
    }])
    _write_kline_cache(conn, "_demo", demo)
    back = _read_kline_cache(conn, "_demo")
    conn.execute("DELETE FROM kline WHERE code = '_demo'")
    conn.execute("DELETE FROM cache_meta WHERE code = '_demo'")
    conn.commit()
    assert back.loc[0, "close"] == Decimal("10.35"), "Decimal 精度保持失败"
    print(f"[OK] 缓存读写闭环验证通过（close 精度保持: {back.loc[0, 'close']}）")
    # 修正记录（P2）：自检补充估值表写入 -> 读取闭环验证（pe/pb Decimal 精度保持）
    demo_val = pd.DataFrame([{
        "trade_date": "2026-08-21", "pe": "5.20", "pb": "0.60",
    }])
    _write_valuation_cache(conn, "_demo", demo_val)
    back_val = _read_valuation_cache(conn, "_demo")
    conn.execute("DELETE FROM valuation WHERE code = '_demo'")
    conn.execute("DELETE FROM cache_meta WHERE code = '_demo' AND data_type = 'valuation'")
    conn.commit()
    assert back_val.loc[0, "pe"] == Decimal("5.20"), "估值 pe Decimal 精度保持失败"
    assert back_val.loc[0, "pb"] == Decimal("0.60"), "估值 pb Decimal 精度保持失败"
    print(f"[OK] 估值缓存读写闭环验证通过（pe/pb 精度保持: "
          f"{back_val.loc[0, 'pe']} / {back_val.loc[0, 'pb']}）")
    print(f"[OK] 真实拉取请调用: get_kline('sh000300', is_index=True) 或 "
          f"get_valuation_history('000001')")


if __name__ == "__main__":
    _self_check()
