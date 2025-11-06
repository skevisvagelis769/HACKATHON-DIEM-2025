# backend/app/services.py
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, UserRole, MeterSample, Offer, OfferStatus, Trade
from app.schemas import MarketItemOut, ProviderOfferOut


# ============================================================================
# Provider Pricing Program (virtual, not stored in DB)
# ============================================================================

@dataclass(frozen=True)
class SurgeWindow:
    hour: int  # 0..23
    multiplier: float


# We choose a surge hour at process start (for hackathon simplicity).
# If surge disabled, this stays None.
_SURGE: Optional[SurgeWindow] = None
def _init_surge_once() -> None:
    global _SURGE
    if _SURGE is not None:
        return
    if not settings.PROVIDER_SURGE_ENABLED:
        _SURGE = None
        return
    start_allowed, end_allowed = settings.PROVIDER_SURGE_ALLOWED_HOURS
    # inclusive range (e.g., 17..21)
    allowed_hours = list(range(start_allowed, end_allowed + 1))
    if not allowed_hours:
        _SURGE = None
        return
    chosen = random.choice(allowed_hours)
    _SURGE = SurgeWindow(hour=chosen, multiplier=settings.PROVIDER_SURGE_MULTIPLIER)


def current_hour_24(ts: Optional[int] = None) -> int:
    """Return local hour (0..23). Uses system localtime (hackathon friendly)."""
    t = ts if ts is not None else int(time.time())
    return time.localtime(t).tm_hour


def provider_multiplier_now(ts: Optional[int] = None) -> float:
    """
    Compute the current multiplier from the daily schedule, possibly overridden
    by the 1-hour 'surge' window (rare peak).
    """
    _init_surge_once()
    hour = current_hour_24(ts)

    # base from schedule
    m = 1.0
    for start_h, end_h, mult in settings.PROVIDER_PRICE_SCHEDULE:
        # schedule tuples are [start_h, end_h) in many systems; we documented (start,end) humanly.
        # We'll treat them as half-open [start, end) for robust mapping except the last that may end at 24.
        if start_h <= hour < end_h:
            m = mult
            break

    # surge override if it matches exactly one hour
    if _SURGE and _SURGE.hour == hour:
        m = _SURGE.multiplier

    return m


def provider_price_eur_per_kwh_now(ts: Optional[int] = None) -> float:
    """Base * multiplier, rounded to 4 decimals for stability."""
    base = settings.PROVIDER_BASE_PRICE_EUR_PER_KWH
    mult = provider_multiplier_now(ts)
    return round(base * mult, 4)


def list_provider_market_items() -> List[MarketItemOut]:
    """
    Build in-memory provider 'offers' (virtual entries).
    These are not DB rows; they are computed each request.
    """
    price = provider_price_eur_per_kwh_now()
    mult = provider_multiplier_now()
    items: List[MarketItemOut] = []
    for name in settings.PROVIDER_NAMES:
        items.append(MarketItemOut(
            kind="provider",
            virtual_id=f"provider-{name}",
            provider_name=name,
            current_multiplier=mult,
            offer_id=None,
            seller_id=None,
            kwh_remaining=None,  # providers are effectively 'infinite' for MVP
            price_eur_per_kwh=price,
        ))
    return items


# ============================================================================
# Users, Seed, and Balances
# ============================================================================

def seed_providers_if_missing(db: Session) -> None:
    """
    Ensure provider 'users' exist (role='provider'). Idempotent.
    """
    existing = {u.email for u in db.scalars(
        select(User).where(User.role == UserRole.provider.value)
    ).all()}
    missing = [n for n in settings.PROVIDER_NAMES if n not in existing]
    if not missing:
        return
    for name in missing:
        u = User(email=name, wallet="", role=UserRole.provider.value, balance_eur=0.0)
        db.add(u)
    db.commit()


def create_user(db: Session, email: str, wallet: str, role: str) -> User:
    user = User(email=email, wallet=wallet, role=role, balance_eur=0.0)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def list_users(db: Session) -> List[User]:
    return db.scalars(select(User).order_by(User.id.asc())).all()


