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
    )

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), nullable=False)  # e.g., PETR4, VALE3
    date = Column(DateTime, nullable=False)  # Candle close datetime
    open_price = Column(Numeric(10, 2), nullable=False)
    high_price = Column(Numeric(10, 2), nullable=False)
    low_price = Column(Numeric(10, 2), nullable=False)
    close_price = Column(Numeric(10, 2), nullable=False)
    volume = Column(Integer, nullable=False)

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
    metadata = Column(JSON)  # Additional context

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

    metadata = Column(JSON)  # Store strategy id, model version, etc.

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
