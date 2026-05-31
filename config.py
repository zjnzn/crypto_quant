"""
config.py

配置数据类。从 config.yaml 加载，或在代码中直接构造。

修改运行模式只需改 config.yaml，不改任何业务代码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class BusConfig:
    backend:       str = "sync"              # "sync" | "kafka"
    kafka_servers: str = "localhost:9092"


@dataclass
class CacheConfig:
    backend:    str = "memory"               # "memory" | "redis"
    redis_host: str = "localhost"
    redis_port: int = 6379


@dataclass
class ExecutionConfig:
    exchange:   str = "paper"               # "paper" | "binance" | "okx"
    api_key:    str = ""
    api_secret: str = ""


@dataclass
class RiskConfig:
    max_weight:       float = 0.10
    max_leverage:     int   = 10
    max_drawdown:     float = 0.05
    max_funding_rate: float = 0.003
    warn_drawdown:    float = 0.03


@dataclass
class AccountConfig:
    main_id:      str   = "main"
    initial_usdt: float = 10_000.0


@dataclass
class PortfolioConfig:
    min_score:      float = 0.15   # 信号合并后最低分数阈值
    order_cooldown: float = 0.0    # 同标的两次下单最小间隔（秒）


@dataclass
class MonitorConfig:
    warn_drawdown:     float = 0.03
    critical_drawdown: float = 0.05


@dataclass
class Config:
    mode:      str            = "backtest"   # "backtest" | "paper" | "live"
    bus:       BusConfig      = field(default_factory=BusConfig)
    cache:     CacheConfig    = field(default_factory=CacheConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk:      RiskConfig       = field(default_factory=RiskConfig)
    account:   AccountConfig    = field(default_factory=AccountConfig)
    portfolio: PortfolioConfig  = field(default_factory=PortfolioConfig)
    monitor:   MonitorConfig    = field(default_factory=MonitorConfig)
    strategies: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str) -> Config:
        import yaml
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        return cls(
            mode       = raw.get("mode", "backtest"),
            bus        = BusConfig(**raw.get("bus", {})),
            cache      = CacheConfig(**raw.get("cache", {})),
            execution  = ExecutionConfig(**raw.get("execution", {})),
            risk       = RiskConfig(**raw.get("risk", {})),
            account    = AccountConfig(**raw.get("account", {})),
            portfolio  = PortfolioConfig(**raw.get("portfolio", {})),
            monitor    = MonitorConfig(**raw.get("monitor", {})),
            strategies = raw.get("strategies", []),
        )

    @classmethod
    def backtest_default(
        cls,
        initial_usdt: float = 10_000.0,
        strategies: list[dict] | None = None,
    ) -> Config:
        """快速构建回测默认配置（代码中直接使用，无需 yaml 文件）。"""
        return cls(
            mode       = "backtest",
            account    = AccountConfig(initial_usdt=initial_usdt),
            strategies = strategies or [],
        )
