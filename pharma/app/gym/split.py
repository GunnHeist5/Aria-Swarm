"""Assisted problem-set ingest.

Two paths into draft problems: an LLM splitter for prose documents (PDF/md/txt
and pasted text) and a no-LLM convention parser for csv/xlsx (columns: problem,
answer required; steps, kind, domain optional). Everything lands as
status='draft' — nothing is runnable until the trainer confirms each problem.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Callable

from app import config
from app.agent import prompts
from app.db import gym
from app.training import ingest as training_ingest

SplitterLLM = Callable[[str, str], str]

_REQUIRED_COLS = ("problem", "answer")
_OPTIONAL_COLS = ("steps", "kind", "domain")


def _default_llm(system: str, user_text: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.PHARMA_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user_text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("splitter request was declined")
    return next((b.text for b in response.content if b.type == "text"), "")


def split_text(text: str, llm: SplitterLLM | None = None) -> list[dict]:
    if llm is None:
        llm = _default_llm
    raw = llm(prompts.GYM_SPLITTER_SYSTEM, f"Document:\n\n{text[:60000]}")
    try:
        start, end = raw.index("["), raw.rindex("]") + 1
        parsed = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        raise ValueError("splitter output unparseable — add problems manually instead")
    candidates = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt", "")).strip()
        answer_key = str(item.get("answer_key", "")).strip()
        if not prompt or not answer_key:
            continue
        kind = item.get("kind") if item.get("kind") in ("conceptual", "data") else "conceptual"
        candidates.append({
            "prompt": prompt,
            "expected_steps": str(item.get("expected_steps", "")).strip(),
            "answer_key": answer_key,
            "kind": kind,
            "domain": str(item.get("domain") or "general"),
        })
    return candidates


def split_tabular(filename: str, data: bytes) -> list[dict]:
    import pandas as pd

    suffix = Path(filename).suffix.lower()
    df = pd.read_csv(io.BytesIO(data)) if suffix == ".csv" else pd.read_excel(io.BytesIO(data))
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}"
                         " (expected: problem, answer; optional: steps, kind, domain)")
    candidates = []
    for _, row in df.iterrows():
        prompt = str(row["problem"]).strip()
        answer_key = str(row["answer"]).strip()
        if not prompt or prompt.lower() == "nan" or not answer_key or answer_key.lower() == "nan":
            continue
        kind = str(row.get("kind", "")).strip().lower()
        steps = str(row.get("steps", "")).strip()
        domain = str(row.get("domain", "")).strip()
        candidates.append({
            "prompt": prompt,
            "expected_steps": "" if steps.lower() == "nan" else steps,
            "answer_key": answer_key,
            "kind": kind if kind in ("conceptual", "data") else "conceptual",
            "domain": domain if domain and domain.lower() != "nan" else "general",
        })
    return candidates


def ingest_problem_document(filename: str, data: bytes, title: str,
                            domain: str | None = None,
                            llm: SplitterLLM | None = None) -> str:
    """Document -> problem set with draft problems. Returns set_id."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".csv", ".xlsx"):
        candidates = split_tabular(filename, data)
    else:
        text, needs_manual = training_ingest.extract_text(filename, data)
        if needs_manual or not text:
            candidates = [{
                "prompt": f"*(needs_manual_extraction: paste the problem from `{filename}` here)*",
                "expected_steps": "",
                "answer_key": "*(paste the answer key here)*",
                "kind": "conceptual",
                "domain": domain or "general",
            }]
        else:
            candidates = split_text(text, llm=llm)

    set_id = gym.create_problem_set(title, domain, filename)
    for c in candidates:
        gym.add_problem(set_id, c["prompt"], c["answer_key"],
                        expected_steps_md=c["expected_steps"] or None,
                        kind=c["kind"], domain=c["domain"])
    return set_id


def ingest_pasted_text(text: str, title: str, domain: str | None = None,
                       llm: SplitterLLM | None = None) -> str:
    candidates = split_text(text, llm=llm)
    set_id = gym.create_problem_set(title, domain, "pasted_text")
    for c in candidates:
        gym.add_problem(set_id, c["prompt"], c["answer_key"],
                        expected_steps_md=c["expected_steps"] or None,
                        kind=c["kind"], domain=c["domain"])
    return set_id
