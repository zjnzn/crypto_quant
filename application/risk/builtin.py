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
from core.domain.position import PositionSide
from core.ports.risk import RiskContext, RiskResult
from application.risk.pipeline import RiskMiddleware, Next

log = logging.getLogger(__name__)


class PositionLimitMiddleware(RiskMiddleware):
    """
    单标的保证金占用不超过账户净值的 max_weight。

    保证金占用 = 名义价值 / 杠杆 = |new_size * price| / leverage
    weight = 保证金占用 / nav_usdt

    若 max_weight=10%，leverage=5x，则名义价值可达 50% NAV。
    """
    name = "position_limit"

    def __init__(self, max_weight: float = 0.10, leverage: int = 1) -> None:
        self._max = Decimal(str(max_weight))
        self._leverage = leverage

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        if order.reduce_only:
            return call_next(order, ctx)   # 平仓单直接放行

        cur = ctx.positions.get(order.instrument.symbol)
        # 带符号当前仓位：多头为正，空头为负
        if cur and not cur.is_empty:
            cur_size = cur.size if cur.side == PositionSide.LONG else -cur.size
        else:
            cur_size = Decimal(0)

        # BUY 增加仓位，SELL 减少仓位
        delta = order.qty if order.side == Side.BUY else -order.qty
        new_size = cur_size + delta

        # 用 limit_price 估算，无 limit_price 用 0（market order，偏保守）
        est_price = order.limit_price or Decimal(1)
        if ctx.nav_usdt > 0:
            # 获取杠杆倍数：优先从仓位取，新开仓用配置的默认值
            leverage = cur.leverage if cur and cur.leverage > 0 else self._leverage

            # 保证金占用 = 名义价值 / 杠杆
            notional = abs(new_size * est_price)
            margin_used = notional / leverage
            weight = margin_used / ctx.nav_usdt

            if weight > self._max:
                return RiskResult.reject(
                    f"{order.instrument.symbol} 保证金占用 {weight:.1%} "
                    f"超过上限 {self._max:.1%}（杠杆 {leverage}x）"
                )
        return call_next(order, ctx)


class MaxLeverageMiddleware(RiskMiddleware):
    """全局最大杠杆倍数检查。"""
    name = "max_leverage"

    def __init__(self, global_max: int = 10) -> None:
        self._max = global_max

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        if order.reduce_only:
            return call_next(order, ctx)

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

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        if order.reduce_only:
            return call_next(order, ctx)

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

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        if order.reduce_only or order.side == Side.SELL:
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

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        est_price = order.limit_price or Decimal(1)
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

    def __init__(self, max_open: int = 20) -> None:
        self._max = max_open

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        count = len([o for o in ctx.open_orders
                     if o.instrument.symbol == order.instrument.symbol
                     and o.status.is_active])
        if count >= self._max:
            return RiskResult.reject(
                f"{order.instrument.symbol} 在途订单 {count} 达到上限 {self._max}"
            )
        return call_next(order, ctx)


class MinRebalanceMiddleware(RiskMiddleware):
    """
    最小调仓量过滤:仓位变化小于 NAV 的 min_delta_pct 比例时拒绝下单。
    
    实盘场景:
      - 避免频繁小额调仓,手续费吞噬收益
      - 例如 min_delta_pct=0.05 表示调仓量必须 ≥ 5% NAV
    
    特例:
      - 完全平仓(订单量等于当前持仓)放行,即使 delta 小于阈值
      - reduce_only 订单检查实际减仓量
    """
    name = "min_rebalance"

    def __init__(self, min_delta_pct: float = 0.05) -> None:
        self._min_delta_pct = Decimal(str(min_delta_pct))

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        # 1. 计算订单名义价值
        est_price = order.limit_price or Decimal("1")
        order_notional = order.qty * est_price
        
        # 2. 获取当前持仓
        cur_pos = ctx.positions.get(order.instrument.symbol)
        
        # 3. 判断是否完全平仓
        if order.reduce_only and cur_pos and not cur_pos.is_empty:
            if order.qty >= cur_pos.size * Decimal("0.99"):  # 允许 1% 滑点
                # 完全平仓,放行
                return call_next(order, ctx)
        
        # 4. 检查调仓量是否足够大
        nav = ctx.nav_usdt
        min_delta_usdt = nav * self._min_delta_pct
        
        if order_notional < min_delta_usdt:
            return RiskResult.reject(
                f"调仓量过小: {order_notional:.2f} USDT < "
                f"最小要求 {min_delta_usdt:.2f} USDT "
                f"({float(self._min_delta_pct)*100:.1f}% NAV)"
            )
        
        return call_next(order, ctx)


class ExpectedProfitMiddleware(RiskMiddleware):
    """
    预期收益必须大于交易成本(手续费 + 滑点)。
    
    实盘场景:
      - 手续费: 币安永续 0.02% (maker) / 0.04% (taker)
      - 滑点: 通常 0.01% ~ 0.05%
      - 总成本: 约 0.05% ~ 0.10%
    
    使用方式:
      1. 策略在 SignalEvent.meta 中传递 expected_return_pct
      2. PortfolioService 将其写入 RiskContext.extra["expected_return_pct"]
      3. 本中间件检查: expected_return ≥ min_profit_multiplier × trading_cost
    
    参数:
      trading_cost_pct: 单边交易成本百分比 (如 0.0007 = 0.07%)
      min_profit_multiplier: 安全系数,预期收益必须 >= multiplier × 成本
                             推荐值 2.0 ~ 3.0
    """
    name = "expected_profit"

    def __init__(
        self,
        trading_cost_pct: float = 0.0007,      # 0.07%
        min_profit_multiplier: float = 3.0,    # 3x 安全系数
    ) -> None:
        self._trading_cost_pct = Decimal(str(trading_cost_pct))
        self._profit_multiplier = Decimal(str(min_profit_multiplier))

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        # 1. 平仓订单直接放行
        if order.reduce_only:
            return call_next(order, ctx)
        
        # 2. 获取预期收益率
        expected_return_pct = ctx.extra.get("expected_return_pct")
        
        if expected_return_pct is None:
            # 无预期收益信息,记录警告后放行
            log.warning(
                "ExpectedProfitMiddleware: %s 无预期收益信息,建议策略传递 expected_return_pct",
                order.instrument.symbol
            )
            return call_next(order, ctx)
        
        expected_return_pct = Decimal(str(expected_return_pct))
        
        # 3. 计算最小要求收益
        min_required_pct = self._trading_cost_pct * self._profit_multiplier
        
        # 4. 比较
        if expected_return_pct < min_required_pct:
            return RiskResult.reject(
                f"预期收益过低: {float(expected_return_pct)*100:.3f}% < "
                f"最小要求 {float(min_required_pct)*100:.3f}% "
                f"(成本{float(self._trading_cost_pct)*100:.3f}% × {float(self._profit_multiplier)}x安全系数)"
            )
        
        return call_next(order, ctx)
