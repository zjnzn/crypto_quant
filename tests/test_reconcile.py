"""
tests/test_reconcile.py

对账服务测试。

使用 MockExchange 模拟不同的交易所状态，
验证对账逻辑在各种场景下的正确性。
"""
from __future__ import annotations

import sys
import pathlib
from decimal import Decimal
from datetime import timezone

import pytest

ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.balance  import Balance
from core.domain.instrument import Instrument, InstrumentKind
from core.domain.position   import Position, PositionSide, MarginMode
from adapters.bus.sync      import SyncEventBus
from adapters.cache.memory  import MemoryCache
from application.services.account   import AccountService
from application.services.reconcile import ReconcileService, PositionDiscrepancy


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def btc_perp() -> Instrument:
    return Instrument(
        symbol="BTC-USDT-PERP", exchange="paper",
        base="BTC", quote="USDT", kind=InstrumentKind.PERP,
        tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
        min_notional=Decimal("5"), max_leverage=125,
    )


@pytest.fixture
def eth_perp() -> Instrument:
    return Instrument(
        symbol="ETH-USDT-PERP", exchange="paper",
        base="ETH", quote="USDT", kind=InstrumentKind.PERP,
        tick_size=Decimal("0.01"), lot_size=Decimal("0.01"),
        min_notional=Decimal("5"), max_leverage=100,
    )


def make_position(instrument, size, entry=65000.0, account_id="main"):
    return Position(
        instrument   = instrument,
        account_id   = account_id,
        strategy_id  = "",
        side         = PositionSide.LONG,
        size         = Decimal(str(size)),
        entry_price  = Decimal(str(entry)),
    )


class MockExchange:
    """可配置的 Mock 交易所，用于对账测试。"""
    exchange_id = "mock"

    def __init__(
        self,
        positions: dict[str, Position | None] | None = None,
        usdt:      Decimal = Decimal("10_000"),
    ) -> None:
        self._positions = positions or {}
        self._usdt      = usdt

    def get_position(self, instrument: Instrument,
                     account_id: str) -> Position | None:
        return self._positions.get(instrument.symbol)

    def get_balance(self, account_id: str) -> Balance:
        return Balance(account_id=account_id,
                       balances={"USDT": self._usdt})

    # 其他 ExecutionPort 方法（对账不需要）
    def submit(self, order):    return "mock"
    def cancel(self, eid):      return True
    def amend(self, *a, **k):   return False
    def get_funding_rate(self, *a): return Decimal(0)
    def normalize_instrument(self, s): raise NotImplementedError
    def denormalize_order(self, o):    return {}


def make_account(bus=None, cache=None,
                 initial_usdt=Decimal("10_000")) -> AccountService:
    return AccountService(
        bus          = bus or SyncEventBus(),
        cache        = cache or MemoryCache(),
        initial_usdt = initial_usdt,
    )


def make_reconciler(exchange, account, cache=None):
    return ReconcileService(
        exchange = exchange,
        account  = account,
        cache    = cache or MemoryCache(),
    )


# ── 正常对账场景 ──────────────────────────────────────────────────────────────

class TestReconcilePositions:
    def test_empty_both_sides_matched(self, btc_perp) -> None:
        """系统和交易所都无持仓 → 对账通过。"""
        exchange = MockExchange(positions={})
        account  = make_account()
        r        = make_reconciler(exchange, account)

        report = r.intraday("main", [btc_perp])
        assert report.matched
        assert report.n_discrepancies == 0

    def test_same_position_matched(self, btc_perp) -> None:
        """持仓完全一致 → 对账通过。"""
        pos      = make_position(btc_perp, size="0.015")
        exchange = MockExchange(positions={btc_perp.symbol: pos})
        account  = make_account()
        # 直接注入持仓（绕过事件流）
        account._positions[("main", btc_perp.symbol)] = pos

        report = r = make_reconciler(exchange, account).intraday("main", [btc_perp])
        assert report.matched

    def test_multiple_instruments_all_matched(self, btc_perp, eth_perp) -> None:
        """多个标的均一致 → 对账通过。"""
        pos_btc = make_position(btc_perp, size="0.01")
        pos_eth = make_position(eth_perp, size="0.5", entry=3500.0)
        exchange = MockExchange(positions={
            btc_perp.symbol: pos_btc,
            eth_perp.symbol: pos_eth,
        })
        account = make_account()
        account._positions[("main", btc_perp.symbol)] = pos_btc
        account._positions[("main", eth_perp.symbol)] = pos_eth

        report = make_reconciler(exchange, account).intraday(
            "main", [btc_perp, eth_perp]
        )
        assert report.matched
        assert report.n_discrepancies == 0


