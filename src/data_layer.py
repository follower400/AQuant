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
  2. cache_meta —— 缓存元信息表
       code        TEXT  证券代码（主键）
       data_type   TEXT  数据类型（当前固定 'kline'）
       updated_at  TEXT  最近刷新时间（ISO8601，用于过期判断）

用法示例：
    from data_layer import get_kline
    df = get_kline("sh000300", is_index=True)      # 沪深300 日线（优先缓存）
    df = get_kline("000001", refresh=True)         # 强制刷新个股日线
    df = get_kline("sh000300", start_date="2026-01-01", end_date="2026-08-21")
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

# 建表语句：K 线主表 + 缓存元信息表 + 索引
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
    CREATE TABLE IF NOT EXISTS cache_meta (
        code       TEXT PRIMARY KEY,
        data_type  TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
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


# ---------------------------------------------------------------------------
# SQLite 连接管理
# ---------------------------------------------------------------------------
_conn: Optional[sqlite3.Connection] = None


def _get_connection() -> sqlite3.Connection:
    """获取全局 SQLite 连接（首次调用时建目录、建表）"""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH))
        _conn.row_factory = sqlite3.Row
        for sql in _SCHEMA_SQL:
            _conn.execute(sql)
        _conn.commit()
    return _conn


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
    """调用 AKShare 获取个股日线（默认前复权）"""
    if ak is None:
        raise DataFetchError("AKShare 未安装，请先执行: pip install akshare")
    raw = ak.stock_zh_a_hist(
        symbol=stock_code, period="daily",
        start_date="19900101", end_date="20500101", adjust=adjust,
    )
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
def _is_cache_fresh(conn: sqlite3.Connection, code: str, expire_days: int) -> bool:
    """缓存是否新鲜：cache_meta.updated_at 距今未超过 expire_days 天"""
    row = conn.execute("SELECT updated_at FROM cache_meta WHERE code = ?", (code,)).fetchone()
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
    print(f"[OK] 真实拉取请调用: get_kline('sh000300', is_index=True)")


if __name__ == "__main__":
    _self_check()
