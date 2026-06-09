"""
application/services/risk.py

风控服务。

订阅：TargetPositionEvent
发布：RiskApprovedEvent | RiskRejectedEvent

职责：
  1. 从 TargetPositionEvent 构建待检查的 Order
  2. 从 cache + account 组装 RiskContext
  3. 调用 RiskPipeline.check()
  4. 发布审核结果事件
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

from core.domain.instrument import Instrument
from core.domain.order import Order, OrderStatus, OrderType, Side
from core.domain.position import PositionSide
from core.ports.account import AccountPort
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from core.ports.execution import ExecutionPort
from core.ports.risk import RiskContext
from application.events import (RiskApprovedEvent, RiskRejectedEvent,
                                 TargetPositionEvent)
from application.risk.pipeline import RiskPipeline

log = logging.getLogger(__name__)


class RiskService:
    """
    将目标仓位提交风控管道审核。

    通过审核 → RiskApprovedEvent（携带最终 Order）
    未通过  → RiskRejectedEvent（携带拒绝原因）

    soft 级别拒绝：记录警告，仍发布 RiskApprovedEvent（策略可接受此风险）
    hard 级别拒绝：发布 RiskRejectedEvent，链路终止
    """

    def __init__(
        self,
        bus:       EventBusPort,
        pipeline:  RiskPipeline,
        account:   AccountPort,
        cache:     CachePort,
        exchange:  ExecutionPort | None = None,
        reversal_threshold: float = 0.50,  # 翻仓最小信号强度
    ) -> None:
        self._bus       = bus
        self._pipeline  = pipeline
        self._account   = account
        self._cache     = cache
        self._exchange  = exchange
        self._reversal_threshold = Decimal(str(reversal_threshold))
        self._bus      = bus
        self._pipeline = pipeline
        self._account  = account
        self._cache    = cache
        self._exchange = exchange

        bus.subscribe(TargetPositionEvent, self._on_target)
        log.info("RiskService 启动，中间件: %s",
                 self._pipeline.middleware_names)

    def _on_target(self, event: TargetPositionEvent) -> None:
        instrument = event.instrument
        sym        = instrument.symbol

        # ── 0. 从交易所获取真实仓位（覆盖系统内部状态）─────────────────────────
        # 系统内部 current_size 可能与交易所不一致（订单失败、重启丢失状态等）
        # 用交易所真实仓位计算 delta，确保下单决策正确
        real_current_size = event.current_size  # 默认使用系统内部值

        if self._exchange:
            try:
                real_pos = self._exchange.get_position(instrument, event.account_id)
                if real_pos and not real_pos.is_empty:
                    # 转换为带符号 size：正=多头，负=空头
                    real_current_size = (real_pos.size if real_pos.side == PositionSide.LONG
                                         else -real_pos.size)
                    log.debug("risk 真实仓位: %s size=%.6f (系统内部=%.6f)",
                              sym, real_current_size, event.current_size)
                else:
                    real_current_size = Decimal(0)
                    log.debug("risk 真实仓位: %s 无持仓 (系统内部=%.6f)",
                              sym, event.current_size)
            except Exception as e:
                log.warning("risk 获取真实仓位失败 %s: %s，使用系统内部值", sym, e)

        # ── 1. 计算下单 delta ─────────────────────────────────────────────────
        # target_size 和 current_size 均带符号：正=多头，负=空头
        # delta > 0 → 需要买入（增多头/减空头）
        # delta < 0 → 需要卖出（减多头/增空头）
        delta = event.target_size - real_current_size
        if abs(delta) < instrument.lot_size:
            return   # delta 太小，忽略

        # ── 2. 翻仓检测 ────────────────────────────────────────────────────────
        # 翻仓：real_current_size 和 target_size 符号相反
        # 例如 current=-0.45 (空头), target=0.20 (多头) → delta=0.65
        # 翻仓时 delta = |平仓量| + |开仓量|，需要保证金远超账户余额
        # 解决：分两步下单，先平仓释放保证金，再开新仓
        is_reversal = (real_current_size > 0 and event.target_size < 0) or \
                      (real_current_size < 0 and event.target_size > 0)

        if is_reversal:
            # ── 翻仓信号强度检查 ────────────────────────────────────────────────
            # 翻仓需要更强的信号(避免频繁翻仓带来双倍手续费)
            signal_score = self._cache.get(f"signal_score:{sym}")

            if signal_score is None:
                log.warning("翻仓检测: %s 无信号分数信息,跳过翻仓", sym)
                return

            signal_score = Decimal(str(signal_score))

            # 检查信号绝对值是否足够强
            if abs(signal_score) < self._reversal_threshold:
                log.warning(
                    "翻仓拒绝: %s 信号强度 %.3f < 阈值 %.3f,跳过翻仓",
                    sym, float(abs(signal_score)), float(self._reversal_threshold)
                )
                return

            # 第一步：平掉当前仓位
            close_qty = instrument.round_qty(abs(real_current_size))
            close_side = Side.SELL if real_current_size > 0 else Side.BUY
            self._publish_close_order(event, instrument, close_side, close_qty)
            # 第二步：开新仓位（将在下一个信号周期自然触发）
            # 因为平仓后 current_size ≈ 0，下次信号计算 delta = target_size
            log.info("翻仓分步: %s 先平仓 %s %.6f，新仓将在下次信号触发",
                     sym, close_side.value, close_qty)
            return

        # ── 3. 构建订单 ───────────────────────────────────────────────────────
        price: Decimal | None = self._cache.get(f"price:{sym}")
        is_buy = delta > 0
        order_side = Side.BUY if is_buy else Side.SELL
        order_qty = instrument.round_qty(abs(delta))

        # 单向持仓模式下，减仓操作必须设 reduceOnly=true
        # 判断逻辑：delta 方向与持仓方向相反 → 减仓
        # 例：多头(delta<0→SELL减仓) 或 空头(delta>0→BUY减仓)
        if real_current_size != 0:
            # 有持仓时，delta 方向与持仓相反 → 纯减仓
            # 多头持仓(current>0) + SELL(delta<0) → reduceOnly
            # 空头持仓(current<0) + BUY(delta>0) → reduceOnly
            is_reducing = (real_current_size > 0 and not is_buy) or \
                          (real_current_size < 0 and is_buy)
            reduce_only = is_reducing
        else:
            reduce_only = False

        order = Order(
            instrument  = instrument,
            account_id  = event.account_id,
            side        = order_side,
            qty         = order_qty,
            order_type  = OrderType.MARKET,
            strategy_id = "",
            leverage    = event.leverage,  # 从事件获取杠杆倍数
            limit_price = price,
            reduce_only = reduce_only,
        )

        # ── 3. 组装 RiskContext ───────────────────────────────────────────────
        ctx = self._build_context(event.account_id, instrument.symbol)

        # ── 4. 执行风控检查 ───────────────────────────────────────────────────
        result = self._pipeline.check(order, ctx)

        # ── 5. 发布结果 ───────────────────────────────────────────────────────
        if result.passed:
            if result.level == "soft" and result.reason:
                log.warning("风控软告警 %s: %s", sym, result.reason)

            final_order = result.amended_order or order
            self._bus.publish(
                RiskApprovedEvent(
                    account_id=event.account_id,
                    order=final_order,
                ).caused_by(event)
            )
            log.debug("risk ✓  %s  qty=%.6f  side=%s",
                      sym, final_order.qty, final_order.side.value)
        else:
            self._bus.publish(
                RiskRejectedEvent(
                    account_id=event.account_id,
                    order=order,
                    reason=result.reason,
                    level=result.level,
                ).caused_by(event)
            )
            log.warning("risk ✗  %s  [%s] %s",
                        sym, result.level, result.reason)

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _publish_close_order(
        self,
        event:      TargetPositionEvent,
        instrument:  Instrument,
        side:        Side,
        qty:         Decimal,
    ) -> None:
        """发布平仓订单（翻仓第一步），reduceOnly=true。"""
        price: Decimal | None = self._cache.get(f"price:{instrument.symbol}")
        order = Order(
            instrument  = instrument,
            account_id  = event.account_id,
            side        = side,
            qty         = qty,
            order_type  = OrderType.MARKET,
            strategy_id = "",
            leverage    = event.leverage,  # 从事件获取杠杆倍数
            limit_price = price,
            reduce_only = True,
        )

        ctx = self._build_context(event.account_id, instrument.symbol)
        result = self._pipeline.check(order, ctx)

        if result.passed:
            final_order = result.amended_order or order
            self._bus.publish(
                RiskApprovedEvent(
                    account_id=event.account_id,
                    order=final_order,
                ).caused_by(event)
            )
            log.debug("risk ✓ (close)  %s  qty=%.6f  side=%s",
                      instrument.symbol, final_order.qty, final_order.side.value)
        else:
            self._bus.publish(
                RiskRejectedEvent(
                    account_id=event.account_id,
                    order=order,
                    reason=result.reason,
                    level=result.level,
                ).caused_by(event)
            )
            log.warning("risk ✗ (close)  %s  [%s] %s",
                        instrument.symbol, result.level, result.reason)

    def _build_context(self, account_id: str, symbol: str) -> RiskContext:
        """从 cache + account 组装风控上下文。"""
        nav = self._account.get_nav_usdt(account_id)

        # 当前持仓（Phase 2：account 存根始终返回 None）
        pos = self._account.get_position(account_id, symbol)
        positions = {symbol: pos} if pos else {}

        # 在途订单（Phase 2：暂无 OMS，为空列表；Phase 3 从 cache 读取）
        open_orders: list[Order] = []

        # 资金费率（从 cache 读取所有 funding:* 键）
        funding_rates: dict[str, Decimal] = {}
        rate = self._cache.get(f"funding:{symbol}")
        if rate is not None:
            funding_rates[symbol] = rate

        # 日内回撤（Phase 2 暂无，Phase 3 由 MonitorService 写入）
        daily_drawdown = self._cache.get(f"drawdown:{account_id}") or Decimal(0)
        
        # ── 价格信息（市价单需要）────────────────────────────────────────────
        # 从 cache 读取最新市场价格，供市价单计算名义价值
        mark_price = self._cache.get(f"price:{symbol}")
        
        # ── 预期收益信息(手续费保护)────────────────────────────────────────────
        # 从 cache 读取 PortfolioService 写入的预期收益率
        expected_return_pct = self._cache.get(f"expected_return:{symbol}")

        return RiskContext(
            account_id    = account_id,
            nav_usdt      = nav,
            positions     = positions,
            open_orders   = open_orders,
            funding_rates = funding_rates,
            extra         = {
                "daily_drawdown": daily_drawdown,
                "expected_return_pct": expected_return_pct,
                "mark_price": mark_price,  # 新增：市场价格
            },
        )
