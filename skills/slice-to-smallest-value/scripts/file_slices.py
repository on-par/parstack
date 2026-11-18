#!/usr/bin/env python3
"""File a passing slice document as GitHub issues, in dependency order.

Dry run by default: prints, in run order, the title, labels, readiness verdict
and body each issue would get, and posts nothing. Dependencies on slices that
are not created yet print as "S1 (not created yet)".

With --create: checks gh, then creates the issues one at a time with
`gh issue create`. Each body is rendered with the issue numbers created so
far, so "Depends on S1" becomes "Depends on #41". External dependencies
(#40, owner/repo#7, ABC-12) print as written. Every body is checked with
check-issue-readiness before it is posted, and each new issue is re-checked
with `check_issue.py --gh N`. Slice id -> issue number is written to a JSON
map after every create; --resume skips slices already in the map.

Exit codes: 0 done, 1 slice check errors, a not-ready issue, or a failed
create, 2 usage or input error (including gh not found with --create).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_slices import InputError, apply_overlap_policy, check, format_human, load_doc  # noqa: E402
from render import ordered, parse_parent_issue, render_slice  # noqa: E402

CHECK_ISSUE = (Path(__file__).resolve().parents[2]
               / "check-issue-readiness" / "scripts" / "check_issue.py")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
ISSUE_URL_RE = re.compile(r"\S+/issues/(\d+)\b")


class CreateError(Exception):
    pass


# ---------------------------------------------------------------- issue map

def default_map_path(slices_path: str) -> Path:
    p = Path(slices_path)
    return p.with_name(p.stem + ".issues.json")


def load_map(path: Path, repo: str | None) -> dict[str, dict]:
    """Return {slice id: {"number", "url", "title"}} from a map file, or {} if absent."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("issues"), dict):
        raise InputError(f'{path}: want {{"repo": ..., "issues": {{...}}}}')
    if repo and data.get("repo") != repo:
        raise InputError(f"{path} is for {data.get('repo')!r}, not {repo!r}")
    issues = {}
    for sid, entry in data["issues"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("number"), int):
            raise InputError(f"{path}: entry {sid!r} has no issue number")
        issues[sid] = entry
    return issues


