"""
股票池快照抓取工具单元测试（tools/fetch_pool_snapshot.py）—— 全部离线 mock

覆盖范围：
- 申万主源：行业名→代码动态映射/逐行业成分拉取/未映射行业跳过/未安装
- Tushare 备源：未配置 Token/未安装/正常拉取/行业映射缺失/异常兜底
- 三级降级链：首源命中不触发后源、逐级降级、全失败、单源异常不中断
- 东财兜底：重试成功/全部失败/缺列跳过
"""

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

# tools 目录不在 conftest 注册的 sys.path 中，手动补齐
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import fetch_pool_snapshot as fps


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeSWIndex:
    """伪 akshare 申万接口：sw_index_first_info + index_component_sw"""

    def __init__(self, info_df, members_map, raise_exc=None):
        self._info = info_df
        self._members = members_map  # {行业代码: DataFrame(证券代码列)}
        self._raise_exc = raise_exc

    def sw_index_first_info(self):
        if self._raise_exc:
            raise self._raise_exc
        return self._info

    def index_component_sw(self, symbol):
        if self._raise_exc:
            raise self._raise_exc
        return self._members.get(symbol, pd.DataFrame({"证券代码": []}))


def install_fake_akshare(monkeypatch, fake_ak):
    """向 sys.modules 注入伪 akshare（含 sw 接口与东财接口）"""
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)


def install_fake_tushare(monkeypatch, *, classify_df, members_map, raise_exc=None):
    """注入伪造 tushare 模块；members_map: {l1_code: DataFrame(ts_code 列)}"""
    calls = {"member_calls": []}

    class FakePro:
        def index_classify(self, src, level):
            if raise_exc:
                raise raise_exc
            return classify_df

        def index_member_all(self, l1_code, is_new):
            calls["member_calls"].append(l1_code)
            return members_map.get(l1_code, pd.DataFrame({"ts_code": []}))

    fake = types.ModuleType("tushare")
    fake.pro_api = lambda token: FakePro()
    monkeypatch.setitem(sys.modules, "tushare", fake)
    return calls


# ---------------------------------------------------------------------------
# 申万主源
# ---------------------------------------------------------------------------
class TestFetchPoolSwindex:
    @pytest.fixture()
    def info(self):
        return pd.DataFrame({
            "行业代码": ["801080.SI", "801750.SI", "801770.SI", "801730.SI"],
            "行业名称": ["电子", "计算机", "通信", "电力设备"],
            "成份个数": [492, 334, 122, 377],
        })

    def test_success_maps_and_fetches(self, monkeypatch, info):
        """行业名→代码动态映射 + 逐行业成分拉取 + 代码去前缀"""
        members = {
            "801080": pd.DataFrame({"证券代码": ["600183", "002475", "688981"]}),
            "801770": pd.DataFrame({"证券代码": ["000063"]}),
            # 电力设备无成分，不应误判整体失败
        }
        install_fake_akshare(monkeypatch, FakeSWIndex(info, members))
        pool = fps.fetch_pool_swindex(["电子", "通信", "电力设备"])
        assert pool == {"600183": "电子", "002475": "电子",
                        "688981": "电子", "000063": "通信"}

    def test_unmapped_industry_skipped(self, monkeypatch, info):
        """白名单行业不在申万分类表中时跳过该行业而非整体失败"""
        members = {"801080": pd.DataFrame({"证券代码": ["600183"]})}
        install_fake_akshare(monkeypatch, FakeSWIndex(info, members))
        pool = fps.fetch_pool_swindex(["电子", "不存在的行业"])
        assert pool == {"600183": "电子"}

    def test_all_empty_returns_none(self, monkeypatch, info):
        """白名单零覆盖时返回 None（触发降级），防止误写空快照"""
        install_fake_akshare(monkeypatch, FakeSWIndex(info, {}))
        assert fps.fetch_pool_swindex(["电子"]) is None

    def test_api_exception_returns_none(self, monkeypatch, info):
        install_fake_akshare(monkeypatch, FakeSWIndex(
            info, {}, raise_exc=RuntimeError("乐咕接口超时")))
        assert fps.fetch_pool_swindex(["电子"]) is None

    def test_not_installed_returns_none(self, monkeypatch):
        """sys.modules 置 None 可令 import 抛 ImportError，模拟未安装"""
        monkeypatch.setitem(sys.modules, "akshare", None)
        assert fps.fetch_pool_swindex(["电子"]) is None


