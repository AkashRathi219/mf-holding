from .base import AMCAdapter, PDFLink
from .generic import GenericAdapter
from .playwright_adapter import PlaywrightAdapter
from .icici import ICICIAdapter
from .navi import NaviAdapter
from .pgim import PGIMAdapter
from .choice import ChoiceAdapter
from .jio import JioBlackRockAdapter
from .union import UnionAdapter
from .taurus import TaurusAdapter
from .bandhan import BandhanAdapter
from .nj import NJAdapter
from .hdfc import HDFCAdapter
from .kotak import KotakAdapter
from .iti import ITIAdapter
from .axis import AxisAdapter
from .uti import UTIAdapter
from .whiteoak import WhiteOakAdapter
from .edelweiss import EdelweissAdapter
from .mahindra import MahindraAdapter
from .jm import JMFinancialAdapter
from .wealthcompany import WealthCompanyAdapter
from .sundaram import SundaramAdapter

ADAPTER_REGISTRY: dict[str, type[AMCAdapter]] = {
    "ICICI Prudential Mutual Fund": ICICIAdapter,
    "Navi Mutual Fund": NaviAdapter,
    "PGIM India Mutual Fund": PGIMAdapter,
    "Choice Mutual Fund": ChoiceAdapter,
    "Jio BlackRock Mutual Fund": JioBlackRockAdapter,
    "Union Mutual Fund": UnionAdapter,
    "Taurus Mutual Fund": TaurusAdapter,
    "Bandhan Mutual Fund": BandhanAdapter,
    "NJ Mutual Fund": NJAdapter,
    "HDFC Mutual Fund": HDFCAdapter,
    "Kotak Mahindra Mutual Fund": KotakAdapter,
    "ITI Mutual Fund": ITIAdapter,
    "Axis Mutual Fund": AxisAdapter,
    "UTI Mutual Fund": UTIAdapter,
    "WhiteOak Capital Mutual Fund": WhiteOakAdapter,
    "Edelweiss Mutual Fund": EdelweissAdapter,
    "Mahindra Manulife Mutual Fund": MahindraAdapter,
    "JM Financial Mutual Fund": JMFinancialAdapter,
    "The Wealth Company Mutual Fund": WealthCompanyAdapter,
    "Sundaram Mutual Fund": SundaramAdapter,
}


def register_adapter(amc_name: str):
    def decorator(cls):
        ADAPTER_REGISTRY[amc_name] = cls
        return cls
    return decorator


class HybridAdapter(AMCAdapter):
    """Try fast HTTP scraping first, fall back to headless browser for JS sites.

    Falls back to Playwright whenever the plain-HTTP scrape yields no links that
    carry a detectable month/year (i.e. no dated portfolio documents).
    """

    def __init__(self):
        self.generic = GenericAdapter()
        self.playwright = PlaywrightAdapter()

    async def discover_documents_all(
        self,
        portfolio_url: str,
        factsheet_url: str,
    ) -> list[PDFLink]:
        results = await self.generic.discover_documents_all(
            portfolio_url, factsheet_url
        )
        dated = [link for link in results if link.month is not None]
        if not dated:
            pw = await self.playwright.discover_documents_all(
                portfolio_url, factsheet_url
            )
            results.extend(pw)
        return results


def get_adapter(amc_name: str) -> AMCAdapter:
    cls = ADAPTER_REGISTRY.get(amc_name)
    if cls:
        return cls()
    return HybridAdapter()