def save_map(path: Path, repo: str, issues: dict[str, dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"repo": repo, "issues": issues}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- readiness and gh

def run_check_issue(check_issue: Path, args: list[str], env: dict | None = None) -> dict:
    proc = subprocess.run([sys.executable, str(check_issue), *args, "--json"],
                          capture_output=True, text=True, env=env)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict) or "verdict" not in data:
        reason = (data.get("skipped") or data.get("input_error") if isinstance(data, dict) else None)
        reason = reason or (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        return {"verdict": "error", "missing": [str(reason)], "assumptions": []}
    return {"verdict": data["verdict"], "missing": data.get("missing", []),
            "assumptions": data.get("assumptions", [])}


def check_body(check_issue: Path | None, title: str, body: str) -> dict:
    if check_issue is None:
        return {"verdict": "skipped", "missing": [], "assumptions": []}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "issue.md"
        path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
        return run_check_issue(check_issue, ["--file", str(path)])


def gh_create(gh: str, repo: str, title: str, body: str, labels: list[str]) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        body_file = Path(tmp) / "body.md"
        body_file.write_text(body, encoding="utf-8")
        cmd = [gh, "issue", "create", "--repo", repo, "--title", title, "--body-file", str(body_file)]
        for label in labels:
            cmd += ["--label", label]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CreateError((proc.stderr or proc.stdout).strip() or f"gh exited {proc.returncode}")
    for line in reversed(proc.stdout.splitlines()):
        m = ISSUE_URL_RE.search(line)
        if m:
            return int(m.group(1)), m.group(0)
    raise CreateError(f"gh printed no issue URL: {proc.stdout.strip()!r}")


# ---------------------------------------------------------------- output

def readiness_lines(r: dict) -> list[str]:
    lines = [f"readiness: {r['verdict']}"]
    lines += [f"  missing: {m}" for m in r["missing"]]
    lines += [f"  assumption: {a}" for a in r["assumptions"]]
    return lines


def created_lines(created: dict[str, dict]) -> list[str]:
    if not created:
        return ["already created: none"]
    return ["already created:"] + [f"  {sid} -> #{e['number']} {e.get('url', '')}".rstrip()
                                   for sid, e in created.items()]


def refusal_lines(sid: str, title: str, r: dict, created: dict[str, dict]) -> list[str]:
    lines = [f'refused: {sid} "{title}" is {r["verdict"]}; not creating it']
    lines += [f"  missing: {m}" for m in r["missing"]]
    lines += created_lines(created)
    lines.append("fix the slice, then re-run with --create --resume")
    return lines


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="File slices as GitHub issues in dependency order (dry run unless --create).")
    ap.add_argument("path", help="slice JSON file, or markdown file with a ```json block")
    ap.add_argument("--repo", required=True, metavar="OWNER/NAME", help="GitHub repo to file into")
    ap.add_argument("--label", action="append", default=[], metavar="L",
                    help="label for every issue (repeatable; no labels by default)")
    ap.add_argument("--parent-issue", metavar="N", help='adds "Part of #N" to each body')
    ap.add_argument("--create", action="store_true", help="create the issues (default: dry run)")
    ap.add_argument("--map-out", metavar="PATH",
                    help="slice id -> issue map (default: <slices>.issues.json next to the slices file)")
    ap.add_argument("--resume", action="store_true", help="skip slices already in the map")
    ap.add_argument("--gh-bin", default="gh", metavar="PATH", help="gh executable (default: gh)")
    ap.add_argument("--check-issue", metavar="PATH",
                    help="path to check-issue-readiness check_issue.py (default: the sibling skill)")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = ap.parse_args(argv)

    out: dict = {"ok": False, "mode": "create" if args.create else "dry-run", "repo": args.repo,
                 "issues": []}
    def say(*new: str) -> None:
        if not args.json:
            print("\n".join(new), flush=True)

    def finish(code: int, error: str | None = None) -> int:
        out["ok"] = code == 0
        if error:
            out["error"] = error
        if args.json:
            print(json.dumps(out, indent=2))
        return code

    def usage_error(msg: str) -> int:
        if not args.json:
            print(msg if msg.startswith("skipped:") else f"input error: {msg}", file=sys.stderr)
            return 2
        key = "skipped" if msg.startswith("skipped:") else "input_error"
        print(json.dumps({"ok": False, key: msg.removeprefix("skipped: ")}))
        return 2

    if not REPO_RE.match(args.repo):
        return usage_error(f"bad --repo {args.repo!r} (want owner/name)")
    if args.path == "-" and not args.map_out:
        return usage_error("--map-out is required when reading slices from stdin")
    map_path = Path(args.map_out) if args.map_out else default_map_path(args.path)
    if args.create and not map_path.parent.is_dir():
        return usage_error(f"map directory not found: {map_path.parent}")
    out["map"] = str(map_path)
    try:
        doc = load_doc(args.path)
        parent_issue = parse_parent_issue(args.parent_issue)
        created = load_map(map_path, args.repo) if args.resume else {}
        if args.create and not args.resume and load_map(map_path, None):
            raise InputError(f"{map_path} already lists created issues; re-run with --resume "
                             f"to finish that run, or move the map away to start over")
    except InputError as exc:
        return usage_error(str(exc))

    result = check(doc)
    apply_overlap_policy(result, "fail")
    summary = format_human(result).splitlines()
    out["check"] = {"ok": result["ok"], "errors": result["errors"], "warnings": result["warnings"]}
    if not result["ok"]:
        say(*summary, "refusing to file: fix the slice errors first")
        return finish(1, "slice check failed")
    say(*[ln for ln in summary if ln == summary[0] or ln.startswith("  warning")])

    gh = None
    if args.create:
        gh = shutil.which(args.gh_bin)
        if not gh:
            return usage_error("skipped: gh not found")
    check_issue: Path | None = Path(args.check_issue) if args.check_issue else CHECK_ISSUE
    if not check_issue.is_file():
        if args.create:
            return usage_error(f"check_issue.py not found at {check_issue}; pass --check-issue PATH")
        check_issue = None

    parent = doc.get("parent") or {}
    slices = ordered(doc)
    issue_map = {sid: f"#{e['number']}" for sid, e in created.items()}
    pending_ids = [s["id"] for s in slices if s["id"] not in created]
    placeholders = {sid: f"{sid} (not created yet)" for sid in pending_ids}
    labels = args.label

    # Plan: render every slice with what is known now and check each body before posting anything.
    plan = []
    for s in slices:
        sid, title = s["id"], str(s.get("title") or "").strip()
        entry = {"id": sid, "title": title, "labels": labels}
        if sid in created:
            entry.update(status="skipped", number=created[sid]["number"], url=created[sid].get("url"))
        else:
            body = render_slice(s, parent, {**issue_map, **placeholders}, parent_issue)
            entry.update(status="planned", body=body, readiness=check_body(check_issue, title, body))
        plan.append((s, entry))
        out["issues"].append(entry)

    blocked = [e for _, e in plan if e.get("readiness", {}).get("verdict") in ("not-ready", "error")]

    if not args.create:
        say(f"dry run: {len(pending_ids)} issue{'s' if len(pending_ids) != 1 else ''} for "
            f"{args.repo}, in this order; nothing is posted (add --create to file them)")
        for n, (_, e) in enumerate(plan, 1):
            say("", f"[{n}/{len(plan)}] {e['id']}")
            if e["status"] == "skipped":
                say(f"already #{e['number']} (from {map_path.name}); skipped")
                continue
            say(f"title: {e['title']}", f"labels: {', '.join(labels) or '(none)'}",
                *readiness_lines(e["readiness"]), "body:", e["body"].rstrip("\n"))
        if any(d in placeholders for s, e in plan if e["status"] == "planned"
               for d in s.get("depends_on") or []):
            say("", 'Dependencies marked "(not created yet)" get their real issue numbers '
                "during --create.")
        if blocked:
            say("", "would refuse: " + ", ".join(e["id"] for e in blocked) + " not ready")
            return finish(1, "not ready: " + ", ".join(e["id"] for e in blocked))
        return finish(0)

    # Create: refuse before posting anything if a body is already known to be not ready.
    for _, e in plan:
        if e in blocked:
            say(*refusal_lines(e["id"], e["title"], e["readiness"], created))
            e["status"] = "refused"
            return finish(1, f"{e['id']} is not ready")

    recheck_env = dict(os.environ)
    recheck_env["PATH"] = str(Path(gh).parent) + os.pathsep + recheck_env.get("PATH", "")
    say(f"creating {len(pending_ids)} issue{'s' if len(pending_ids) != 1 else ''} in {args.repo}")
    made = 0
    for s, e in plan:
        if e["status"] == "skipped":
            say(f"skip {e['id']}: already #{e['number']}")
            continue
        sid, title = e["id"], e["title"]
        body = render_slice(s, parent, issue_map, parent_issue)
        readiness = check_body(check_issue, title, body)
        e.update(body=body, readiness=readiness)
        if readiness["verdict"] in ("not-ready", "error"):
            e["status"] = "refused"
            say(*refusal_lines(sid, title, readiness, created))
            return finish(1, f"{sid} is not ready")
        try:
            number, url = gh_create(gh, args.repo, title, body, labels)
        except CreateError as exc:
            e["status"] = "failed"
            say(f"failed: gh issue create for {sid}: {exc}", *created_lines(created),
                "re-run with --create --resume to finish")
            return finish(1, f"gh issue create failed for {sid}")
        created[sid] = {"number": number, "url": url, "title": title}
        issue_map[sid] = f"#{number}"
        save_map(map_path, args.repo, created)
        made += 1
        recheck = run_check_issue(check_issue, ["--gh", str(number), "--repo", args.repo],
                                  env=recheck_env)
        e.update(status="created", number=number, url=url, recheck=recheck["verdict"])
        say(f"created {sid} -> #{number} {url} (readiness before: {readiness['verdict']})",
            f"  re-check #{number}: {recheck['verdict']}"
            + "".join(f"\n    missing: {m}" for m in recheck["missing"]))
    say(f"done: {made} created, {len(plan) - made} skipped; map: {map_path}")
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
