"""
DCA 仓位管理器
状态机驱动的渐进建仓/减仓管理

状态流转:
  IDLE → ACCUMULATING → FULL (→ REDUCING → IDLE)
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List

logger = logging.getLogger("position_manager")


class DCAState(Enum):
    """DCA 状态机"""
    IDLE = "idle"                  # 无仓位，等待信号
    ACCUMULATING = "accumulating"  # 建仓中，逐笔加仓
    FULL = "full"                  # 满仓，等待
    REDUCING = "reducing"          # 减仓中


@dataclass
class DCAEntry:
    """单笔 DCA 记录"""
    timestamp: float
    paradex_price: Decimal
    variational_price: Decimal
    size: Decimal
    spread: Decimal
    direction: str  # "long" / "short"


class PositionManager:
    """
    DCA 仓位状态机

    核心逻辑:
      1. should_enter(): 价差在 [mingap, maxgap] 内 + 间隔已过 + 未满仓 → 加仓
      2. should_reduce(): closegap > 0 + 反向价差超阈值 → 减仓
      3. record_entry() / record_reduction(): 更新状态
    """

    def __init__(
        self,
        direction: str,
        qty: Decimal,
        max_position: Decimal,
        mingap: Decimal,
        maxgap: Decimal,
        closegap: Decimal,
        interval: float,
    ):
        self.direction = direction
        self.qty = qty
        self.max_position = max_position
        self.mingap = mingap
        self.maxgap = maxgap
        self.closegap = closegap
        self.interval = interval

        # 状态
        self.state: DCAState = DCAState.IDLE
        self.entries: List[DCAEntry] = []
        self.total_position: Decimal = Decimal("0")
        self.last_entry_time: float = 0

        # 交易所实际仓位 (带符号)
        self.paradex_position: Decimal = Decimal("0")
        self.variational_position: Decimal = Decimal("0")

        # 统计
        self.total_entries: int = 0
        self.total_reductions: int = 0
        self.total_volume: Decimal = Decimal("0")  # 累计交易量 (单边 USD)

    @property
    def net_position(self) -> Decimal:
        """净持仓 (理想值 0)"""
        return self.paradex_position + self.variational_position

    @property
    def entry_count(self) -> int:
        """已执行的入场笔数"""
        return len(self.entries)

    @property
    def needed(self) -> int:
        """还需要几笔才到满仓 (向上取整)"""
        if self.qty <= 0:
            return 0
        remaining = self.max_position - self.total_position
        if remaining <= 0:
            return 0
        count = remaining / self.qty
        int_count = int(count)
        return int_count if count == int_count else int_count + 1

    @property
    def avg_entry_spread(self) -> Decimal:
        """平均入场价差"""
        if not self.entries:
            return Decimal("0")
        return sum(e.spread for e in self.entries) / len(self.entries)

    @property
    def avg_entry_price_paradex(self) -> Decimal:
        """Paradex 平均入场价"""
        if not self.entries:
            return Decimal("0")
        return sum(e.paradex_price for e in self.entries) / len(self.entries)

    @property
    def avg_entry_price_variational(self) -> Decimal:
        """Variational 平均入场价"""
        if not self.entries:
            return Decimal("0")
        return sum(e.variational_price for e in self.entries) / len(self.entries)

    def should_enter(self, spread: Decimal) -> bool:
        """
        检查是否应该加仓

        条件:
        1. 状态为 IDLE 或 ACCUMULATING
        2. spread >= mingap
        3. spread <= maxgap (不追极端行情)
        4. 距上次加仓 >= interval 秒
        5. total_position + qty <= max_position
        """
        if self.state not in (DCAState.IDLE, DCAState.ACCUMULATING):
            return False

        if spread < self.mingap:
            return False

        if spread > self.maxgap:
            return False

        now = time.time()
        if now - self.last_entry_time < self.interval:
            return False

        if self.total_position + self.qty > self.max_position:
            return False

        return True

    def should_reduce(self, reverse_spread: Decimal) -> bool:
        """
        检查是否应该减仓

        条件:
        1. closegap > 0 (功能已启用)
        2. 有仓位 (total_position > 0)
        3. 反向价差 >= closegap (反向有利可图)
        4. 距上次操作 >= interval 秒
        """
        if self.closegap <= 0:
            return False

        if self.total_position <= 0:
            return False

        if reverse_spread < self.closegap:
            return False

        now = time.time()
        if now - self.last_entry_time < self.interval:
            return False

        return True

    def record_entry(self, entry: DCAEntry) -> None:
        """记录一笔成功的 DCA 加仓"""
        self.entries.append(entry)
        self.total_position += entry.size
        self.total_entries += 1
        self.last_entry_time = time.time()

        # 更新状态
        if self.total_position >= self.max_position:
            self.state = DCAState.FULL
        else:
            self.state = DCAState.ACCUMULATING

        logger.info(
            f"[DCA +] #{self.total_entries} | "
            f"spread={entry.spread:.2f} | size={entry.size} | "
            f"total={self.total_position:.6f}/{self.max_position} | "
            f"needed={self.needed} | state={self.state.value}"
        )

    def record_reduction(self, size: Decimal, spread: Decimal) -> None:
        """记录一笔成功的减仓"""
        self.total_position -= size
        self.total_reductions += 1
        self.last_entry_time = time.time()

        if self.total_position <= 0:
            self.total_position = Decimal("0")
            self.state = DCAState.IDLE
            self.entries.clear()
            logger.info(
                f"[DCA -] 全部平仓 | spread={spread:.2f} | state=IDLE"
            )
        else:
            self.state = DCAState.REDUCING
            logger.info(
                f"[DCA -] 减仓 #{self.total_reductions} | "
                f"spread={spread:.2f} | size={size} | "
                f"remaining={self.total_position:.6f} | state=REDUCING"
            )

    def update_positions(self, paradex: Decimal, variational: Decimal) -> None:
        """从交易所实际仓位同步 (取两边较大值，更安全)"""
        self.paradex_position = paradex
        self.variational_position = variational

        # 以两边仓位的较大值为准 (避免单边残留被忽略)
        p_abs = abs(paradex)
        v_abs = abs(variational)
        actual = max(p_abs, v_abs)

        # 检测仓位不平衡
        if (p_abs > 0 or v_abs > 0) and abs(p_abs - v_abs) > self.qty:
            logger.warning(
                f"仓位不平衡: Paradex={paradex:+.6f}, "
                f"Variational={variational:+.6f}, "
                f"差异={abs(p_abs - v_abs):.6f}"
            )

        if actual != self.total_position:
            if self.total_position > 0:
                logger.debug(
                    f"仓位同步: internal={self.total_position:.6f} "
                    f"→ actual={actual:.6f}"
                )
            self.total_position = actual

        # 同步状态
        if self.total_position <= 0:
            if self.state != DCAState.IDLE:
                self.state = DCAState.IDLE
        elif self.total_position >= self.max_position:
            if self.state not in (DCAState.FULL, DCAState.REDUCING):
                self.state = DCAState.FULL

    def get_reduce_size(self) -> Decimal:
        """获取减仓数量 (min(qty, current_position))"""
        return min(self.qty, self.total_position)

    def get_status(self) -> dict:
        """获取当前状态 (用于心跳/日志)"""
        return {
            "state": self.state.value,
            "direction": self.direction,
            "total_position": float(self.total_position),
            "max_position": float(self.max_position),
            "entry_count": self.entry_count,
            "total_entries": self.total_entries,
            "total_reductions": self.total_reductions,
            "needed": self.needed,
            "paradex_position": float(self.paradex_position),
            "variational_position": float(self.variational_position),
            "net_position": float(self.net_position),
            "avg_entry_spread": float(self.avg_entry_spread),
            "avg_price_paradex": float(self.avg_entry_price_paradex),
            "avg_price_variational": float(self.avg_entry_price_variational),
            "total_volume": float(self.total_volume),
        }
