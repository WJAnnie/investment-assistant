import os

from .akshare import AkShareProvider
from .board_http import EastMoneyBoardClient
from .collector import MarketCollector
from .fund_nav import AkShareFundNavProvider
from .fundamentals import AkShareStockEvidenceProvider
from .global_markets import FredTreasuryProvider, YahooGlobalMarketProvider
from .hk_index import AkShareHKIndexProvider
from .industries import AkShareIndustryProvider
from .minute.eastmoney import create_minute_snapshot_loader
from .sina import SinaProvider
from .tencent import TencentHistoryProvider
from .tushare import TushareProvider
from app.portfolio.valuation import PortfolioValuationRouter


def create_fundamental_provider():
    """Create the stock fundamental/valuation provider without any request."""
    return AkShareStockEvidenceProvider()


def create_industry_provider(session=None):
    """Create the industry ranking provider without any request."""
    return AkShareIndustryProvider(
        client=EastMoneyBoardClient(session=session, timeout=10)
    )


def create_global_market_providers(session=None):
    """Create the overnight global-market providers without any request.

    The transport boundary in ``app.market.global_markets`` already forces
    ``trust_env`` off on whatever session it is handed and refuses redirects, so
    no session policy is duplicated or weakened here. Construction performs no
    I/O: the first request only happens when the workflow calls ``fetch``, which
    keeps this factory safe to call before any network-dependent step.
    """
    return (
        YahooGlobalMarketProvider(session=session),
        FredTreasuryProvider(session=session),
    )


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