# ---------------------------------------------------------------------------
# Tushare 备源
# ---------------------------------------------------------------------------
class TestFetchPoolTushare:
    @pytest.fixture()
    def classify(self):
        return pd.DataFrame({
            "l1_code": ["801080.SI", "801750.SI", "801770.SI"],
            "l1_name": ["电子", "计算机", "通信"],
        })

    def test_no_token_skipped(self, monkeypatch):
        assert fps.fetch_pool_tushare(["电子"], token=None) is None
        assert fps.fetch_pool_tushare(["电子"], token="") is None

    def test_not_installed_returns_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "tushare", None)
        assert fps.fetch_pool_tushare(["电子"], token="fake-token") is None

    def test_success_converts_ts_code(self, monkeypatch, classify):
        members = {
            "801080.SI": pd.DataFrame({"ts_code": ["600183.SH", "002475.SZ"]}),
            "801770.SI": pd.DataFrame({"ts_code": ["000063.SZ"]}),
        }
        calls = install_fake_tushare(monkeypatch, classify_df=classify,
                                     members_map=members)
        pool = fps.fetch_pool_tushare(["电子", "通信", "计算机"], token="tk")
        assert pool == {"600183": "电子", "002475": "电子", "000063": "通信"}
        # 计算机行业有映射但成分为空，不应误判整体失败
        assert "801750.SI" in calls["member_calls"]

    def test_unmapped_industry_skipped(self, monkeypatch, classify):
        """白名单行业不在申万分类表中时跳过该行业而非整体失败"""
        members = {"801080.SI": pd.DataFrame({"ts_code": ["600183.SH"]})}
        install_fake_tushare(monkeypatch, classify_df=classify, members_map=members)
        pool = fps.fetch_pool_tushare(["电子", "不存在的行业"], token="tk")
        assert pool == {"600183": "电子"}

    def test_api_exception_returns_none(self, monkeypatch, classify):
        install_fake_tushare(monkeypatch, classify_df=classify, members_map={},
                             raise_exc=RuntimeError("积分不足"))
        assert fps.fetch_pool_tushare(["电子"], token="tk") is None

    def test_all_industries_empty_returns_none(self, monkeypatch, classify):
        install_fake_tushare(monkeypatch, classify_df=classify, members_map={})
        assert fps.fetch_pool_tushare(["电子"], token="tk") is None


