from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models  # noqa: F401
from app.ai.reducer import apply_rolling_output
from app.models.meeting import Meeting
from app.models.state import Fact, Question
from app.schemas.ai_output import RollingAnalysisOutput


def _db():
    eng = create_engine("sqlite+pysqlite:///:memory:",
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)()


def _meeting(db):
    m = Meeting(title="t", status="active")
    db.add(m); db.commit(); db.refresh(m)
    return m


def _out(**kw):
    base = dict(understanding="u", key_discussion=[], facts=[], decisions=[],
                action_items=[], risks=[], questions_now=[], questions_later=[],
                potential_response=None, missing_information=[], contradictions=[],
                next_steps=[])
    base.update(kw)
    return RollingAnalysisOutput.model_validate(base)


def test_new_tentative_fact_created():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "production_go_live", "value": "October 30",
        "status": "tentative", "confidence": 0.6,
        "source_text": "go-live date is October 30",
    }]))
    db.commit()
    f = db.query(Fact).one()
    assert f.status == "tentative" and f.value == "October 30"


def test_conflicting_new_fact_without_supersedes_is_disputed():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "production_go_live", "value": "October 30",
        "status": "confirmed", "confidence": 0.9,
    }]))
    db.commit()
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "production_go_live", "value": "October 20",
        "status": "confirmed", "confidence": 0.9,
    }]))
    db.commit()
    statuses = [(r.value, r.status) for r in db.query(Fact).order_by(Fact.id).all()]
    assert ("October 30", "confirmed") in statuses
    assert ("October 20", "disputed") in statuses


def test_supersedes_with_valid_target_flips_old_to_superseded():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "production_go_live", "value": "October 30",
        "status": "tentative", "confidence": 0.6,
    }]))
    db.commit()
    first = db.query(Fact).one()

    apply_rolling_output(db, m.id, _out(facts=[
        {"key": "production_go_live", "value": "October 20",
         "status": "confirmed", "confidence": 0.95,
         "supersedes_fact_id": first.id},
        {"key": "customer_availability", "value": "October 30",
         "status": "confirmed", "confidence": 0.9},
    ]))
    db.commit()

    by_key = {r.key: r for r in db.query(Fact).all()}
    assert by_key["production_go_live"].value == "October 20"
    assert by_key["production_go_live"].status == "confirmed"
    assert by_key["production_go_live"].supersedes_fact_id == first.id
    assert by_key["customer_availability"].value == "October 30"
    orig = db.get(Fact, first.id)
    assert orig is not None and orig.status == "superseded"


def test_supersedes_invalid_target_downgrades_new_fact():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(facts=[{
        "key": "production_go_live", "value": "October 30",
        "status": "tentative", "confidence": 0.6,
    }]))
    db.commit()
    s = apply_rolling_output(db, m.id, _out(facts=[{
        "key": "production_go_live", "value": "October 20",
        "status": "confirmed", "confidence": 0.9,
        "supersedes_fact_id": 99999,
    }]))
    db.commit()
    new_row = db.query(Fact).order_by(Fact.id.desc()).first()
    assert new_row.status in ("tentative", "disputed")
    assert any("not found" in w for w in s.warnings)


def test_questions_is_new_lifecycle():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(questions_now=[{
        "text": "What measurable outcome does the POC target?", "category": "now",
    }]))
    db.commit()
    assert db.query(Question).one().is_new is True

    apply_rolling_output(db, m.id, _out(questions_now=[{
        "text": "What measurable outcome does the POC target?", "category": "now",
    }]))
    db.commit()
    assert db.query(Question).one().is_new is False


def test_questions_disappeared_are_retained():
    db = _db(); m = _meeting(db)
    apply_rolling_output(db, m.id, _out(questions_now=[{
        "text": "Old question?", "category": "now",
    }]))
    db.commit()
    apply_rolling_output(db, m.id, _out())
    db.commit()
    assert db.query(Question).one().status == "open"
