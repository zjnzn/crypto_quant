"""
application/services/monitor.py  (Phase 5 完整版)

监控服务：P&L / 回撤 / 告警 / 绩效指标。

新增（Phase 5）：
  - max_drawdown:  历史最大回撤（非仅当前）
  - sharpe_ratio:  年化 Sharpe 比率（基于逐笔交易收益）
  - win_rate:      盈利交易比例
  - daily_pnl:     每日 P&L 字典
  - snapshot():    一次性获取全部指标

订阅：
  TradeEvent           → 更新标记价格
  PositionUpdatedEvent → 更新已实现 P&L，记录胜负
  RiskRejectedEvent    → hard 拒绝告警
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from core.ports.account import AccountPort
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.events import (AlertEvent, PnLEvent,
                                 PositionUpdatedEvent,
                                 RiskRejectedEvent, TradeEvent)

log = logging.getLogger(__name__)


class MonitorService:
    """
    完整监控服务（Phase 5）。
    所有属性只读，由内部事件驱动更新。
    """

    def __init__(
        self,
        bus:         EventBusPort,
        cache:       CachePort,
        account:     AccountPort,
        account_id:  str,
        initial_nav: Decimal,
        warn_dd:     float = 0.03,
        critical_dd: float = 0.05,
    ) -> None:
        self._bus            = bus
        self._cache          = cache
        self._account        = account
        self._account_id     = account_id
        self._initial_nav    = initial_nav
        self._warn_dd        = Decimal(str(warn_dd))
        self._critical_dd    = Decimal(str(critical_dd))

        # ── P&L 追踪 ─────────────────────────────────────────────────────────
        self._realized_pnl   = Decimal(0)
        self._high_watermark = initial_nav

        # ── 历史最大回撤 ──────────────────────────────────────────────────────
        self._max_drawdown   = Decimal(0)

        # ── Sharpe：收益序列（每笔交易的相对收益率）──────────────────────────
        self._returns:       list[float] = []
        self._prev_nav:      Decimal     = initial_nav

        # ── 胜率追踪 ──────────────────────────────────────────────────────────
        self._wins:          int = 0
        self._losses:        int = 0
        self._breakeven:     int = 0

        # ── 每日 P&L ─────────────────────────────────────────────────────────
        self._daily_pnl:    dict[str, Decimal] = defaultdict(Decimal)

        # ── 告警状态 ──────────────────────────────────────────────────────────
        self._last_alert_dd = Decimal(0)

        bus.subscribe(TradeEvent,           self._on_trade)
        bus.subscribe(PositionUpdatedEvent, self._on_position_updated)
        bus.subscribe(RiskRejectedEvent,    self._on_risk_rejected)

        log.info("MonitorService 启动  init_nav=%s  warn=%.0f%%  critical=%.0f%%",
                 initial_nav, warn_dd * 100, critical_dd * 100)

    # ── 事件处理 ──────────────────────────────────────────────────────────────

    def _on_trade(self, event: TradeEvent) -> None:
        self._cache.set(f"price:{event.instrument.symbol}", event.price)

    def _on_position_updated(self, event: PositionUpdatedEvent) -> None:
        pnl = event.realized_pnl
        self._realized_pnl += pnl

        # 胜率统计（只统计有实际平仓的事件）
        if pnl > Decimal("0.01"):
            self._wins += 1
        elif pnl < Decimal("-0.01"):
            self._losses += 1
        else:
            if abs(pnl) > Decimal("0.0001"):   # 有成交才计
                self._breakeven += 1

        # NAV & Sharpe
        nav = self.nav
        if self._prev_nav > 0 and nav != self._prev_nav:
            ret = float((nav - self._prev_nav) / self._prev_nav)
            self._returns.append(ret)
            self._prev_nav = nav

        # 每日 P&L
        day_key = str(self._clock_date())
        self._daily_pnl[day_key] += pnl

        self._emit_pnl(parent=event)

    def _on_risk_rejected(self, event: RiskRejectedEvent) -> None:
        if event.level == "hard":
            self._bus.publish(AlertEvent(
                level  = "warn",
                title  = "风控拦截",
                detail = {
                    "account": event.account_id,
                    "symbol":  (event.order.instrument.symbol
                          if event.order and event.order.instrument
                          else ""),
                    "reason":  event.reason,
                },
            ).caused_by(event))

    # ── 内部计算 ──────────────────────────────────────────────────────────────

    def _emit_pnl(self, parent) -> None:
        nav   = self.nav
        total = nav - self._initial_nav

        # 高水位 & 回撤
        if nav > self._high_watermark:
            self._high_watermark = nav
        if self._high_watermark > 0:
            dd = max(Decimal(0),
                     (self._high_watermark - nav) / self._high_watermark)
        else:
            dd = Decimal(0)

        # 更新历史最大回撤
        if dd > self._max_drawdown:
            self._max_drawdown = dd

        # 写缓存（供 DrawdownMiddleware 读取）
        self._cache.set(f"drawdown:{self._account_id}", dd)

        unrealized = total - self._realized_pnl

        self._bus.publish(PnLEvent(
            account_id = self._account_id,
            realized   = self._realized_pnl,
            unrealized = unrealized,
            total      = total,
            nav_usdt   = nav,
        ).caused_by(parent))

        self._check_drawdown_alert(dd, parent)

    def _check_drawdown_alert(self, dd: Decimal, parent) -> None:
        if dd >= self._critical_dd and self._last_alert_dd < self._critical_dd:
            self._last_alert_dd = dd
            self._bus.publish(AlertEvent(
                level  = "critical",
                title  = f"回撤熔断 {float(dd)*100:.1f}%",
                detail = {"drawdown": float(dd), "account": self._account_id},
            ).caused_by(parent))
            log.warning("!! 回撤熔断 %.1f%% !!", float(dd) * 100)
        elif dd >= self._warn_dd and self._last_alert_dd < self._warn_dd:
            self._last_alert_dd = dd
            self._bus.publish(AlertEvent(
                level  = "warn",
                title  = f"回撤告警 {float(dd)*100:.1f}%",
                detail = {"drawdown": float(dd), "account": self._account_id},
            ).caused_by(parent))
            log.warning("回撤告警 %.1f%%", float(dd) * 100)
        elif dd < self._warn_dd * Decimal("0.5"):
            self._last_alert_dd = Decimal(0)

    def _clock_date(self) -> date:
        try:
            ts = self._cache.get(f"last_ts:{self._account_id}")
            return ts.date() if ts else date.today()
        except Exception:
            return date.today()

    # ── 公开属性（只读）──────────────────────────────────────────────────────

    @property
    def nav(self) -> Decimal:
        return self._account.get_nav_usdt(self._account_id)

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def current_drawdown(self) -> Decimal:
        return self._cache.get(f"drawdown:{self._account_id}") or Decimal(0)

    @property
    def max_drawdown(self) -> Decimal:
        """历史最大回撤（回测期间峰值到谷底的最大下跌比例）。"""
        return self._max_drawdown

    @property
    def win_rate(self) -> float:
        """盈利交易占比 [0, 1]。仅统计有已实现盈亏的交易。"""
        total = self._wins + self._losses
        return self._wins / total if total > 0 else 0.0

    @property
    def total_trades(self) -> int:
        return self._wins + self._losses + self._breakeven

    @property
    def sharpe_ratio(self) -> float:
        """
        年化 Sharpe 比率（假设无风险利率 = 0）。

        基于逐笔交易的相对收益率计算。
        假设每笔交易平均持续 1 小时，年化因子 = sqrt(8760)。
        """
        returns = self._returns
        n = len(returns)
        if n < 4:      # 至少 4 笔交易才有统计意义
            return 0.0

        mean   = sum(returns) / n
        var    = sum((r - mean) ** 2 for r in returns) / n
        std    = math.sqrt(var)
        if std < 1e-10:
            return float("inf") if mean > 0 else 0.0

        # 年化（每小时一笔 → 8760 笔/年）
        annualized = mean / std * math.sqrt(8760)
        return round(annualized, 3)

    @property
    def daily_pnl(self) -> dict[str, float]:
        """每日已实现 P&L（键为 YYYY-MM-DD 字符串）。"""
        return {k: float(v) for k, v in self._daily_pnl.items()}

    def snapshot(self) -> dict:
        """返回全部监控指标的字典快照（用于日报/API 接口）。"""
        nav   = float(self.nav)
        total = nav - float(self._initial_nav)
        return {
            "account_id":      self._account_id,
            "nav_usdt":        nav,
            "initial_nav":     float(self._initial_nav),
            "total_pnl":       total,
            "total_pnl_pct":   total / float(self._initial_nav) * 100,
            "realized_pnl":    float(self._realized_pnl),
            "unrealized_pnl":  total - float(self._realized_pnl),
            "current_drawdown_pct": float(self.current_drawdown) * 100,
            "max_drawdown_pct":     float(self._max_drawdown) * 100,
            "sharpe_ratio":    self.sharpe_ratio,
            "win_rate_pct":    self.win_rate * 100,
            "total_trades":    self.total_trades,
            "wins":            self._wins,
            "losses":          self._losses,
            "daily_pnl":       self.daily_pnl,
        }

    def reset_baseline(self) -> None:
        """
        重置监控基准为当前真实净值。

        必须在实盘启动检查（rebuild_from_exchange）之后调用，
        否则 MonitorService 会用 config 里的 initial_usdt 作基准，
        而实际账户余额可能与配置值差距很大（例如 testnet 赠送资金）。

        调用时机：
          system = build(cfg)
          startup_checks(system, instruments)   # 同步真实余额
          system.monitor.reset_baseline()        # ← 必须在此调用
          feed.start()
        """
        actual_nav           = self.nav
        self._initial_nav    = actual_nav
        self._high_watermark = actual_nav
        self._prev_nav       = actual_nav
        self._realized_pnl   = Decimal(0)
        self._returns.clear()
        self._wins = self._losses = self._breakeven = 0
        self._daily_pnl.clear()
        self._last_alert_dd  = Decimal(0)
        self._max_drawdown   = Decimal(0)
        # 清除回撤缓存，避免 RiskService 读到旧值
        self._cache.delete(f"drawdown:{self._account_id}")
        log.info("MonitorService 基准重置 → %.2f USDT（实际账户净值）",
                 float(actual_nav))

