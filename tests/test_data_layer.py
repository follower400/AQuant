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


def make_valuation_df(n=10, pe_base="10.0", pb_base="1.0", with_none=False):
    """构造标准估值 DataFrame（pe/pb 为 Decimal，可选含缺失值行）"""
    df = pd.DataFrame({
        "trade_date": [f"2026-01-{i + 1:02d}" for i in range(n)],
        "pe": [Decimal(pe_base) + Decimal(i) / Decimal(10) for i in range(n)],
        "pb": [Decimal(pb_base) + Decimal(i) / Decimal(100) for i in range(n)],
    })
    if with_none and n > 1:
        df.loc[1, "pe"] = None  # 模拟某日缺 PE（百度接口两个序列日期可能不对齐）
    return df


class FakeValuationAk:
    """模拟 AKShare 的 stock_zh_valuation_baidu：按 indicator 返回不同序列"""

    @staticmethod
    def stock_zh_valuation_baidu(symbol, indicator, period):
        n = 10
        if indicator == "市盈率(TTM)":
            base, step = Decimal("10.0"), Decimal("0.1")
        elif indicator == "市净率":
            base, step = Decimal("1.0"), Decimal("0.01")
        else:
            raise ValueError(f"未知 indicator: {indicator}")
        return pd.DataFrame({
            "date": [f"2026-01-{i + 1:02d}" for i in range(n)],
            "value": [base + step * i for i in range(n)],
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


# ---------------------------------------------------------------------------
# P2 新增：cache_meta 复合主键迁移（P1 旧库 -> 新结构无损迁移）
# ---------------------------------------------------------------------------
class TestCacheMetaMigration:
    def test_old_schema_migrated_losslessly(self, fresh_db):
        """P1 旧表（code 单主键）应无损迁移为 (code, data_type) 复合主键"""
        fresh_db.execute("DROP TABLE cache_meta")
        fresh_db.execute("""
            CREATE TABLE cache_meta (
                code       TEXT PRIMARY KEY,
                data_type  TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        fresh_db.execute(
            "INSERT INTO cache_meta VALUES ('sh000300', 'kline', '2026-08-01T00:00:00')")
        fresh_db.commit()

        dl._migrate_cache_meta(fresh_db)

        rows = [tuple(r) for r in fresh_db.execute(
            "SELECT code, data_type, updated_at FROM cache_meta ORDER BY code")]
        assert rows == [("sh000300", "kline", "2026-08-01T00:00:00")]  # 旧数据完整保留
        # 复合主键生效：同 code 不同 data_type 可共存
        fresh_db.execute(
            "INSERT INTO cache_meta VALUES ('sh000300', 'valuation', '2026-08-02T00:00:00')")
        fresh_db.commit()
        assert fresh_db.execute("SELECT COUNT(*) FROM cache_meta").fetchone()[0] == 2

    def test_new_schema_untouched(self, fresh_db):
        """已是新结构的表重复迁移应跳过且数据不变"""
        dl._write_kline_cache(fresh_db, "sh000300", make_kline_df())
        dl._migrate_cache_meta(fresh_db)  # 不应报错、不应丢数据
        rows = [tuple(r) for r in fresh_db.execute(
            "SELECT code, data_type FROM cache_meta")]
        assert rows == [("sh000300", "kline")]


# ---------------------------------------------------------------------------
# P2 新增：估值历史缓存（valuation 表）
# ---------------------------------------------------------------------------
class TestValuationSchema:
    def test_valuation_table_created(self, fresh_db):
        """valuation 表应随连接初始化自动创建"""
        tables = {r[0] for r in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "valuation" in tables

    def test_valuation_primary_key(self, fresh_db):
        """(code, trade_date) 联合主键：重复写入应冲突"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df())
        with pytest.raises(Exception):
            fresh_db.execute(
                "INSERT INTO valuation (code, trade_date, pe, pb) "
                "VALUES ('000001', '2026-01-01', '1', '1')")


class TestValuationRoundtrip:
    def test_write_read_decimal_precision(self, fresh_db):
        """写入 -> 读取后 pe/pb 必须保持 Decimal 精度"""
        df = make_valuation_df()
        dl._write_valuation_cache(fresh_db, "000001", df)
        back = dl._read_valuation_cache(fresh_db, "000001")
        assert len(back) == len(df)
        assert back.loc[0, "pe"] == Decimal("10.0")
        assert back.loc[0, "pb"] == Decimal("1.0")
        assert back.loc[1, "pe"] == Decimal("10.1")  # 0.1 步长无浮点误差
        assert isinstance(back.loc[0, "pb"], Decimal)

    def test_none_value_preserved(self, fresh_db):
        """缺失值（某日缺 PE）应写为 NULL 并读回 None"""
        df = make_valuation_df(with_none=True)
        dl._write_valuation_cache(fresh_db, "000001", df)
        back = dl._read_valuation_cache(fresh_db, "000001")
        assert back.loc[1, "pe"] is None
        assert back.loc[1, "pb"] is not None

    def test_negative_value_preserved(self, fresh_db):
        """负值（亏损股 PE 为负 / 资不抵债 PB 为负）必须原样保存"""
        df = pd.DataFrame([{"trade_date": "2026-01-01", "pe": "-3.50", "pb": "-0.20"}])
        dl._write_valuation_cache(fresh_db, "000001", df)
        back = dl._read_valuation_cache(fresh_db, "000001")
        assert back.loc[0, "pe"] == Decimal("-3.50")
        assert back.loc[0, "pb"] == Decimal("-0.20")

    def test_date_range_filter(self, fresh_db):
        """按日期范围过滤应只返回区间内数据"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df(10))
        part = dl._read_valuation_cache(fresh_db, "000001",
                                        start_date="2026-01-03", end_date="2026-01-05")
        assert part["trade_date"].tolist() == ["2026-01-03", "2026-01-04", "2026-01-05"]

    def test_rewrite_overwrites_old(self, fresh_db):
        """重复写入应全量覆盖旧数据（不产生重复行）"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df(5))
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df(8))
        back = dl._read_valuation_cache(fresh_db, "000001")
        assert len(back) == 8


class TestValuationFreshness:
    def test_fresh_after_write(self, fresh_db):
        """写入后估值缓存应为新鲜（data_type='valuation' 独立记录）"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df())
        assert dl._is_cache_fresh(fresh_db, "000001", expire_days=5, data_type="valuation")

    def test_kline_meta_independent(self, fresh_db):
        """kline 与 valuation 的新鲜度互不影响：只写过 kline 时 valuation 应为过期"""
        dl._write_kline_cache(fresh_db, "000001", make_kline_df())
        assert dl._is_cache_fresh(fresh_db, "000001", expire_days=5)  # kline 新鲜
        assert not dl._is_cache_fresh(fresh_db, "000001", expire_days=5,
                                      data_type="valuation")  # valuation 无记录

    def test_expired_when_old_timestamp(self, fresh_db):
        """updated_at 距今超过 expire_days 应判定为过期（仅改 valuation 记录）"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df())
        old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        fresh_db.execute(
            "UPDATE cache_meta SET updated_at = ? "
            "WHERE code = '000001' AND data_type = 'valuation'", (old,))
        fresh_db.commit()
        assert not dl._is_cache_fresh(fresh_db, "000001", expire_days=5,
                                      data_type="valuation")


class TestNormalizeValuation:
    def test_merge_pe_pb_by_date(self):
        """PE/PB 两个序列应按日期外连接合并为标准表"""
        pe_df = pd.DataFrame({
            "date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "value": ["10.0", "10.1", "10.2"],
        })
        pb_df = pd.DataFrame({
            "date": ["2026-01-01", "2026-01-03", "2026-01-04"],  # 缺 01-02、多 01-04
            "value": ["1.0", "1.2", "1.3"],
        })
        merged = dl._normalize_valuation(pe_df, pb_df, "000001")
        assert merged["trade_date"].tolist() == [
            "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
        # 修正记录（P2）：_normalize_valuation 产出为 Decimal 字符串（落库格式），
        # Decimal 对象还原发生在 _read_valuation_cache；此前误断言为 Decimal 导致失败
        assert merged.loc[1, "pe"] == "10.1"
        assert merged.loc[1, "pb"] is None  # PE 有值而 PB 缺失
        assert merged.loc[3, "pe"] is None  # PB 有值而 PE 缺失


# ---------------------------------------------------------------------------
# P2 新增：get_valuation_history 主流程（ak 置 None 或 Fake，模拟断网/正常）
# ---------------------------------------------------------------------------
class TestGetValuationHistory:
    def test_cache_hit_short_circuit(self, fresh_db, monkeypatch):
        """估值缓存新鲜时直接返回，不触发任何 AKShare 调用"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df())
        monkeypatch.setattr(dl, "ak", None)
        df = dl.get_valuation_history("000001")
        assert len(df) == 10
        assert df.loc[0, "pe"] == Decimal("10.0")

    def test_no_cache_and_fetch_fails_raises(self, fresh_db, monkeypatch):
        """无缓存且拉取失败（ak=None）应抛 DataFetchError"""
        monkeypatch.setattr(dl, "ak", None)
        with pytest.raises(dl.DataFetchError):
            dl.get_valuation_history("000001")

    def test_stale_cache_fallback_on_failure(self, fresh_db, monkeypatch):
        """估值缓存过期且拉取失败时降级返回过期缓存（不抛错）"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df())
        old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        fresh_db.execute(
            "UPDATE cache_meta SET updated_at = ? "
            "WHERE code = '000001' AND data_type = 'valuation'", (old,))
        fresh_db.commit()
        monkeypatch.setattr(dl, "ak", None)
        df = dl.get_valuation_history("000001")
        assert len(df) == 10  # 降级返回过期缓存

    def test_fetch_success_updates_cache(self, fresh_db, monkeypatch):
        """拉取成功后应写入缓存，二次调用走缓存且数据一致"""
        monkeypatch.setattr(dl, "ak", FakeValuationAk)
        df1 = dl.get_valuation_history("000001")
        assert len(df1) == 10
        assert df1.loc[0, "pe"] == Decimal("10.0")
        assert df1.loc[0, "pb"] == Decimal("1.0")
        # 第一次调用后缓存已建立，改用故障 AK 再次调用应命中缓存
        monkeypatch.setattr(dl, "ak", None)
        df2 = dl.get_valuation_history("000001")
        assert len(df2) == 10
        assert df1.equals(df2)

    def test_refresh_forces_fetch(self, fresh_db, monkeypatch):
        """refresh=True 时忽略缓存新鲜度强制拉取"""
        dl._write_valuation_cache(fresh_db, "000001", make_valuation_df(3))
        monkeypatch.setattr(dl, "ak", FakeValuationAk)
        df = dl.get_valuation_history("000001", refresh=True)
        assert len(df) == 10  # 强制刷新后取回 Fake 接口的 10 行数据
