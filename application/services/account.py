"""
application/services/account.py

完整账号服务（Phase 3）。替换 Phase 2 的 SimpleAccount 存根。

实现 AccountPort 协议 + 订阅 SettlementEvent。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from core.domain.order import Order, OrderType, Side
from core.domain.position import Position, PositionSide
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.events import (BalanceUpdatedEvent, PositionUpdatedEvent,
                                 SettlementEvent)

log = logging.getLogger(__name__)


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

        bus.subscribe(SettlementEvent, self._on_settlement)
        log.info("AccountService 启动（初始 USDT: %s）", initial_usdt)

    # ── AccountPort ───────────────────────────────────────────────────────────

    def get_position(self, account_id: str, symbol: str) -> Position | None:
        pos = self._positions.get((account_id, symbol))
        return pos if (pos and not pos.is_empty) else None

    def get_nav_usdt(self, account_id: str) -> Decimal:
        """
        NAV = USDT 余额 + 所有持仓市值。

        多头：持仓市值 = size * mark_price（资产价值）
        空头：持仓市值 = -size * mark_price（负债价值）

        注意：买入时 USDT 已扣除全额成本，所以 NAV 要加回全额持仓市值，
        不能只加浮盈差值（那样差了整个成本基础）。

        示例：以 65000 买入 0.01 BTC
          USDT 减少：0.01 * 65000.1 + commission ≈ 650.65
          持仓市值：0.01 * mark_price ≈ 650
          NAV ≈ 9999.35  ← 仅亏手续费 + 滑点
        """
        nav = self._usdt[account_id]
        for (acc_id, sym), pos in self._positions.items():
            if acc_id != account_id or pos.is_empty:
                continue
            mark = self._cache.get(f"price:{sym}") or pos.entry_price
            if pos.side == PositionSide.LONG:
                nav += pos.size * mark   # 多头：资产
            elif pos.side == PositionSide.SHORT:
                nav -= pos.size * mark   # 空头：负债
        return nav
    def net_orders(self, account_id: str, orders: list[Order]) -> list[Order]:
        """同标的多策略订单轧差。"""
        net:  dict[str, Decimal] = defaultdict(Decimal)
        insts: dict[str, object] = {}
        for o in orders:
            sym = o.instrument.symbol
            insts[sym] = o.instrument
            net[sym] += o.qty if o.side == Side.BUY else -o.qty

        result = []
        for sym, net_qty in net.items():
            inst = insts[sym]
            if abs(net_qty) < inst.lot_size:
                continue
            result.append(Order(
                instrument = inst,
                account_id = account_id,
                side       = Side.BUY if net_qty > 0 else Side.SELL,
                qty        = inst.round_qty(abs(net_qty)),
                order_type = OrderType.MARKET,
            ))
        return result

    # ── SettlementEvent ───────────────────────────────────────────────────────

    def _on_settlement(self, event: SettlementEvent) -> None:
        acc_id = event.account_id
        sym    = event.instrument.symbol
        key    = (acc_id, sym)
        pos    = self._positions.get(key)

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

        self._bus.publish(PositionUpdatedEvent(
            account_id   = acc_id,
            instrument   = event.instrument,
            new_size     = new_pos.size,
            entry_price  = new_pos.entry_price,
            realized_pnl = realized_pnl,
        ).caused_by(event))

        self._bus.publish(BalanceUpdatedEvent(
            account_id  = acc_id,
            currency    = "USDT",
            delta       = usdt_delta,
            new_balance = new_usdt,
        ).caused_by(event))

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _apply_buy(self, pos, event) -> tuple[Decimal, Position]:
        qty, price = event.settled_qty, event.avg_price
        if pos is None or pos.is_empty:
            # 无仓位 → 开多
            new_size, new_entry = qty, price
            new_side = PositionSide.LONG
        elif pos.side == PositionSide.LONG:
            # 多头 → 加多
            new_size = pos.size + qty
            new_entry = (pos.size * pos.entry_price + qty * price) / new_size
            new_side = PositionSide.LONG
        elif pos.side == PositionSide.SHORT:
            # 空头 → 平空（可能同时开多）
            close_qty = min(qty, pos.size)
            realized_pnl = close_qty * (pos.entry_price - price)
            remaining_short = pos.size - close_qty
            excess_buy = qty - close_qty  # 超出平仓部分的买入量
            if remaining_short <= Decimal(0) and excess_buy > Decimal(0):
                # 空头已全部平掉，多余部分开多
                return realized_pnl, Position(
                    instrument=event.instrument, account_id=event.account_id,
                    strategy_id="", side=PositionSide.LONG,
                    size=excess_buy, entry_price=price,
                )
            elif remaining_short <= Decimal(0):
                return realized_pnl, Position(
                    instrument=event.instrument, account_id=event.account_id,
                    strategy_id="", side=PositionSide.LONG,
                    size=Decimal(0), entry_price=Decimal(0),
                )
            else:
                return realized_pnl, Position(
                    instrument=event.instrument, account_id=event.account_id,
                    strategy_id="", side=PositionSide.SHORT,
                    size=remaining_short, entry_price=pos.entry_price,
                )
        else:
            new_size, new_entry, new_side = qty, price, PositionSide.LONG
        return Decimal(0), Position(
            instrument=event.instrument, account_id=event.account_id,
            strategy_id="", side=new_side,
            size=new_size, entry_price=new_entry,
        )

    def _apply_sell(self, pos, event) -> tuple[Decimal, Position]:
        qty, price = event.settled_qty, event.avg_price
        if pos is None or pos.is_empty:
            # 无仓位 → 开空
            new_size, new_entry = qty, price
            new_side = PositionSide.SHORT
        elif pos.side == PositionSide.SHORT:
            # 空头 → 加空
            new_size = pos.size + qty
            new_entry = (pos.size * pos.entry_price + qty * price) / new_size
            new_side = PositionSide.SHORT
        elif pos.side == PositionSide.LONG:
            # 多头 → 平多（可能同时开空）
            close_qty = min(qty, pos.size)
            realized_pnl = close_qty * (price - pos.entry_price)
            remaining_long = pos.size - close_qty
            excess_sell = qty - close_qty  # 超出平仓部分的卖出量
            if remaining_long <= Decimal(0) and excess_sell > Decimal(0):
                # 多头已全部平掉，多余部分开空
                return realized_pnl, Position(
                    instrument=event.instrument, account_id=event.account_id,
                    strategy_id="", side=PositionSide.SHORT,
                    size=excess_sell, entry_price=price,
                )
            elif remaining_long <= Decimal(0):
                return realized_pnl, Position(
                    instrument=event.instrument, account_id=event.account_id,
                    strategy_id="", side=PositionSide.LONG,
                    size=Decimal(0), entry_price=Decimal(0),
                )
            else:
                return realized_pnl, Position(
                    instrument=event.instrument, account_id=event.account_id,
                    strategy_id="", side=PositionSide.LONG,
                    size=remaining_long, entry_price=pos.entry_price,
                )
        else:
            new_size, new_entry, new_side = qty, price, PositionSide.SHORT
        return Decimal(0), Position(
            instrument=event.instrument, account_id=event.account_id,
            strategy_id="", side=new_side,
            size=new_size, entry_price=new_entry,
        )

    def force_sync_position(
        self,
        account_id: str,
        symbol:     str,
        exchange_pos: "Position | None",
    ) -> None:
        """
        灾难恢复入口：用交易所实际持仓覆盖系统内部状态。
        由 ReconcileService 在确认存在差异后调用。
        系统重启后应先调用此方法，再启动策略。
        """
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
        """用交易所余额覆盖 USDT 余额（灾难恢复）。"""
        self._usdt[account_id] = usdt
        log.warning("force_sync: %s USDT → %.2f", account_id, usdt)

    def snapshot(self, account_id: str) -> dict:
        """返回账号快照（用于对账和调试）。"""
        positions = {
            sym: {"size": float(pos.size), "entry": float(pos.entry_price)}
            for (acc, sym), pos in self._positions.items()
            if acc == account_id and not pos.is_empty
        }
        return {
            "account_id": account_id,
            "usdt":       float(self._usdt[account_id]),
            "nav_usdt":   float(self.get_nav_usdt(account_id)),
            "positions":  positions,
        }

