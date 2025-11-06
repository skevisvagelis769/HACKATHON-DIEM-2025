# backend/app/main.py
from __future__ import annotations

import time
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.config import settings
from app.db import Base, engine, get_db
from app import services
from app.schemas import (
    HealthOut,
    UserCreate, UserOut,
    StatusOut,
    MeterSampleIn,
    OfferCreate, OfferOut,
    MarketItemOut,
    AcceptIn, TradeOut,
    ChainOfferConfirmIn, ChainTradeConfirmIn,
)
from app.models import MeterSample, Trade  # used by /meter/last and chain confirm

from app.background import start_simulator, stop_simulator

# -----------------------------------------------------------------------------
# App + CORS
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Smart Energy Marketplace (Hackathon MVP)",
    version="0.1.0",
    description=(
        "Household-to-household energy trading, with provider virtual pricing.\n"
        "Providers (ΔΕΗ, ΗΡΩΝ) are returned as virtual market items with a time-of-day multiplier schedule.\n"
        "No auto-offers: only user-initiated offers are stored in DB."
    ),
)

# In dev, allow everything for speed; narrow if you prefer.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # e.g., ["http://localhost:5173", "http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Startup / Shutdown
# -----------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    # Create tables
    Base.metadata.create_all(bind=engine)
    # Ensure provider users exist (ΔΕΗ, ΗΡΩΝ, etc.)
    db = next(get_db())
    try:
        services.seed_providers_if_missing(db)
    finally:
        db.close()

    start_simulator()
    # (Optional) If you later add background simulation for meter samples or surge scheduler,
    # you can start it here. For now, mentor requested NO auto-offers.


