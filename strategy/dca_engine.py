"""
DCA/Grid 套利引擎
Paradex × Variational 跨所渐进建仓策略

核心逻辑:
  1. 并行获取双边 BBO
  2. 固定阈值 (mingap/maxgap) 判断信号
  3. 每次信号只加仓一个 qty (不一次性开仓)
  4. 双腿 asyncio.gather 并行执行
  5. 单腿失败精确撤销 (P6 移植)
  6. 系统熔断器 + 连续失败暂停 (P6 移植)
"""

import asyncio
import logging
import signal
import sys
import time
import traceback
from datetime import datetime
from decimal import Decimal
from typing import Optional

import pytz

from exchanges.paradex_client import ParadexInteractiveClient
from exchanges.variational_client import VariationalClient
from exchanges.base import BBO, OrderResult
from helpers.logger import TradingLogger
from helpers.telegram_bot import TelegramNotifier
from helpers.pnl_tracker import PnLTracker

from .spread_monitor import SpreadMonitor
from .position_manager import PositionManager, DCAEntry, DCAState

logger = logging.getLogger("dca_engine")


class DCAEngine:
    """
    DCA/Grid 套利引擎

    主循环 (每秒):
      1. 系统暂停检查 (熔断器)
      2. Variational 限速检查
      3. 并行获取双边 BBO
      4. 计算价差
      5. should_enter() → execute_entry()
      6. should_reduce() → execute_reduce()
      7. 心跳 (5 分钟)
      8. 定期检查 (余额/仓位/限速)
    """

    def __init__(
        self,
        paradex: ParadexInteractiveClient,
        variational: VariationalClient,
        paradex_market: str,
        variational_market: str,
        ticker: str,
        direction: str,
        qty: Decimal,
        max_position: Decimal,
        mingap: Decimal,
        maxgap: Decimal,
        closegap: Decimal = Decimal("0"),
        interval: float = 30.0,
        fill_timeout: int = 5,
        min_balance: Decimal = Decimal("10"),
        telegram: Optional[TelegramNotifier] = None,
        dry_run: bool = False,
    ):
        self.paradex = paradex
        self.variational = variational
        self.paradex_market = paradex_market
        self.variational_market = variational_market
        self.ticker = ticker
        self.direction = direction
        self.qty = qty
        self.fill_timeout = fill_timeout
        self.min_balance = min_balance
        self.dry_run = dry_run

        # 策略模块
        self.spread_monitor = SpreadMonitor(direction=direction)
        self.position_manager = PositionManager(
            direction=direction,
            qty=qty,
            max_position=max_position,
            mingap=mingap,
            maxgap=maxgap,
            closegap=closegap,
            interval=interval,
        )

        # 辅助模块
        self.telegram = telegram
        self.pnl_tracker = PnLTracker(ticker=ticker)
        self.trading_logger = TradingLogger(ticker=ticker)

        # 控制标志
        self.stop_flag = False
        self._cleanup_done = False

        # 统计
        self.trade_count = 0
        self.start_time = time.time()
        self.start_time_str = ""  # 格式化启动时间
        self.last_balance_report_time = time.time()
        self.last_heartbeat_time: float = 0
        self.heartbeat_interval: float = 300.0  # 5 分钟

        # BBO 失败计数
        self._bbo_fail_count: int = 0

        # 系统熔断器 (P6 移植)
        self._system_pause_until: float = 0
        self._consecutive_leg_failures: int = 0
        self._max_consecutive_failures: int = 3
        self._system_pause_duration: float = 600.0    # 系统错误暂停 10 分钟
        self._leg_failure_pause_duration: float = 300.0  # 连续失败暂停 5 分钟

        # 余额缓存
        self._paradex_balance: Decimal = Decimal("0")
        self._variational_balance: Decimal = Decimal("0")

        # 时区
        self._tz = pytz.timezone("Asia/Shanghai")

    # ========== 信号处理与优雅退出 ==========

    def setup_signal_handlers(self) -> None:
        """注册 Ctrl+C 优雅退出"""
        signal.signal(signal.SIGINT, self._signal_handler)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        if self.stop_flag:
            return
        self.stop_flag = True
        logger.info("收到退出信号，准备优雅退出...")

    async def _graceful_shutdown(self) -> None:
        """
        优雅退出:
        1. 取消所有挂单
        2. 市价平仓两边
        3. 验证仓位归零
        4. 最终统计
        """
        if self._cleanup_done:
            return
        self._cleanup_done = True

        logger.info("=" * 60)
        logger.info("开始优雅退出流程...")

        # 1. 取消挂单
        logger.info("[退出] 取消 Paradex 所有挂单...")
        await self.paradex.cancel_all_orders(self.paradex_market)
        logger.info("[退出] 取消 Variational 所有挂单...")
        await self.variational.cancel_all_orders(self.variational_market)

        # 2. 市价平仓
        logger.info("[退出] 市价平仓 Paradex...")
        await self.paradex.close_position(self.paradex_market)
        logger.info("[退出] 市价平仓 Variational...")
        await self.variational.close_position(self.variational_market)

        # 3. 验证仓位归零
        for i in range(10):
            await asyncio.sleep(1)
            p_pos = await self.paradex.get_position_size(self.paradex_market)
            v_pos = await self.variational.get_position_size(
                self.variational_market
            )
            logger.info(
                f"[退出] 验证仓位 ({i + 1}/10): "
                f"Paradex={p_pos}, Variational={v_pos}"
            )
            if p_pos == 0 and v_pos == 0:
                logger.info("[退出] 两边仓位已归零")
                break
            if p_pos != 0:
                await self.paradex.close_position(self.paradex_market)
            if v_pos != 0:
                await self.variational.close_position(self.variational_market)

        # 4. 最终统计
        elapsed = time.time() - self.start_time
        pnl = self.pnl_tracker.get_summary()
        logger.info("=" * 60)
        logger.info(f"运行时间: {elapsed / 3600:.2f} 小时")
        logger.info(f"总交易次数: {self.trade_count}")
        logger.info(f"总利润: ${pnl['total_gross_profit']:.2f}")
        logger.info(f"总交易量: ${pnl['total_volume']:.2f}")
        logger.info("=" * 60)

        if self.telegram:
            self.telegram.send(
                f"🛑 <b>DCA 机器人已退出</b>\n\n"
                f"运行: {elapsed / 3600:.2f}h\n"
                f"交易: {self.trade_count} 笔\n"
                f"利润: ${pnl['total_gross_profit']:.2f}\n"
                f"交易量: ${pnl['total_volume']:.2f}"
            )

        await self.paradex.disconnect()
        await self.variational.disconnect()

    # ========== 主循环 ==========

    async def run(self) -> None:
        """运行 DCA 引擎"""
        self.setup_signal_handlers()
        try:
            await self._initialize()
            await self._trading_loop()
        except KeyboardInterrupt:
            logger.info("收到键盘中断...")
        except asyncio.CancelledError:
            logger.info("任务被取消...")
        except Exception as e:
            logger.error(f"DCA 引擎异常: {e}")
            logger.error(traceback.format_exc())
        finally:
            await self._graceful_shutdown()

    async def _initialize(self) -> None:
        """初始化: 连接交易所、获取市场信息、同步仓位"""
        self.start_time_str = datetime.now(self._tz).strftime("%Y-%m%d-%H:%M")

        dir_label = "Paradex做多+Variational做空" if self.direction == "long" else "Paradex做空+Variational做多"

        logger.info("=" * 60)
        logger.info(f"DCA/Grid 套利引擎 [{dir_label}]")
        logger.info(f"交易对: {self.ticker}")
        logger.info(f"Paradex: {self.paradex_market}")
        logger.info(f"Variational: {self.variational_market}")
        logger.info(
            f"参数: qty={self.qty} | max={self.position_manager.max_position} | "
            f"mingap={self.position_manager.mingap} | maxgap={self.position_manager.maxgap} | "
            f"closegap={self.position_manager.closegap} | interval={self.position_manager.interval}s"
        )
        if self.dry_run:
            logger.info("*** DRY RUN 模式 — 不会执行真实交易 ***")
        logger.info("=" * 60)

        # 连接交易所
        logger.info("连接 Paradex (Interactive Token)...")
        await self.paradex.connect()
        logger.info("连接 Variational...")
        await self.variational.connect()

        # 获取市场信息
        p_info = await self.paradex.get_market_info(self.paradex_market)
        v_info = await self.variational.get_market_info(self.variational_market)
        if p_info:
            logger.info(
                f"Paradex: tick={p_info.tick_size}, step={p_info.step_size}"
            )
        if v_info:
            logger.info(
                f"Variational: tick={v_info.tick_size}, step={v_info.step_size}"
            )

        # 同步仓位
        p_pos = await self.paradex.get_position_size(self.paradex_market)
        v_pos = await self.variational.get_position_size(
            self.variational_market
        )
        self.position_manager.update_positions(p_pos, v_pos)
        logger.info(
            f"仓位: Paradex={p_pos:+.6f} | Variational={v_pos:+.6f} | "
            f"净={self.position_manager.net_position:+.6f}"
        )

        # 余额
        self._paradex_balance = await self.paradex.get_balance() or Decimal("0")
        self._variational_balance = await self.variational.get_balance() or Decimal("0")
        total_equity = self._paradex_balance + self._variational_balance
        self.pnl_tracker.set_initial_equity(total_equity)
        logger.info(
            f"余额: Paradex={self._paradex_balance} | "
            f"Variational={self._variational_balance} | "
            f"总权益=${total_equity:.2f}"
        )

        # Telegram 启动通知
        if self.telegram:
            status = self.position_manager.get_status()
            self.telegram.send(
                f"🚀 <b>DCA 机器人启动</b>\n\n"
                f"方向: {dir_label}\n"
                f"交易对: {self.ticker}\n"
                f"qty={self.qty} | max={self.position_manager.max_position}\n"
                f"mingap={self.position_manager.mingap} | "
                f"maxgap={self.position_manager.maxgap}\n"
                f"interval={self.position_manager.interval}s\n"
                f"余额: P={self._paradex_balance}, V={self._variational_balance}\n"
                f"仓位: P={p_pos:+.6f}, V={v_pos:+.6f}\n"
                f"needed={status['needed']}"
                + ("\n⚠️ DRY RUN 模式" if self.dry_run else "")
            )

        logger.info("初始化完成，开始价差监控...")

    async def _trading_loop(self) -> None:
        """主交易循环"""
        cycle = 0

        while not self.stop_flag:
            try:
                cycle += 1

                # 1. 系统暂停检查 (熔断器)
                if self._is_system_paused(cycle):
                    await asyncio.sleep(1.0)
                    continue

                # 2. Variational 限速检查
                if self.variational.is_rate_limited:
                    if cycle % 60 == 0:
                        remaining = max(
                            0,
                            int(self.variational._rate_limited_until - time.time()),
                        )
                        logger.info(
                            f"[V 限速中] 剩余 {remaining}s"
                        )
                    await asyncio.sleep(1.0)
                    continue

                # 3. Paradex Interactive 限速检查
                if self.paradex.should_pause_trading:
                    if cycle % 60 == 0:
                        rate_info = self.paradex.get_rate_info()
                        logger.info(
                            f"[P 限速中] 1h={rate_info['orders_1h']}/200 "
                            f"paused={rate_info['paused']}"
                        )
                    await asyncio.sleep(1.0)
                    continue

                # 4. 获取双边 BBO
                p_bbo, v_bbo = await self._fetch_both_bbo()
                if not p_bbo or not v_bbo:
                    self._bbo_fail_count += 1
                    if self._bbo_fail_count % 10 == 1:
                        fail = []
                        if not p_bbo:
                            fail.append("Paradex")
                        if not v_bbo:
                            fail.append("Variational")
                        logger.warning(
                            f"BBO 获取失败 ({self._bbo_fail_count}x): "
                            f"{'+'.join(fail)}"
                        )
                    await asyncio.sleep(0.5)
                    continue

                if self._bbo_fail_count > 0:
                    logger.info(
                        f"BBO 恢复 (之前失败 {self._bbo_fail_count}x)"
                    )
                    self._bbo_fail_count = 0

                # 5. 计算价差
                spread = self.spread_monitor.update(
                    p_bbo.bid, p_bbo.ask, v_bbo.bid, v_bbo.ask
                )

                # 6. 检查 DCA 加仓
                if (
                    not self.stop_flag
                    and self.position_manager.should_enter(spread)
                ):
                    await self._execute_entry(p_bbo, v_bbo, spread)

                # 7. 检查减仓 (如果 closegap > 0)
                if (
                    not self.stop_flag
                    and self.position_manager.closegap > 0
                    and self.position_manager.total_position > 0
                ):
                    reverse_spread = self.spread_monitor.reverse_spread
                    if self.position_manager.should_reduce(reverse_spread):
                        await self._execute_reduce(
                            p_bbo, v_bbo, reverse_spread
                        )

                # 8. 心跳
                self._heartbeat_if_needed(p_bbo, v_bbo, spread)

                # 9. 定期检查
                await self._periodic_checks()

                # 10. 休眠
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"循环异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(1)

    # ========== BBO 获取 ==========

    async def _fetch_both_bbo(self) -> tuple:
        """并行获取双边 BBO (Variational 使用 RFQ 实时报价)"""
        try:
            if self.variational.is_rate_limited:
                p_bbo = await self.paradex.get_bbo(self.paradex_market)
                return (p_bbo, None)

            p_task = asyncio.create_task(
                self.paradex.get_bbo(self.paradex_market)
            )
            v_task = asyncio.create_task(
                self.variational.get_bbo(
                    self.variational_market, size=self.qty
                )
            )

            p_bbo, v_bbo = await asyncio.gather(
                p_task, v_task, return_exceptions=True
            )

            if isinstance(p_bbo, Exception):
                logger.warning(f"Paradex BBO 失败: {p_bbo}")
                p_bbo = None
            if isinstance(v_bbo, Exception):
                logger.warning(f"Variational BBO 失败: {v_bbo}")
                v_bbo = None

            return p_bbo, v_bbo

        except Exception as e:
            logger.error(f"BBO 获取异常: {e}")
            return None, None

    # ========== DCA 加仓执行 ==========

    async def _execute_entry(
        self, p_bbo: BBO, v_bbo: BBO, spread: Decimal
    ) -> None:
        """
        执行 DCA 加仓 — 双腿并行

        long: Paradex BUY + Variational SELL
        short: Paradex SELL + Variational BUY
        """
        if self.stop_flag:
            return

        qty = self.qty

        if self.direction == "long":
            p_side, v_side = "BUY", "SELL"
            p_price = p_bbo.ask
            v_price = v_bbo.bid
        else:
            p_side, v_side = "SELL", "BUY"
            p_price = p_bbo.bid
            v_price = v_bbo.ask

        # 利润预估
        gross_profit = spread * qty

        logger.info(
            f"[DCA 加仓] spread=${spread:.2f} | "
            f"Paradex {p_side} {qty} @ ~${p_price:.2f} | "
            f"Variational {v_side} {qty} | "
            f"预估利润=${gross_profit:.4f}"
        )

        # Dry run 模式
        if self.dry_run:
            logger.info("[DRY RUN] 跳过实际下单")
            # 模拟记录
            entry = DCAEntry(
                timestamp=time.time(),
                paradex_price=p_price,
                variational_price=v_price,
                size=qty,
                spread=spread,
                direction=self.direction,
            )
            self.position_manager.record_entry(entry)
            self.trade_count += 1
            return

        # 双腿并行下单 (asyncio.gather, P0b 移植)
        p_coro = self.paradex.place_market_order(
            market=self.paradex_market,
            side=p_side,
            size=qty,
            price=p_price,
        )
        v_coro = self.variational.place_market_order(
            market=self.variational_market,
            side=v_side,
            size=qty,
            quote_id=v_bbo.quote_id,
        )

        results = await asyncio.gather(p_coro, v_coro, return_exceptions=True)
        p_result, v_result = results

        p_ok = isinstance(p_result, OrderResult) and p_result.success
        v_ok = isinstance(v_result, OrderResult) and v_result.success

        p_err = (
            p_result.error_message
            if isinstance(p_result, OrderResult)
            else str(p_result)
        )
        v_err = (
            v_result.error_message
            if isinstance(v_result, OrderResult)
            else str(v_result)
        )

        # ===== 结果 1: 双成功 =====
        if p_ok and v_ok:
            self.trade_count += 1
            self._consecutive_leg_failures = 0

            actual_p_price = p_result.price if p_result.price else p_price
            actual_v_price = v_price  # Variational RFQ 不返回成交价

            entry = DCAEntry(
                timestamp=time.time(),
                paradex_price=actual_p_price,
                variational_price=actual_v_price,
                size=qty,
                spread=spread,
                direction=self.direction,
            )
            self.position_manager.record_entry(entry)

            # 更新交易量
            self.position_manager.total_volume += actual_p_price * qty

            # P&L 记录
            self.pnl_tracker.record_entry(
                direction=self.direction,
                spread=spread,
                size=qty,
                paradex_price=actual_p_price,
                variational_price=actual_v_price,
                paradex_bbo_price=p_price,
                variational_bbo_price=v_price,
            )

            # 交易日志
            self.trading_logger.log_trade(
                self.direction.upper(), qty, actual_p_price, spread
            )

            logger.info(
                f"[DCA 成交] #{self.trade_count} | "
                f"P {p_side} @ ${actual_p_price:.2f} | "
                f"V {v_side} | spread=${spread:.2f} | "
                f"profit~=${gross_profit:.4f}"
            )

            # 刷新实际仓位
            await self._refresh_positions()

            # Telegram
            if self.telegram:
                status = self.position_manager.get_status()
                self.telegram.send(
                    f"✅ <b>DCA 加仓 #{self.trade_count}</b>\n\n"
                    f"Paradex {p_side}: {qty} @ ${actual_p_price:.2f}\n"
                    f"Variational {v_side}: {qty}\n"
                    f"价差: ${spread:.2f} | 利润~${gross_profit:.4f}\n"
                    f"仓位: {status['total_position']:.6f}/"
                    f"{status['max_position']} | "
                    f"needed={status['needed']}"
                )

        # ===== 结果 2: Paradex 成功, Variational 失败 =====
        elif p_ok and not v_ok:
            logger.error(f"[单腿失败] Variational {v_side} 失败: {v_err}")
            await self._undo_succeeded_leg(
                "Paradex", self.paradex, self.paradex_market,
                p_side, qty,
            )
            self._handle_leg_failure(f"Variational {v_side}", v_err)
            if self.telegram:
                self.telegram.send(
                    f"⚠️ <b>单腿失败</b>\n\n"
                    f"Variational {v_side} 失败: {v_err}\n"
                    f"已撤销 Paradex {p_side} {qty}"
                )

        # ===== 结果 3: Variational 成功, Paradex 失败 =====
        elif not p_ok and v_ok:
            logger.error(f"[单腿失败] Paradex {p_side} 失败: {p_err}")
            await self._undo_succeeded_leg(
                "Variational", self.variational, self.variational_market,
                v_side, qty,
            )
            self._handle_leg_failure(f"Paradex {p_side}", p_err)
            if self.telegram:
                self.telegram.send(
                    f"⚠️ <b>单腿失败</b>\n\n"
                    f"Paradex {p_side} 失败: {p_err}\n"
                    f"已撤销 Variational {v_side} {qty}"
                )

        # ===== 结果 4: 双失败 =====
        else:
            logger.warning(
                f"[双腿失败] Paradex: {p_err} | Variational: {v_err}"
            )
            if "SYSTEM_STATUS_" in (p_err or ""):
                self._handle_leg_failure(f"Paradex {p_side}", p_err)

    # ========== DCA 减仓执行 ==========

    async def _execute_reduce(
        self, p_bbo: BBO, v_bbo: BBO, reverse_spread: Decimal
    ) -> None:
        """
        执行 DCA 减仓 — 反向双腿并行

        long 减仓: Paradex SELL + Variational BUY
        short 减仓: Paradex BUY + Variational SELL
        """
        if self.stop_flag:
            return

        qty = self.position_manager.get_reduce_size()
        if qty <= 0:
            return

        if self.direction == "long":
            p_side, v_side = "SELL", "BUY"
            p_price = p_bbo.bid
            v_price = v_bbo.ask
        else:
            p_side, v_side = "BUY", "SELL"
            p_price = p_bbo.ask
            v_price = v_bbo.bid

        gross_profit = reverse_spread * qty

        logger.info(
            f"[DCA 减仓] reverse_spread=${reverse_spread:.2f} | "
            f"Paradex {p_side} {qty} | Variational {v_side} {qty} | "
            f"利润=${gross_profit:.4f}"
        )

        if self.dry_run:
            logger.info("[DRY RUN] 跳过实际下单")
            self.position_manager.record_reduction(qty, reverse_spread)
            return

        # 双腿并行
        p_coro = self.paradex.place_market_order(
            market=self.paradex_market,
            side=p_side,
            size=qty,
            price=p_price,
        )
        v_coro = self.variational.place_market_order(
            market=self.variational_market,
            side=v_side,
            size=qty,
            quote_id=v_bbo.quote_id,
        )

        results = await asyncio.gather(p_coro, v_coro, return_exceptions=True)
        p_result, v_result = results

        p_ok = isinstance(p_result, OrderResult) and p_result.success
        v_ok = isinstance(v_result, OrderResult) and v_result.success

        p_err = (
            p_result.error_message
            if isinstance(p_result, OrderResult)
            else str(p_result)
        )
        v_err = (
            v_result.error_message
            if isinstance(v_result, OrderResult)
            else str(v_result)
        )

        if p_ok and v_ok:
            self.trade_count += 1
            self._consecutive_leg_failures = 0

            actual_p_price = p_result.price if p_result.price else p_price
            self.position_manager.record_reduction(qty, reverse_spread)

            self.pnl_tracker.record_reduction(
                direction=self.direction,
                spread=reverse_spread,
                size=qty,
                paradex_price=actual_p_price,
                variational_price=v_price,
            )

            await self._refresh_positions()

            logger.info(
                f"[DCA 减仓成交] #{self.trade_count} | "
                f"spread=${reverse_spread:.2f} | profit~=${gross_profit:.4f}"
            )

            if self.telegram:
                status = self.position_manager.get_status()
                self.telegram.send(
                    f"📉 <b>DCA 减仓</b>\n\n"
                    f"P {p_side} + V {v_side}: {qty}\n"
                    f"价差: ${reverse_spread:.2f}\n"
                    f"剩余仓位: {status['total_position']:.6f}"
                )

        elif p_ok and not v_ok:
            logger.error(f"[减仓单腿失败] V {v_side}: {v_err}")
            await self._undo_succeeded_leg(
                "Paradex", self.paradex, self.paradex_market,
                p_side, qty,
            )
            self._handle_leg_failure(f"Variational {v_side}", v_err)

        elif not p_ok and v_ok:
            logger.error(f"[减仓单腿失败] P {p_side}: {p_err}")
            await self._undo_succeeded_leg(
                "Variational", self.variational, self.variational_market,
                v_side, qty,
            )
            self._handle_leg_failure(f"Paradex {p_side}", p_err)

        else:
            logger.warning(
                f"[减仓双失败] P: {p_err} | V: {v_err}"
            )
            if "SYSTEM_STATUS_" in (p_err or ""):
                self._handle_leg_failure(f"Paradex {p_side}", p_err)

    # ========== 单腿失败撤销 (P6 移植) ==========

    async def _undo_succeeded_leg(
        self,
        exchange_name: str,
        exchange,
        market: str,
        succeeded_side: str,
        size: Decimal,
    ) -> bool:
        """
        单腿失败时撤销成功的那条腿
        只撤销刚执行的 size，不是 close_position 全部平仓
        """
        undo_side = "SELL" if succeeded_side == "BUY" else "BUY"
        logger.info(
            f"[撤销] {exchange_name} {undo_side} {size} "
            f"(撤销刚才的 {succeeded_side} {size})"
        )

        try:
            result = await exchange.place_market_order(
                market=market,
                side=undo_side,
                size=size,
            )
            if isinstance(result, OrderResult) and result.success:
                logger.info(
                    f"[撤销成功] {exchange_name} {undo_side} {size}"
                )
                await self._refresh_positions()
                return True
            else:
                err = (
                    result.error_message
                    if isinstance(result, OrderResult)
                    else str(result)
                )
                logger.error(
                    f"[撤销失败] {exchange_name} {undo_side} {size}: {err}"
                )
                await self._refresh_positions()
                return False
        except Exception as e:
            logger.error(
                f"[撤销异常] {exchange_name} {undo_side} {size}: {e}"
            )
            return False

    # ========== 熔断器 (P6 移植) ==========

    def _handle_leg_failure(self, failed_leg: str, error: str) -> None:
        """处理单腿失败: 系统错误检测 + 连续失败计数"""
        if "SYSTEM_STATUS_" in (error or ""):
            self._activate_system_pause(f"{failed_leg}: {error}")
            return

        self._consecutive_leg_failures += 1
        if self._consecutive_leg_failures >= self._max_consecutive_failures:
            self._activate_leg_failure_pause()

    def _activate_system_pause(self, reason: str) -> None:
        """系统维护暂停 (CANCEL_ONLY / POST_ONLY)"""
        self._system_pause_until = time.time() + self._system_pause_duration
        self._consecutive_leg_failures = 0
        logger.warning(
            f"[系统熔断] {reason}\n"
            f"暂停交易 {self._system_pause_duration:.0f} 秒"
        )
        if self.telegram:
            self.telegram.send(
                f"🚨 <b>系统熔断</b>\n\n"
                f"原因: {reason}\n"
                f"暂停: {int(self._system_pause_duration // 60)} 分钟"
            )

    def _activate_leg_failure_pause(self) -> None:
        """连续单腿失败暂停"""
        self._system_pause_until = (
            time.time() + self._leg_failure_pause_duration
        )
        logger.warning(
            f"[连续失败熔断] 连续 {self._consecutive_leg_failures} 次\n"
            f"暂停 {self._leg_failure_pause_duration:.0f} 秒"
        )
        if self.telegram:
            self.telegram.send(
                f"🚨 <b>连续失败熔断</b>\n\n"
                f"连续失败: {self._consecutive_leg_failures} 次\n"
                f"暂停: {int(self._leg_failure_pause_duration // 60)} 分钟"
            )
        self._consecutive_leg_failures = 0

    def _is_system_paused(self, cycle: int) -> bool:
        """检查系统暂停状态"""
        now = time.time()
        in_pause = now < self._system_pause_until

        if in_pause and cycle % 60 == 0:
            remaining = int(self._system_pause_until - now)
            logger.info(f"[系统暂停中] 剩余 {remaining} 秒")
        elif not in_pause and self._system_pause_until > 0:
            logger.info("[系统暂停结束] 恢复交易")
            self._system_pause_until = 0

        return in_pause

    # ========== 仓位刷新 ==========

    async def _refresh_positions(self) -> None:
        """刷新双边仓位"""
        try:
            p_pos = await self.paradex.get_position_size(self.paradex_market)
            v_pos = await self.variational.get_position_size(
                self.variational_market
            )
            self.position_manager.update_positions(p_pos, v_pos)
        except Exception as e:
            logger.warning(f"刷新仓位失败: {e}")

    # ========== 心跳 (模仿竞品格式) ==========

    def _heartbeat_if_needed(
        self, p_bbo: BBO, v_bbo: BBO, spread: Decimal
    ) -> None:
        """5 分钟心跳"""
        now = time.time()
        if now - self.last_heartbeat_time < self.heartbeat_interval:
            return
        self.last_heartbeat_time = now

        if not p_bbo or not v_bbo:
            return

        status = self.position_manager.get_status()
        pnl = self.pnl_tracker.get_summary()
        runtime_h = (now - self.start_time) / 3600

        total_equity = self._paradex_balance + self._variational_balance

        # 控制台心跳
        logger.info("=" * 60)
        logger.info(
            f"💚 心跳 | 启动时间={self.start_time_str} | {self.ticker} | "
            f"mingap={self.position_manager.mingap} "
            f"maxgap={self.position_manager.maxgap} "
            f"qty={self.qty} interval={self.position_manager.interval} "
            f"needed={status['needed']}"
        )
        logger.info(
            f"A持仓={status['paradex_position']:+.6f} | "
            f"B持仓={status['variational_position']:+.6f} | "
            f"净持仓={status['net_position']:.6f}"
        )
        logger.info(
            f"账户余额: EX={self._paradex_balance:.2f} "
            f"LG={self._variational_balance:.2f} | "
            f"总策略权益=${total_equity:.2f} | "
            f"盈亏=${pnl['total_gross_profit']:.2f} | "
            f"总交易量=${pnl['total_volume']:.2f}"
        )
        logger.info(
            f"初始总权益=${pnl['initial_equity']:.2f} | "
            f"交易次数={self.trade_count} | "
            f"state={status['state']}"
        )
        logger.info(
            f"当前价差=${spread:.2f} | "
            f"平均价差=${pnl['avg_spread']:.2f} | "
            f"总平均偏移: A={pnl['avg_slippage_a']:.2f} "
            f"({pnl['avg_slippage_a_bps']:.2f}bps) "
            f"B={pnl['avg_slippage_b']:.4f} "
            f"({pnl['avg_slippage_b_bps']:.2f}bps)"
        )
        logger.info("=" * 60)

        # Telegram 心跳 (模仿竞品格式)
        if self.telegram:
            self.telegram.send(
                f"💚 心跳 | 启动时间={self.start_time_str} | {self.ticker} | "
                f"mingap={self.position_manager.mingap} "
                f"maxgap={self.position_manager.maxgap} "
                f"qty={self.qty} interval={self.position_manager.interval} "
                f"needed={status['needed']} | "
                f"A持仓={status['paradex_position']:+.6f} | "
                f"B持仓={status['variational_position']:+.6f} | "
                f"净持仓={status['net_position']:.6f}\n"
                f"账户余额: EX={self._paradex_balance:.2f} "
                f"LG={self._variational_balance:.2f} | "
                f"总策略权益=${total_equity:.2f} | "
                f"盈亏=${pnl['total_gross_profit']:.2f} | "
                f"总交易量=${pnl['total_volume']:.2f}\n"
                f"初始总权益=${pnl['initial_equity']:.2f} | "
                f"交易次数={self.trade_count}\n"
                f"总平均偏移: A={pnl['avg_slippage_a']:.2f} "
                f"({pnl['avg_slippage_a_bps']:.2f}bps) "
                f"B={pnl['avg_slippage_b']:.4f} "
                f"({pnl['avg_slippage_b_bps']:.2f}bps)"
            )

    # ========== 定期检查 ==========

    async def _periodic_checks(self) -> None:
        """定期检查: 余额/仓位/限速 (每 30 分钟)"""
        now = time.time()
        if now - self.last_balance_report_time < 1800:
            return

        self.last_balance_report_time = now

        # 刷新余额
        p_bal = await self.paradex.get_balance()
        v_bal = await self.variational.get_balance()

        if p_bal is not None:
            self._paradex_balance = p_bal
        if v_bal is not None:
            self._variational_balance = v_bal

        logger.info(
            f"[余额报告] Paradex={p_bal} | Variational={v_bal}"
        )

        # 余额不足
        if p_bal is not None and p_bal < self.min_balance:
            logger.error(
                f"Paradex 余额不足 ({p_bal} < {self.min_balance})!"
            )
            self.stop_flag = True
            return

        if v_bal is not None and v_bal < self.min_balance:
            logger.error(
                f"Variational 余额不足 ({v_bal} < {self.min_balance})!"
            )
            self.stop_flag = True
            return

        # 仓位不平衡检测
        await self._refresh_positions()
        net = abs(self.position_manager.net_position)
        if net > self.qty * 2:
            logger.warning(
                f"[仓位不平衡] 净仓位={net:.6f} > {self.qty * 2:.6f}"
            )
            if self.telegram:
                self.telegram.send(
                    f"⚠️ <b>仓位不平衡</b>\n"
                    f"净仓位: {net:.6f}\n"
                    f"P: {self.position_manager.paradex_position:+.6f}\n"
                    f"V: {self.position_manager.variational_position:+.6f}"
                )

        # 限速状态
        rate_info = self.paradex.get_rate_info()
        logger.info(
            f"[限速状态] 1h={rate_info['orders_1h']}/200 "
            f"24h={rate_info['orders_24h']}/1000 "
            f"paused={rate_info['paused']}"
        )
