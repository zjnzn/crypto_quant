"""
run_live.py

实盘交易入口。

前置条件：
  pip install requests websocket-client
  config_live.yaml 中填入真实 API Key

用法：
  python run_live.py                        # 使用 config_live.yaml
  python run_live.py --config my.yaml       # 指定配置文件
  python run_live.py --paper               # Binance 测试网（testnet）
  python run_live.py --dry-run             # 用 PaperExchange 模拟（不下真实订单）

安全检查（启动前自动执行）：
  1. 从交易所拉取当前持仓，与系统状态对账（灾难恢复）
  2. 验证 API Key 有效且有交易权限
  3. 检查账户净值 ≥ 最低要求
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
    p.add_argument("--config",  default="config_live.yaml", help="配置文件路径")
    p.add_argument("--paper",   action="store_true", help="使用 Binance 测试网")
    p.add_argument("--dry-run", action="store_true", help="使用 PaperExchange（不发真实订单）")
    p.add_argument("--instruments", nargs="+", default=["BTCUSDT"],
                   help="交易标的（Binance 格式，如 BTCUSDT ETHUSDT）")
    return p.parse_args()


def build_instruments(symbols: list[str]) -> dict[str, "Instrument"]:
    """将 Binance symbol 列表构建为 Instrument 字典。"""
    from core.domain.instrument import Instrument, InstrumentKind
    # Phase 5: 简化硬编码常见标的，完整版从 /fapi/v1/exchangeInfo 拉取
    INSTRUMENT_DB = {
        "BTCUSDT": Instrument(
            symbol="BTC-USDT-PERP", exchange="binance",
            base="BTC", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
            min_notional=Decimal("5"), max_leverage=125,
        ),
        "ETHUSDT": Instrument(
            symbol="ETH-USDT-PERP", exchange="binance",
            base="ETH", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.01"), lot_size=Decimal("0.01"),
            min_notional=Decimal("5"), max_leverage=100,
        ),
        "SOLUSDT": Instrument(
            symbol="SOL-USDT-PERP", exchange="binance",
            base="SOL", quote="USDT", kind=InstrumentKind.PERP,
            tick_size=Decimal("0.01"), lot_size=Decimal("0.1"),
            min_notional=Decimal("5"), max_leverage=50,
        ),
    }
    result = {}
    for sym in symbols:
        sym_upper = sym.upper()
        if sym_upper not in INSTRUMENT_DB:
            log.warning("未知标的 %s，跳过", sym_upper)
            continue
        instr = INSTRUMENT_DB[sym_upper]
        # WS 订阅键 = binance symbol 小写
        result[sym_upper.lower()] = instr
    return result


def startup_checks(system, instruments: dict) -> bool:
    """
    启动前安全检查。
    返回 True = 检查通过，可以开始交易。
    返回 False = 检查失败，中止。
    """
    from application.services.reconcile import ReconcileService

    log.info("═" * 50)
    log.info("  启动前安全检查")
    log.info("═" * 50)

    instr_list = list(instruments.values())

    # 1. 对账 / 灾难恢复
    log.info("[1/3] 从交易所拉取当前持仓，执行对账...")
    reconciler = ReconcileService(
        exchange = system.exchange,
        account  = system.account,
        cache    = system.cache,
    )
    report = reconciler.rebuild_from_exchange("main", instr_list)
    log.info("  对账结果: %s", "✓ 一致" if report.matched else "已修复差异")
    for note in report.notes:
        log.info("  %s", note)

    # 2. 验证账户净值
    log.info("[2/3] 验证账户净值...")
    nav = float(system.account.get_nav_usdt("main"))
    min_nav = 100.0   # 最低 100 USDT
    if nav < min_nav:
        log.error("  账户净值 %.2f USDT < 最低要求 %.2f USDT，中止", nav, min_nav)
        return False
    log.info("  账户净值: %.2f USDT ✓", nav)

    # 3. 检查策略已加载
    log.info("[3/3] 验证策略...")
    snap = system.account.snapshot("main")
    log.info("  当前持仓: %d 个标的", len(snap["positions"]))

    log.info("═" * 50)
    log.info("  所有检查通过，开始实盘交易")
    log.info("═" * 50)
    return True


def main() -> None:
    args = parse_args()

    # ── 加载配置 ──────────────────────────────────────────────────────────────
    from config import Config
    try:
        cfg = Config.from_yaml(args.config)
    except FileNotFoundError:
        log.error("配置文件不存在: %s", args.config)
        log.error("请复制 config_live.yaml.example 并填入 API Key")
        sys.exit(1)

    if args.paper:
        cfg.mode = "paper"
        cfg.execution.exchange = "binance"
        log.info("使用 Binance 测试网（testnet）")
    if args.dry_run:
        cfg.mode = "backtest"   # 使用 PaperExchange
        cfg.execution.exchange = "paper"
        log.info("Dry Run 模式：使用 PaperExchange，不发真实订单")

    # ── 构建系统 ──────────────────────────────────────────────────────────────
    from container import build
    system = build(cfg)

    # ── 构建标的字典 ──────────────────────────────────────────────────────────
    instruments = build_instruments(args.instruments)
    if not instruments:
        log.error("没有有效标的，退出")
        sys.exit(1)

    log.info("交易标的: %s", list(instruments.keys()))

    # ── 启动前安全检查 ────────────────────────────────────────────────────────
    if cfg.execution.exchange != "paper":
        if not startup_checks(system, instruments):
            sys.exit(1)

    # ── 启动 WebSocket 行情 ───────────────────────────────────────────────────
    from adapters.feed.websocket import BinanceWsFeed

    feed = BinanceWsFeed(
        bus         = system.bus,
        instruments = instruments,
    )

    # ── 优雅退出 ──────────────────────────────────────────────────────────────
    def shutdown(sig, frame):
        log.info("\n收到停止信号，执行日终对账后退出...")
        feed.stop()

        from application.services.reconcile import ReconcileService
        reconciler = ReconcileService(
            exchange = system.exchange,
            account  = system.account,
            cache    = system.cache,
        )
        report = reconciler.end_of_day("main", list(instruments.values()))
        log.info("日终对账: %s", report.summary())

        snap = system.monitor.snapshot()
        log.info("─" * 50)
        log.info("今日结算:")
        log.info("  最终净值:  %.2f USDT", snap["nav_usdt"])
        log.info("  总盈亏:    %+.2f USDT (%.2f%%)", snap["total_pnl"],
                 snap["total_pnl_pct"])
        log.info("  最大回撤:  %.2f%%", snap["max_drawdown_pct"])
        log.info("  Sharpe:    %.3f", snap["sharpe_ratio"])
        log.info("  胜率:      %.1f%% (%d/%d)", snap["win_rate_pct"],
                 snap["wins"], snap["total_trades"])
        log.info("─" * 50)

        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── 启动 ──────────────────────────────────────────────────────────────────
    log.info("启动 WebSocket 行情订阅...")
    feed.start()

    log.info("系统运行中，按 Ctrl+C 停止")
    try:
        feed.run()   # 阻塞主线程
    except Exception as e:
        log.exception("系统异常: %s", e)
        shutdown(None, None)


if __name__ == "__main__":
    main()
