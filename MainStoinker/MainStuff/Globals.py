from dataclasses import dataclass
import datetime
from dateutil.relativedelta import relativedelta
import logging


@dataclass(frozen=True)
class Config:
    """Application configuration settings (immutable)."""

    # TWS Connection
    TWS_HOST: str = '127.0.0.1'
    TWS_PORT: int = 7497
    TWS_CLIENT_ID: int = 123

    # API Timeouts (seconds)
    TIMEOUT_ORDER_ID: int = 5
    TIMEOUT_POSITIONS: int = 2
    TIMEOUT_ORDERS: int = 2
    TIMEOUT_COMPLETED_ORDERS: int = 2
    TIMEOUT_EXECUTIONS: int = 15

    # Mode Flags
    LiveData: bool = False
    LiveTrading: bool = False
    Debug: bool = False
    offline: bool = False
    collectofflinedata: bool = False

    # Frontend
    FrontEndDisplay: bool = True
    FrontEndPort: str = '192.168.0.69:3000'
    intraMinuteDisplay: bool = True

    # Backtesting
    Duration: int = 1  # days
    TimeDelayPerPoint: float = 0.5  # seconds

    # Logging
    log_level: int = logging.DEBUG
    loggers: tuple = ("dum_dum",)


# Module-level config instance
config = Config()

# Computed values (depend on config)
StartDate = datetime.datetime.now() - relativedelta(month=0, weeks=0, day=config.Duration)

# Runtime state (mutable, populated during execution)
tickers: dict = {}
algos: list = []
updating: int = 1
tickerIndex: int = 0
