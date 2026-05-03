from dataclasses import dataclass
from typing import Literal, Optional

Availability = Literal["available", "kindle_unavailable", "page_404"]


@dataclass
class ScrapedItem:
    asin: str
    title: str
    author: Optional[str]
    product_url: str
    current_price_cents: Optional[int]
    list_price_cents: Optional[int]
    availability: Availability


@dataclass
class Wishlist:
    id: int
    url: str
    label: Optional[str]
    added_at: str


@dataclass
class BookRow:
    asin: str
    title: str
    author: Optional[str]
    product_url: str
    current_price_cents: Optional[int]
    list_price_cents: Optional[int]
    prev_price_cents: Optional[int]
    availability: Availability
    observed_at: str
    drop_dollar: Optional[float] = None
    drop_pct: Optional[float] = None
