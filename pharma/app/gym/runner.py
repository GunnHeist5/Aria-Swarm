"""Practice runner: executes confirmed problems through the real analyst loop
inside a dedicated practice tenant.

The practice tenant is a tenant like any other, which is the whole point:
uploads, sandbox, toolbox, usage metering, and learnings quarantine are reused
unchanged. All gym code addresses the tenant only through
ensure_practice_tenant() — nothing accepts a tenant id from the outside. The
agent never sees the answer key or the expected steps.
"""

from __future__ import annotations

import hashlib
import json

from app import config
from app.agent import analyst
from app.db import control, gym, tenant as tdb
from app.gym import grader as grader_mod

PRACTICE_TENANT_NAME = "Training Gym"
_META_KEY = "practice_tenant_id"


def ensure_practice_tenant() -> str:
    tenant_id = gym.get_meta(_META_KEY)
    if tenant_id and control.get_tenant(tenant_id):
        return tenant_id
    tenant_id = control.create_tenant(PRACTICE_TENANT_NAME, "enterprise")
    gym.set_meta(_META_KEY, tenant_id)
    return tenant_id


def _place_dataset(tenant_id: str, problem: dict) -> str:
    """Copy the problem's dataset into the practice tenant (idempotent).
    Stored as {problem_id}_{filename} so same-named files never collide."""
    source = config.gym_files_root() / problem["set_id"] / problem["dataset_filename"]
    if not source.is_file():
        raise FileNotFoundError(f"dataset missing: {problem['dataset_filename']}")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    stored = f"{problem['problem_id']}_{problem['dataset_filename']}"

    for existing in tdb.list_datasets(tenant_id):
        if existing["filename"] == stored and existing["content_hash"] == digest:
            return stored

    rows = None
    schema_json = None
    try:
        import io

        import pandas as pd

        df = pd.read_csv(io.BytesIO(data)) if stored.lower().endswith(".csv") \
            else pd.read_excel(io.BytesIO(data))
        rows = len(df)
        schema_json = json.dumps({c: str(t) for c, t in df.dtypes.items()})
    except Exception:
        pass
    dest = tdb.resolve_tenant_file(tenant_id, f"uploads/{stored}")
    dest.write_bytes(data)
    tdb.add_dataset(tenant_id, stored, f"uploads/{stored}", digest, rows, schema_json)
    return stored


def _compose_question(problem: dict, set_title: str, stored_filename: str | None) -> str:
    question = problem["prompt_md"]
    note = f'\n\n(Practice problem {problem["seq"]} of set "{set_title}".'
    if stored_filename:
        note += f" For this problem use ONLY the dataset file: {stored_filename}."
    note += ")"
    return question + note


def run_one(run_id: str, problem: dict, tenant_id: str,
            llm: analyst.LLM | None = None,
            grader: grader_mod.GraderLLM | None = None) -> str:
    """One attempt: create first (UNIQUE(run_id, problem_id) blocks
    double-attempting on resume), run, grade. Returns attempt_id."""
    attempt_id = gym.create_attempt(run_id, problem["problem_id"], problem["version"])
    run = gym.get_run(run_id)
    problem_set = gym.get_problem_set(run["set_id"])

    events: list[dict] = []
    try:
        stored = _place_dataset(tenant_id, problem) if problem["kind"] == "data" else None
        question = _compose_question(problem, problem_set["title"], stored)
        # Pre-create the analysis so the live-view bridge can stream feed rows
        # under a known id while the transcript list is captured as before.
        from app import tasks as tasks_mod

        analysis_id = tdb.create_analysis(tenant_id, question)
        bridge = tasks_mod.make_event_bridge(tenant_id, analysis_id)

        def tee(event: dict) -> None:
            events.append(event)
            bridge(event)

        result = analyst.run_analysis(
            tenant_id, question, conversation_id=None, llm=llm,
            on_tool_event=tee, analysis_id=analysis_id,
        )
        gym.finish_attempt(attempt_id, result.status, result.answer,
                           json.dumps(events), result.tokens_in, result.tokens_out,
                           analysis_id=result.analysis_id)
    except Exception as exc:
        gym.finish_attempt(attempt_id, "failed", f"runner error: {exc}",
                           json.dumps(events), 0, 0)

    grader_mod.grade_attempt(gym.get_attempt(attempt_id), problem, llm=grader)
    return attempt_id


def run_set(set_id: str, limit: int | None = None, run_id: str | None = None,
            llm: analyst.LLM | None = None,
            grader: grader_mod.GraderLLM | None = None,
            progress: callable = None) -> dict:
    tenant_id = ensure_practice_tenant()

    if run_id is None:
        if not gym.list_problems(set_id, status="confirmed"):
            raise ValueError("no confirmed problems in set")
        run_id = gym.create_run(set_id)
    else:
        run = gym.get_run(run_id)
        if not run or run["set_id"] != set_id:
            raise ValueError("run does not belong to this set")
        if run["status"] != "running":
            raise ValueError("run is already finished")

    todo = gym.unattempted_problems(run_id)
    batch = todo if limit is None else todo[:limit]
    for i, problem in enumerate(batch, 1):
        attempt_id = run_one(run_id, problem, tenant_id, llm=llm, grader=grader)
        if progress:
            grade_verdict = None
            attempt = gym.get_attempt(attempt_id)
            with gym.connect() as conn:
                row = conn.execute(
                    "SELECT verdict, score FROM grades WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
            if row:
                grade_verdict = f"{row['verdict']} {row['score']:.2f}"
            progress(i, len(batch), problem["problem_id"], attempt["status"], grade_verdict)

    remaining = len(gym.unattempted_problems(run_id))
    if remaining == 0:
        gym.finish_run(run_id)
    return {"run_id": run_id, "attempted": len(batch), "remaining": remaining,
            "status": "done" if remaining == 0 else "running"}
