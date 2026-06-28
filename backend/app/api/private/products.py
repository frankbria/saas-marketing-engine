"""Product registry CRUD (private dashboard API, TECH_SPEC §4 / S0.3).

Register a product via the onboarding form; persistence creates an isolated workspace
dir + empty credentials vault and starts the product in the `draft` lifecycle state.
No auth — the private surface is firewalled at deploy time (NFR-1).
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
    name: str
    repo_url: str | None = None
    repo_local_path: str | None = None
    description: str | None = None
    monetization_model: MonetizationModel = MonetizationModel.CC_SUB
    marketing_domain: str | None = None
    token_budget_cents_month: int = 0


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
    token_budget_cents_month: int | None = None


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
    session.add(product)
    session.commit()
    session.refresh(product)
    create_workspace(product.slug)
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
