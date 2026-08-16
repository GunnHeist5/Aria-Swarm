import hashlib
import json
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config
from app.db import control, gym, knowledge
from app.gym import runner, split
from app.routes import templates
from app.security import Identity, require_trainer

router = APIRouter(prefix="/trainer/gym")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DOMAINS = ("general", "brand_analytics", "market_access", "comp_intel")


def _sets_ctx(error: str | None = None):
    sets = gym.list_problem_sets()
    for s in sets:
        problems = gym.list_problems(s["set_id"])
        s["confirmed"] = sum(1 for p in problems if p["status"] == "confirmed")
        s["drafts"] = sum(1 for p in problems if p["status"] == "draft")
        scorecard = gym.run_scorecard(s["set_id"])
        latest = scorecard[-1] if scorecard else None
        s["latest_pass_rate"] = (
            f"{latest['passes']}/{latest['graded']}" if latest and latest["graded"] else "—"
        )
    return {"sets": sets, "domains": _DOMAINS, "error": error}


@router.get("", response_class=HTMLResponse)
def gym_home(request: Request, identity: Identity = Depends(require_trainer)):
    return templates.TemplateResponse(request, "gym.html", _sets_ctx())


@router.post("/ingest")
async def ingest_doc(request: Request, file: UploadFile, title: str = Form(...),
                     domain: str = Form("general"),
                     identity: Identity = Depends(require_trainer)):
    data = await file.read()
    try:
        set_id = split.ingest_problem_document(file.filename or "problems", data, title, domain)
    except ValueError as exc:
        return templates.TemplateResponse(request, "gym.html", _sets_ctx(str(exc)), status_code=400)
    control.audit(identity.key_id, "gym_ingest", resource=set_id)
    return RedirectResponse(f"/trainer/gym/sets/{set_id}", status_code=303)


@router.post("/paste")
def ingest_paste(request: Request, text: str = Form(...), title: str = Form(...),
                 domain: str = Form("general"),
                 identity: Identity = Depends(require_trainer)):
    try:
        set_id = split.ingest_pasted_text(text, title, domain)
    except (ValueError, RuntimeError) as exc:
        return templates.TemplateResponse(request, "gym.html", _sets_ctx(str(exc)), status_code=400)
    control.audit(identity.key_id, "gym_ingest", resource=set_id)
    return RedirectResponse(f"/trainer/gym/sets/{set_id}", status_code=303)


def _set_ctx(set_id: str, error: str | None = None):
    problem_set = gym.get_problem_set(set_id)
    if not problem_set:
        raise HTTPException(status_code=404)
    problems = gym.list_problems(set_id)
    runs = gym.list_runs(set_id)
    scorecard = gym.run_scorecard(set_id)
    latest_run = scorecard[-1] if scorecard else None
    misses = gym.miss_queue(set_id)
    for m in misses:
        m["gaps"] = json.loads(m["gaps_json"]) if m["gaps_json"] else []
    return {
        "set": problem_set,
        "problems": problems,
        "open_runs": [r for r in runs if r["status"] == "running"],
        "scorecard": scorecard,
        "breakdown": gym.domain_breakdown(latest_run["run_id"]) if latest_run else [],
        "misses": misses,
        "approved_methods": knowledge.list_methods("approved"),
        "domains": _DOMAINS,
        "web_limit": config.GYM_WEB_RUN_LIMIT,
        "error": error,
    }


@router.get("/sets/{set_id}", response_class=HTMLResponse)
def set_page(request: Request, set_id: str, identity: Identity = Depends(require_trainer)):
    return templates.TemplateResponse(request, "gym_set.html", _set_ctx(set_id))


@router.post("/sets/{set_id}/problems")
def add_problem(set_id: str, prompt_md: str = Form(...), answer_key_md: str = Form(...),
                expected_steps_md: str = Form(""), kind: str = Form("conceptual"),
                domain: str = Form("general"), identity: Identity = Depends(require_trainer)):
    problem_id = gym.add_problem(set_id, prompt_md, answer_key_md,
                                 expected_steps_md or None, kind, domain)
    control.audit(identity.key_id, "gym_problem_added", resource=problem_id)
    return RedirectResponse(f"/trainer/gym/sets/{set_id}", status_code=303)


@router.post("/problems/{problem_id}/edit")
def edit_problem(problem_id: str, prompt_md: str = Form(...), answer_key_md: str = Form(...),
                 expected_steps_md: str = Form(""), kind: str = Form("conceptual"),
                 domain: str = Form("general"), identity: Identity = Depends(require_trainer)):
    problem = gym.get_problem(problem_id)
    if not problem:
        raise HTTPException(status_code=404)
    try:
        gym.update_problem(problem_id, prompt_md, expected_steps_md or None,
                           answer_key_md, kind, domain)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    control.audit(identity.key_id, "gym_problem_edited", resource=problem_id)
    return RedirectResponse(f"/trainer/gym/sets/{problem['set_id']}", status_code=303)