# ── 差异检测场景 ──────────────────────────────────────────────────────────────

class TestDiscrepancyDetection:
    def test_size_mismatch_detected(self, btc_perp) -> None:
        """系统持仓与交易所不同 → 差异被发现。"""
        ex_pos  = make_position(btc_perp, size="0.015")
        sys_pos = make_position(btc_perp, size="0.010")

        exchange = MockExchange(positions={btc_perp.symbol: ex_pos})
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = sys_pos

        report = make_reconciler(exchange, account).intraday("main", [btc_perp])
        assert not report.matched
        assert report.n_discrepancies == 1
        assert report.discrepancies[0].symbol == btc_perp.symbol

    def test_discrepancy_values_correct(self, btc_perp) -> None:
        """差异报告中的数值准确。"""
        ex_pos  = make_position(btc_perp, size="0.020")
        sys_pos = make_position(btc_perp, size="0.015")

        exchange = MockExchange(positions={btc_perp.symbol: ex_pos})
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = sys_pos

        report = make_reconciler(exchange, account).intraday("main", [btc_perp])
        d = report.discrepancies[0]
        assert d.exchange_size == Decimal("0.020")
        assert d.system_size   == Decimal("0.015")
        assert d.diff          == Decimal("0.005")

    def test_system_has_position_exchange_flat(self, btc_perp) -> None:
        """系统有持仓但交易所已平仓 → 差异。"""
        sys_pos  = make_position(btc_perp, size="0.010")
        exchange = MockExchange(positions={btc_perp.symbol: None})
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = sys_pos

        report = make_reconciler(exchange, account).intraday("main", [btc_perp])
        assert not report.matched
        assert report.discrepancies[0].exchange_size == Decimal(0)
        assert report.discrepancies[0].system_size   == Decimal("0.010")

    def test_exchange_has_position_system_flat(self, btc_perp) -> None:
        """交易所有持仓但系统无记录 → 差异。"""
        ex_pos   = make_position(btc_perp, size="0.012")
        exchange = MockExchange(positions={btc_perp.symbol: ex_pos})
        account  = make_account()   # 空持仓

        report = make_reconciler(exchange, account).intraday("main", [btc_perp])
        assert not report.matched
        assert report.discrepancies[0].system_size   == Decimal(0)
        assert report.discrepancies[0].exchange_size == Decimal("0.012")

    def test_tiny_diff_below_lot_size_ignored(self, btc_perp) -> None:
        """小于最小下单单位的差异（浮点精度）被忽略。"""
        size = Decimal("0.015")
        ex_pos  = make_position(btc_perp, size=size)
        # 差异 = 0.0005（< lot_size=0.001）
        sys_pos = make_position(btc_perp,
                                size=size - Decimal("0.0005"))
        exchange = MockExchange(positions={btc_perp.symbol: ex_pos})
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = sys_pos

        report = make_reconciler(exchange, account).intraday("main", [btc_perp])
        assert report.matched, "低于 lot_size 的差异应忽略"

    def test_multiple_instruments_partial_mismatch(
        self, btc_perp, eth_perp
    ) -> None:
        """多个标的，部分有差异，部分无差异。"""
        btc_same = make_position(btc_perp, size="0.01")
        eth_sys  = make_position(eth_perp, size="0.5", entry=3500.0)
        eth_ex   = make_position(eth_perp, size="0.8", entry=3500.0)  # 不同

        exchange = MockExchange(positions={
            btc_perp.symbol: btc_same,
            eth_perp.symbol: eth_ex,
        })
        account = make_account()
        account._positions[("main", btc_perp.symbol)] = btc_same
        account._positions[("main", eth_perp.symbol)] = eth_sys

        report = make_reconciler(exchange, account).intraday(
            "main", [btc_perp, eth_perp]
        )
        assert not report.matched
        assert report.n_discrepancies == 1
        assert report.discrepancies[0].symbol == eth_perp.symbol