# ---------------------------------------------------------------------------
# 三级降级链
# ---------------------------------------------------------------------------
class TestFetchPoolWithFallback:
    def test_first_source_hit_skips_rest(self, monkeypatch):
        """申万源成功时不应调用 Tushare/东财"""
        monkeypatch.setattr(fps, "fetch_pool_swindex", lambda ind: {"000063": "通信"})

        def boom(*a, **k):
            raise AssertionError("不应被调用")

        monkeypatch.setattr(fps, "fetch_pool_tushare", boom)
        monkeypatch.setattr(fps, "fetch_pool_eastmoney", boom)
        pool, source = fps.fetch_pool_with_fallback(["通信"], 3, None)
        assert pool == {"000063": "通信"}
        assert "申万" in source

    def test_fallback_to_tushare(self, monkeypatch):
        monkeypatch.setattr(fps, "fetch_pool_swindex", lambda ind: None)
        monkeypatch.setattr(fps, "fetch_pool_tushare",
                            lambda ind, token: {"600183": "电子"})
        monkeypatch.setattr(fps, "fetch_pool_eastmoney", lambda *a, **k: None)
        pool, source = fps.fetch_pool_with_fallback(["电子"], 3, "tk")
        assert pool == {"600183": "电子"}
        assert "Tushare" in source

    def test_fallback_to_eastmoney(self, monkeypatch):
        monkeypatch.setattr(fps, "fetch_pool_swindex", lambda ind: None)
        monkeypatch.setattr(fps, "fetch_pool_tushare", lambda ind, token: None)
        monkeypatch.setattr(fps, "fetch_pool_eastmoney",
                            lambda ind, retries=3: {"000001": "电子"})
        pool, source = fps.fetch_pool_with_fallback(["电子"], 3, None)
        assert pool == {"000001": "电子"}
        assert "东财" in source

    def test_all_sources_failed(self, monkeypatch):
        monkeypatch.setattr(fps, "fetch_pool_swindex", lambda ind: None)
        monkeypatch.setattr(fps, "fetch_pool_tushare", lambda ind, token: None)
        monkeypatch.setattr(fps, "fetch_pool_eastmoney", lambda *a, **k: None)
        pool, source = fps.fetch_pool_with_fallback(["电子"], 3, None)
        assert pool is None
        assert "无可用" in source

    def test_source_exception_does_not_break_chain(self, monkeypatch):
        """单源内部未预期异常应被兜住并继续降级（防一个源崩溃毁掉整个工具）"""
        def boom(ind):
            raise OSError("socket 断连")

        monkeypatch.setattr(fps, "fetch_pool_swindex", boom)
        monkeypatch.setattr(fps, "fetch_pool_tushare",
                            lambda ind, token: {"600183": "电子"})
        monkeypatch.setattr(fps, "fetch_pool_eastmoney", lambda *a, **k: None)
        pool, source = fps.fetch_pool_with_fallback(["电子"], 3, "tk")
        assert pool == {"600183": "电子"}
        assert "Tushare" in source


# ---------------------------------------------------------------------------
# 东财兜底源
# ---------------------------------------------------------------------------
class TestFetchPoolEastmoney:
    class FakeAK:
        """伪 akshare 东财接口：按行业预置返回或异常"""

        def __init__(self, behavior):
            self._behavior = behavior  # {industry: DataFrame 或 Exception}

        def stock_board_industry_cons_em(self, symbol):
            r = self._behavior.get(symbol)
            if isinstance(r, Exception):
                raise r
            return r

    def test_success(self, monkeypatch):
        fake_ak = self.FakeAK({"电子": pd.DataFrame({"代码": ["600183", "002475"]})})
        install_fake_akshare(monkeypatch, fake_ak)
        monkeypatch.setattr(fps.time, "sleep", lambda s: None)
        pool = fps.fetch_pool_eastmoney(["电子"], retries=1)
        assert pool == {"600183": "电子", "002475": "电子"}

    def test_retry_then_success(self, monkeypatch):
        """首次异常、第二次成功，重试机制生效"""
        attempts = {"n": 0}
        fake_ak = self.FakeAK({"电子": pd.DataFrame({"代码": ["600183"]})})
        orig = fake_ak.stock_board_industry_cons_em

        def flaky(symbol):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("第一次失败")
            return orig(symbol)

        fake_ak.stock_board_industry_cons_em = flaky
        install_fake_akshare(monkeypatch, fake_ak)
        monkeypatch.setattr(fps.time, "sleep", lambda s: None)
        pool = fps.fetch_pool_eastmoney(["电子"], retries=2)
        assert pool == {"600183": "电子"}
        assert attempts["n"] == 2

    def test_all_failed_returns_none(self, monkeypatch):
        fake_ak = self.FakeAK({"电子": ConnectionError("被反爬封锁")})
        install_fake_akshare(monkeypatch, fake_ak)
        monkeypatch.setattr(fps.time, "sleep", lambda s: None)
        assert fps.fetch_pool_eastmoney(["电子"], retries=2) is None

    def test_missing_code_column_skipped(self, monkeypatch):
        fake_ak = self.FakeAK({"电子": pd.DataFrame({"名称": ["生益科技"]})})
        install_fake_akshare(monkeypatch, fake_ak)
        monkeypatch.setattr(fps.time, "sleep", lambda s: None)
        assert fps.fetch_pool_eastmoney(["电子"], retries=1) is None

    def test_not_installed_returns_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "akshare", None)
        assert fps.fetch_pool_eastmoney(["电子"]) is None
