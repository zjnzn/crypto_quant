"""
run_backtest.py

Phase 5 最终演示：三策略（动量 + 均值回归 + 资金费率套利）完整回测。
直接运行：python run_backtest.py
"""
from __future__ import annotations
import io, logging, math, random, sys, pathlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

ROOT = pathlib.Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.instrument import Instrument, InstrumentKind
from application.events import (SignalEvent, RiskApprovedEvent,
                                 RiskRejectedEvent, FillEvent, PnLEvent,
                                 AlertEvent, FundingRateEvent)
from config    import Config
from container import build

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)-5s: %(message)s")
W = 64


def gen_csv(n=300, start=65_000, trend=60.0, noise=250.0, seed=42) -> str:
    random.seed(seed)
    rows = ["timestamp,symbol,open,high,low,close,volume"]
    price = float(start)
    base  = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        ts    = (base + timedelta(hours=i)).isoformat()
        cycle = math.sin(2 * math.pi * i / 40) * 300
        price = max(1_000, price + trend + cycle/40 + random.uniform(-noise, noise))
        rows.append(f"{ts},BTC-USDT-PERP,{price:.1f},{price+150:.1f},"
                    f"{price-150:.1f},{price:.1f},{random.uniform(50,300):.2f}")
    return "\n".join(rows)


def synthetic_funding_rate(ts: datetime) -> Decimal:
    """模拟资金费率：牛市上升、周期性波动，偶尔出现极值。"""
    hour = ts.hour + ts.timetuple().tm_yday * 24
    base  = 0.001 + 0.0005 * math.sin(2 * math.pi * hour / (24 * 14))
    spike = 0.004 if (hour % 72 == 0) else 0.0
    return Decimal(str(round(base + spike, 6)))


def main():
    N_BARS = 300
    INIT   = 10_000.0

    btc_perp = Instrument(
        symbol="BTC-USDT-PERP", exchange="paper",
        base="BTC", quote="USDT", kind=InstrumentKind.PERP,
        tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
        min_notional=Decimal("5"), max_leverage=125,
    )

    cfg = Config.backtest_default(
        initial_usdt=INIT,
        strategies=[
            {"module": "strategies.momentum.MomentumStrategy",
             "params": {"window": 20, "scale": 0.05, "min_score": 0.15}},
            {"module": "strategies.mean_reversion.MeanReversionStrategy",
             "params": {"window": 20, "z_entry": 1.5, "z_threshold": 2.5}},
            {"module": "strategies.funding_rate_arb.FundingRateArbStrategy",
             "params": {"min_rate": 0.001, "max_rate": 0.005,
                        "cooldown_bars": 8}},
        ],
    )
    system = build(cfg)

    # ── 统计 ──────────────────────────────────────────────────────────────────
    by_strat: dict[str, list] = defaultdict(list)
    stats:    dict[str, list] = defaultdict(list)

    system.bus.subscribe(SignalEvent,
        lambda e: (stats["sigs"].append(e),
                   by_strat[e.strategy_id].append(e)))
    system.bus.subscribe(RiskApprovedEvent,  lambda e: stats["approved"].append(e))
    system.bus.subscribe(RiskRejectedEvent,  lambda e: stats["rejected"].append(e))
    system.bus.subscribe(FillEvent,          lambda e: stats["fills"].append(e))
    system.bus.subscribe(FundingRateEvent,   lambda e: stats["funding"].append(e))
    system.bus.subscribe(AlertEvent,         lambda e: stats["alerts"].append(e))

    feed = system.make_feed(
        io.StringIO(gen_csv(n=N_BARS)),
        {"BTC-USDT-PERP": btc_perp},
        funding_rate_fn=synthetic_funding_rate,
    )
    feed.run()

    snap = system.monitor.snapshot()
    nav  = snap["nav_usdt"]
    pnl  = snap["total_pnl"]
    pct  = snap["total_pnl_pct"]

    print("=" * W)
    print(f"  Crypto Quant ─ Phase 5 三策略回测")
    print(f"  动量 + 均值回归 + 资金费率套利 │ {N_BARS}根K线 │ 初始 ${INIT:,.0f}")
    print("=" * W)

    print(f"\n  {'─'*28}  绩效  {'─'*26}")
    print(f"  {'初始净值':<22}  ${INIT:>16,.2f}")
    print(f"  {'最终净值':<22}  ${nav:>16,.2f}")
    print(f"  {'总盈亏':<22}  ${pnl:>+16,.2f}  ({pct:+.2f}%)")
    print(f"  {'  已实现盈亏':<22}  ${snap['realized_pnl']:>+16,.2f}")
    print(f"  {'  未实现盈亏':<22}  ${snap['unrealized_pnl']:>+16,.2f}")
    print(f"  {'最大回撤':<22}  {snap['max_drawdown_pct']:>16.2f}%")
    print(f"  {'当前回撤':<22}  {snap['current_drawdown_pct']:>16.2f}%")
    print(f"  {'Sharpe 比率':<22}  {snap['sharpe_ratio']:>16.3f}")
    print(f"  {'胜率':<22}  {snap['win_rate_pct']:>15.1f}%")
    print(f"  {'交易笔数':<22}  {snap['total_trades']:>16,}  "
          f"(盈={snap['wins']} 亏={snap['losses']})")

    print(f"\n  {'─'*28}  事件  {'─'*26}")
    print(f"  {'信号总数':<22}  {len(stats['sigs']):>16,}")
    for sid, sigs in sorted(by_strat.items()):
        longs  = sum(1 for s in sigs if s.score > 0)
        shorts = sum(1 for s in sigs if s.score < 0)
        avg    = sum(s.score for s in sigs) / len(sigs) if sigs else 0
        print(f"    {sid:<20}  {len(sigs):>6}  多={longs} 空={shorts}  均={avg:+.3f}")
    print(f"  {'资金费率事件':<22}  {len(stats['funding']):>16,}")
    print(f"  {'风控通过/拒绝':<22}  {len(stats['approved']):>8,} / {len(stats['rejected']):<6,}")
    fills = stats["fills"]
    print(f"  {'成交笔数':<22}  {len(fills):>16,}  "
          f"(买={sum(1 for f in fills if f.side.value=='buy')} "
          f"卖={sum(1 for f in fills if f.side.value=='sell')})")
    if fills:
        comm = sum(float(f.commission) for f in fills)
        print(f"  {'累计手续费':<22}  ${comm:>16.4f}")
    print(f"  {'告警次数':<22}  {len(stats['alerts']):>16,}")

    if snap["daily_pnl"]:
        days = sorted(snap["daily_pnl"])
        best  = max(snap["daily_pnl"].values())
        worst = min(snap["daily_pnl"].values())
        print(f"\n  每日P&L  最佳: ${best:+.2f}  最差: ${worst:+.2f}  "
              f"共 {len(days)} 日")

    print(f"\n  回测终止: {system.clock.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * W)


if __name__ == "__main__":
    main()
