"""
价差监控器
简化版：纯固定阈值，无 mean/warmup/rolling 逻辑
根据交易方向计算当前价差和反向价差
"""

import logging
from decimal import Decimal

logger = logging.getLogger("spread_monitor")


class SpreadMonitor:
    """
    简化价差监控器 — DCA 策略专用

    计算方式：
      long 方向: spread = variational_bid - paradex_ask
        (Paradex 买入, Variational 卖出; V 价格 > P 价格时有利润)
      short 方向: spread = paradex_bid - variational_ask
        (Paradex 卖出, Variational 买入; P 价格 > V 价格时有利润)

    PositionManager 负责所有入场/出场判断，
    本模块只做价差计算。
    """

    def __init__(self, direction: str):
        self.direction = direction
        self.current_spread: Decimal = Decimal("0")
        self.reverse_spread: Decimal = Decimal("0")
        self.sample_count: int = 0

        # 统计
        self.max_spread: Decimal = Decimal("-999999")
        self.min_spread: Decimal = Decimal("999999")
        self._spread_sum: Decimal = Decimal("0")

    def update(
        self,
        paradex_bid: Decimal,
        paradex_ask: Decimal,
        variational_bid: Decimal,
        variational_ask: Decimal,
    ) -> Decimal:
        """
        更新价差并返回当前值

        同时计算反向价差，用于减仓判断
        """
        if self.direction == "long":
            self.current_spread = variational_bid - paradex_ask
            self.reverse_spread = paradex_bid - variational_ask
        else:
            self.current_spread = paradex_bid - variational_ask
            self.reverse_spread = variational_bid - paradex_ask

        self.sample_count += 1

        # 更新统计
        if self.current_spread > self.max_spread:
            self.max_spread = self.current_spread
        if self.current_spread < self.min_spread:
            self.min_spread = self.current_spread
        self._spread_sum += self.current_spread

        return self.current_spread

    @property
    def avg_spread(self) -> Decimal:
        """历史平均价差"""
        if self.sample_count == 0:
            return Decimal("0")
        return self._spread_sum / self.sample_count

    def get_status(self) -> dict:
        """获取当前状态（用于心跳日志）"""
        return {
            "direction": self.direction,
            "current_spread": float(self.current_spread),
            "reverse_spread": float(self.reverse_spread),
            "avg_spread": float(self.avg_spread),
            "max_spread": float(self.max_spread) if self.max_spread > Decimal("-999999") else 0.0,
            "min_spread": float(self.min_spread) if self.min_spread < Decimal("999999") else 0.0,
            "sample_count": self.sample_count,
        }
