"""Database query objects used by application services."""

from .catalog import CatalogRepository, EventUpsertResult
from .companions import CompanionRepository
from .contacts import ContactRepository
from .demo import DemoRepository
from .feed import FeedRepository
from .notifications import NotificationRepository
from .onboarding import OnboardingError, OnboardingRepository

__all__ = [
    "CatalogRepository",
    "CompanionRepository",
    "ContactRepository",
    "DemoRepository",
    "EventUpsertResult",
    "FeedRepository",
    "NotificationRepository",
    "OnboardingError",
    "OnboardingRepository",
]
