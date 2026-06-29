"""Product registry CRUD (private dashboard API, TECH_SPEC §4 / S0.3).

Register a product via the onboarding form; persistence creates an isolated workspace
dir + empty credentials vault and starts the product in the `draft` lifecycle state.
No auth — the private surface is firewalled at deploy time (NFR-1).
"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from app.db import get_session
from app.models.product import MonetizationModel, Product
from app.workspace import create_workspace, remove_workspace

router = APIRouter(prefix="/products", tags=["products"])

SessionDep = Annotated[Session, Depends(get_session)]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "product"


class ProductCreate(BaseModel):
    name: str = Field(min_length=1)
    repo_url: str | None = None
    repo_local_path: str | None = None
    description: str | None = None
    monetization_model: MonetizationModel = MonetizationModel.CC_SUB
    marketing_domain: str | None = None
    token_budget_cents_month: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()


# lifecycle_state is intentionally NOT editable here — transitions go through the
# state machine via dedicated actions in later phases (S1.4 approve, S3.2 go-live),
# not a raw field set. This PATCH edits product *config* only.
class ProductUpdate(BaseModel):
    name: str | None = None
    repo_url: str | None = None
    repo_local_path: str | None = None
    description: str | None = None
    monetization_model: MonetizationModel | None = None
    marketing_domain: str | None = None
    token_budget_cents_month: int | None = Field(default=None, ge=0)
    # Pricing is recommended by S1.3 and editable by the owner here. Interval stays a free string to
    # match the column; the recommender constrains its own output to month/year.
    price_amount_cents: int | None = Field(default=None, gt=0)
    price_interval: str | None = None
    # Brand kit (S1.2) is owner-editable here (S1.4 review). Stored as a JSON string; guard
    # well-formedness so an edit can't corrupt the brand the crank reads.
    brand_json: str | None = None

    @field_validator("brand_json")
    @classmethod
    def _well_formed_brand(cls, v: str | None) -> str | None:
        if v is not None:
            json.loads(v)  # raises → 422 via pydantic
        return v


def _unique_slug(session: Session, name: str) -> str:
    """Slug from name; append -2, -3, … if taken (concurrent-safe enough for one operator)."""
    base = slugify(name)
    slug, n = base, 2
    while session.exec(select(Product).where(Product.slug == slug)).first() is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


@router.post("", status_code=201)
def create_product(payload: ProductCreate, session: SessionDep) -> Product:
    product = Product(slug=_unique_slug(session, payload.name), **payload.model_dump())
    # Scaffold the workspace BEFORE persisting the row: if it fails the operator gets a
    # 500 with nothing in the DB to retry around. create_workspace is idempotent, so a
    # later commit failure leaves only a harmless empty dir that the retry reuses.
    create_workspace(product.slug)
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@router.get("")
def list_products(session: SessionDep) -> list[Product]:
    return list(session.exec(select(Product).order_by(Product.created_at.desc())).all())


@router.get("/{product_id}")
def get_product(product_id: int, session: SessionDep) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


@router.patch("/{product_id}")
def update_product(product_id: int, payload: ProductUpdate, session: SessionDep) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    product.updated_at = datetime.now(UTC)
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@router.delete("/{product_id}", status_code=204)
def delete_product(product_id: int, session: SessionDep) -> None:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    slug = product.slug
    session.delete(product)
    session.commit()
    remove_workspace(slug)