# ── 日终对账（含余额检查）────────────────────────────────────────────────────

class TestEndOfDay:
    def test_balance_match(self, btc_perp) -> None:
        """余额一致 → balance_matched=True。"""
        exchange = MockExchange(usdt=Decimal("9_500"))
        account  = make_account(initial_usdt=Decimal("10_000"))
        # 模拟系统已扣除成本（近似 9500）
        account._usdt["main"] = Decimal("9_500")

        report = make_reconciler(exchange, account).end_of_day("main", [btc_perp])
        assert report.balance_matched

    def test_balance_mismatch_detected(self, btc_perp) -> None:
        """余额差异超过阈值 → balance_matched=False。"""
        exchange = MockExchange(usdt=Decimal("9_500"))
        account  = make_account(initial_usdt=Decimal("10_000"))
        account._usdt["main"] = Decimal("8_000")   # 差 1500

        report = make_reconciler(exchange, account).end_of_day("main", [btc_perp])
        assert not report.balance_matched
        assert not report.matched

    def test_end_of_day_checks_both_positions_and_balance(
        self, btc_perp
    ) -> None:
        """日终对账同时检查持仓和余额。"""
        pos = make_position(btc_perp, size="0.01")
        exchange = MockExchange(
            positions={btc_perp.symbol: pos},
            usdt=Decimal("9_350"),
        )
        account = make_account()
        account._positions[("main", btc_perp.symbol)] = pos
        account._usdt["main"] = Decimal("9_350")

        report = make_reconciler(exchange, account).end_of_day("main", [btc_perp])
        assert report.matched
        assert report.balance_matched


# ── 灾难恢复 ──────────────────────────────────────────────────────────────────

class TestRebuildFromExchange:
    def test_rebuild_fixes_size_mismatch(self, btc_perp) -> None:
        """rebuild_from_exchange 后系统持仓与交易所一致。"""
        ex_pos  = make_position(btc_perp, size="0.015")
        sys_pos = make_position(btc_perp, size="0.010")

        exchange = MockExchange(positions={btc_perp.symbol: ex_pos},
                                usdt=Decimal("9_000"))
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = sys_pos
        account._usdt["main"] = Decimal("9_000")

        r = make_reconciler(exchange, account)
        report = r.rebuild_from_exchange("main", [btc_perp])

        # 修复后持仓应等于交易所
        pos_after = account.get_position("main", btc_perp.symbol)
        assert pos_after is not None
        assert pos_after.size == Decimal("0.015")

        assert "已修复" in "\n".join(report.notes)

    def test_rebuild_clears_position_if_exchange_flat(self, btc_perp) -> None:
        """交易所无持仓时，rebuild 清空系统记录。"""
        sys_pos  = make_position(btc_perp, size="0.010")
        exchange = MockExchange(positions={btc_perp.symbol: None})
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = sys_pos

        make_reconciler(exchange, account).rebuild_from_exchange(
            "main", [btc_perp]
        )

        pos_after = account.get_position("main", btc_perp.symbol)
        assert pos_after is None

    def test_rebuild_syncs_balance(self, btc_perp) -> None:
        """rebuild_from_exchange 同步余额。"""
        exchange = MockExchange(usdt=Decimal("8_800"))
        account  = make_account(initial_usdt=Decimal("10_000"))
        account._usdt["main"] = Decimal("7_000")   # 错误的系统余额

        make_reconciler(exchange, account).rebuild_from_exchange(
            "main", [btc_perp]
        )
        assert account._usdt["main"] == Decimal("8_800")

    def test_rebuild_when_matched_no_changes(self, btc_perp) -> None:
        """已一致时 rebuild 不修改任何状态。"""
        pos      = make_position(btc_perp, size="0.015")
        exchange = MockExchange(positions={btc_perp.symbol: pos},
                                usdt=Decimal("9_000"))
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = pos
        account._usdt["main"] = Decimal("9_000")

        report = make_reconciler(exchange, account).rebuild_from_exchange(
            "main", [btc_perp]
        )
        assert "无需重建" in "\n".join(report.notes)

    def test_rebuild_report_kind_is_rebuild(self, btc_perp) -> None:
        exchange = MockExchange()
        account  = make_account()
        report   = make_reconciler(exchange, account).rebuild_from_exchange(
            "main", [btc_perp]
        )
        assert report.kind == "rebuild"


