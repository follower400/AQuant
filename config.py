"""
config.py —— Layer 0 配置层：加载并校验 settings.yaml 与环境变量

设计约束（遵循 .coderule 与 PROJECT_PLAN.md Layer 0 规划）：
- 本模块是全项目唯一的配置入口，其他任何模块严禁硬编码策略阈值；
- 启动时配置缺失或非法立即抛错退出，严禁携带默认参数静默运行；
- 涉及金额、价格、仓位比例的字段一律转换为 Decimal，规避浮点精度丢失；
- API Key 等敏感信息一律通过 .env 读取，严禁硬编码在代码中。

用法示例：
    from config import get_settings
    settings = get_settings()
    settings.market_regime.ma_period   # 60（int）
    settings.tech.stop_loss            # Decimal("0.08")
    settings.env["dashscope_api_key"]  # 阿里云百炼 Key（未配置时为 None）
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = PROJECT_ROOT / "settings.yaml"
ENV_FILE = PROJECT_ROOT / ".env"

# ---------------------------------------------------------------------------
# 环境变量登记表：逻辑名 -> 候选环境变量名（按优先级依次回退）
# 注意：dashscope_base_url 为百炼 API 端点地址（如 /compatible-mode/v1），
#       与 API Key 独立配置，两者缺一不可用于 AI 层调用。
# ---------------------------------------------------------------------------
ENV_REGISTRY: Dict[str, Tuple[str, ...]] = {
    "dashscope_api_key": ("DASHSCOPE_API_KEY", "API_KEY_aliyun"),
    "dashscope_base_url": ("DASHSCOPE_BASE_URL", "OPENAI_BASE_URL"),
    "deepseek_api_key": ("API_KEY_Deepseek",),
    "pushplus_token": ("PUSHPLUS_TOKEN",),
}

# 模拟总资产的环境变量名与默认值（单位：元）
CAPITAL_ENV_KEY = "SIMULATED_TOTAL_CAPITAL"
DEFAULT_CAPITAL = "1000000"

# .env 文件候选编码（Windows 中文环境编辑器可能以 GBK 保存，需自动适配）
_ENV_ENCODINGS = ("utf-8", "gbk")

# ---------------------------------------------------------------------------
# settings.yaml 字段校验类型约定：
#   INT       正整数（周期、天数等离散量）
#   BOOL      布尔值
#   LEVEL     0~100 的指标阈值（如 RSI 阈值）
#   RATIO     0~1 的比例（Decimal，如 0.03 表示 3%）
#   PRICE     正价格（Decimal，单位元）
#   FACTOR    正倍数（Decimal，可大于 1，如量能倍数 1.2）
#   LIST_INT  严格递增的正整数列表
#   LIST_STR  非空字符串列表
#   CHOICE    限定枚举字符串（可选值见 _CHOICES，P2 新增）
#   STR       非空字符串（P3 新增，用于模型名称等单字符串字段）
# ---------------------------------------------------------------------------
_INT, _BOOL, _LEVEL, _RATIO, _PRICE, _FACTOR, _LIST_INT, _LIST_STR, _CHOICE, _STR = (
    "INT", "BOOL", "LEVEL", "RATIO", "PRICE", "FACTOR", "LIST_INT", "LIST_STR",
    "CHOICE", "STR",
)

# 枚举型字段的可选值表：{(section, field): 允许值集合}
_CHOICES: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("operation", "valuation_history_period"): (
        "近一年", "近三年", "近五年", "近十年", "全部",
    ),
}

_RULES: Dict[str, Dict[str, str]] = {
    "market_regime": {
        "ma_period": _INT,
        "macd_fast": _INT,
        "macd_slow": _INT,
        "macd_signal": _INT,
        "rsi_period": _INT,
        "rsi_bull_threshold": _LEVEL,
        "rsi_bear_low": _LEVEL,
        "rsi_bear_high": _LEVEL,
        "volume_ma_period": _INT,
        "volume_ratio": _FACTOR,
        "force_reduce_loss_days": _INT,
        "force_reduce_loss_drop": _RATIO,
    },
    "tech": {
        "price_min": _PRICE,
        "price_max": _PRICE,
        "industries": _LIST_STR,
        "drawdown_window": _INT,
        "max_drawdown_required": _RATIO,
        "rebound_from_low_max": _RATIO,
        "ma_trend_periods": _LIST_INT,
        "limit_up_months": _INT,
        "volatility_min": _RATIO,
        "volatility_max": _RATIO,
        "rsi_min": _LEVEL,
        "rsi_max": _LEVEL,
        "pe_percentile_years": _INT,
        "pe_percentile_max": _RATIO,
        "position_1st": _RATIO,
        "position_2nd": _RATIO,
        "position_3rd": _RATIO,
        "first_volume_ratio": _FACTOR,
        "pause_add_drop": _RATIO,
        "accelerate_add_rise": _RATIO,
        "limit_up_main": _RATIO,
        "limit_up_chinext": _RATIO,
        "limit_up_consecutive": _INT,
        "limit_up_sell_ratio": _RATIO,
        "limit_up_trail_ma": _INT,
        "time_stop_days": _INT,
        "t_sell_rise": _RATIO,
        "t_sell_rsi": _LEVEL,
        "t_sell_ratio_min": _RATIO,
        "t_sell_ratio_max": _RATIO,
        "t_buy_below_ma": _RATIO,
        "stop_loss": _RATIO,
    },
    "value": {
        "pb_percentile_years": _INT,
        "pb_percentile_max": _RATIO,
        "dividend_yield_min": _RATIO,
        "roe_min_percentile": _RATIO,
        "debt_ratio_max_percentile": _RATIO,
        "etf_list": _LIST_STR,
        "init_pb_percentile": _RATIO,
        "init_pe_percentile": _RATIO,
        "init_position_ratio": _RATIO,
        "trend_ma_periods": _LIST_INT,
        "panic_drop_1d": _RATIO,
        "panic_drop_days": _INT,
        "panic_add_ratio": _RATIO,
        "take_profit_percentile": _RATIO,
        "take_profit_ratio": _RATIO,
    },
    "risk": {
        "half_position_loss_days": _INT,
        "forbid_open_rise": _RATIO,
        "forbid_open_rsi": _LEVEL,
        "clear_tech_ma": _INT,
        "position_single_stock_max": _RATIO,
        "position_tech_total_max": _RATIO,
        "position_value_total_max": _RATIO,
        "annual_return_target": _RATIO,
        "volatility_target": _RATIO,
        "max_drawdown_limit": _RATIO,
    },
    "operation": {
        "schedule_times": _LIST_STR,
        "rebalance_monthly": _BOOL,
        "data_retry_times": _INT,
        "cache_expire_days": _INT,
        "valuation_history_period": _CHOICE,
    },
    # P3 新增：AI 解读层配置（模型代理池 + 超时 + 开关）
    "ai": {
        "enable_ai_analysis": _BOOL,
        "primary_model": _STR,           # 主力模型 ID（如 qwen3.8-max）
        "fallback_models": _LIST_STR,    # 降级备用模型列表
        "max_retries_per_model": _INT,
        "timeout_seconds": _INT,
        "max_tokens": _INT,
        "temperature": _RATIO,           # 0~1 浮点，复用 RATIO 校验
    },
}


class ConfigError(ValueError):
    """配置错误：settings.yaml 或 .env 缺失/非法时抛出，阻止程序带错启动"""


# ---------------------------------------------------------------------------
# settings.yaml 读取与字段校验
# ---------------------------------------------------------------------------
def _read_settings_yaml() -> Dict[str, Any]:
    """读取并解析 settings.yaml；文件缺失或 YAML 语法错误时抛 ConfigError"""
    if not SETTINGS_FILE.exists():
        raise ConfigError(f"配置文件不存在: {SETTINGS_FILE}")
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"settings.yaml 解析失败: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("settings.yaml 根节点必须为映射（字典）")
    return raw


def _coerce_field(section: str, field: str, value: Any, kind: str) -> Any:
    """按规则转换并校验单个字段；非法时抛 ConfigError（含中文明细）"""
    try:
        if kind == _INT:
            if isinstance(value, bool):
                raise ValueError("不能为布尔值")
            v = int(value)
            if v < 1:
                raise ValueError("必须为正整数")
            return v
        if kind == _BOOL:
            if not isinstance(value, bool):
                raise ValueError("必须为布尔值")
            return value
        if kind == _LEVEL:
            if isinstance(value, bool):
                raise ValueError("不能为布尔值")
            v = float(value)
            if not 0 <= v <= 100:
                raise ValueError("必须位于 0~100 之间")
            return v
        if kind == _RATIO:
            v = Decimal(str(value))
            if not 0 <= v <= 1:
                raise ValueError("必须位于 0~1 之间（小数表示比例）")
            return v
        if kind == _PRICE:
            v = Decimal(str(value))
            if v <= 0:
                raise ValueError("必须为正数（单位：元）")
            return v
        if kind == _FACTOR:
            v = Decimal(str(value))
            if v <= 0:
                raise ValueError("必须为正数（倍数）")
            return v
        if kind == _LIST_INT:
            if not isinstance(value, list) or not value:
                raise ValueError("必须为非空列表")
            v = [int(x) for x in value]
            if any(x < 1 for x in v):
                raise ValueError("元素必须为正整数")
            if v != sorted(v) or len(set(v)) != len(v):
                raise ValueError("元素必须严格递增且不重复")
            return v
        if kind == _LIST_STR:
            if not isinstance(value, list) or not value:
                raise ValueError("必须为非空列表")
            if not all(isinstance(x, str) and x.strip() for x in value):
                raise ValueError("必须为非空字符串列表")
            return value
        if kind == _CHOICE:
            allowed = _CHOICES.get((section, field), ())
            if value not in allowed:
                raise ValueError(f"必须为 {list(allowed)} 之一")
            return value
        if kind == _STR:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("必须为非空字符串")
            return value.strip()
    except (TypeError, ValueError) as e:
        raise ConfigError(f"settings.yaml [{section}.{field}] 非法: {e}") from e
    raise ConfigError(f"settings.yaml [{section}.{field}] 未知校验类型: {kind}")


def _cross_validate(section: str, data: Dict[str, Any]) -> List[str]:
    """节内交叉校验（下界 < 上界、仓位合计 = 100%、时间格式等），返回错误列表"""
    errors: List[str] = []

    def need(*fields: str) -> bool:
        """交叉校验依赖的字段是否都已成功解析（缺失项已在主校验报告）"""
        return all(f in data for f in fields)

    if section == "market_regime":
        if need("rsi_bear_low", "rsi_bear_high") and data["rsi_bear_low"] >= data["rsi_bear_high"]:
            errors.append("market_regime.rsi_bear_low 必须小于 rsi_bear_high")
        if need("macd_fast", "macd_slow") and data["macd_fast"] >= data["macd_slow"]:
            errors.append("market_regime.macd_fast 必须小于 macd_slow")

    if section == "tech":
        if need("rsi_min", "rsi_max") and data["rsi_min"] >= data["rsi_max"]:
            errors.append("tech.rsi_min 必须小于 rsi_max")
        if need("volatility_min", "volatility_max") and data["volatility_min"] >= data["volatility_max"]:
            errors.append("tech.volatility_min 必须小于 volatility_max")
        if need("price_min", "price_max") and data["price_min"] >= data["price_max"]:
            errors.append("tech.price_min 必须小于 price_max")
        if need("t_sell_ratio_min", "t_sell_ratio_max") and data["t_sell_ratio_min"] > data["t_sell_ratio_max"]:
            errors.append("tech.t_sell_ratio_min 不能大于 t_sell_ratio_max")
        if need("position_1st", "position_2nd", "position_3rd"):
            total = data["position_1st"] + data["position_2nd"] + data["position_3rd"]
            if total != Decimal("1.0"):
                errors.append(f"tech 分批建仓比例合计必须等于 100%，当前为 {total}")

    if section == "operation":
        if need("schedule_times"):
            for t in data["schedule_times"]:
                if not re.fullmatch(r"\d{2}:\d{2}", t):
                    errors.append(f"operation.schedule_times 时间格式非法: {t}（应为 HH:MM）")

    return errors


# ---------------------------------------------------------------------------
# 环境变量读取（.env 文件 + 进程环境变量）
# ---------------------------------------------------------------------------
def _parse_env_file(path: Path) -> Dict[str, str]:
    """解析 .env 文件（KEY=VALUE、# 注释、忽略空行），返回键值字典

    编码兼容：优先按 UTF-8 解码，失败回退 GBK，再失败以替换符兜底，
    保证 Windows 下以任意编码保存的 .env 都不会导致解析崩溃。
    """
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    raw_bytes = path.read_bytes()
    text = ""
    for enc in _ENV_ENCODINGS:
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        # 兜底：逐字节替换非法序列，保证不抛异常（注释乱码不影响键值解析）
        text = raw_bytes.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def _load_env() -> Dict[str, Any]:
    """读取 .env 并合并进程环境变量，按 ENV_REGISTRY 组装；缺失项为 None"""
    file_vars = _parse_env_file(ENV_FILE)
    merged = {**os.environ, **file_vars}  # .env 文件变量优先于进程环境变量

    env: Dict[str, Any] = {}
    for name, candidates in ENV_REGISTRY.items():
        env[name] = next((merged[c] for c in candidates if merged.get(c)), None)

    # 模拟总资产：金额字段必须使用 Decimal，默认 100 万
    capital_raw = merged.get(CAPITAL_ENV_KEY, DEFAULT_CAPITAL)
    try:
        capital = Decimal(str(capital_raw))
    except Exception as e:
        raise ConfigError(f"环境变量 {CAPITAL_ENV_KEY} 非法: {capital_raw!r}") from e
    if capital <= 0:
        raise ConfigError(f"环境变量 {CAPITAL_ENV_KEY} 必须为正数")
    env["simulated_capital"] = capital
    return env


# ---------------------------------------------------------------------------
# Settings 对象
# ---------------------------------------------------------------------------
class _Section:
    """配置节容器：以属性方式访问字段，如 settings.market_regime.ma_period"""

    def __init__(self, values: Dict[str, Any]) -> None:
        self.__dict__.update(values)


class Settings:
    """全局配置对象：策略节（属性访问）+ 环境变量（env 字典）"""

    def __init__(self, data: Dict[str, Dict[str, Any]], env: Dict[str, Any]) -> None:
        for section, values in data.items():
            setattr(self, section, _Section(values))
        self.env = env

    @property
    def simulated_capital(self) -> Decimal:
        """模拟总资产（元），用于计算模拟仓位"""
        return self.env["simulated_capital"]

    @property
    def dashscope_api_key(self) -> Optional[str]:
        """阿里云百炼 API Key（AI 解读层），未配置时为 None"""
        return self.env["dashscope_api_key"]

    @property
    def dashscope_base_url(self) -> Optional[str]:
        """阿里云百炼 API 端点地址（与 Key 独立配置），未配置时为 None"""
        return self.env["dashscope_base_url"]

    @property
    def deepseek_api_key(self) -> Optional[str]:
        """DeepSeek API Key（备用大模型），未配置时为 None"""
        return self.env["deepseek_api_key"]

    @property
    def pushplus_token(self) -> Optional[str]:
        """PushPlus 微信推送 Token，未配置时为 None（推送降级为控制台输出）"""
        return self.env["pushplus_token"]

    @property
    def enable_ai_analysis(self) -> bool:
        """AI 分析总开关（P3 新增）：False 时跳过 AI 层，直接输出纯量化信号"""
        return self.ai.enable_ai_analysis


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------
def load_settings() -> Settings:
    """加载并校验全部配置；聚合所有错误一次性抛出，便于一次性修正"""
    raw = _read_settings_yaml()
    errors: List[str] = []
    data: Dict[str, Dict[str, Any]] = {}

    for section, fields in _RULES.items():
        if section not in raw:
            errors.append(f"settings.yaml 缺少必需节 [{section}]")
            continue
        if not isinstance(raw[section], dict):
            errors.append(f"settings.yaml [{section}] 必须为映射（字典）")
            continue
        data[section] = {}
        for field, kind in fields.items():
            if field not in raw[section]:
                errors.append(f"settings.yaml 缺少必需字段 [{section}.{field}]")
                continue
            try:
                data[section][field] = _coerce_field(section, field, raw[section][field], kind)
            except ConfigError as e:
                errors.append(str(e))
        errors.extend(_cross_validate(section, data[section]))

    if errors:
        detail = "\n  - ".join(errors)
        raise ConfigError(f"配置校验失败（共 {len(errors)} 项），拒绝启动:\n  - {detail}")

    return Settings(data, _load_env())


_cached_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """获取全局配置单例（首次调用时加载并缓存）"""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = load_settings()
    return _cached_settings


# ---------------------------------------------------------------------------
# 命令行自检：python config.py 打印配置概览（绝不打印密钥本身）
# ---------------------------------------------------------------------------
def _self_check() -> None:
    """自检入口：验证配置可正常加载，并输出关键参数摘要"""
    s = get_settings()
    print(f"[OK] settings.yaml 加载成功: {SETTINGS_FILE}")
    print(f"[OK] 宏观判定 MA 周期: {s.market_regime.ma_period} 日 | 强制减仓: "
          f"连续 {s.market_regime.force_reduce_loss_days} 日收阴且跌幅 >= {s.market_regime.force_reduce_loss_drop}")
    print(f"[OK] 科技股区间: {s.tech.price_min} ~ {s.tech.price_max} 元 | 硬止损: {s.tech.stop_loss}")
    print(f"[OK] 分批建仓: {s.tech.position_1st} / {s.tech.position_2nd} / {s.tech.position_3rd}")
    print(f"[OK] 仓位上限: 单票 {s.risk.position_single_stock_max}, "
          f"科技板块 {s.risk.position_tech_total_max}, 底仓 {s.risk.position_value_total_max}")
    print(f"[OK] 调度时刻: {s.operation.schedule_times} | 月末再平衡: {s.operation.rebalance_monthly}")
    print(f"[OK] 模拟总资产: {s.simulated_capital} 元")
    print(f"[OK] 阿里云百炼 Key: {'已配置' if s.dashscope_api_key else '未配置'} | "
          f"Base URL: {'已配置' if s.dashscope_base_url else '未配置'}")
    print(f"[OK] PushPlus Token: {'已配置' if s.pushplus_token else '未配置'}")
    # P3 新增：AI 层配置摘要（不打印 API Key）
    print(f"[OK] AI 分析开关: {'开启' if s.enable_ai_analysis else '关闭'} | "
          f"主力模型: {s.ai.primary_model} | 备用: {s.ai.fallback_models}")
    print(f"[OK] AI 超时: {s.ai.timeout_seconds}s | 重试: {s.ai.max_retries_per_model}/模型 | "
          f"温度: {s.ai.temperature} | max_tokens: {s.ai.max_tokens}")


if __name__ == "__main__":
    _self_check()