@router.post("/problems/{problem_id}/dataset")
async def attach_dataset(problem_id: str, file: UploadFile,
                         identity: Identity = Depends(require_trainer)):
    problem = gym.get_problem(problem_id)
    if not problem:
        raise HTTPException(status_code=404)
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413)
    filename = _SAFE_NAME_RE.sub("_", file.filename or "dataset.csv").lstrip(".")
    dest_dir = config.gym_files_root() / problem["set_id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / filename).write_bytes(data)
    gym.attach_dataset(problem_id, filename, hashlib.sha256(data).hexdigest())
    control.audit(identity.key_id, "gym_dataset_attached", resource=problem_id)
    return RedirectResponse(f"/trainer/gym/sets/{problem['set_id']}", status_code=303)


@router.post("/problems/{problem_id}/confirm")
def confirm_problem(problem_id: str, identity: Identity = Depends(require_trainer)):
    problem = gym.get_problem(problem_id)
    if not problem:
        raise HTTPException(status_code=404)
    try:
        gym.confirm_problem(problem_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    control.audit(identity.key_id, "gym_problem_confirmed", resource=problem_id)
    return RedirectResponse(f"/trainer/gym/sets/{problem['set_id']}", status_code=303)


@router.post("/problems/{problem_id}/retire")
def retire_problem(problem_id: str, identity: Identity = Depends(require_trainer)):
    problem = gym.get_problem(problem_id)
    if not problem:
        raise HTTPException(status_code=404)
    gym.retire_problem(problem_id)
    control.audit(identity.key_id, "gym_problem_retired", resource=problem_id)
    return RedirectResponse(f"/trainer/gym/sets/{problem['set_id']}", status_code=303)


@router.post("/sets/{set_id}/run")
def run_set(request: Request, set_id: str, limit: int = Form(None), run_id: str = Form(None),
            identity: Identity = Depends(require_trainer)):
    capped = min(limit or config.GYM_WEB_RUN_LIMIT, config.GYM_WEB_RUN_LIMIT)
    control.audit(identity.key_id, "gym_run_started", resource=set_id,
                  detail={"limit": capped, "resume": run_id})
    try:
        result = runner.run_set(set_id, limit=capped, run_id=run_id or None)
    except ValueError as exc:
        return templates.TemplateResponse(request, "gym_set.html",
                                          _set_ctx(set_id, str(exc)), status_code=400)
    control.audit(identity.key_id, "gym_run_finished", resource=result["run_id"],
                  detail={"attempted": result["attempted"], "remaining": result["remaining"]})
    return RedirectResponse(f"/trainer/gym/sets/{set_id}", status_code=303)


@router.post("/grades/{grade_id}/override")
def override_grade(grade_id: str, override: str = Form(...), note: str = Form(""),
                   identity: Identity = Depends(require_trainer)):
    grade = gym.get_grade(grade_id)
    if not grade:
        raise HTTPException(status_code=404)
    try:
        gym.override_grade(grade_id, override, note or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    attempt = gym.get_attempt(grade["attempt_id"])
    problem = gym.get_problem(attempt["problem_id"])
    control.audit(identity.key_id, "gym_grade_override", resource=grade_id,
                  detail={"override": override})
    return RedirectResponse(f"/trainer/gym/sets/{problem['set_id']}", status_code=303)


@router.post("/grades/{grade_id}/correction")
def file_correction(grade_id: str, title: str = Form(...), body_md: str = Form(...),
                    domain: str = Form("general"),
                    identity: Identity = Depends(require_trainer)):
    """A miss becomes a draft method that still walks the scan+approve gate —
    the only path from gym content into production behavior."""
    grade = gym.get_grade(grade_id)
    if not grade:
        raise HTTPException(status_code=404)
    attempt = gym.get_attempt(grade["attempt_id"])
    problem = gym.get_problem(attempt["problem_id"])
    method_id = knowledge.create_draft(
        kind="playbook", title=title, body_md=body_md, domain=domain,
        source_kind="gym_correction", source_ref=problem["problem_id"],
    )
    gym.set_grade_correction(grade_id, method_id)
    control.audit(identity.key_id, "gym_correction_filed", resource=method_id,
                  detail={"grade_id": grade_id})
    return RedirectResponse(f"/trainer/gym/sets/{problem['set_id']}", status_code=303)