@app.on_event("shutdown")
def on_shutdown():
    stop_simulator()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _bad_request(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------
@app.get("/health", response_model=HealthOut, tags=["system"])
def health() -> HealthOut:
    return HealthOut(ok=True, ts=int(time.time()))


# -----------------------------------------------------------------------------
# Users
# -----------------------------------------------------------------------------
@app.get("/users", response_model=List[UserOut], tags=["users"])
def list_users(db: Session = Depends(get_db)) -> List[UserOut]:
    return services.list_users(db)


@app.post("/register", response_model=UserOut, tags=["users"])
def register(payload: UserCreate, db: Session = Depends(get_db)) -> UserOut:
    try:
        return services.create_user(db, email=payload.email, wallet=payload.wallet, role=payload.role)
    except ValueError as e:
        raise _bad_request(str(e))


@app.post("/users/{user_id}/fund/{amount}", response_model=StatusOut, tags=["users"])
def fund_user(user_id: int, amount: float, db: Session = Depends(get_db)) -> StatusOut:
    try:
        new_balance = services.fund_user(db, user_id, amount)
        status = services.get_user_status(db, user_id)
        status["balance_eur"] = new_balance
        return StatusOut(**status)
    except ValueError as e:
        raise _bad_request(str(e))


# -----------------------------------------------------------------------------
# Status / Dashboard
# -----------------------------------------------------------------------------
@app.get("/status/{user_id}", response_model=StatusOut, tags=["status"])
def status(user_id: int, db: Session = Depends(get_db)) -> StatusOut:
    try:
        data = services.get_user_status(db, user_id)
        return StatusOut(**data)
    except ValueError as e:
        raise _bad_request(str(e))


# -----------------------------------------------------------------------------
# Meter Samples (manual ingestion; simulator does NOT create auto-offers)
# -----------------------------------------------------------------------------
@app.post("/meter_sample", tags=["meter"])
def post_meter_sample(payload: MeterSampleIn, db: Session = Depends(get_db)) -> dict:
    """
    Records a new meter sample via services; services is expected to persist the sample
    and update derived status as your design dictates (e.g., surplus computation).
    """
    try:
        mid = services.record_meter_sample(
            db=db,
            user_id=payload.user_id,
            prod_kwh=payload.production_kwh,
            cons_kwh=payload.consumption_kwh,
            ts=payload.ts,  # may be None; services can assign 'now' if missing
        )
        return {"id": mid}
    except ValueError as e:
        raise _bad_request(str(e))


@app.get("/meter/last", tags=["meter"])
def meter_last(user_id: int = Query(..., ge=1), db: Session = Depends(get_db)) -> dict:
    """
    Returns the latest production/consumption sample for a user.
    Used by the frontend to draw Usage/Production charts.
    """
    row = db.execute(
        select(MeterSample.production_kwh, MeterSample.consumption_kwh, MeterSample.ts)
        .where(MeterSample.user_id == user_id)
        .order_by(MeterSample.ts.desc())
        .limit(1)
    ).first()
    if not row:
        return {"user_id": user_id, "production_kwh": 0.0, "consumption_kwh": 0.0, "ts": 0}
    prod, cons, ts = row
    return {"user_id": user_id, "production_kwh": float(prod), "consumption_kwh": float(cons), "ts": int(ts)}

@app.get("/meter/series", tags=["meter"])
def meter_series(
    user_id: int = Query(..., ge=1),
    hours: int = Query(12, ge=1, le=72),
    db: Session = Depends(get_db),
):
    """
    Return last {hours} hours of meter samples for user.
    Response: { user_id, hours, samples: [{ts, production_kwh, consumption_kwh, surplus_kwh}] }
    """
    now = int(time.time())
    since_ts = now - hours * 3600
    rows = services.list_meter_series(db, user_id=user_id, since_ts=since_ts)
    samples = [
        {
            "ts": ts,
            "production_kwh": prod,
            "consumption_kwh": cons,
            "surplus_kwh": max(0.0, round(prod - cons, 4)),
        }
        for (ts, prod, cons) in rows
    ]
    return {"user_id": user_id, "hours": hours, "samples": samples}


@app.get("/provider/series", tags=["market"])
def provider_series(
    hours: int = Query(12, ge=1, le=72),
):
    """
    Return hourly provider prices for the past {hours} hours using the schedule/surge.
    Response: { hours, points: [{ts, price_eur_per_kwh}] }
    """
    points = [
        {"ts": ts, "price_eur_per_kwh": price}
        for (ts, price) in services.provider_series_past_hours(hours)
    ]
    return {"hours": hours, "points": points}


# -----------------------------------------------------------------------------
# Marketplace
# -----------------------------------------------------------------------------
@app.get("/offers", response_model=List[MarketItemOut], tags=["market"])
def list_market(
    limit_household: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> List[MarketItemOut]:
    """
    Unified marketplace list:
      - Provider virtual items (ΔΕΗ, ΗΡΩΝ) with dynamic price (base * schedule multiplier, surge hour possible)
      - Household offers from DB (user-initiated only)
    Sorted overall by price ascending.
    """
    return services.list_market_items(db, limit_household=limit_household)


@app.post("/offers", response_model=OfferOut, tags=["market"])
def create_offer(payload: OfferCreate, db: Session = Depends(get_db)) -> OfferOut:
    """
    Create a household offer (no auto-offers allowed).
    Only users with role 'producer' or 'both' can create offers.
    """
    try:
        offer = services.create_offer(
            db=db,
            seller_id=payload.seller_id,
            kwh=payload.kwh,
            price_eur_per_kwh=payload.price_eur_per_kwh,
        )
        return offer
    except ValueError as e:
        raise _bad_request(str(e))


# -----------------------------------------------------------------------------
# Accept Household Offer (Buy)
# -----------------------------------------------------------------------------
@app.post("/accept", response_model=TradeOut, tags=["trades"])
def accept(payload: AcceptIn, db: Session = Depends(get_db)) -> TradeOut:
    """
    Accept a household offer.
    If settings.REQUIRE_TX_HASH_ON_ACCEPT is True, tx_hash must be provided (MetaMask).
    """
    if settings.REQUIRE_TX_HASH_ON_ACCEPT and not payload.tx_hash:
        raise _bad_request("tx_hash is required by server configuration")

    try:
        t = services.accept_offer(
            db=db,
            buyer_id=payload.buyer_id,
            offer_id=payload.offer_id,
            kwh=payload.kwh,
            tx_hash=payload.tx_hash,
        )
        return t
    except ValueError as e:
        raise _bad_request(str(e))


# -----------------------------------------------------------------------------
# Trades
# -----------------------------------------------------------------------------
@app.get("/trades", response_model=List[TradeOut], tags=["trades"])
def list_trades(user_id: int = Query(...), db: Session = Depends(get_db)) -> List[TradeOut]:
    try:
        return services.list_trades_for_user(db, user_id=user_id)
    except ValueError as e:
        raise _bad_request(str(e))


# -----------------------------------------------------------------------------
# Blockchain confirmations (optional; used when you wire MetaMask later)
# -----------------------------------------------------------------------------
@app.post("/chain/offer-confirm", tags=["chain"])
def chain_offer_confirm(payload: ChainOfferConfirmIn) -> dict:
    """
    Optional endpoint: if you decide to mirror offer creation on-chain and want to attach tx_hash.
    For MVP we don't store provider entries on-chain; household offers are DB-native.
    """
    return {"ok": True, "offer_id": payload.offer_id, "tx_hash": payload.tx_hash}


@app.post("/chain/trade-confirm", tags=["chain"])
def chain_trade_confirm(payload: ChainTradeConfirmIn, db: Session = Depends(get_db)) -> dict:
    """
    Optional endpoint: attach a blockchain tx_hash to an existing trade after a MetaMask tx.
    """
    tr = db.get(Trade, payload.trade_id)
    if not tr:
        raise _bad_request("Trade not found")
    tr.tx_hash = payload.tx_hash
    db.commit()
    return {"ok": True, "trade_id": payload.trade_id, "tx_hash": payload.tx_hash}
    
    # ---[ 12h time-series endpoints ]--------------------------------------------

@app.get("/meter/series", tags=["meter"])
def meter_series(
    user_id: int = Query(..., ge=1),
    hours: int = Query(12, ge=1, le=72),
    db: Session = Depends(get_db),
):
    """
    Return last {hours} hours of meter samples for user.
    Response: { user_id, hours, samples: [{ts, production_kwh, consumption_kwh, surplus_kwh}] }
    """
    now = int(time.time())
    since_ts = now - hours * 3600
    rows = services.list_meter_series(db, user_id=user_id, since_ts=since_ts)
    samples = [
        {
            "ts": ts,
            "production_kwh": prod,
            "consumption_kwh": cons,
            "surplus_kwh": max(0.0, round(prod - cons, 4)),
        }
        for (ts, prod, cons) in rows
    ]
    return {"user_id": user_id, "hours": hours, "samples": samples}


@app.get("/provider/series", tags=["market"])
def provider_series(
    hours: int = Query(12, ge=1, le=72),
):
    """
    Return hourly provider prices for the past {hours} hours using the schedule/surge.
    Response: { hours, points: [{ts, price_eur_per_kwh}] }
    """
    points = [
        {"ts": ts, "price_eur_per_kwh": price}
        for (ts, price) in services.provider_series_past_hours(hours)
    ]
    return {"hours": hours, "points": points}

