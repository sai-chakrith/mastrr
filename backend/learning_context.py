"""Read observed learner evidence without assigning invented misconception labels."""
from .database import get_cursor, loads


def learner_context(learner_id: str, sub_skill: str) -> dict:
    observations = []
    with get_cursor() as cur:
        cur.execute("SELECT * FROM mastery_estimates WHERE learner_id=? AND sub_skill=?",
                    (learner_id, sub_skill))
        mastery = cur.fetchone()
        cur.execute("SELECT session_id, history FROM diagnostic_sessions WHERE learner_id=? AND sub_skill=? ORDER BY rowid DESC LIMIT 10",
                    (learner_id, sub_skill))
        diagnostics = cur.fetchall()
        cur.execute("SELECT session_id, answers, result FROM learning_sessions WHERE learner_id=? AND sub_skill=? ORDER BY rowid DESC LIMIT 10",
                    (learner_id, sub_skill))
        sessions = cur.fetchall()
        cur.execute("SELECT from_skill FROM sub_skill_graph_edges WHERE to_skill=? ORDER BY from_skill", (sub_skill,))
        prerequisites = []
        for row in cur.fetchall():
            name = row["from_skill"]
            cur.execute("SELECT score FROM mastery_estimates WHERE learner_id=? AND sub_skill=?", (learner_id, name))
            estimate = cur.fetchone()
            # Read evidence, not a second copy of Teammate 1's mastery policy.
            prerequisites.append({"sub_skill": name, "assessed": estimate is not None,
                                  "score": estimate["score"] if estimate else None})

        def add_answer(answer, source, session_id):
            cur.execute("SELECT * FROM questions WHERE id=? AND sub_skill=?", (answer.get("question_id"), sub_skill))
            q = cur.fetchone()
            if q is None:
                return
            observations.append({
                "question_id": q["id"], "question_text": q["text"],
                "difficulty": q["difficulty"], "correct": answer["correct"],
                "selected_answer": answer.get("selected_answer"),
                "expected_answer": q["correct_answer"],
                "source": source, "session_id": session_id,
            })

        for session in sessions:
            for answer in reversed(loads(session["answers"])):
                add_answer(answer, "learning", session["session_id"])
        for session in diagnostics:
            for answer in reversed(loads(session["history"])):
                add_answer(answer, "diagnostic", session["session_id"])

    failures = 0
    for session in sessions:
        result = loads(session["result"])
        if not result or result.get("passed") is None:
            continue
        if result["passed"]:
            break
        failures += 1
    errors = [a for a in observations if not a["correct"]][:5]
    difficulty = errors[0]["difficulty"] if errors else 3
    strategy = "target_observed_errors" if errors else "check_understanding"
    if failures >= 2:
        strategy = "revisit_prerequisites" if prerequisites else "simpler_worked_example"
        difficulty = max(1, difficulty - 1)
    return {
        "learner_id": learner_id, "sub_skill": sub_skill,
        "mastery_evidence": dict(mastery) if mastery else None,
        "observed_errors": errors, "n_observations": len(observations),
        "selected_answers_available": any(a["selected_answer"] is not None for a in errors),
        "prerequisites": prerequisites,
        "consecutive_unsuccessful_sessions": failures,
        "teaching_strategy": strategy,
        "suggested_practice_difficulty": difficulty,
    }
