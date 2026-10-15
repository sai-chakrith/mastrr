"""Teammate 2's API surface. Identity is provisional until auth is integrated."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .database import get_cursor
from . import learning_service as learning
from .tutor import generate_tutoring_explanation

router = APIRouter(prefix="/learning", tags=["learning"])


class TopicRequest(BaseModel):
    learner_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    sub_skill: str = Field(min_length=1, max_length=200, pattern=r"\S")


class SessionRequest(TopicRequest):
    purpose: Literal["practice", "verification", "review"] = "practice"
    count: int = Field(default=3, ge=1, le=6, strict=True)


class AnswerRequest(BaseModel):
    learner_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    question_id: str = Field(min_length=1, max_length=200)
    selected_answer: str = Field(min_length=1, max_length=4000)


class LearnerRequest(BaseModel):
    learner_id: str = Field(min_length=1, max_length=200, pattern=r"\S")


def _call(function, *args):
    try:
        return function(*args)
    except learning.LearningConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tutor")
def tutor(body: TopicRequest):
    with get_cursor() as cur:
        _call(learning._require_topic, cur, body.sub_skill)
    return generate_tutoring_explanation(body.sub_skill, learner_id=body.learner_id)


@router.post("/sessions")
def start(body: SessionRequest):
    return _call(learning.start_learning_session, body.learner_id, body.sub_skill, body.purpose, body.count)


@router.get("/sessions/{session_id}")
def resume(session_id: str, learner_id: str = Query(min_length=1, max_length=200, pattern=r"\S")):
    return _call(learning.get_learning_session, learner_id, session_id)


@router.post("/sessions/{session_id}/answer")
def answer(session_id: str, body: AnswerRequest):
    return _call(learning.submit_learning_answer, body.learner_id, session_id, body.question_id, body.selected_answer)


@router.post("/sessions/{session_id}/abandon")
def abandon(session_id: str, body: LearnerRequest):
    return _call(learning.abandon_learning_session, body.learner_id, session_id)


@router.get("/reviews/{learner_id}")
def reviews(learner_id: str):
    return learning.list_reviews(learner_id)