def fund_user(db: Session, user_id: int, amount: float) -> float:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    user = db.get(User, user_id)
    if not user:
        raise ValueError("User not found")
    user.balance_eur = round(user.balance_eur + amount, 4)
    db.commit()
    return user.balance_eur


# ============================================================================
# Meter Samples & Surplus
# ============================================================================

def record_meter_sample(db: Session, user_id: int, prod_kwh: float, cons_kwh: float, ts: int) -> int:
    if prod_kwh < 0 or cons_kwh < 0:
        raise ValueError("Energy values must be non-negative")
    if not db.get(User, user_id):
        raise ValueError("User not found")
    m = MeterSample(user_id=user_id, production_kwh=prod_kwh, consumption_kwh=cons_kwh, ts=ts)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m.id


def compute_latest_surplus(db: Session, user_id: int) -> float:
    """
    MVP interpretation: surplus = latest production - latest consumption.
    If no samples, surplus = 0.
    """
    row = db.execute(
        select(MeterSample.production_kwh, MeterSample.consumption_kwh)
        .where(MeterSample.user_id == user_id)
        .order_by(MeterSample.ts.desc())
        .limit(1)
    ).first()
    if not row:
        return 0.0
    prod, cons = row
    return round(max(0.0, prod - cons), 4)


def compute_surplus_last_hours(db: Session, user_id: int, hours: int = 12) -> float:
    """
    Stored surplus over the last {hours} hours:
    Sum of max(0, production - consumption) for each sample in the window.
    """
    now = int(time.time())
    since_ts = now - hours * 3600
    rows = list_meter_series(db, user_id=user_id, since_ts=since_ts)
    total = 0.0
    for (ts, prod, cons) in rows:
        total += max(0.0, prod - cons)
    return round(total, 4)


def get_user_status(db: Session, user_id: int) -> dict:
    """
    Return wallet balance and a realistic 'stored_surplus_kwh' = sum of positive
    (production - consumption) over the last 12 hours (windowed storage).
    """
    user = db.get(User, user_id)
    if not user:
        raise ValueError("User not found")

    stored_12h = compute_surplus_last_hours(db, user_id, hours=12)
    return {
        "user_id": user_id,
        "stored_surplus_kwh": stored_12h,
        "balance_eur": round(user.balance_eur, 4),
    }



# ============================================================================
# Household Offers (user-initiated only)
# ============================================================================

