"""
application/risk/builtin.py  —  内置风控中间件

加密交易专用内置规则：
  PositionLimitMiddleware   — 单标的仓位上限
  MaxLeverageMiddleware     — 全局最大杠杆
  DrawdownMiddleware        — 日内回撤熔断
  FundingRateMiddleware     — 资金费率过高禁止开多
  MinNotionalMiddleware     — 最小名义价值检查
  OpenOrdersLimitMiddleware — 在途订单数量上限
"""
from __future__ import annotations

import logging
from decimal import Decimal

from core.domain.order import Order, Side
from core.ports.risk import RiskContext, RiskResult
from application.risk.pipeline import RiskMiddleware, Next

log = logging.getLogger(__name__)


class PositionLimitMiddleware(RiskMiddleware):
    """
    单标的仓位权重不超过账户净值的 max_weight。

    weight = |new_size * price| / nav_usdt
    """
    name = "position_limit"

    def __init__(self, max_weight: float = 0.10) -> None:
        self._max = Decimal(str(max_weight))

    def _do_check(self, order: Order, ctx: RiskContext,
                  call_next: Next) -> RiskResult:
        cur = ctx.positions.get(order.instrument.symbol)
        cur_size = cur.size if cur else Decimal(0)

        # 在途订单增量（由 RiskService 预计算）
        pending_delta = ctx.extra.get("pending_delta", Decimal(0))

        delta    = order.qty if order.side == Side.BUY else -order.qty
        # new_size = 当前持仓 + 在途增量 + 本单增量
        new_size = cur_size + pending_delta + delta

        # ★ market order 无 limit_price，从 limit_price 或 cache 解析预估价
        est_price = self._resolve_est_price(order, ctx)
        if est_price is None:
            # 无法获取价格，拒绝订单（保守策略）
            return RiskResult.reject(
                f"{order.instrument.symbol} 无法估算价格，拒绝 market order"
            )
        if ctx.nav_usdt > 0:
            weight = abs(new_size * est_price) / ctx.nav_usdt
            # 小账户容差：当 NAV 不足以开交易所最小仓位时，允许适度超限
            # 只在 min_notional / nav > max_weight 时启用（即"被迫超限"）
            max_allowed = self._max
            if ctx.nav_usdt > 0:
                min_weight_needed = order.instrument.min_notional / ctx.nav_usdt
                if min_weight_needed > self._max:
                    # 被迫超限：放宽到 max_weight * 1.5
                    max_allowed = self._max * Decimal("1.5")
            if weight > max_allowed:
                return RiskResult.reject(
                    f"{order.instrument.symbol} 权重 {weight:.1%} "
                    f"超过上限 {max_allowed:.1%}"
                )
        return call_next(order, ctx)


class MaxLeverageMiddleware(RiskMiddleware):
    """全局最大杠杆倍数检查。"""
    name = "max_leverage"

    def __init__(self, global_max: int = 10) -> None:
        self._max = global_max

    def _do_check(self, order: Order, ctx: RiskContext,
                  call_next: Next) -> RiskResult:
        cur = ctx.positions.get(order.instrument.symbol)
        leverage = cur.leverage if cur else 1
        allowed  = min(order.instrument.max_leverage, self._max)

        if leverage > allowed:
            return RiskResult.reject(
                f"{order.instrument.symbol} 杠杆 {leverage}x "
                f"超过上限 {allowed}x"
            )
        return call_next(order, ctx)


class DrawdownMiddleware(RiskMiddleware):
    """
    日内回撤超过阈值时，禁止所有非平仓开仓。
    需要 ctx.extra["daily_drawdown"] 由 MonitorService 定期写入。
    """
    name = "drawdown"

    def __init__(self, max_drawdown: float = 0.05) -> None:
        self._max = Decimal(str(max_drawdown))

    def _do_check(self, order: Order, ctx: RiskContext,
                  call_next: Next) -> RiskResult:
        drawdown = ctx.extra.get("daily_drawdown", Decimal(0))
        if isinstance(drawdown, float):
            drawdown = Decimal(str(drawdown))

        if drawdown > self._max:
            return RiskResult.reject(
                f"日内回撤 {drawdown:.1%} 超过熔断线 {self._max:.1%}，"
                f"禁止开仓",
                level="hard",
            )
        elif drawdown > self._max * Decimal("0.8"):
            log.warning("回撤 %.1f%% 接近熔断线 %.1f%%",
                        float(drawdown) * 100, float(self._max) * 100)
            return call_next(order, ctx)  # 软告警，放行但记录

        return call_next(order, ctx)


class FundingRateMiddleware(RiskMiddleware):
    """
    资金费率过高时禁止开多仓（持多仓需持续支付资金费，侵蚀收益）。
    对平仓单、空仓开仓单一律放行。
    """
    name = "funding_rate"

    def __init__(self, max_rate: float = 0.003) -> None:
        """max_rate: 每8小时资金费率上限，默认 0.3%。"""
        self._max = Decimal(str(max_rate))

    def _do_check(self, order: Order, ctx: RiskContext,
                  call_next: Next) -> RiskResult:
        # 卖单（平多/开空）不需付资金费，放行；reduce_only 已由基类过滤
        if order.side == Side.SELL:
            return call_next(order, ctx)

        rate = ctx.funding_rates.get(order.instrument.symbol, Decimal(0))
        if rate > self._max:
            return RiskResult.reject(
                f"{order.instrument.symbol} 资金费率 {rate:.4%}/8h "
                f"超过阈值 {self._max:.4%}，禁止开多",
                level="soft",   # soft：记录但不强制拒绝（策略可覆盖）
            )
        return call_next(order, ctx)


class MinNotionalMiddleware(RiskMiddleware):
    """订单名义价值必须满足交易所最小要求。"""
    name = "min_notional"
    check_reduce_only = False   # 平仓单也需满足最小名义价值

    def _do_check(self, order: Order, ctx: RiskContext,
                  call_next: Next) -> RiskResult:
        # ★ market order 无 limit_price，从 limit_price 或 cache 解析预估价
        est_price = self._resolve_est_price(order, ctx)
        if est_price is None:
            # 无法获取价格，跳过最小名义价值检查（不阻塞交易）
            return call_next(order, ctx)
        notional  = order.qty * est_price
        min_n     = order.instrument.min_notional

        if notional < min_n:
            return RiskResult.reject(
                f"名义价值 {notional:.2f} {order.instrument.quote} "
                f"低于最小要求 {min_n} {order.instrument.quote}"
            )
        return call_next(order, ctx)


class OpenOrdersLimitMiddleware(RiskMiddleware):
    """在途订单数量不超过上限（防止意外堆积）。"""
    name = "open_orders_limit"
    check_reduce_only = False   # 平仓单也计入在途数量限制

    def __init__(self, max_open: int = 20) -> None:
        self._max = max_open

    def _do_check(self, order: Order, ctx: RiskContext,
                  call_next: Next) -> RiskResult:
        count = len([o for o in ctx.open_orders
                     if o.instrument.symbol == order.instrument.symbol
                     and o.status.is_active])
        if count >= self._max:
            return RiskResult.reject(
                f"{order.instrument.symbol} 在途订单 {count} 达到上限 {self._max}"
            )
        return call_next(order, ctx)
