#!/usr/bin/env python3
"""
Paradex × Variational DCA/Grid 套利机器人
入口文件: 解析参数 → 加载配置 → 创建客户端 → 启动引擎
"""

import argparse
import asyncio
import sys
from decimal import Decimal

from config import AppConfig
from exchanges.paradex_client import ParadexInteractiveClient
from exchanges.variational_client import VariationalClient
from helpers.logger import setup_logging
from helpers.telegram_bot import TelegramNotifier
from strategy.dca_engine import DCAEngine


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Paradex × Variational DCA/Grid 跨所套利机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 标准 BTC 做多 DCA
  python main.py --ticker BTC --direction long --qty 0.005 \\
    --max-position 0.1 --mingap 33 --maxgap 44 --interval 30

  # 保守模式 (小仓位, 高阈值)
  python main.py --ticker BTC --direction long --qty 0.001 \\
    --max-position 0.01 --mingap 40 --maxgap 55 --interval 60

  # 试运行 (不实际下单)
  python main.py --ticker BTC --direction long --qty 0.005 \\
    --max-position 0.1 --mingap 20 --maxgap 44 --dry-run
        """,
    )

    # 核心参数
    parser.add_argument(
        "--ticker", type=str, default="BTC",
        help="交易对 (默认: BTC). 支持: BTC, ETH, SOL, ARB, DOGE, AVAX, LINK, OP, WIF, PEPE",
    )
    parser.add_argument(
        "--direction", type=str, default="long",
        choices=["long", "short"],
        help="交易方向: long=Paradex做多+Variational做空, short=反向 (默认: long)",
    )
    parser.add_argument(
        "--qty", type=str, required=True,
        help="每次加仓数量 (如 0.005 BTC)",
    )
    parser.add_argument(
        "--max-position", type=str, required=True,
        help="最大累积仓位 (如 0.1 BTC)",
    )

    # DCA 参数
    parser.add_argument(
        "--mingap", type=str, required=True,
        help="最小价差阈值 USD (如 33). 价差 >= mingap 时加仓",
    )
    parser.add_argument(
        "--maxgap", type=str, default="9999",
        help="最大价差阈值 USD (如 44). 价差 > maxgap 时停止加仓 (默认: 9999=不限)",
    )
    parser.add_argument(
        "--closegap", type=str, default="0",
        help="减仓阈值 USD (如 20). 反向价差 >= closegap 时减仓. 0=不主动减仓 (默认: 0)",
    )
    parser.add_argument(
        "--interval", type=float, default=30.0,
        help="加仓最小间隔秒数 (默认: 30)",
    )

    # 风控参数
    parser.add_argument(
        "--fill-timeout", type=int, default=5,
        help="订单成交超时秒数 (默认: 5)",
    )
    parser.add_argument(
        "--min-balance", type=str, default="10",
        help="最低余额阈值 USDC (默认: 10)",
    )

    # 运行模式
    parser.add_argument(
        "--dry-run", action="store_true",
        help="试运行模式: 监控价差但不实际下单",
    )

    # 配置文件
    parser.add_argument(
        "--env-file", type=str, default=".env",
        help=".env 文件路径 (默认: .env)",
    )
    parser.add_argument(
        "--variational-auth-mode", type=str, default="cookie",
        choices=["cookie"],
        help="Variational 认证模式 (默认: cookie)",
    )

    return parser.parse_args()


async def main():
    args = parse_arguments()

    # 设置日志
    setup_logging(args.ticker)

    # 加载配置
    config = AppConfig.load(args.env_file)
    config.variational_auth_mode = args.variational_auth_mode

    # 设置 DCA 交易参数
    config.trading.ticker = args.ticker
    config.trading.direction = args.direction
    config.trading.qty = Decimal(args.qty)
    config.trading.max_position = Decimal(args.max_position)
    config.trading.mingap = Decimal(args.mingap)
    config.trading.maxgap = Decimal(args.maxgap)
    config.trading.closegap = Decimal(args.closegap)
    config.trading.interval = args.interval
    config.trading.fill_timeout = args.fill_timeout
    config.trading.min_balance = Decimal(args.min_balance)
    config.trading.dry_run = args.dry_run

    # 验证配置
    config.validate()

    t = config.trading

    # 创建交易所客户端
    paradex = ParadexInteractiveClient(
        l2_private_key=config.paradex.l2_private_key,
        l2_address=config.paradex.l2_address,
        environment=config.paradex.environment,
    )

    variational = VariationalClient(
        vr_token=config.variational.vr_token,
        wallet_address=config.variational.wallet_address,
        cookies=config.variational.cookies,
        private_key=config.variational.private_key,
        base_url=config.variational.base_url,
        auth_mode=config.variational_auth_mode,
    )

    # 创建 Telegram 通知器
    telegram = None
    if config.telegram.enabled:
        telegram = TelegramNotifier(
            bot_token=config.telegram.bot_token,
            chat_id=config.telegram.group_id,
            account_label=config.telegram.account_label,
        )

    # 创建并运行 DCA 引擎
    engine = DCAEngine(
        paradex=paradex,
        variational=variational,
        paradex_market=t.paradex_market,
        variational_market=t.variational_market,
        ticker=t.ticker,
        direction=t.direction,
        qty=t.qty,
        max_position=t.max_position,
        mingap=t.mingap,
        maxgap=t.maxgap,
        closegap=t.closegap,
        interval=t.interval,
        fill_timeout=t.fill_timeout,
        min_balance=t.min_balance,
        telegram=telegram,
        dry_run=t.dry_run,
    )

    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
