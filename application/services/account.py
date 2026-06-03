"""
application/services/account.py

完整账号服务（Phase 3）。替换 Phase 2 的 SimpleAccount 存根。

实现 AccountPort 协议 + 订阅 SettlementEvent。
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from decimal import Decimal

from core.domain.order import Order, OrderType, Side
from core.domain.position import Position, PositionSide
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.events import (BalanceUpdatedEvent, PositionUpdatedEvent,
                                 SettlementEvent)

log = logging.getLogger(__name__)


def _critical_wrapper(fn):
    """包装 handler 使其携带 _critical 标记，供 SyncEventBus 识别。"""
    def wrapper(event):
        return fn(event)
    wrapper._critical = True
    wrapper.__qualname__ = fn.__qualname__
    return wrapper


class AccountService:
    """满足 AccountPort 协议（结构化子类型）。"""

    def __init__(
        self,
        bus:          EventBusPort,
        cache:        CachePort,
        initial_usdt: Decimal = Decimal("10_000"),
    ) -> None:
        self._bus          = bus
        self._cache        = cache
        self._initial_usdt = initial_usdt
        self._usdt: dict[str, Decimal] = defaultdict(lambda: initial_usdt)
        self._positions: dict[tuple[str, str], Position] = {}
        self._lock         = threading.RLock()

        # 标记关键 handler，异常时向上传播
        self._on_settlement = _critical_wrapper(self._on_settlement)
        bus.subscribe(SettlementEvent, self._on_settlement)
        log.info("AccountService 启动（初始 USDT: %s）", initial_usdt)

    # ── AccountPort ───────────────────────────────────────────────────────────

    def get_position(self, account_id: str, symbol: str) -> Position | None:
        with self._lock:
            pos = self._positions.get((account_id, symbol))
            return pos if (pos and not pos.is_empty) else None

    def get_all_positions(self, account_id: str) -> dict[str, Position]:
        """返回指定账号的所有非空仓位。"""
        with self._lock:
            return {
                sym: pos
                for (acc_id, sym), pos in self._positions.items()
                if acc_id == account_id and not pos.is_empty
            }

    def get_nav_usdt(self, account_id: str) -> Decimal:
        with self._lock:
            nav = self._usdt[account_id]
            for (acc_id, sym), pos in self._positions.items():
                if acc_id != account_id or pos.is_empty:
                    continue
                mark = self._cache.get(f"price:{sym}") or pos.entry_price
                if pos.side == PositionSide.LONG:
                    nav += pos.size * mark
                elif pos.side == PositionSide.SHORT:
                    nav -= pos.size * mark
            return nav
    def net_orders(self, account_id: str, orders: list[Order]) -> list[Order]:
        """同标的多策略订单轧差。
        
        注意：轧差后的净订单会记录所有贡献策略的 ID（逗号分隔），
        便于后续追踪和归因分析。
        """
        net:  dict[str, Decimal] = defaultdict(Decimal)
        insts: dict[str, object] = {}
        strategies: dict[str, list[str]] = defaultdict(list)  # 记录每个标的的策略列表
        
        for o in orders:
            sym = o.instrument.symbol
            insts[sym] = o.instrument
            net[sym] += o.qty if o.side == Side.BUY else -o.qty
            # 记录策略 ID（去重）
            if o.strategy_id and o.strategy_id not in strategies[sym]:
                strategies[sym].append(o.strategy_id)
        
        result = []
        for sym, net_qty in net.items():
            inst = insts[sym]
            if abs(net_qty) < inst.lot_size:
                continue
            
            # 合并策略 ID
            strategy_ids = strategies[sym]
            strategy_id = ",".join(strategy_ids) if strategy_ids else ""
            
            result.append(Order(
                instrument = inst,
                account_id = account_id,
                side       = Side.BUY if net_qty > 0 else Side.SELL,
                qty        = inst.round_qty(abs(net_qty)),
                order_type = OrderType.MARKET,
                strategy_id = strategy_id,  # 保留策略追踪信息
            ))
        return result

    # ── SettlementEvent ───────────────────────────────────────────────────────

    def _on_settlement(self, event: SettlementEvent) -> None:
        acc_id = event.account_id
        sym    = event.instrument.symbol
        key    = (acc_id, sym)

        with self._lock:
            pos = self._positions.get(key)

            if event.side == Side.BUY:
                realized_pnl, new_pos = self._apply_buy(pos, event)
                usdt_delta = -(event.settled_qty * event.avg_price + event.commission)
            else:
                realized_pnl, new_pos = self._apply_sell(pos, event)
                usdt_delta = event.settled_qty * event.avg_price - event.commission

            self._positions[key] = new_pos
            self._usdt[acc_id]  += usdt_delta
            new_usdt = self._usdt[acc_id]

        log.debug("account %s %s size=%.6f entry=%.2f pnl=%.4f usdt=%.2f",
                  event.side.value, sym, new_pos.size,
                  new_pos.entry_price, realized_pnl, new_usdt)

        position_event = PositionUpdatedEvent(
            account_id=acc_id,
            instrument=event.instrument,
            new_size=new_pos.size,
            entry_price=new_pos.entry_price,
            realized_pnl=realized_pnl,
        ).caused_by(event)
        balance_event = BalanceUpdatedEvent(
            account_id=acc_id,
            currency="USDT",
            delta=usdt_delta,
            new_balance=new_usdt,
        ).caused_by(event)

        self._bus.publish(position_event)
        self._bus.publish(balance_event)

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _apply_buy(self, pos, event) -> tuple[Decimal, Position]:
        return self._apply_fill(pos, event, signed_qty=event.settled_qty)

    def _apply_sell(self, pos, event) -> tuple[Decimal, Position]:
        return self._apply_fill(pos, event, signed_qty=-event.settled_qty)

    def _apply_fill(self, pos, event, signed_qty: Decimal) -> tuple[Decimal, Position]:
        price = event.avg_price
        current_signed = Decimal(0)
        if pos is not None and not pos.is_empty:
            current_signed = pos.size if pos.side == PositionSide.LONG else -pos.size

        new_signed = current_signed + signed_qty
        realized_pnl = Decimal(0)

        if current_signed > 0 and signed_qty < 0:
            close_qty = min(current_signed, abs(signed_qty))
            realized_pnl = close_qty * (price - pos.entry_price)
        elif current_signed < 0 and signed_qty > 0:
            close_qty = min(abs(current_signed), signed_qty)
            realized_pnl = close_qty * (pos.entry_price - price)

        if new_signed == 0:
            return realized_pnl, Position(
                instrument=event.instrument,
                account_id=event.account_id,
                strategy_id="",
                side=PositionSide.LONG,
                size=Decimal(0),
                entry_price=Decimal(0),
            )

        if current_signed == 0 or (current_signed > 0) != (new_signed > 0):
            entry_price = price
        elif (current_signed > 0) == (signed_qty > 0):
            added_qty = abs(signed_qty)
            total_qty = abs(new_signed)
            entry_price = (
                abs(current_signed) * pos.entry_price + added_qty * price
            ) / total_qty
        else:
            entry_price = pos.entry_price

        side = PositionSide.LONG if new_signed > 0 else PositionSide.SHORT
        return realized_pnl, Position(
            instrument=event.instrument,
            account_id=event.account_id,
            strategy_id="",
            side=side,
            size=abs(new_signed),
            entry_price=entry_price,
        )

    def force_sync_position(
        self,
        account_id: str,
        symbol:     str,
        exchange_pos: "Position | None",
    ) -> None:
        with self._lock:
            key = (account_id, symbol)
            if exchange_pos is None or exchange_pos.is_empty:
                self._positions.pop(key, None)
                log.warning("force_sync: %s %s → 清空仓位", account_id, symbol)
            else:
                self._positions[key] = exchange_pos
                log.warning("force_sync: %s %s → size=%.6f @ %.2f",
                            account_id, symbol,
                            exchange_pos.size, exchange_pos.entry_price)

    def force_sync_balance(self, account_id: str, usdt: Decimal) -> None:
        with self._lock:
            self._usdt[account_id] = usdt
        log.warning("force_sync: %s USDT → %.2f", account_id, usdt)

    def snapshot(self, account_id: str) -> dict:
        with self._lock:
            positions = {
                sym: {"size": float(pos.size), "entry": float(pos.entry_price)}
                for (acc, sym), pos in self._positions.items()
                if acc == account_id and not pos.is_empty
            }
            usdt = float(self._usdt[account_id])
        return {
            "account_id": account_id,
            "usdt":       usdt,
            "nav_usdt":   float(self.get_nav_usdt(account_id)),
            "positions":  positions,
        }

