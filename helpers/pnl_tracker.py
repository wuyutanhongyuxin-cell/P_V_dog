"""
P&L 追踪模块
记录每笔 DCA entry 的利润、滑点、累计统计
"""

import csv
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

import pytz

logger = logging.getLogger("pnl_tracker")


class PnLTracker:
    """
    P&L 与滑点追踪器

    功能:
    - 记录每笔 DCA entry 的 spread × qty = gross_profit
    - 追踪滑点: 实际成交价 vs BBO 价
    - 累积统计: 总利润、平均价差、交易量
    - CSV 持久化
    """

    def __init__(self, ticker: str, log_dir: str = "logs"):
        self.ticker = ticker
        self.timezone = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))

        os.makedirs(log_dir, exist_ok=True)
        self.pnl_file = os.path.join(log_dir, f"dca_{ticker}_pnl.csv")

        # 累积统计
        self.total_gross_profit: Decimal = Decimal("0")
        self.total_entries: int = 0
        self.total_volume: Decimal = Decimal("0")  # 单边 USD 交易量
        self._spread_sum: Decimal = Decimal("0")

        # 滑点追踪
        self._slippage_a_sum: Decimal = Decimal("0")  # Paradex 累计滑点 (USD)
        self._slippage_b_sum: Decimal = Decimal("0")  # Variational 累计滑点 (USD)
        self._slippage_count: int = 0

        # 初始权益 (启动时设置)
        self.initial_equity: Decimal = Decimal("0")

        self._init_csv()

    def _init_csv(self):
        """初始化 CSV 文件"""
        if not os.path.exists(self.pnl_file):
            with open(self.pnl_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "direction", "spread", "size",
                    "gross_profit", "cumulative_profit",
                    "paradex_price", "variational_price",
                    "slippage_a", "slippage_b",
                ])

    def set_initial_equity(self, equity: Decimal):
        """设置初始权益 (启动时调用)"""
        self.initial_equity = equity

    def record_entry(
        self,
        direction: str,
        spread: Decimal,
        size: Decimal,
        paradex_price: Decimal,
        variational_price: Decimal,
        paradex_bbo_price: Optional[Decimal] = None,
        variational_bbo_price: Optional[Decimal] = None,
    ):
        """记录一笔 DCA 入场"""
        gross_profit = spread * size
        self.total_gross_profit += gross_profit
        self.total_entries += 1
        self.total_volume += paradex_price * size
        self._spread_sum += spread

        # 滑点计算
        slippage_a = Decimal("0")
        slippage_b = Decimal("0")
        if paradex_bbo_price and paradex_bbo_price > 0:
            slippage_a = paradex_price - paradex_bbo_price
            self._slippage_a_sum += slippage_a
        if variational_bbo_price and variational_bbo_price > 0:
            slippage_b = variational_price - variational_bbo_price
            self._slippage_b_sum += slippage_b
        if paradex_bbo_price or variational_bbo_price:
            self._slippage_count += 1

        # 写 CSV
        try:
            timestamp = datetime.now(self.timezone).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            with open(self.pnl_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp, direction,
                    f"{spread:.4f}", f"{size:.6f}",
                    f"{gross_profit:.6f}", f"{self.total_gross_profit:.6f}",
                    f"{paradex_price:.2f}", f"{variational_price:.2f}",
                    f"{slippage_a:.4f}", f"{slippage_b:.4f}",
                ])
        except Exception as e:
            logger.error(f"写入 PnL CSV 失败: {e}")

    def record_reduction(
        self,
        direction: str,
        spread: Decimal,
        size: Decimal,
        paradex_price: Decimal,
        variational_price: Decimal,
    ):
        """记录一笔减仓（利润为负值表示减仓）"""
        # 减仓时利润 = 反向价差 × size
        gross_profit = spread * size
        self.total_gross_profit += gross_profit
        self.total_volume += paradex_price * size

        try:
            timestamp = datetime.now(self.timezone).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            with open(self.pnl_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp, f"CLOSE_{direction.upper()}",
                    f"{spread:.4f}", f"{size:.6f}",
                    f"{gross_profit:.6f}", f"{self.total_gross_profit:.6f}",
                    f"{paradex_price:.2f}", f"{variational_price:.2f}",
                    "0", "0",
                ])
        except Exception as e:
            logger.error(f"写入 PnL CSV 失败: {e}")

    @property
    def avg_spread(self) -> Decimal:
        """平均入场价差"""
        if self.total_entries == 0:
            return Decimal("0")
        return self._spread_sum / self.total_entries

    @property
    def avg_slippage_a(self) -> Decimal:
        """Paradex 平均滑点"""
        if self._slippage_count == 0:
            return Decimal("0")
        return self._slippage_a_sum / self._slippage_count

    @property
    def avg_slippage_b(self) -> Decimal:
        """Variational 平均滑点"""
        if self._slippage_count == 0:
            return Decimal("0")
        return self._slippage_b_sum / self._slippage_count

    @property
    def avg_slippage_a_bps(self) -> Decimal:
        """Paradex 平均滑点 (bps)"""
        # 粗略估算：假设价格 ~65000
        avg = self.avg_slippage_a
        if avg == 0:
            return Decimal("0")
        return avg / Decimal("65000") * Decimal("10000")

    @property
    def avg_slippage_b_bps(self) -> Decimal:
        """Variational 平均滑点 (bps)"""
        avg = self.avg_slippage_b
        if avg == 0:
            return Decimal("0")
        return avg / Decimal("65000") * Decimal("10000")

    def get_summary(self) -> dict:
        """获取 P&L 摘要 (用于心跳)"""
        return {
            "total_gross_profit": float(self.total_gross_profit),
            "total_entries": self.total_entries,
            "total_volume": float(self.total_volume),
            "avg_spread": float(self.avg_spread),
            "initial_equity": float(self.initial_equity),
            "avg_slippage_a": float(self.avg_slippage_a),
            "avg_slippage_b": float(self.avg_slippage_b),
            "avg_slippage_a_bps": float(self.avg_slippage_a_bps),
            "avg_slippage_b_bps": float(self.avg_slippage_b_bps),
        }
