"""配置层 AI 节单元测试（P3 新增 ai section 校验）

覆盖范围：ai 节字段类型校验、enable_ai_analysis 属性、_STR 新类型、
配置缺失字段报错、交叉校验。
"""

from decimal import Decimal

import pytest
import yaml

import config


@pytest.fixture
def base_settings(tmp_path, monkeypatch):
    """返回一份合法的最小 settings dict（含 ai 节），便于逐项修改后测试"""
    return {
        "market_regime": {
            "ma_period": 60, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_bull_threshold": 50, "rsi_bear_low": 30,
            "rsi_bear_high": 50, "volume_ma_period": 5, "volume_ratio": 1.0,
            "force_reduce_loss_days": 5, "force_reduce_loss_drop": 0.03,
        },
        "tech": {
            "price_min": 1.0, "price_max": 20.0,
            "industries": ["电子", "计算机"],
            "drawdown_window": 60, "max_drawdown_required": 0.30,
            "rebound_from_low_max": 0.15, "ma_trend_periods": [5, 10, 20],
            "limit_up_months": 3, "volatility_min": 0.20, "volatility_max": 0.60,
            "rsi_min": 30, "rsi_max": 55, "pe_percentile_years": 5,
            "pe_percentile_max": 0.50,
            "position_1st": 0.30, "position_2nd": 0.30, "position_3rd": 0.40,
            "first_volume_ratio": 1.2, "pause_add_drop": 0.05,
            "accelerate_add_rise": 0.08,
            "limit_up_main": 0.095, "limit_up_chinext": 0.195,
            "limit_up_consecutive": 2, "limit_up_sell_ratio": 0.50,
            "limit_up_trail_ma": 5, "time_stop_days": 20,
            "t_sell_rise": 0.05, "t_sell_rsi": 70,
            "t_sell_ratio_min": 0.20, "t_sell_ratio_max": 0.30,
            "t_buy_below_ma": 0.02, "stop_loss": 0.08,
        },
        "value": {
            "pb_percentile_years": 5, "pb_percentile_max": 0.30,
            "dividend_yield_min": 0.03, "roe_min_percentile": 0.40,
            "debt_ratio_max_percentile": 0.60,
            "etf_list": ["创业板ETF"],
            "init_pb_percentile": 0.20, "init_pe_percentile": 0.30,
            "init_position_ratio": 0.60, "trend_ma_periods": [120, 250],
            "panic_drop_1d": 0.03, "panic_drop_days": 3,
            "panic_add_ratio": 0.10, "take_profit_percentile": 0.70,
            "take_profit_ratio": 0.50,
        },
        "risk": {
            "half_position_loss_days": 5, "forbid_open_rise": 0.03,
            "forbid_open_rsi": 70, "clear_tech_ma": 120,
            "position_single_stock_max": 0.15, "position_tech_total_max": 0.60,
            "position_value_total_max": 0.40, "annual_return_target": 0.12,
            "volatility_target": 0.18, "max_drawdown_limit": 0.25,
        },
        "operation": {
            "schedule_times": ["09:40"], "rebalance_monthly": True,
            "data_retry_times": 3, "cache_expire_days": 5,
            "valuation_history_period": "近五年",
        },
        "ai": {
            "enable_ai_analysis": True,
            "primary_model": "qwen3.8-max",
            "fallback_models": ["deepseek-v4-pro-0813", "kimi-k3"],
            "max_retries_per_model": 2,
            "timeout_seconds": 60,
            "max_tokens": 2000,
            "max_batch_size": 5,
            "temperature": 0.3,
        },
    }


def _write_and_load(data, tmp_path, monkeypatch):
    """将 settings dict 写入临时文件，monkeypatch 路径后加载"""
    settings_file = tmp_path / "settings.yaml"
    with open(settings_file, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True)
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    # 重置缓存的单例
    monkeypatch.setattr(config, "_cached_settings", None)
    return config.load_settings()


