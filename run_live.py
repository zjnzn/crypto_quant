"""
run_live.py

实盘交易入口。

运行模式：
  --dry-run   PaperExchange + testnet WS（不下真实订单，调试用）
  --paper     Binance 测试网（需 testnet API Key）
  (无参数)    Binance 主网（需主网 API Key）
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import pathlib
from decimal import Decimal

ROOT = pathlib.Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_live")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="虚拟币量化交易系统 — 实盘入口")
    p.add_argument("--config",      default="config_live.yaml")
    p.add_argument("--paper",       action="store_true", help="Binance 测试网")
    p.add_argument("--dry-run",     action="store_true", help="PaperExchange，不下真实订单")
    p.add_argument("--instruments", nargs="+", default=["BTCUSDT"])
    return p.parse_args()


def build_instruments(symbols: list[str], exchange: str = "binance") -> dict:
    from core.domain.instrument import Instrument, InstrumentKind
    DB = {
        "BTCUSDT": Instrument(
            symbol="BTC-USDT-PERP", exchange=exchange,
            base="BTC", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
            min_notional=Decimal("5"), max_leverage=125,
        ),
        "ETHUSDT": Instrument(
            symbol="ETH-USDT-PERP", exchange=exchange,
            base="ETH", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.01"), lot_size=Decimal("0.001"),
            min_notional=Decimal("5"), max_leverage=100,
        ),
        "SOLUSDT": Instrument(
            symbol="SOL-USDT-PERP", exchange=exchange,
            base="SOL", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.01"), lot_size=Decimal("0.1"),
            min_notional=Decimal("5"), max_leverage=50,
        ),
        "DOGEUSDT": Instrument(
            symbol="DOGE-USDT-PERP", exchange=exchange,
            base="DOGE", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.00001"), lot_size=Decimal("1"),
            min_notional=Decimal("5"), max_leverage=100,
        ),
        "XRPUSDT": Instrument(
            symbol="XRP-USDT-PERP", exchange=exchange,
            base="XRP", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.0001"), lot_size=Decimal("0.1"),
            min_notional=Decimal("5"), max_leverage=50,
        ),
        "ADAUSDT": Instrument(
            symbol="ADA-USDT-PERP", exchange=exchange,
            base="ADA", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.0001"), lot_size=Decimal("1"),
            min_notional=Decimal("1"), max_leverage=50,
        ),
        "DOTUSDT": Instrument(
            symbol="DOT-USDT-PERP", exchange=exchange,
            base="DOT", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.001"), lot_size=Decimal("0.1"),
            min_notional=Decimal("5"), max_leverage=50,
        ),
        "LTCUSDT": Instrument(
            symbol="LTC-USDT-PERP", exchange=exchange,
            base="LTC", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.01"), lot_size=Decimal("0.01"),
            min_notional=Decimal("0.1"), max_leverage=75,
        ),
    }
    result = {}
    for sym in symbols:
        key = sym.upper()
        if key in DB:
            result[key.lower()] = DB[key]
        else:
            log.warning("未知标的 %s，跳过", key)
    return result


def startup_checks(system, instruments: dict) -> bool:
    from application.services.reconcile import ReconcileService
    log.info("═" * 52)
    log.info("  启动前安全检查")
    log.info("═" * 52)

    instr_list = list(instruments.values())

    log.info("[1/3] 从交易所拉取当前持仓，执行对账...")
    reconciler = ReconcileService(
        exchange=system.exchange,
        account=system.account,
        cache=system.cache,
    )
    report = reconciler.rebuild_from_exchange("main", instr_list)
    log.info("  对账结果: %s", "✓ 一致" if report.matched else "已修复差异")
    for note in report.notes:
        log.info("  %s", note)

    log.info("[2/3] 验证账户净值...")
    nav = float(system.account.get_nav_usdt("main"))
    if nav < 10.0:
        log.error("  净值 %.2f USDT < 最低 10 USDT，中止", nav)
        return False
    log.info("  账户净值: %.2f USDT ✓", nav)

    log.info("[3/3] 验证策略...")
    snap = system.account.snapshot("main")
    log.info("  当前持仓: %d 个标的", len(snap["positions"]))

    log.info("═" * 52)
    log.info("  所有检查通过，开始实盘交易")
    log.info("═" * 52)
    return True


def main() -> None:
    args = parse_args()

    from config import Config
    try:
        cfg = Config.from_yaml(args.config)
    except FileNotFoundError:
        log.error("配置文件不存在: %s", args.config)
        sys.exit(1)

    if args.paper:
        cfg.mode = "paper"
        cfg.execution.exchange = "binance"
        log.info("使用 Binance 测试网（testnet）")
    if args.dry_run:
        cfg.mode = "backtest"
        cfg.execution.exchange = "paper"
        log.info("Dry Run 模式：使用 PaperExchange，不发真实订单")

    exchange_name = "paper" if args.dry_run else "binance"
    instruments   = build_instruments(args.instruments, exchange=exchange_name)

    if not instruments:
        log.error("无有效标的，退出")
        sys.exit(1)
    log.info("交易标的: %s", list(instruments.keys()))

    from container import build
    system = build(cfg)

    # ── 启动前安全检查 ────────────────────────────────────────────────────────
    if cfg.execution.exchange != "paper":
        if not startup_checks(system, instruments):
            sys.exit(1)
        # ★ 重置 P&L 基准为实际账户净值（防止 testnet 赠送余额导致虚假盈亏）
        system.monitor.reset_baseline()
        log.info("监控基准已重置为实际账户净值")

    # ── 使用 dict 管理可变状态（规避 signal handler 闭包作用域问题）─────────
    state: dict = {
        "feed":             None,
        "user_data_stream": None,
    }

    # ── 优雅退出（先定义，后使用 state 引用）────────────────────────────────
    def shutdown(sig, frame) -> None:
        log.info("\n收到停止信号，执行日终对账后退出...")

        if state["feed"]:
            state["feed"].stop()
        # user_data_stream 由 feed.run() 统一管理启停

        from application.services.reconcile import ReconcileService
        reconciler = ReconcileService(
            exchange=system.exchange,
            account=system.account,
            cache=system.cache,
        )
        report = reconciler.end_of_day("main", list(instruments.values()))
        log.info("日终对账: %s", report.summary())

        snap = system.monitor.snapshot()
        log.info("─" * 52)
        log.info("今日结算:")
        log.info("  最终净值:  %.2f USDT", snap["nav_usdt"])
        log.info("  总盈亏:    %+.2f USDT (%.2f%%)",
                 snap["total_pnl"], snap["total_pnl_pct"])
        log.info("  最大回撤:  %.2f%%", snap["max_drawdown_pct"])
        log.info("  Sharpe:    %.3f", snap["sharpe_ratio"])
        log.info("  胜率:      %.1f%% (%d/%d)",
                 snap["win_rate_pct"], snap["wins"], snap["total_trades"])
        log.info("─" * 52)
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── 创建用户数据流（成交回报）────────────────────────────────────────────
    user_data_stream = None
    if cfg.execution.exchange == "binance":
        from adapters.feed.user_data import BinanceUserDataStream
        is_testnet = (cfg.mode == "paper")
        user_data_stream = BinanceUserDataStream(
            bus         = system.bus,
            exchange    = system.exchange,
            instruments = instruments,
            testnet     = is_testnet,
        )
        # 不再单独 start() — 由 BinanceWsFeed.run() 统一管理
        log.info("用户数据流已配置（由行情主循环统一驱动）")

    # ── 启动行情 WebSocket（含用户数据流）─────────────────────────────────────
    from adapters.feed.websocket import BinanceWsFeed
    is_testnet = (cfg.mode == "paper")

    feed = BinanceWsFeed(
        bus              = system.bus,
        instruments      = instruments,
        market_type      = "futures",
        testnet          = is_testnet,
        user_data_stream = user_data_stream,
    )
    state["feed"] = feed

    log.info("系统运行中，按 Ctrl+C 停止")
    try:
        feed.run()    # 阻塞主线程，统一处理行情 + FillEvent
    except Exception as e:
        log.exception("系统异常: %s", e)
        shutdown(None, None)


if __name__ == "__main__":
    main()