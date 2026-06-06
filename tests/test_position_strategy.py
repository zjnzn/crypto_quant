"""
测试持仓策略：状态机逻辑
- 无持仓：信号 < min_score → 不开仓，信号 ≥ min_score → 正常计算
- 有持仓：信号在 [0, min_score] → 保持最低持仓（按 min_score 算）
"""
import pytest
from decimal import Decimal

from core.domain.instrument import Instrument, InstrumentKind
from core.domain.position import Position, PositionSide
from adapters.bus.sync import SyncEventBus
from adapters.cache.memory import MemoryCache
from application.services.account import AccountService
from application.services.portfolio import PortfolioService
from application.events import SignalEvent, TargetPositionEvent


@pytest.fixture
def btc_perp():
    return Instrument(
        symbol="BTC-USDT-PERP", exchange="binance",
        base="BTC", quote="USDT", kind=InstrumentKind.PERP,
        tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
        min_notional=Decimal("5"), max_leverage=125,
    )


def test_no_position_below_threshold_no_open(btc_perp):
    """无持仓 + 信号低于阈值 → 不开仓"""
    bus = SyncEventBus()
    cache = MemoryCache()
    account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10000"))
    
    targets = []
    bus.subscribe(TargetPositionEvent, targets.append)
    
    portfolio = PortfolioService(
        bus=bus, cache=cache, account=account, account_id="main",
        max_weight=0.10, min_score=0.15, leverage=5,
    )
    
    cache.set("price:BTC-USDT-PERP", Decimal("60000"))
    
    bus.publish(SignalEvent(
        instrument=btc_perp, strategy_id="test",
        score=0.10, confidence=0.8,
    ))
    
    # 无持仓且信号弱 → 不开仓
    assert len(targets) == 0


def test_no_position_above_threshold_opens(btc_perp):
    """无持仓 + 信号高于阈值 → 正常开仓"""
    bus = SyncEventBus()
    cache = MemoryCache()
    account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10000"))
    
    targets = []
    bus.subscribe(TargetPositionEvent, targets.append)
    
    portfolio = PortfolioService(
        bus=bus, cache=cache, account=account, account_id="main",
        max_weight=0.10, min_score=0.15, leverage=5,
    )
    
    cache.set("price:BTC-USDT-PERP", Decimal("60000"))
    
    bus.publish(SignalEvent(
        instrument=btc_perp, strategy_id="test",
        score=0.30, confidence=0.8,
    ))
    
    # 正常计算仓位：0.30 × 0.10 × 5 × 10000 / 60000 = 0.025 BTC
    assert len(targets) > 0
    assert abs(float(targets[0].target_size) - 0.025) < 0.002


def test_has_position_below_threshold_reduces_to_min(btc_perp):
    """有持仓 + 信号低于阈值 → 减仓到最低持仓"""
    bus = SyncEventBus()
    cache = MemoryCache()
    account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10000"))
    
    # 手动设置持仓（模拟已开仓）
    # NAV = USDT + 持仓市值 = 10000 + 0.05 × 60000 = 13000
    account._positions[("main", "BTC-USDT-PERP")] = Position(
        instrument=btc_perp, account_id="main", strategy_id="test",
        side=PositionSide.LONG, size=Decimal("0.05"),
        entry_price=Decimal("60000"), leverage=5,
    )
    
    targets = []
    bus.subscribe(TargetPositionEvent, targets.append)
    
    portfolio = PortfolioService(
        bus=bus, cache=cache, account=account, account_id="main",
        max_weight=0.10, min_score=0.15, leverage=5,
    )
    
    cache.set("price:BTC-USDT-PERP", Decimal("60000"))
    
    # 信号跌到阈值以下
    bus.publish(SignalEvent(
        instrument=btc_perp, strategy_id="test",
        score=0.05, confidence=0.8,
    ))
    
    # 有持仓 → 保持最低持仓
    # min_position = 0.15 × 0.10 × 5 × NAV / price
    # NAV = 13000（见上文）
    # min_position = 0.15 × 0.10 × 5 × 13000 / 60000 = 0.01625 BTC
    assert len(targets) > 0
    assert targets[0].target_size > 0, "应保持多头持仓"
    assert targets[0].target_size < Decimal("0.05"), "应减仓"
    # 验证接近最低持仓（允许误差）
    expected_min = Decimal("0.15") * Decimal("0.10") * 5 * Decimal("13000") / Decimal("60000")
    assert abs(targets[0].target_size - expected_min) < Decimal("0.002")


def test_has_position_signal_zero_keeps_min(btc_perp):
    """有持仓 + 信号为 0 → 保持最低持仓"""
    bus = SyncEventBus()
    cache = MemoryCache()
    account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10000"))
    
    account._positions[("main", "BTC-USDT-PERP")] = Position(
        instrument=btc_perp, account_id="main", strategy_id="test",
        side=PositionSide.LONG, size=Decimal("0.05"),
        entry_price=Decimal("60000"), leverage=5,
    )
    
    targets = []
    bus.subscribe(TargetPositionEvent, targets.append)
    
    portfolio = PortfolioService(
        bus=bus, cache=cache, account=account, account_id="main",
        max_weight=0.10, min_score=0.15, leverage=5,
    )
    
    cache.set("price:BTC-USDT-PERP", Decimal("60000"))
    
    bus.publish(SignalEvent(
        instrument=btc_perp, strategy_id="test",
        score=0.0, confidence=0.8,
    ))
    
    # 有持仓 → 保持最低持仓
    assert len(targets) > 0
    assert targets[0].target_size > 0


def test_position_reversal_allowed(btc_perp):
    """信号翻仓：多头 → 空头（allow_short=True）"""
    bus = SyncEventBus()
    cache = MemoryCache()
    account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10000"))
    
    account._positions[("main", "BTC-USDT-PERP")] = Position(
        instrument=btc_perp, account_id="main", strategy_id="test",
        side=PositionSide.LONG, size=Decimal("0.05"),
        entry_price=Decimal("60000"), leverage=5,
    )
    
    targets = []
    bus.subscribe(TargetPositionEvent, targets.append)
    
    portfolio = PortfolioService(
        bus=bus, cache=cache, account=account, account_id="main",
        max_weight=0.10, min_score=0.15, leverage=5, allow_short=True,
    )
    
    cache.set("price:BTC-USDT-PERP", Decimal("60000"))
    
    # 信号翻空（低于阈值）
    bus.publish(SignalEvent(
        instrument=btc_perp, strategy_id="test",
        score=-0.05, confidence=0.8,
    ))
    
    # 有持仓 → 保持空头最低持仓
    assert len(targets) > 0
    assert targets[0].target_size < 0, "应翻为空头"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
