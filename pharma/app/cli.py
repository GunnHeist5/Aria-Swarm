"""Operator CLI.

  python -m app.cli create-tenant "Name" [tier]
  python -m app.cli issue-key <client|trainer|admin> [tenant_id] [label]
  python -m app.cli seed-demo
  python -m app.cli distill <trainer_session_id> "<title>" [domain]
  python -m app.cli gym-init
  python -m app.cli gym-ingest <file> "<title>" [domain]
  python -m app.cli gym-confirm <set_id>
  python -m app.cli gym-run <set_id> [--limit N] [--resume <run_id>]
  python -m app.cli gym-score <set_id>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from app.db import control, knowledge, tenant as tdb

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def create_tenant(name: str, tier: str = "starter") -> str:
    tenant_id = control.create_tenant(name, tier)
    print(f"tenant: {tenant_id}")
    return tenant_id


def issue_key(role: str, tenant_id: str | None = None, label: str | None = None) -> None:
    key_id, raw_key = control.issue_key(role, tenant_id, label)
    print(f"key_id: {key_id}")
    print(f"token (shown once): {raw_key}")


def _load_dataset(tenant_id: str, csv_path: Path) -> None:
    import pandas as pd

    data = csv_path.read_bytes()
    df = pd.read_csv(csv_path)
    dest = tdb.resolve_tenant_file(tenant_id, f"uploads/{csv_path.name}")
    dest.write_bytes(data)
    tdb.add_dataset(
        tenant_id, csv_path.name, f"uploads/{csv_path.name}",
        hashlib.sha256(data).hexdigest(), len(df),
        json.dumps({c: str(t) for c, t in df.dtypes.items()}),
    )


def _seed_methods() -> None:
    text = (SAMPLE_DIR / "seed_methods.md").read_text()
    for block in text.split("\n---\n"):
        block = block.strip()
        if not block:
            continue
        title = block.splitlines()[0].lstrip("# ").strip()
        body = "\n".join(block.splitlines()[1:]).strip()
        method_id = knowledge.create_draft(
            kind="playbook", title=title, body_md=body,
            domain="brand_analytics", source_kind="deck_upload", source_ref="seed_methods.md",
        )
        knowledge.mark_anonymized(method_id, True)
        knowledge.approve(method_id, "seed")
        print(f"method approved: {method_id} — {title}")


def seed_demo() -> None:
    tenant_id = create_tenant("Demo Pharma Co", "starter")
    print("-- client key --")
    issue_key("client", tenant_id, "demo client")
    print("-- trainer key --")
    issue_key("trainer", None, "demo trainer")
    print("-- admin key --")
    issue_key("admin", None, "demo admin")
    _load_dataset(tenant_id, SAMPLE_DIR / "demo_sales.csv")
    print("dataset loaded: demo_sales.csv")
    _seed_methods()
    print("\nDone. Start the app with `make dev` and sign in at http://127.0.0.1:8100/login")


def distill(session_id: str, title: str, domain: str = "general") -> None:
    from app.training import distill as distill_mod

    method_id = distill_mod.distill_session(session_id, title, domain)
    print(f"draft created: {method_id} — review it in the trainer console")


def gym_init() -> None:
    from app.gym import runner

    tenant_id = runner.ensure_practice_tenant()
    print(f"practice tenant: {tenant_id} ({runner.PRACTICE_TENANT_NAME})")


def gym_ingest(path: str, title: str, domain: str = "general") -> None:
    from app.gym import split

    file_path = Path(path)
    set_id = split.ingest_problem_document(file_path.name, file_path.read_bytes(), title, domain)
    from app.db import gym as gym_db

    problems = gym_db.list_problems(set_id)
    print(f"set: {set_id} — {len(problems)} draft problem(s)")
    print("Review and confirm them in the console (/trainer/gym) or run gym-confirm.")


def gym_confirm(set_id: str) -> None:
    from app.db import gym as gym_db

    confirmed = 0
    for problem in gym_db.list_problems(set_id, status="draft"):
        try:
            gym_db.confirm_problem(problem["problem_id"])
            confirmed += 1
        except ValueError as exc:
            print(f"skipped {problem['problem_id']}: {exc}")
    print(f"confirmed {confirmed} problem(s)")


def gym_run(set_id: str, *flags: str) -> None:
    from app.gym import runner

    limit = None
    run_id = None
    args = list(flags)
    while args:
        flag = args.pop(0)
        if flag == "--limit" and args:
            limit = int(args.pop(0))
        elif flag == "--resume" and args:
            run_id = args.pop(0)
        else:
            print(f"unknown flag: {flag}")
            return

    def progress(i, total, problem_id, status, grade):
        print(f"[{i}/{total}] {problem_id} {status} {grade or ''}")

    result = runner.run_set(set_id, limit=limit, run_id=run_id, progress=progress)
    print(f"run: {result['run_id']} attempted={result['attempted']}"
          f" remaining={result['remaining']} status={result['status']}")
    if result["remaining"]:
        print(f"resume with: python -m app.cli gym-run {set_id} --resume {result['run_id']}")


def gym_score(set_id: str) -> None:
    from app.db import gym as gym_db

    for row in gym_db.run_scorecard(set_id):
        avg = f"{row['avg_score']:.2f}" if row["avg_score"] is not None else "—"
        print(f"{row['run_id']} {row['started_at'][:16]} graded={row['graded']}"
              f" pass={row['passes'] or 0} partial={row['partials'] or 0}"
              f" fail={row['fails'] or 0} avg={avg} [{row['status']}]")
    misses = gym_db.miss_queue(set_id)
    print(f"unreviewed misses: {len(misses)}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *args = argv
    commands = {
        "create-tenant": create_tenant,
        "issue-key": issue_key,
        "seed-demo": seed_demo,
        "distill": distill,
        "gym-init": gym_init,
        "gym-ingest": gym_ingest,
        "gym-confirm": gym_confirm,
        "gym-run": gym_run,
        "gym-score": gym_score,
    }
    fn = commands.get(cmd)
    if fn is None:
        print(__doc__)
        return 1
    fn(*args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
