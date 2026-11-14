from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProductStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    LIVE = "LIVE"
    DELIVERY_READY = "DELIVERY_READY"
    DELISTED = "DELISTED"
    REJECTED = "REJECTED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    DELIVERY_FAILED = "DELIVERY_FAILED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    WAITING_USER = "WAITING_USER"


ALLOWED_PRODUCT_TRANSITIONS: dict[ProductStatus, set[ProductStatus]] = {
    ProductStatus.DRAFT: {ProductStatus.PENDING_APPROVAL},
    ProductStatus.PENDING_APPROVAL: {
        ProductStatus.APPROVED,
        ProductStatus.REJECTED,
        ProductStatus.DRAFT,
    },
    ProductStatus.APPROVED: {
        ProductStatus.PUBLISHING,
        ProductStatus.DRAFT,
        ProductStatus.REJECTED,
    },
    ProductStatus.PUBLISHING: {
        ProductStatus.LIVE,
        ProductStatus.PUBLISH_FAILED,
    },
    ProductStatus.LIVE: {
        ProductStatus.DELIVERY_READY,
        ProductStatus.DELIVERY_FAILED,
        ProductStatus.DELISTED,
    },
    ProductStatus.DELIVERY_READY: {ProductStatus.DELISTED},
    ProductStatus.DELIVERY_FAILED: {
        ProductStatus.DELIVERY_READY,
        ProductStatus.DELISTED,
    },
    ProductStatus.REJECTED: {ProductStatus.DRAFT},
    ProductStatus.PUBLISH_FAILED: {ProductStatus.DRAFT},
}


class StateTransitionError(ValueError):
    pass


def require_transition(current: ProductStatus, target: ProductStatus) -> None:
    if target not in ALLOWED_PRODUCT_TRANSITIONS.get(current, set()):
        raise StateTransitionError(f"illegal transition: {current} -> {target}")


@dataclass(frozen=True, slots=True)
class ProductSpec:
    slug: str
    title: str
    description: str
    category: str
    price_cents: int
    normal_price_cents: int
    pages: int
    risk_flags: tuple[str, ...] = ()
    metadata: dict | None = None