def create_offer(db: Session, seller_id: int, kwh: float, price_eur_per_kwh: float, ts: Optional[int] = None) -> Offer:
    seller = db.get(User, seller_id)
    if not seller:
        raise ValueError("Seller not found")
    if seller.role not in (UserRole.producer.value, UserRole.both.value):
        raise ValueError("Only producers or both can create offers")
    if kwh <= 0 or price_eur_per_kwh <= 0:
        raise ValueError("kWh and price must be positive")

    now = int(time.time()) if ts is None else ts
    offer = Offer(
        seller_id=seller_id,
        kwh_total=round(kwh, 4),
        kwh_remaining=round(kwh, 4),
        price_eur_per_kwh=round(price_eur_per_kwh, 4),
        status=OfferStatus.active.value,
        created_ts=now,
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def list_active_household_offers(db: Session, limit: int = 100) -> List[Offer]:
    return db.scalars(
        select(Offer)
        .where(Offer.status == OfferStatus.active.value, Offer.kwh_remaining > 0.0)
        .order_by(Offer.price_eur_per_kwh.asc(), Offer.created_ts.desc())
        .limit(limit)
    ).all()


# ============================================================================
# Unified Marketplace (providers + household offers)
# ============================================================================

def list_market_items(db: Session, limit_household: int = 100) -> List[MarketItemOut]:
    """
    Returns a mixed list:
      - Provider virtual items (computed price_now)
      - Household offers from DB
    Frontend renders one list using 'kind' field.
    """
    items: List[MarketItemOut] = []

    # Providers (virtual)
    if settings.PROVIDER_VIRTUAL_PRICING:
        items.extend(list_provider_market_items())

    # Household offers (DB)
    offers = list_active_household_offers(db, limit=limit_household)
    for o in offers:
        items.append(MarketItemOut(
            kind="household",
            virtual_id=None,
            provider_name=None,
            current_multiplier=None,
            offer_id=o.id,
            seller_id=o.seller_id,
            kwh_remaining=o.kwh_remaining,
            price_eur_per_kwh=o.price_eur_per_kwh,
        ))

    # Sort overall by price ascending so providers anchor the market visually
    items.sort(key=lambda it: it.price_eur_per_kwh)
    return items


# ============================================================================
# Accepting an Offer (buy) — atomic update with platform fee
# ============================================================================

def accept_offer(
    db: Session,
    buyer_id: int,
    offer_id: int,
    kwh: float,
    tx_hash: Optional[str] = None,
) -> Trade:
    """
    Atomic purchase:
      - validate offer and buyer
      - compute total and platform fee
      - move funds buyer->seller(net)
      - reduce kwh_remaining
      - close offer if fully filled
      - create Trade (with optional tx_hash for blockchain audit)
    """
    if kwh <= 0:
        raise ValueError("kWh must be positive")

    now = int(time.time())
    with db.begin():  # atomic block
        offer = db.get(Offer, offer_id)
        if not offer:
            raise ValueError("Offer not found")
        if offer.status != OfferStatus.active.value or offer.kwh_remaining <= 0:
            raise ValueError("Offer not available")

        if kwh > offer.kwh_remaining + 1e-9:
            raise ValueError("Requested kWh exceeds remaining")

        buyer = db.get(User, buyer_id)
        if not buyer:
            raise ValueError("Buyer not found")

        seller = db.get(User, offer.seller_id)
        if not seller:
            raise ValueError("Seller not found")

        total = round(kwh * offer.price_eur_per_kwh, 4)
        fee = round(total * settings.PLATFORM_FEE_RATE, 4)
        net = round(total - fee, 4)

        # MVP settlement using demo EUR balance (DB is SoT; chain is audit)
        if buyer.balance_eur + 1e-9 < total:
            raise ValueError("Insufficient buyer balance")

        buyer.balance_eur = round(buyer.balance_eur - total, 4)
        seller.balance_eur = round(seller.balance_eur + net, 4)

        # reduce remaining energy and possibly close offer
        new_rem = round(offer.kwh_remaining - kwh, 4)
        offer.kwh_remaining = new_rem
        if new_rem <= 1e-9:
            offer.kwh_remaining = 0.0
            offer.status = OfferStatus.closed.value

        trade = Trade(
            offer_id=offer.id,
            buyer_id=buyer.id,
            kwh=round(kwh, 4),
            total_eur=total,
            ts=now,
            tx_hash=tx_hash,
        )
        db.add(trade)

    # out-of-transaction refresh
    db.refresh(trade)
    return trade


def list_trades_for_user(db: Session, user_id: int, limit: int = 50) -> List[Trade]:
    return db.scalars(
        select(Trade)
        .where(Trade.buyer_id == user_id)
        .order_by(Trade.ts.desc())
        .limit(limit)
    ).all()
    

def list_meter_series(db: Session, user_id: int, since_ts: int) -> List[Tuple[int, float, float]]:
    """
    Return [(ts, production_kwh, consumption_kwh)] ascending by ts for user since 'since_ts'.
    """
    rows = db.execute(
        select(MeterSample.ts, MeterSample.production_kwh, MeterSample.consumption_kwh)
        .where(MeterSample.user_id == user_id, MeterSample.ts >= since_ts)
        .order_by(MeterSample.ts.asc())
    ).all()
    # rows: List[Row(tuple)], convert to plain tuples
    return [(int(ts), float(prod), float(cons)) for (ts, prod, cons) in rows]


def provider_series_past_hours(hours: int) -> List[Tuple[int, float]]:
    """
    Produce hourly provider prices for the past 'hours' hours (inclusive of current hour),
    using the existing schedule + optional surge window.
    Returns [(ts, price_eur_per_kwh)] with ts at the start of each hour.
    """
    now = int(time.time())
    # Align to current hour start
    hour_start = now - (now % 3600)
    out: List[Tuple[int, float]] = []
    for i in range(hours - 1, -1, -1):
        ts = hour_start - i * 3600
        price = provider_price_eur_per_kwh_now(ts)
        out.append((ts, float(price)))
    return out

