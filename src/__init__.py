"""
Polymarket Arbitrage Bot - Core Trading Library

A production-ready Python trading library for Polymarket with comprehensive
features for automated trading, order management, and real-time market data.

Key Features:
    - Encrypted private key storage (PBKDF2 + Fernet)
    - Gasless transactions via Builder Program
    - Real-time WebSocket orderbook updates
    - Modular architecture for easy extension
    - Comprehensive error handling and logging

Quick Start:
    # Option 1: From environment variables
    from src import create_bot_from_env
    bot = create_bot_from_env()

    # Option 2: Manual configuration
    from src import TradingBot, Config
    config = Config(safe_address="0x...")
    bot = TradingBot(config=config, private_key="0x...")

    # Place an order
    result = await bot.place_order(token_id, price=0.5, size=1.0, side="BUY")

Core Modules:
    bot.py            - TradingBot class (main trading interface)
    config.py         - Configuration management and loading
    client.py         - API clients (CLOB, Relayer)
    signer.py         - EIP-712 order signing and verification
    crypto.py         - Private key encryption and management
    websocket_client.py - Real-time WebSocket client for market data
    gamma_client.py   - 15-minute market discovery and information
    utils.py          - Helper functions and utilities
"""

"""Top-level package exports.

Historically this module eagerly imported everything (web3, eth-account, ...).
That made *any* import of `src.*` fail unless all trading dependencies were
installed, even if you only wanted the lightweight Gamma/event ingesters.

We now import optional components behind try/except so utility scripts like
`apps/ingest_markets_pg.py` can run with just requests/psycopg2 installed.
"""

# Always-available lightweight clients
from .gamma_client import GammaClient  # noqa: F401


__version__ = "1.0.0"
__author__ = "Polymarket Arbitrage Bot Contributors"

# Optional heavy components (trading stack)
try:  # pragma: no cover
    from .bot import TradingBot, OrderResult  # noqa: F401
    from .signer import OrderSigner, Order  # noqa: F401
    from .client import ApiClient, ClobClient, RelayerClient  # noqa: F401
    from .crypto import KeyManager  # noqa: F401
    from .config import Config, BuilderConfig  # noqa: F401
    from .websocket_client import MarketWebSocket, OrderbookManager, OrderbookSnapshot  # noqa: F401
    from .utils import (  # noqa: F401
        create_bot_from_env,
        validate_address,
        validate_private_key,
        format_price,
        format_usdc,
        truncate_address,
    )
except Exception:  # pragma: no cover
    # Keep package importable even if optional deps are missing.
    pass

__all__ = ["GammaClient"]

# Expose optional names when available
for _name in [
    "TradingBot",
    "OrderResult",
    "OrderSigner",
    "Order",
    "ApiClient",
    "ClobClient",
    "RelayerClient",
    "KeyManager",
    "Config",
    "BuilderConfig",
    "MarketWebSocket",
    "OrderbookManager",
    "OrderbookSnapshot",
    "create_bot_from_env",
    "validate_address",
    "validate_private_key",
    "format_price",
    "format_usdc",
    "truncate_address",
]:
    if _name in globals():
        __all__.append(_name)
