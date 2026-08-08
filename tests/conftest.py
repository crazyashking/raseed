"""Shared test fixtures.

Everything here reads from `tests/fixtures/`. Nothing reads from `data/`, which
is gitignored and therefore absent on a fresh clone.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from raseed.db.engine import create_engine
from raseed.db.models import Base, User
from raseed.db.seed import bootstrap

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: The three committed receipt fixtures, chosen because they cover distinct shapes:
#: 001 has a line with a null MRP, 026 has an order-level coupon, 032 has no line
#: items at all. All three are synthetic, with no PII.
FIXTURE_NAMES = ("blinkit_001", "blinkit_026", "blinkit_032")


def load_fixture(name: str) -> dict[str, Any]:
    """Load one `*.expected.json` as a plain dict."""
    path = FIXTURE_DIR / f"{name}.expected.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def as_extraction_payload(name: str, **overrides: Any) -> dict[str, Any]:
    """Turn a fixture into something `ExtractionResult` can validate.

    The fixtures are the *input* that rendered each receipt image, so they hold
    what is printed and nothing more. A real extraction additionally carries the
    reasoning header from brief section 21.2, which a renderer has no reason to
    produce. This prepends a passing header so the two line up.
    """
    payload: dict[str, Any] = {
        "is_receipt": True,
        "receipt_confidence": 1.0,
        "rejection_reason": None,
    }
    payload.update(load_fixture(name))
    payload.update(overrides)
    return payload


@pytest.fixture(params=FIXTURE_NAMES)
def fixture_name(request: pytest.FixtureRequest) -> str:
    """Parametrised over every committed fixture."""
    name: str = request.param
    return name


@pytest.fixture
def session() -> Iterator[Session]:
    """An in-memory ledger with the schema created and nothing seeded.

    Built from `Base.metadata` rather than by running Alembic, so a schema test
    failure points at the models. `test_models.py` separately asserts that the
    migration produces the same shape.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture
def seeded(session: Session) -> tuple[Session, User]:
    """A ledger with the single user and the seed categories in place."""
    user = bootstrap(session)
    session.commit()
    return session, user
