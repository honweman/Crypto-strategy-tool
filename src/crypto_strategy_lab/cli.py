from __future__ import annotations

import argparse
from pathlib import Path

from .broker import SimulatedBroker
from .data import CsvBarDataSource, CsvExternalEventSource, MergedEventFeed
from .engine import BacktestConfig, BacktestEngine
from .risk import RiskConfig, RiskManager
from .strategy import StatisticalEventConfig, StatisticalEventStrategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crypto-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest = subparsers.add_parser("backtest", help="Run an event-driven backtest.")
    backtest.add_argument("--bars", required=True, help="CSV file with OHLCV bars.")
    backtest.add_argument("--events", help="CSV file with timestamped external events.")
    backtest.add_argument("--symbols", nargs="*", help="Optional symbols to include, e.g. BTCUSDT ETHUSDT.")
    backtest.add_argument("--cash", type=float, default=10_000.0)
    backtest.add_argument("--fee-bps", type=float, default=5.0)
    backtest.add_argument("--slippage-bps", type=float, default=2.0)
    backtest.add_argument("--lookback", type=int, default=20)
    backtest.add_argument("--entry-z", type=float, default=-1.2)
    backtest.add_argument("--exit-z", type=float, default=0.2)
    backtest.add_argument("--base-weight", type=float, default=0.20)
    backtest.add_argument("--event-weight", type=float, default=0.25)
    backtest.add_argument("--event-threshold", type=float, default=0.60)
    backtest.add_argument("--max-order-notional", type=float, default=2_500.0)
    backtest.add_argument("--max-symbol-weight", type=float, default=0.35)
    backtest.add_argument("--max-gross-exposure", type=float, default=0.80)

    args = parser.parse_args(argv)
    if args.command == "backtest":
        return run_backtest(args)
    return 2


def run_backtest(args: argparse.Namespace) -> int:
    symbols = {symbol.upper() for symbol in args.symbols} if args.symbols else None
    sources = [CsvBarDataSource(args.bars, symbols=symbols)]
    if args.events:
        sources.append(CsvExternalEventSource(args.events, symbols=symbols))

    strategy = StatisticalEventStrategy(
        StatisticalEventConfig(
            lookback=args.lookback,
            entry_z=args.entry_z,
            exit_z=args.exit_z,
            base_weight=args.base_weight,
            event_weight=args.event_weight,
            event_threshold=args.event_threshold,
        )
    )
    engine = BacktestEngine(
        strategy=strategy,
        broker=SimulatedBroker(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps),
        risk_manager=RiskManager(
            RiskConfig(
                max_order_notional=args.max_order_notional,
                max_symbol_weight=args.max_symbol_weight,
                max_gross_exposure=args.max_gross_exposure,
            )
        ),
        config=BacktestConfig(initial_cash=args.cash),
    )

    result = engine.run(MergedEventFeed(sources))
    metrics = result.metrics
    print("Backtest summary")
    print(f"bars_file={Path(args.bars).name}")
    print(f"starting_equity={metrics.starting_equity:.2f}")
    print(f"ending_equity={metrics.ending_equity:.2f}")
    print(f"total_return={metrics.total_return:.2%}")
    print(f"max_drawdown={metrics.max_drawdown:.2%}")
    print(f"sharpe={metrics.sharpe:.2f}")
    print(f"fills={metrics.trade_count}")
    print(f"fees={metrics.total_fees:.2f}")
    print(f"rejected_orders={len(result.rejected_orders)}")

    if result.fills:
        print("\nLast fills")
        for fill in result.fills[-5:]:
            print(
                f"{fill.timestamp.isoformat()} {fill.side.value.upper()} "
                f"{fill.symbol} qty={fill.quantity:.8f} price={fill.price:.4f} fee={fill.fee:.4f} reason={fill.reason}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