# ── ReconcileReport 格式 ──────────────────────────────────────────────────────

class TestReconcileReport:
    def test_summary_contains_account_id(self, btc_perp) -> None:
        exchange = MockExchange()
        account  = make_account()
        report   = make_reconciler(exchange, account).intraday("main", [btc_perp])
        assert "main" in report.summary()

    def test_summary_shows_discrepancy(self, btc_perp) -> None:
        ex_pos  = make_position(btc_perp, size="0.020")
        sys_pos = make_position(btc_perp, size="0.010")
        exchange = MockExchange(positions={btc_perp.symbol: ex_pos})
        account  = make_account()
        account._positions[("main", btc_perp.symbol)] = sys_pos

        report = make_reconciler(exchange, account).intraday("main", [btc_perp])
        summary = report.summary()
        assert btc_perp.symbol in summary
        assert "0.020" in summary or "0.010" in summary

    def test_diff_pct_calc(self, btc_perp) -> None:
        d = PositionDiscrepancy(
            symbol        = btc_perp.symbol,
            system_size   = Decimal("0.010"),
            exchange_size = Decimal("0.020"),
            diff          = Decimal("0.010"),
        )
        assert abs(d.diff_pct - 50.0) < 0.01   # 50% 差异

    def test_diff_pct_exchange_zero(self, btc_perp) -> None:
        d = PositionDiscrepancy(
            symbol        = btc_perp.symbol,
            system_size   = Decimal("0.010"),
            exchange_size = Decimal(0),
            diff          = Decimal("0.010"),
        )
        assert d.diff_pct == float("inf")

    def test_ts_is_utc(self, btc_perp) -> None:
        report = make_reconciler(MockExchange(), make_account()).intraday(
            "main", [btc_perp]
        )
        assert report.ts.tzinfo is not None


# ── force_sync 直接测试 ───────────────────────────────────────────────────────

class TestForceSync:
    def test_force_sync_position_sets_position(self, btc_perp) -> None:
        account = make_account()
        pos     = make_position(btc_perp, size="0.015")
        account.force_sync_position("main", btc_perp.symbol, pos)
        assert account.get_position("main", btc_perp.symbol).size == Decimal("0.015")

    def test_force_sync_position_clears_with_none(self, btc_perp) -> None:
        account = make_account()
        pos     = make_position(btc_perp, size="0.015")
        account._positions[("main", btc_perp.symbol)] = pos
        account.force_sync_position("main", btc_perp.symbol, None)
        assert account.get_position("main", btc_perp.symbol) is None

    def test_force_sync_balance(self) -> None:
        account = make_account(initial_usdt=Decimal("10_000"))
        account.force_sync_balance("main", Decimal("8_500"))
        assert account._usdt["main"] == Decimal("8_500")

    def test_snapshot(self, btc_perp) -> None:
        account = make_account(initial_usdt=Decimal("10_000"))
        pos     = make_position(btc_perp, size="0.01", entry=65000.0)
        account._positions[("main", btc_perp.symbol)] = pos

        snap = account.snapshot("main")
        assert snap["account_id"] == "main"
        assert btc_perp.symbol in snap["positions"]
        assert snap["positions"][btc_perp.symbol]["size"] == 0.01
