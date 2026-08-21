"""数据层单元测试（Layer 1 data_layer.py）—— 全部离线，不依赖网络与 AKShare

覆盖范围：Schema 建表、缓存读写闭环（Decimal 精度）、缓存新鲜度判定、
缓存命中短路、拉取失败降级、无缓存可降级时抛错。
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

import src.data_layer as dl


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """每个测试使用独立临时数据库，避免污染真实缓存 data/market_cache.db"""
    dl.close()  # 关闭可能已存在的全局连接
    monkeypatch.setattr(dl, "DB_PATH", tmp_path / "test_cache.db")
    dl._conn = None  # 重置全局连接，指向临时库
    conn = dl._get_connection()
    yield conn
    dl.close()
    dl._conn = None


def make_kline_df(n=10, base=10.0):
    """构造标准 K 线 DataFrame（价格列 Decimal 字符串/对象均可）"""
    return pd.DataFrame({
        "trade_date": [f"2026-01-{i + 1:02d}" for i in range(n)],
        "open": [Decimal(str(base + i)) for i in range(n)],
        "high": [Decimal(str(base + i + 0.5)) for i in range(n)],
        "low": [Decimal(str(base + i - 0.5)) for i in range(n)],
        "close": [Decimal(str(base + i)) for i in range(n)],
        "volume": [1000.0 + i for i in range(n)],
        "amount": [Decimal(str(10000.5 + i)) for i in range(n)],
    })


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
class TestSchema:
    def test_tables_created(self, fresh_db):
        """kline 与 cache_meta 两张表应自动创建"""
        tables = {r[0] for r in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"kline", "cache_meta"} <= tables

    def test_kline_primary_key(self, fresh_db):
        """(code, trade_date) 联合主键：重复写入应冲突"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df())
        with pytest.raises(Exception):
            fresh_db.execute(
                "INSERT INTO kline (code, trade_date, open, high, low, close, volume, amount) "
                "VALUES ('sh000300', '2026-01-01', '1', '1', '1', '1', 1, '1')")


# ---------------------------------------------------------------------------
# 缓存读写闭环
# ---------------------------------------------------------------------------
class TestCacheRoundtrip:
    def test_write_read_decimal_precision(self, fresh_db):
        """写入 -> 读取后价格列必须保持 Decimal 精度"""
        df = make_kline_df()
        dl._write_kline_cache(fresh_db, "sh000300", df)
        back = dl._read_kline_cache(fresh_db, "sh000300")
        assert len(back) == len(df)
        assert back.loc[0, "close"] == Decimal("10")
        assert back.loc[0, "amount"] == Decimal("10000.5")
        assert isinstance(back.loc[0, "close"], Decimal)

    def test_rewrite_overwrites_old(self, fresh_db):
        """重复写入应全量覆盖旧数据（不产生重复行）"""
        dl._write_kline_cache(fresh_db, "sz000001", make_kline_df(5))
        dl._write_kline_cache(fresh_db, "sz000001", make_kline_df(8))
        back = dl._read_kline_cache(fresh_db, "sz000001")
        assert len(back) == 8

    def test_date_range_filter(self, fresh_db):
        """按日期范围过滤应只返回区间内数据"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df(10))
        part = dl._read_kline_cache(fresh_db, "sh000300",
                                    start_date="2026-01-03", end_date="2026-01-05")
        assert part["trade_date"].tolist() == ["2026-01-03", "2026-01-04", "2026-01-05"]


# ---------------------------------------------------------------------------
# 缓存新鲜度
# ---------------------------------------------------------------------------
class TestCacheFreshness:
    def test_fresh_after_write(self, fresh_db):
        """写入后缓存应为新鲜（updated_at 为当前时间）"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df())
        assert dl._is_cache_fresh(fresh_db, "sh000300", expire_days=5)

    def test_expired_when_old_timestamp(self, fresh_db):
        """updated_at 距今超过 expire_days 应判定为过期"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df())
        old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        fresh_db.execute("UPDATE cache_meta SET updated_at = ? WHERE code = 'sh000300'",
                         (old,))
        fresh_db.commit()
        assert not dl._is_cache_fresh(fresh_db, "sh000300", expire_days=5)

    def test_unknown_code_not_fresh(self, fresh_db):
        """无缓存记录的标的应判定为不新鲜"""
        assert not dl._is_cache_fresh(fresh_db, "sh000300", expire_days=5)


# ---------------------------------------------------------------------------
# get_kline 主流程（ak 置 None，模拟未安装/断网）
# ---------------------------------------------------------------------------
class TestGetKline:
    def test_cache_hit_short_circuit(self, fresh_db, monkeypatch):
        """缓存新鲜时直接返回，不触发任何 AKShare 调用（ak=None 也不报错）"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df())
        monkeypatch.setattr(dl, "ak", None)
        df = dl.get_kline("sh000300", is_index=True)
        assert len(df) == 10
        assert df.loc[0, "close"] == Decimal("10")

    def test_no_cache_and_fetch_fails_raises(self, fresh_db, monkeypatch):
        """无缓存且拉取失败（ak=None）应抛 DataFetchError"""
        monkeypatch.setattr(dl, "ak", None)
        with pytest.raises(dl.DataFetchError):
            dl.get_kline("sh000300", is_index=True)

    def test_stale_cache_fallback_on_failure(self, fresh_db, monkeypatch):
        """缓存过期且拉取失败时降级返回过期缓存（不抛错）"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df())
        old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        fresh_db.execute("UPDATE cache_meta SET updated_at = ? WHERE code = 'sh000300'",
                         (old,))
        fresh_db.commit()
        monkeypatch.setattr(dl, "ak", None)
        df = dl.get_kline("sh000300", is_index=True)
        assert len(df) == 10  # 降级返回过期缓存

    def test_fetch_success_updates_cache(self, fresh_db, monkeypatch):
        """拉取成功后应写入缓存，二次调用走缓存且数据一致"""
        fetched = make_kline_df(7)

        class FakeAk:
            @staticmethod
            def stock_zh_index_daily(symbol):
                return fetched

        monkeypatch.setattr(dl, "ak", FakeAk)
        df1 = dl.get_kline("sh000300", is_index=True)
        assert len(df1) == 7
        # 第一次调用后缓存已建立，改用故障 AK 再次调用应命中缓存
        monkeypatch.setattr(dl, "ak", None)
        df2 = dl.get_kline("sh000300", is_index=True)
        assert len(df2) == 7
        assert df1.equals(df2)

    def test_refresh_forces_fetch(self, fresh_db, monkeypatch):
        """refresh=True 时忽略缓存新鲜度强制拉取"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df(3))

        class FakeAk:
            @staticmethod
            def stock_zh_index_daily(symbol):
                return make_kline_df(6)

        monkeypatch.setattr(dl, "ak", FakeAk)
        df = dl.get_kline("sh000300", is_index=True, refresh=True)
        assert len(df) == 6  # 强制刷新后取回新数据
