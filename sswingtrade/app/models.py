"""
SQLAlchemy ORM Models for SSWingTrade
Domain entities: MarketCandle, Account, Position, Order, etc.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    Column,
    String,
    Float,
    Integer,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    CheckConstraint,
    UniqueConstraint,
    Numeric,
    Boolean,
    Text,
    JSON,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


def _utcnow() -> datetime:
    """Module-level helper (class bodies may shadow the datetime name)."""
    return datetime.utcnow()


class OrderStatus(PyEnum):
    """Order status enumeration."""
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OperationType(PyEnum):
    """Trading operation type."""
    BUY = "buy"
    SELL = "sell"


class PositionStatus(PyEnum):
    """Position status enumeration."""
    OPEN = "open"
    CLOSED = "closed"
    PARTIAL = "partial"


# ============================================================================
# Market Data Models
# ============================================================================

class MarketCandle(Base):
    """
    OHLCV (Open, High, Low, Close, Volume) candles for technical analysis.
    Stores end-of-day data for swing trade analysis.

    Partitioned by date for TimescaleDB optimization in FASE 3+
    """
    __tablename__ = "market_candles"
    __table_args__ = (
        Index("ix_candle_ticker_date", "ticker", "date"),
        Index("ix_candle_date", "date"),
        UniqueConstraint("ticker", "date", name="uq_candle_ticker_date"),
    )

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), nullable=False)  # e.g., PETR4, VALE3
    date = Column(DateTime, nullable=False)  # Candle close datetime
    open_price = Column(Numeric(10, 2), nullable=False)
    high_price = Column(Numeric(10, 2), nullable=False)
    low_price = Column(Numeric(10, 2), nullable=False)
    close_price = Column(Numeric(10, 2), nullable=False)
    volume = Column(BigInteger, nullable=False)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TickerInfo(Base):
    """Ticker metadata and configuration."""
    __tablename__ = "ticker_info"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), unique=True, nullable=False)
    name = Column(String(255), nullable=True)
    sector = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    min_price = Column(Numeric(10, 2))  # Minimum price for position
    max_price = Column(Numeric(10, 2))  # Maximum price for position

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============================================================================
# Capital & Account Models
# ============================================================================

class Account(Base):
    """
    Trading account with capital tracking and compound interest calculation.
    Implements the R$ 500/week, R$ 100/day allocation model.
    """
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(100), nullable=False, unique=True)

    # Capital tracking
    initial_capital = Column(Numeric(15, 2), nullable=False)  # BRL
    current_balance = Column(Numeric(15, 2), nullable=False)  # Total capital
    available_balance = Column(Numeric(15, 2), nullable=False)  # Available for trading
    invested_capital = Column(Numeric(15, 2), default=0)  # Currently in positions

    # Weekly allocation
    weekly_base_allocation = Column(Numeric(15, 2), default=500.00)  # R$ 500/week
    daily_limit_per_operation = Column(Numeric(15, 2), default=100.00)  # R$ 100/day

    # Compound interest tracking
    total_gains = Column(Numeric(15, 2), default=0)
    total_losses = Column(Numeric(15, 2), default=0)
    monthly_deposits = Column(Numeric(15, 2), default=0)

    # Risk control
    max_daily_loss_percent = Column(Float, default=1.5)  # 1.5% circuit breaker
    daily_loss_triggered = Column(Boolean, default=False)  # Circuit breaker status

    # Metadata
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    positions = relationship("Position", back_populates="account")
    orders = relationship("Order", back_populates="account")


class CapitalHistory(Base):
    """
    Audit trail for all capital movements and reinvestment calculations.
    Used for transparency and debugging capital compounding.
    """
    __tablename__ = "capital_history"
    __table_args__ = (
        Index("ix_capital_history_account_date", "account_id", "date"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)

    event_type = Column(String(50), nullable=False)  # deposit, trade_pnl, reinvestment, monthly_deposit
    amount = Column(Numeric(15, 2), nullable=False)
    balance_before = Column(Numeric(15, 2), nullable=False)
    balance_after = Column(Numeric(15, 2), nullable=False)

    description = Column(Text)
    extra_data = Column("metadata", JSON)  # Additional context (attr renamed: "metadata" is reserved)

    date = Column(DateTime, default=datetime.utcnow)


# ============================================================================
# Position & Order Models
# ============================================================================

class Position(Base):
    """
    Represents an open or closed trading position.
    Tracks entry, exit, P&L, and decision scores.
    """
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_position_account_ticker", "account_id", "ticker"),
        Index("ix_position_status", "status"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    ticker = Column(String(10), nullable=False)

    # Position details
    operation_type = Column(Enum(OperationType), nullable=False)  # buy/sell
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Numeric(10, 2), nullable=False)
    entry_date = Column(DateTime, nullable=False)

    # Exit details
    exit_price = Column(Numeric(10, 2), nullable=True)
    exit_date = Column(DateTime, nullable=True)

    # Risk management
    stop_loss_price = Column(Numeric(10, 2), nullable=True)
    take_profit_price = Column(Numeric(10, 2), nullable=True)
    atr_value = Column(Numeric(10, 2), nullable=True)  # ATR at entry time

    # P&L
    gross_pnl = Column(Numeric(15, 2))  # Gross profit/loss
    fees = Column(Numeric(10, 2), default=0)  # Brokerage + taxes
    net_pnl = Column(Numeric(15, 2))  # Net profit/loss
    pnl_percent = Column(Float)  # % return

    # Decision scores at entry
    technical_score = Column(Float)  # 0-100
    news_score = Column(Float)  # 0-100
    ml_probability = Column(Float)  # 0-1
    composite_score = Column(Float)  # 0-100 (weighted average)

    # Status
    status = Column(Enum(PositionStatus), default=PositionStatus.OPEN)

    extra_data = Column("metadata", JSON)  # Strategy id, model version, etc.

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = relationship("Account", back_populates="positions")
    orders = relationship("Order", back_populates="position")


class Order(Base):
    """
    Represents individual buy/sell orders.
    Can have multiple orders per position (scale in/out).
    """
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_order_position", "position_id"),
        Index("ix_order_date", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)

    # Order details
    ticker = Column(String(10), nullable=False)
    operation_type = Column(Enum(OperationType), nullable=False)
    quantity = Column(Integer, nullable=False)
    target_price = Column(Numeric(10, 2), nullable=False)

    # Execution
    executed_price = Column(Numeric(10, 2), nullable=True)
    executed_quantity = Column(Integer, nullable=True)

    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    execution_date = Column(DateTime, nullable=True)

    # Costs
    fees = Column(Numeric(10, 2), default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = relationship("Account", back_populates="orders")
    position = relationship("Position", back_populates="orders")


# ============================================================================
# Intraday Candles & Collection Log (FASE 2)
# ============================================================================

class IntradayCandle(Base):
    """
    Intraday OHLCV candles (hourly) for more granular analysis.
    Complements daily candles for entry/exit timing in swing trades.
    """
    __tablename__ = "intraday_candles"
    __table_args__ = (
        Index("ix_intraday_ticker_datetime", "ticker", "datetime"),
        Index("ix_intraday_datetime", "datetime"),
        UniqueConstraint("ticker", "datetime", "interval", name="uq_intraday_ticker_dt_interval"),
    )

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), nullable=False)
    datetime = Column(DateTime, nullable=False)
    interval = Column(String(10), nullable=False, default="1h")  # 1h, 15m, 5m
    open_price = Column(Numeric(10, 2), nullable=False)
    high_price = Column(Numeric(10, 2), nullable=False)
    low_price = Column(Numeric(10, 2), nullable=False)
    close_price = Column(Numeric(10, 2), nullable=False)
    volume = Column(BigInteger, nullable=False)

    created_at = Column(DateTime, default=_utcnow, nullable=False)


class CollectionLog(Base):
    """
    Logs each data collection run for observability and debugging.
    Tracks success/failure, rows collected, and timing.
    """
    __tablename__ = "collection_logs"
    __table_args__ = (
        Index("ix_collection_log_date", "started_at"),
    )

    id = Column(Integer, primary_key=True)
    collection_type = Column(String(50), nullable=False)  # daily, intraday, manual
    tickers_requested = Column(JSON, nullable=False)  # List of tickers
    tickers_succeeded = Column(JSON)  # Tickers that succeeded
    tickers_failed = Column(JSON)  # Tickers that failed
    total_rows_inserted = Column(Integer, default=0)
    total_rows_updated = Column(Integer, default=0)
    status = Column(String(20), nullable=False, default="running")  # running, completed, failed
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)

    metadata_extra = Column(JSON)  # Additional context


class TechnicalIndicator(Base):
    """Daily technical indicators computed locally from market_candles (FASE 3)."""
    __tablename__ = "technical_indicators"
    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_indicator_ticker_date"),
        Index("ix_indicator_ticker_date", "ticker", "date"),
    )

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), nullable=False)
    date = Column(DateTime, nullable=False)

    close = Column(Float)
    sma_5 = Column(Float)
    sma_10 = Column(Float)
    sma_20 = Column(Float)
    sma_50 = Column(Float)
    ema_9 = Column(Float)
    ema_21 = Column(Float)
    rsi_14 = Column(Float)
    macd = Column(Float)
    macd_signal = Column(Float)
    macd_hist = Column(Float)
    atr_14 = Column(Float)
    bb_upper = Column(Float)
    bb_middle = Column(Float)
    bb_lower = Column(Float)
    bb_pctb = Column(Float)
    vwap_20 = Column(Float)
    volume_sma_20 = Column(Float)
    volume_change = Column(Float)
    volume_ratio = Column(Float)
    technical_score = Column(Float)  # 0-100

    created_at = Column(DateTime, default=_utcnow, nullable=False)


# ============================================================================
# Event & Analysis Models (FASE 6+)
# ============================================================================

class NewsEvent(Base):
    """
    Market news and corporate events.
    Analyzed by Ollama agents for sentiment and impact.
    """
    __tablename__ = "news_events"
    __table_args__ = (
        Index("ix_news_ticker_date", "ticker", "event_date"),
    )

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), nullable=False)

    title = Column(String(500), nullable=False)
    content = Column(Text)
    source = Column(String(100))
    event_date = Column(DateTime, nullable=False)

    # LLM Analysis (FASE 6+)
    sentiment_score = Column(Float)  # -1.0 to +1.0
    impact_score = Column(Float)  # 0-10
    confidence = Column(Float)  # 0-1
    horizon = Column(String(50))  # short_term_1_5_days, medium, long
    event_type = Column(String(100))  # earnings, dividend, merger, etc.

    analysis_metadata = Column(JSON)  # Full LLM response
    content_hash = Column(String(64), unique=True)  # Prevent duplicate processing

    created_at = Column(DateTime, default=datetime.utcnow)


# ============================================================================
# Backtesting Models
# ============================================================================

class BacktestRun(Base):
    """
    Backtesting execution records for strategy validation.
    Used to test strategies before paper trading.
    """
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True)

    name = Column(String(255), nullable=False)
    strategy_id = Column(String(100), nullable=False)

    # Date range
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)

    # Results
    total_trades = Column(Integer)
    winning_trades = Column(Integer)
    losing_trades = Column(Integer)
    win_rate = Column(Float)  # %

    initial_capital = Column(Numeric(15, 2))
    final_capital = Column(Numeric(15, 2))
    total_return = Column(Float)  # %
    max_drawdown = Column(Float)  # %
    sharpe_ratio = Column(Float)

    parameters = Column(JSON)  # Strategy parameters used
    results = Column(JSON)  # Detailed trade-by-trade results

    created_at = Column(DateTime, default=datetime.utcnow)


if __name__ == "__main__":
    print("SQLAlchemy ORM models defined successfully")
    print(f"Tables: {Base.metadata.tables.keys()}")
