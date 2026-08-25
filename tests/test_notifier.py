"""通知层单元测试（Layer 5 notifier.py）—— 全部离线，不调用真实 PushPlus API

覆盖范围：PushPlus 推送（成功 / 返回码异常 / 网络异常 / 响应解析失败）、
Token 未配置降级控制台、AI 报告 / 降级报告 / 异常告警三种格式化函数。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import requests

import src.notifier as notifier


# ---------------------------------------------------------------------------
# 辅助：模拟 AnalysisResult 与内部结构（与 ai_layer 接口兼容）
# ---------------------------------------------------------------------------
@dataclass
class FakeRec:
    code: str
    action: str
    confidence: int
    reasoning: str


@dataclass
class FakeAnalysis:
    stock_recommendations: list = field(default_factory=list)
    market_summary: str = ""
    risk_warnings: List[str] = field(default_factory=list)


@dataclass
class FakeResult:
    analysis: object = None
    used_ai: bool = False
    used_model: str = ""
    degraded_signal: str = ""


class FakeResponse:
    """模拟 requests.Response（可控 json 返回与 raise_for_status 行为）"""

    def __init__(self, json_data=None, raise_http=False, bad_json=False):
        self._json_data = json_data
        self._raise_http = raise_http
        self._bad_json = bad_json

    def raise_for_status(self):
        if self._raise_http:
            raise requests.HTTPError("500 Server Error")

    def json(self):
        if self._bad_json:
            # 模拟 requests 真实行为：继承 ValueError 的解析异常
            raise notifier.requests.exceptions.JSONDecodeError(
                "非法 JSON", "", 0)
        return self._json_data


# ---------------------------------------------------------------------------
# PushPlus 推送
# ---------------------------------------------------------------------------
class TestSendNotification:
    def test_success(self, monkeypatch):
        """code=200 时推送成功并返回 True"""
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            captured["timeout"] = timeout
            return FakeResponse(json_data={"code": 200, "msg": "ok"})

        monkeypatch.setattr(notifier.requests, "post", fake_post)
        ok = notifier.send_notification("标题", "内容", token="test-token")
        assert ok is True
        assert captured["url"] == notifier.PUSHPLUS_API_URL
        assert captured["payload"]["token"] == "test-token"
        assert captured["payload"]["template"] == "markdown"
        assert captured["payload"]["title"] == "标题"
        assert captured["payload"]["content"] == "内容"

    def test_pushplus_error_code(self, monkeypatch):
        """PushPlus 返回非 200 码时应降级控制台并返回 False"""
        fallback = []
        monkeypatch.setattr(notifier.requests, "post",
                            lambda *a, **k: FakeResponse(
                                json_data={"code": 903, "msg": "token无效"}))
        monkeypatch.setattr(notifier, "_console_fallback",
                            lambda t, c: fallback.append((t, c)))
        ok = notifier.send_notification("标题", "内容", token="bad-token")
        assert ok is False
        assert fallback == [("标题", "内容")]

    def test_network_error(self, monkeypatch):
        """网络异常时应降级控制台并返回 False（不抛出）"""
        def fake_post(*args, **kwargs):
            raise requests.ConnectionError("连接失败")

        fallback = []
        monkeypatch.setattr(notifier.requests, "post", fake_post)
        monkeypatch.setattr(notifier, "_console_fallback",
                            lambda t, c: fallback.append((t, c)))
        ok = notifier.send_notification("标题", "内容", token="t")
        assert ok is False
        assert len(fallback) == 1

    def test_http_error(self, monkeypatch):
        """HTTP 状态码异常（如 500）时应降级控制台并返回 False"""
        fallback = []
        monkeypatch.setattr(notifier.requests, "post",
                            lambda *a, **k: FakeResponse(raise_http=True))
        monkeypatch.setattr(notifier, "_console_fallback",
                            lambda t, c: fallback.append((t, c)))
        ok = notifier.send_notification("标题", "内容", token="t")
        assert ok is False
        assert len(fallback) == 1

    def test_bad_json_response(self, monkeypatch):
        """响应体非法 JSON 时应降级控制台并返回 False（不抛出）

        P4 修正验证：requests 实际抛 RequestJSONDecodeError（继承 ValueError），
        notifier 必须按 ValueError 兜底而非仅 json.JSONDecodeError。
        """
        fallback = []
        monkeypatch.setattr(notifier.requests, "post",
                            lambda *a, **k: FakeResponse(bad_json=True))
        monkeypatch.setattr(notifier, "_console_fallback",
                            lambda t, c: fallback.append((t, c)))
        ok = notifier.send_notification("标题", "内容", token="t")
        assert ok is False
        assert len(fallback) == 1

    def test_no_token_console_fallback(self, monkeypatch):
        """Token 未配置时不发请求，降级控制台并返回 False"""
        import config as cfg

        class FakeSettings:
            pushplus_token = None

        monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())
        called = []
        monkeypatch.setattr(notifier.requests, "post",
                            lambda *a, **k: called.append(1))
        fallback = []
        monkeypatch.setattr(notifier, "_console_fallback",
                            lambda t, c: fallback.append((t, c)))
        ok = notifier.send_notification("标题", "内容")
        assert ok is False
        assert called == []          # 未发起 HTTP 请求
        assert fallback == [("标题", "内容")]

    def test_token_from_settings(self, monkeypatch):
        """未显式传 token 时应从 settings.pushplus_token 读取"""
        import config as cfg

        class FakeSettings:
            pushplus_token = "settings-token"

        monkeypatch.setattr(cfg, "get_settings", lambda: FakeSettings())
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["token"] = json["token"]
            return FakeResponse(json_data={"code": 200})

        monkeypatch.setattr(notifier.requests, "post", fake_post)
        ok = notifier.send_notification("标题", "内容")
        assert ok is True
        assert captured["token"] == "settings-token"


# ---------------------------------------------------------------------------
# AI 报告格式化
# ---------------------------------------------------------------------------
class TestFormatAiReport:
    def test_full_report(self):
        """完整 AI 结果：模型名 / 市场概况 / 建议明细 / 风险提示均应出现"""
        analysis = FakeAnalysis(
            stock_recommendations=[
                FakeRec("000063", "买入", 4, "PE 分位 0.35，趋势确认"),
                FakeRec("300750", "观望", 3, "估值中性"),
                FakeRec("000001", "回避", 1, "回撤过大"),
                FakeRec("600000", "未知动作", 2, "测试未知 emoji 兜底"),
            ],
            market_summary="沪深300 震荡，MACD 金叉",
            risk_warnings=["注意回撤风险"],
        )
        report = notifier.format_ai_report(
            FakeResult(analysis=analysis, used_ai=True,
                       used_model="qwen3.8-max"))
        assert "qwen3.8-max" in report
        assert "沪深300 震荡，MACD 金叉" in report
        assert "000063" in report and "🟢 买入" in report
        assert "🟡 观望" in report
        assert "🔴 回避" in report
        assert "⚪ 未知动作" in report
        assert "⭐⭐⭐⭐" in report       # 信心 4 = 4 星
        assert "PE 分位 0.35，趋势确认" in report
        assert "注意回撤风险" in report
        assert "由 AQuant 量化系统生成" in report

    def test_empty_recommendations(self):
        """无推荐时应输出占位文案而非崩溃"""
        analysis = FakeAnalysis(stock_recommendations=[],
                                market_summary="震荡")
        report = notifier.format_ai_report(
            FakeResult(analysis=analysis, used_ai=True, used_model="m"))
        assert "无候选股票推荐" in report

    def test_analysis_none(self):
        """analysis 为 None 时市场概况输出占位且不崩溃"""
        report = notifier.format_ai_report(
            FakeResult(analysis=None, used_ai=True, used_model="m"))
        assert "无数据" in report
        assert "无候选股票推荐" in report


# ---------------------------------------------------------------------------
# 降级报告格式化
# ---------------------------------------------------------------------------
class TestFormatDegradedReport:
    def test_with_signal(self):
        result = FakeResult(degraded_signal="**市场状态**: 低落横盘")
        report = notifier.format_degraded_report(result)
        assert "纯量化信号" in report
        assert "低落横盘" in report
        assert "AI 层不可用" in report

    def test_empty_signal(self):
        """降级信号为空时输出占位文案"""
        report = notifier.format_degraded_report(FakeResult(degraded_signal=""))
        assert "无降级信号数据" in report


# ---------------------------------------------------------------------------
# 异常告警格式化
# ---------------------------------------------------------------------------
class TestFormatErrorAlert:
    def test_contains_error_msg(self):
        alert = notifier.format_error_alert("DataFetchError: 指数拉取失败")
        assert "运行告警" in alert
        assert "DataFetchError: 指数拉取失败" in alert
        assert "```" in alert  # 错误信息包裹在代码块中


# ---------------------------------------------------------------------------
# 纯筛选报告格式化（P4 策略调整：AI 层停用期间替代 AI 报告）
# ---------------------------------------------------------------------------
@dataclass
class FakeScreenerCandidate:
    """stock_screener.StockCandidate 的替身（仅含报告用到的字段）"""
    code: str = "000063"
    industry: str = "通信"
    price: Optional[Decimal] = Decimal("12.34")
    pe_percentile: Optional[Decimal] = Decimal("0.35")
    pb_percentile: Optional[Decimal] = None
    passed: bool = True
    rsi: Optional[Decimal] = Decimal("42.5")
    max_drawdown: Optional[Decimal] = Decimal("0.36")
    rebound_from_low: Optional[Decimal] = Decimal("0.05")
    error: Optional[str] = None


RISK_FLAGS_NONE = {
    "强制减仓": False, "科技股减半": False, "禁止开仓": False,
    "科技股清仓": False, "恐慌加仓": False,
}


class TestFormatScreeningReport:
    def test_full_report(self):
        """完整报告：市场状态 / 统计 / 风控 / 通过股票指标明细均应出现"""
        cands = [
            FakeScreenerCandidate(),
            FakeScreenerCandidate(code="300750", industry="电力设备",
                                  passed=False),
            FakeScreenerCandidate(code="600519", industry="电子",
                                  passed=False, error="拉取超时"),
        ]
        report = notifier.format_screening_report(cands, "低落横盘",
                                                  RISK_FLAGS_NONE)
        assert "筛选结果报告" in report
        assert "低落横盘" in report                 # 市场状态
        assert "评估: 3 只" in report
        assert "通过初筛: 1 只" in report
        assert "数据异常: 1 只" in report           # error 计数不含未通过无异常者
        assert "全部未触发" in report               # 风控无触发占位文案
        assert "000063（通信）" in report
        assert "12.34" in report                    # 收盘价（去冗余精度）
        assert "35.0%" in report                    # PE 分位百分比化
        assert "42.5" in report                     # RSI
        assert "36.0%" in report                    # 最大回撤百分比化
        assert "AI 解读已临时停用" in report

    def test_no_passed_placeholder(self):
        """0 只通过时输出占位文案而非崩溃"""
        cands = [FakeScreenerCandidate(passed=False)]
        report = notifier.format_screening_report(cands, "景气上行",
                                                  RISK_FLAGS_NONE)
        assert "通过初筛: 0 只" in report
        assert "本次无股票通过初筛" in report

    def test_triggered_risk_flags_listed(self):
        """风控触发时列出触发项名称"""
        flags = dict(RISK_FLAGS_NONE)
        flags["科技股清仓"] = True
        report = notifier.format_screening_report([], "低落横盘", flags)
        assert "科技股清仓" in report
        assert "全部未触发" not in report

    def test_missing_indicators_omitted(self):
        """指标缺失（None）时对应行不显示而非输出 N/A（金融股候选只有收盘价+PB）"""
        cands = [FakeScreenerCandidate(pe_percentile=None, rsi=None,
                                       max_drawdown=None, rebound_from_low=None)]
        report = notifier.format_screening_report(cands, "景气上行",
                                                  RISK_FLAGS_NONE)
        assert "PE 分位" not in report
        assert "RSI14" not in report
        assert "近60日最大回撤" not in report
        assert "距低点反弹" not in report

    def test_pb_percentile_shown_for_finance(self):
        """金融股候选：显示 PB 分位行（百分比化）"""
        cands = [FakeScreenerCandidate(code="601398", industry="银行",
                                       pe_percentile=None,
                                       pb_percentile=Decimal("0.25"))]
        report = notifier.format_screening_report(cands, "BEAR",
                                                  RISK_FLAGS_NONE)
        assert "PB 分位**: 25.0%" in report
        assert "PE 分位" not in report


class TestFormatReduceSuggestions:
    def test_pool_entries_rendered(self):
        """减仓建议：逐条渲染代码/行业/推荐价/首次入池日期/累计次数"""
        pool = {
            "000063": {"industry": "通信", "price": "35.2",
                       "first_recommended_at": "2026-08-01T09:40:00",
                       "last_recommended_at": "2026-08-20T14:40:00",
                       "recommend_count": 3},
            "300750": {"industry": "电力设备", "price": "",
                       "first_recommended_at": "",
                       "recommend_count": 1},
        }
        block = notifier.format_reduce_suggestions(pool)
        assert "减仓建议" in block and "清仓" in block
        assert "**000063**（通信）" in block
        assert "推荐价 35.2" in block
        assert "首次入池 2026-08-01" in block
        assert "累计入池 3 次" in block
        assert "**300750**（电力设备）" in block
        assert "推荐价 N/A" in block            # 空价格兼容
        assert "首次入池 未知" in block          # 空时间兼容