# ---------------------------------------------------------------------------
# AI 节字段校验
# ---------------------------------------------------------------------------
class TestAISection:
    def test_ai_section_loads_correctly(self, base_settings, tmp_path, monkeypatch):
        """ai 节全部字段合法时应正常加载"""
        s = _write_and_load(base_settings, tmp_path, monkeypatch)
        assert s.ai.enable_ai_analysis is True
        assert s.ai.primary_model == "qwen3.8-max"
        assert s.ai.fallback_models == ["deepseek-v4-pro-0813", "kimi-k3"]
        assert s.ai.max_retries_per_model == 2
        assert s.ai.timeout_seconds == 60
        assert s.ai.max_tokens == 2000
        assert s.ai.temperature == Decimal("0.3")

    def test_enable_ai_analysis_false(self, base_settings, tmp_path, monkeypatch):
        """enable_ai_analysis=false 时应正确解析为布尔 False"""
        base_settings["ai"]["enable_ai_analysis"] = False
        s = _write_and_load(base_settings, tmp_path, monkeypatch)
        assert s.enable_ai_analysis is False

    def test_enable_ai_analysis_property(self, base_settings, tmp_path, monkeypatch):
        """Settings.enable_ai_analysis 属性应代理 ai.enable_ai_analysis"""
        s = _write_and_load(base_settings, tmp_path, monkeypatch)
        assert s.enable_ai_analysis is True
        base_settings["ai"]["enable_ai_analysis"] = False
        s2 = _write_and_load(base_settings, tmp_path, monkeypatch)
        assert s2.enable_ai_analysis is False

    def test_primary_model_must_be_string(self, base_settings, tmp_path, monkeypatch):
        """primary_model 为非字符串时应报 ConfigError"""
        base_settings["ai"]["primary_model"] = 12345
        with pytest.raises(config.ConfigError, match="primary_model"):
            _write_and_load(base_settings, tmp_path, monkeypatch)

    def test_primary_model_empty_string(self, base_settings, tmp_path, monkeypatch):
        """primary_model 为空字符串时应报 ConfigError"""
        base_settings["ai"]["primary_model"] = ""
        with pytest.raises(config.ConfigError, match="primary_model"):
            _write_and_load(base_settings, tmp_path, monkeypatch)

    def test_fallback_models_must_be_list(self, base_settings, tmp_path, monkeypatch):
        """fallback_models 为字符串（非列表）时应报 ConfigError"""
        base_settings["ai"]["fallback_models"] = "not-a-list"
        with pytest.raises(config.ConfigError, match="fallback_models"):
            _write_and_load(base_settings, tmp_path, monkeypatch)

    def test_temperature_out_of_range(self, base_settings, tmp_path, monkeypatch):
        """temperature 超出 0~1 范围时应报 ConfigError"""
        base_settings["ai"]["temperature"] = 1.5
        with pytest.raises(config.ConfigError, match="temperature"):
            _write_and_load(base_settings, tmp_path, monkeypatch)

    def test_max_tokens_must_be_positive(self, base_settings, tmp_path, monkeypatch):
        """max_tokens 为 0 或负数时应报 ConfigError"""
        base_settings["ai"]["max_tokens"] = 0
        with pytest.raises(config.ConfigError, match="max_tokens"):
            _write_and_load(base_settings, tmp_path, monkeypatch)

    def test_ai_section_missing(self, base_settings, tmp_path, monkeypatch):
        """缺少 ai 节时应报 ConfigError"""
        del base_settings["ai"]
        with pytest.raises(config.ConfigError, match="缺少必需节.*ai"):
            _write_and_load(base_settings, tmp_path, monkeypatch)

    def test_ai_field_missing(self, base_settings, tmp_path, monkeypatch):
        """ai 节缺少必需字段时应报 ConfigError"""
        del base_settings["ai"]["primary_model"]
        with pytest.raises(config.ConfigError, match="primary_model"):
            _write_and_load(base_settings, tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# _STR 新类型校验
# ---------------------------------------------------------------------------
class TestSTRType:
    def test_str_type_valid_string(self):
        """_STR 类型接受非空字符串"""
        result = config._coerce_field("ai", "primary_model", "qwen3.8-max", config._STR)
        assert result == "qwen3.8-max"

    def test_str_type_strips_whitespace(self):
        """_STR 类型应自动去除首尾空白"""
        result = config._coerce_field("ai", "primary_model", "  qwen3.8-max  ", config._STR)
        assert result == "qwen3.8-max"

    def test_str_type_rejects_empty(self):
        """_STR 类型拒绝空字符串"""
        with pytest.raises(config.ConfigError, match="非空字符串"):
            config._coerce_field("ai", "primary_model", "", config._STR)

    def test_str_type_rejects_non_string(self):
        """_STR 类型拒绝非字符串类型"""
        with pytest.raises(config.ConfigError, match="非空字符串"):
            config._coerce_field("ai", "primary_model", 42, config._STR)
