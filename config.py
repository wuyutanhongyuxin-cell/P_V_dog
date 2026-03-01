"""
配置管理模块
DCA/Grid 套利策略配置
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict

from dotenv import load_dotenv


# 交易对名称映射：ticker -> (Paradex合约名, Variational合约名)
MARKET_MAPPING: Dict[str, Dict[str, str]] = {
    "BTC": {"paradex": "BTC-USD-PERP", "variational": "BTC"},
    "ETH": {"paradex": "ETH-USD-PERP", "variational": "ETH"},
    "SOL": {"paradex": "SOL-USD-PERP", "variational": "SOL"},
    "ARB": {"paradex": "ARB-USD-PERP", "variational": "ARB"},
    "DOGE": {"paradex": "DOGE-USD-PERP", "variational": "DOGE"},
    "AVAX": {"paradex": "AVAX-USD-PERP", "variational": "AVAX"},
    "LINK": {"paradex": "LINK-USD-PERP", "variational": "LINK"},
    "OP": {"paradex": "OP-USD-PERP", "variational": "OP"},
    "WIF": {"paradex": "WIF-USD-PERP", "variational": "WIF"},
    "PEPE": {"paradex": "PEPE-USD-PERP", "variational": "PEPE"},
}


@dataclass
class ParadexConfig:
    """Paradex 交易所配置"""
    l2_private_key: str = ""
    l2_address: str = ""
    environment: str = "prod"

    @classmethod
    def from_env(cls) -> "ParadexConfig":
        return cls(
            l2_private_key=os.getenv("PARADEX_L2_PRIVATE_KEY", ""),
            l2_address=os.getenv("PARADEX_L2_ADDRESS", ""),
            environment=os.getenv("PARADEX_ENVIRONMENT", "prod"),
        )

    def validate(self):
        if not self.l2_private_key:
            raise ValueError("PARADEX_L2_PRIVATE_KEY 未设置")
        if not self.l2_address:
            raise ValueError("PARADEX_L2_ADDRESS 未设置")


@dataclass
class VariationalConfig:
    """Variational 交易所配置"""
    vr_token: str = ""
    wallet_address: str = ""
    cookies: str = ""
    private_key: str = ""
    base_url: str = "https://omni.variational.io"
    auth_mode: str = "cookie"

    @classmethod
    def from_env(cls) -> "VariationalConfig":
        return cls(
            vr_token=os.getenv("VARIATIONAL_VR_TOKEN", ""),
            wallet_address=os.getenv("VARIATIONAL_WALLET_ADDRESS", ""),
            cookies=os.getenv("VARIATIONAL_COOKIES", ""),
            private_key=os.getenv("VARIATIONAL_PRIVATE_KEY", ""),
            base_url=os.getenv(
                "VARIATIONAL_BASE_URL", "https://omni.variational.io"
            ),
        )

    def validate(self, auth_mode: str):
        self.auth_mode = auth_mode
        if auth_mode == "cookie":
            if not self.vr_token and not self.cookies:
                raise ValueError(
                    "Cookie 模式需要设置 VARIATIONAL_VR_TOKEN 或 VARIATIONAL_COOKIES\n"
                    "从浏览器 F12 控制台执行 document.cookie 获取"
                )
            if not self.wallet_address:
                raise ValueError(
                    "需要设置 VARIATIONAL_WALLET_ADDRESS\n"
                    "你在 Variational 上连接的钱包地址"
                )


@dataclass
class TelegramConfig:
    """Telegram 通知配置"""
    bot_token: str = ""
    group_id: str = ""
    account_label: str = ""
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        group_id = os.getenv("TELEGRAM_GROUP_ID", "")
        return cls(
            bot_token=bot_token,
            group_id=group_id,
            account_label=os.getenv("ACCOUNT_LABEL", "DCA-1"),
            enabled=bool(bot_token and group_id),
        )


@dataclass
class DCAConfig:
    """DCA/Grid 交易配置"""
    ticker: str = "BTC"
    direction: str = "long"           # "long" = Paradex买+Variational卖, "short" = 反向
    qty: Decimal = Decimal("0.005")   # 每次加仓数量
    max_position: Decimal = Decimal("0.1")  # 最大累积仓位
    mingap: Decimal = Decimal("33")   # 最小价差阈值 (USD)
    maxgap: Decimal = Decimal("44")   # 最大价差阈值 (USD), 超过不加仓
    closegap: Decimal = Decimal("0")  # 减仓阈值 (0=不主动减仓)
    interval: float = 30.0            # 加仓最小间隔 (秒)
    fill_timeout: int = 5
    min_balance: Decimal = Decimal("10")
    dry_run: bool = False             # 试运行模式 (不下单)

    # 自动解析的合约名
    paradex_market: str = ""
    variational_market: str = ""

    def resolve_markets(self):
        """根据 ticker 解析两边的合约名称"""
        env_paradex = os.getenv("PARADEX_MARKET", "")
        env_variational = os.getenv("VARIATIONAL_MARKET", "")

        if env_paradex:
            self.paradex_market = env_paradex
        elif self.ticker in MARKET_MAPPING:
            self.paradex_market = MARKET_MAPPING[self.ticker]["paradex"]
        else:
            self.paradex_market = f"{self.ticker}-USD-PERP"

        if env_variational:
            self.variational_market = env_variational
        elif self.ticker in MARKET_MAPPING:
            self.variational_market = MARKET_MAPPING[self.ticker]["variational"]
        else:
            self.variational_market = self.ticker

    def validate(self):
        """验证 DCA 交易参数"""
        if self.qty <= 0:
            raise ValueError(f"qty 必须为正数, 当前: {self.qty}")
        if self.max_position <= 0:
            raise ValueError(f"max_position 必须为正数, 当前: {self.max_position}")
        if self.qty > self.max_position:
            raise ValueError(
                f"qty ({self.qty}) 不能大于 max_position ({self.max_position})"
            )
        if self.mingap < 0:
            raise ValueError(f"mingap 不能为负数, 当前: {self.mingap}")
        if self.maxgap <= self.mingap:
            raise ValueError(
                f"maxgap ({self.maxgap}) 必须大于 mingap ({self.mingap})"
            )
        if self.interval < 0:
            raise ValueError(f"interval 不能为负数, 当前: {self.interval}")


@dataclass
class AppConfig:
    """应用总配置"""
    paradex: ParadexConfig = field(default_factory=ParadexConfig)
    variational: VariationalConfig = field(default_factory=VariationalConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    trading: DCAConfig = field(default_factory=DCAConfig)
    variational_auth_mode: str = "cookie"

    @classmethod
    def load(cls, env_file: str = ".env") -> "AppConfig":
        """从环境变量加载配置"""
        load_dotenv(env_file)
        config = cls(
            paradex=ParadexConfig.from_env(),
            variational=VariationalConfig.from_env(),
            telegram=TelegramConfig.from_env(),
        )
        return config

    def validate(self):
        """验证所有必要配置"""
        self.paradex.validate()
        self.variational.validate(self.variational_auth_mode)
        self.trading.resolve_markets()
        self.trading.validate()
