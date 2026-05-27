"""
application/services/reconcile.py

对账服务。比较系统内部状态与交易所实际状态，报告差异，支持灾难恢复。

职责：
  1. 日内对账：每笔成交后（可选），比对单个标的的仓位
  2. 日终对账：全量比对所有持仓和余额
  3. 灾难恢复：系统重启后以交易所数据为准，重建内部状态

使用方式（在 container 或 run 脚本中）：
  reconciler = ReconcileService(exchange, account, cache)

  # 日终对账
  report = reconciler.end_of_day(account_id="main", instruments=[btc_perp])
  if not report.matched:
      reconciler.rebuild_from_exchange("main", [btc_perp])

  # 系统重启后立即执行
  reconciler.rebuild_from_exchange("main", instruments)  # 必须在 feed.run() 之前
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from core.domain.instrument import Instrument
from core.domain.position import Position
from core.ports.cache import CachePort
from core.ports.execution import ExecutionPort

log = logging.getLogger(__name__)


# ── 对账结果数据类 ─────────────────────────────────────────────────────────────

@dataclass
class PositionDiscrepancy:
    """单个标的的持仓差异。"""
    symbol:        str
    system_size:   Decimal
    exchange_size: Decimal
    diff:          Decimal

    @property
    def diff_pct(self) -> float:
        if self.exchange_size == 0:
            return float("inf")
        return float(self.diff / self.exchange_size * 100)


@dataclass
class ReconcileReport:
    """一次对账的完整结果。"""
    account_id:       str
    ts:               datetime
    kind:             str                    # "intraday" | "end_of_day" | "rebuild"
    matched:          bool
    discrepancies:    list[PositionDiscrepancy] = field(default_factory=list)
    balance_system:   Decimal = Decimal(0)
    balance_exchange: Decimal = Decimal(0)
    balance_matched:  bool    = True
    notes:            list[str] = field(default_factory=list)

    @property
    def n_discrepancies(self) -> int:
        return len(self.discrepancies)

    def summary(self) -> str:
        lines = [
            f"[{self.kind.upper()}] 对账 {self.ts.strftime('%Y-%m-%d %H:%M:%S')}",
            f"  账号: {self.account_id}  结果: {'✓ 一致' if self.matched else '✗ 存在差异'}",
        ]
        for d in self.discrepancies:
            lines.append(
                f"  {d.symbol}: 系统={d.system_size:.6f}  "
                f"交易所={d.exchange_size:.6f}  "
                f"差异={d.diff:.6f} ({d.diff_pct:.2f}%)"
            )
        if not self.balance_matched:
            lines.append(
                f"  USDT余额: 系统={self.balance_system:.2f}  "
                f"交易所={self.balance_exchange:.2f}"
            )
        for note in self.notes:
            lines.append(f"  [NOTE] {note}")
        return "\n".join(lines)


# ── 对账服务 ──────────────────────────────────────────────────────────────────

class ReconcileService:
    """
    持仓对账服务。

    不订阅任何事件（纯查询型），可以在任意时间点手动调用。
    """

    # 余额差异容忍阈值（USDT）
    BALANCE_TOLERANCE = Decimal("1.0")
    # 持仓差异容忍阈值：低于 1 个 lot_size 的差异忽略
    POSITION_TOLERANCE_FACTOR = Decimal("1")

    def __init__(
        self,
        exchange: ExecutionPort,
        account:  "AccountService",          # 用 duck typing，避免循环 import
        cache:    CachePort,
    ) -> None:
        self._exchange = exchange
        self._account  = account
        self._cache    = cache

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def intraday(
        self,
        account_id:  str,
        instruments: list[Instrument],
    ) -> ReconcileReport:
        """
        日内对账：仅比对指定标的的仓位，不检查余额。
        适合在每笔成交后调用以快速发现问题。
        """
        return self._reconcile_positions(account_id, instruments, kind="intraday")

    def end_of_day(
        self,
        account_id:  str,
        instruments: list[Instrument],
    ) -> ReconcileReport:
        """
        日终对账：比对所有持仓 + USDT 余额。
        建议每日收盘后自动触发。
        """
        report = self._reconcile_positions(account_id, instruments,
                                           kind="end_of_day")
        self._check_balance(account_id, report)
        return report

    def rebuild_from_exchange(
        self,
        account_id:  str,
        instruments: list[Instrument],
    ) -> ReconcileReport:
        """
        灾难恢复：以交易所数据为准，覆盖系统内部状态。

        调用时机：
          - 系统重启后，feed.run() 之前
          - 发现持仓严重错误时

        注意：此操作不可逆，请先记录当前系统状态。
        """
        # 先对账，找出差异
        report = self._reconcile_positions(account_id, instruments,
                                           kind="rebuild")
        report.notes.append("执行灾难恢复：以交易所数据覆盖系统状态")

        # ── 无论持仓是否一致，始终同步余额 ──────────────────────────────────
        try:
            exchange_bal = self._exchange.get_balance(account_id)
            usdt = exchange_bal.available("USDT")
            if usdt > 0:
                self._account.force_sync_balance(account_id, usdt)
                report.notes.append(f"已同步 USDT 余额: {usdt:.2f}")
        except Exception:
            log.exception("余额同步失败")

        if report.matched:
            report.notes.append("系统持仓与交易所一致，无需重建仓位")
            log.info("reconcile rebuild: 持仓无差异")
            return report

        # 逐个修复仓位差异
        for disc in report.discrepancies:
            instr = next((i for i in instruments if i.symbol == disc.symbol), None)
            if instr is None:
                continue

            exchange_pos = self._exchange.get_position(instr, account_id)
            self._account.force_sync_position(account_id, disc.symbol,
                                              exchange_pos)
            report.notes.append(
                f"已修复 {disc.symbol}: "
                f"{disc.system_size:.6f} → {disc.exchange_size:.6f}"
            )

        log.warning("reconcile rebuild 完成: 修复了 %d 处差异",
                    len(report.discrepancies))
        return report

    # ── 内部逻辑 ──────────────────────────────────────────────────────────────

    def _reconcile_positions(
        self,
        account_id:  str,
        instruments: list[Instrument],
        kind:        str,
    ) -> ReconcileReport:
        discrepancies: list[PositionDiscrepancy] = []

        for instr in instruments:
            exchange_pos = self._exchange.get_position(instr, account_id)
            system_pos   = self._account.get_position(account_id, instr.symbol)

            ex_size  = exchange_pos.size if exchange_pos else Decimal(0)
            sys_size = system_pos.size   if system_pos   else Decimal(0)
            diff     = abs(ex_size - sys_size)

            # 容忍阈值：低于 1 个最小下单单位忽略（浮点/精度差异）
            tolerance = instr.lot_size * self.POSITION_TOLERANCE_FACTOR
            if diff >= tolerance:
                discrepancies.append(PositionDiscrepancy(
                    symbol        = instr.symbol,
                    system_size   = sys_size,
                    exchange_size = ex_size,
                    diff          = diff,
                ))
                log.warning(
                    "position mismatch  %s  sys=%.6f  exch=%.6f  diff=%.6f",
                    instr.symbol, sys_size, ex_size, diff,
                )

        report = ReconcileReport(
            account_id    = account_id,
            ts            = datetime.now(tz=timezone.utc),
            kind          = kind,
            matched       = len(discrepancies) == 0,
            discrepancies = discrepancies,
        )

        if report.matched:
            log.info("reconcile %s: ✓ 所有持仓一致", kind)
        else:
            log.warning("reconcile %s: ✗ %d 处差异",
                        kind, len(discrepancies))

        return report

    def _check_balance(self, account_id: str, report: ReconcileReport) -> None:
        """日终对账附加余额检查。"""
        try:
            exchange_bal = self._exchange.get_balance(account_id)
            ex_usdt   = exchange_bal.available("USDT")
            sys_usdt  = self._account._usdt.get(account_id, Decimal(0))
            diff      = abs(ex_usdt - sys_usdt)

            report.balance_system   = sys_usdt
            report.balance_exchange = ex_usdt
            report.balance_matched  = diff <= self.BALANCE_TOLERANCE

            if not report.balance_matched:
                report.matched = False
                log.warning(
                    "balance mismatch  sys=%.2f  exch=%.2f  diff=%.2f",
                    sys_usdt, ex_usdt, diff,
                )
        except Exception:
            log.exception("余额对账失败（交易所接口异常）")
            report.notes.append("余额对账失败，请手动核查")
