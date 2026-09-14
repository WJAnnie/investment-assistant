import os

from .akshare import AkShareProvider
from .collector import MarketCollector
from .fund_nav import AkShareFundNavProvider
from .hk_index import AkShareHKIndexProvider
from .sina import SinaProvider
from .tencent import TencentHistoryProvider
from .tushare import TushareProvider
from app.portfolio.valuation import PortfolioValuationRouter


def create_default_collector(cache=None, cache_expire=300):
    """Create the configured research collector without making a network request."""
    token = os.getenv("TUSHARE_TOKEN")
    fallback = [SinaProvider(), TencentHistoryProvider()]
    if token:
        fallback.append(TushareProvider(token=token))
    return MarketCollector(
        primary=AkShareProvider(),
        fallback=fallback,
        cache=cache,
        cache_expire=cache_expire,
    )


def create_portfolio_valuation_router(cache=None, cache_expire=300):
    """Create portfolio valuation dependencies without making network requests."""
    return PortfolioValuationRouter(
        exchange_collector=create_default_collector(cache=cache, cache_expire=cache_expire),
        nav_provider=AkShareFundNavProvider(),
        hk_provider=AkShareHKIndexProvider(),
    )
