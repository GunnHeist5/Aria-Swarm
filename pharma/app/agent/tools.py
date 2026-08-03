"""Tenant-bound agent tools.

`build_toolbox(tenant_id, analysis_id)` returns (specs, impls) where every impl
is a closure over the validated tenant_id. The specs the model sees carry NO
tenant parameter — a prompt-injected model cannot address another tenant because
the address does not exist in its interface.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.db import knowledge, tenant as tdb
from app.agent import sandbox

TOOL_SPECS: list[dict] = [
    {
        "name": "list_datasets",
        "description": "List the client's uploaded datasets with row counts and column schemas.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_dataset_schema",
        "description": "Get the filename, column names/dtypes, and row count for one dataset. Use the filename it returns when reading the file in run_python (the file is in your working directory under uploads/).",
        "input_schema": {
            "type": "object",
            "properties": {"dataset_id": {"type": "string"}},
            "required": ["dataset_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_python",
        "description": "Run Python analysis code in a sandbox. cwd is your workspace; uploaded datasets are at uploads/<filename>. pandas and matplotlib are available (backend Agg — savefig to PNG, never plt.show). No network access. Print what you want to see.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_methods",
        "description": "Search the firm's approved methods library for frameworks/playbooks relevant to a topic.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_deliverable",
        "description": "Register a file you created in your workspace (chart PNG, report .md) as a client deliverable. Path is relative to your workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": ["chart", "table", "report"]},
            },
            "required": ["path", "title", "kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "note_learning",
        "description": "Record a client-specific insight worth remembering for future analyses of THIS client (data quirks, definitions, preferences).",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]

_MIME = {"chart": "image/png", "table": "text/csv", "report": "text/markdown"}


def build_toolbox(tenant_id: str, analysis_id: str | None = None) -> tuple[list[dict], dict[str, Callable[..., str]]]:
    tdb.validate_tenant_id(tenant_id)
    root = tdb.tenant_dir(tenant_id)
    workspace = root / "workspace"

    # Uploaded datasets are exposed to sandbox code via a symlink inside the
    # workspace, so cwd containment covers them too.
    uploads_link = workspace / "uploads"
    if not uploads_link.exists():
        uploads_link.symlink_to(root / "uploads")

    def list_datasets() -> str:
        rows = [
            {"dataset_id": d["dataset_id"], "filename": d["filename"], "rows": d["rows"],
             "schema": json.loads(d["schema_json"]) if d["schema_json"] else None}
            for d in tdb.list_datasets(tenant_id)
        ]
        return json.dumps(rows) if rows else "No datasets uploaded yet."

    def read_dataset_schema(dataset_id: str) -> str:
        d = tdb.get_dataset(tenant_id, dataset_id)
        if not d:
            return "Error: unknown dataset_id."
        return json.dumps({
            "dataset_id": d["dataset_id"], "filename": d["filename"], "rows": d["rows"],
            "schema": json.loads(d["schema_json"]) if d["schema_json"] else None,
            "read_with": f"pd.read_csv('uploads/{d['filename']}')",
        })

    def run_python(code: str) -> str:
        result = sandbox.run_python(code, workspace)
        out = []
        if result.stdout:
            out.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            out.append(f"stderr:\n{result.stderr}")
        out.append(f"return_code: {result.returncode}")
        return "\n".join(out)

    def search_methods(query: str) -> str:
        methods = knowledge.search_methods(query, limit=3)
        if not methods:
            return "No matching approved methods."
        return "\n\n".join(f"## {m['title']}\n{m['body_md']}" for m in methods)

    def save_deliverable(path: str, title: str, kind: str) -> str:
        try:
            resolved = tdb.resolve_tenant_file(tenant_id, f"workspace/{path}")
        except PermissionError:
            return "Error: path escapes workspace."
        if not resolved.is_file():
            return f"Error: no file at {path}. Create it with run_python first."
        # Move into deliverables/ so the gallery serves a stable location.
        dest_rel = f"deliverables/{resolved.name}"
        dest = tdb.resolve_tenant_file(tenant_id, dest_rel)
        resolved.replace(dest)
        deliverable_id = tdb.add_deliverable(
            tenant_id, analysis_id, kind, title, dest_rel, _MIME.get(kind, "application/octet-stream")
        )
        return f"Saved deliverable {deliverable_id} ({title})."

    def note_learning(text: str) -> str:
        tdb.add_learning(tenant_id, "conversation", text)
        return "Noted."

    impls = {
        "list_datasets": list_datasets,
        "read_dataset_schema": read_dataset_schema,
        "run_python": run_python,
        "search_methods": search_methods,
        "save_deliverable": save_deliverable,
        "note_learning": note_learning,
    }
    return TOOL_SPECS, impls
