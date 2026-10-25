"""Shopping lists, preferences, offers, quotes and purchase requests."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jarvis.db.base import Base, UTCDateTime, created_at_col, updated_at_col, uuid_pk


class ShoppingList(Base):
    __tablename__ = "shopping_lists"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Основной")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = created_at_col()


class ShoppingItem(Base):
    __tablename__ = "shopping_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    list_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shopping_lists.id", ondelete="CASCADE"), index=True)
    requested_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    quantity_unit: Mapped[str | None] = mapped_column(String(20))
    base_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    dimension: Mapped[str | None] = mapped_column(String(10))
    preferred_brand: Mapped[str | None] = mapped_column(String(100))
    allowed_brands: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    excluded_brands: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    required_attributes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    substitution_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="ask")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")  # open|bought|removed
    # Where the item came from when Jarvis moved it («todoist:<task id>:<n>»): moving twice adds nothing.
    source_ref: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        Index("uq_shopping_items_source_ref", "source_ref", unique=True, postgresql_where="source_ref IS NOT NULL"),
    )


class ShoppingPreference(Base):
    """Explicit user preferences. Never inferred silently from purchases."""

    __tablename__ = "shopping_preferences"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ShoppingStore(Base):
    __tablename__ = "shopping_stores"

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    store_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GEL")
    delivery_fee_minor: Mapped[int | None] = mapped_column(BigInteger)  # NULL = unknown
    service_fee_minor: Mapped[int | None] = mapped_column(BigInteger)
    small_order_threshold_minor: Mapped[int | None] = mapped_column(BigInteger)
    small_order_fee_minor: Mapped[int | None] = mapped_column(BigInteger)
    fees_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (UniqueConstraint("provider", "store_key"),)


class ShoppingOffer(Base):
    __tablename__ = "shopping_offers"

    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shopping_stores.id", ondelete="CASCADE"), index=True)
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str | None] = mapped_column(String(100))
    package_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    package_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    promo_price_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GEL")
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attributes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # manual_import|user_assisted|official_api
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (Index("ix_shopping_offers_name", "product_name"),)


class ShoppingQuote(Base):
    __tablename__ = "shopping_quotes"

    id: Mapped[uuid.UUID] = uuid_pk()
    list_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shopping_lists.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    best_total_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GEL")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = created_at_col()


class PurchaseRequest(Base):
    __tablename__ = "purchase_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shopping_quotes.id"), index=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="awaiting_approval")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
