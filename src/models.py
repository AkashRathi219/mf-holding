from __future__ import annotations

from pydantic import BaseModel


class Holding(BaseModel):
    stock_name: str = ""
    isin: str = ""
    quantity: str = ""
    market_value: str = ""
    percent_of_nav: str = ""
    sector: str = ""


class DebtHolding(BaseModel):
    instrument: str = ""
    isin: str = ""
    coupon: str = ""
    maturity: str = ""
    rating: str = ""
    market_value: str = ""
    percent_of_nav: str = ""


class SectorAllocation(BaseModel):
    sector: str = ""
    percent: str = ""


class FundSummary(BaseModel):
    fund_name: str = ""
    amc_name: str = ""
    nav: str = ""
    aum_cr: str = ""
    date: str = ""
    expense_ratio: str = ""
    equity_count: int = 0
    debt_count: int = 0
    top_holding_name: str = ""
    top_holding_weight: str = ""
    cash_percent: str = ""


class PortfolioData(BaseModel):
    source_file: str = ""
    metadata: dict = {}
    equity_holdings: list[dict] = []
    debt_holdings: list[dict] = []
    sector_allocation: list[dict] = []
    top_holdings: list[dict] = []
    cash_allocation: dict | None = None
