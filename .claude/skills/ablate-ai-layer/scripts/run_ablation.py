#!/usr/bin/env python3
"""Run the ablation experiment. The agent calls this; the user watches.

Executes the same probe task N times with the AI layer intact and N times with it
stripped, in throwaway git worktrees, and collects the resulting diffs for grading.

Two properties make this safe to run unattended:

  * Your working tree is never modified. Every run happens in a one-commit snapshot
    of HEAD in a temp directory (no history, no refs: see `snapshot`), deleted
    afterwards. Nothing is moved aside, so there is no restore step that can fail.
  * Worktrees live OUTSIDE the repo. An agent started inside the repo would walk
    up and find the very CLAUDE.md this script just removed.

Why N runs per arm rather than one: agent runs are nondeterministic. A single
control-vs-stripped pair is a point estimate, and two runs of the SAME arm can
differ more than the two arms differ. Only a rule that behaves consistently across
runs is evidence of anything.

Usage:
  python run_ablation.py <repo> --task-file task.md
  python run_ablation.py <repo> --task-file task.md --runs 3 --scope all
  python run_ablation.py <repo> --task-file task.md --dry-run
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from map_layer import ALWAYS, ONDEMAND, iter_matches  # noqa: E402

CONTROL, STRIPPED = "control", "stripped"
# Always-loaded surfaces that live OUTSIDE the repo layer, strippable one arm at a time.
SURFACES = ("user-claude-md", "memory", "caches")


# ---------------------------------------------------------------- git plumbing

def git(args: list[str], cwd: Path, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def repo_root(start: Path) -> Path:
    out = git(["rev-parse", "--show-toplevel"], start).strip()
    return Path(out).resolve()


def head_sha(root: Path) -> str:
    return git(["rev-parse", "HEAD"], root).strip()


def is_dirty(root: Path) -> bool:
    return bool(git(["status", "--porcelain"], root).strip())


# ---------------------------------------------------------------- the layer

def layer_targets(root: Path, scope: str) -> list[dict]:
    """The artifacts the stripped arm removes. Enforcement is never touched:
    hooks and permissions run as code and spend no attention budget, so removing
    them would change mechanics rather than the variable under test."""
    kinds = {ALWAYS} if scope == "always" else {ALWAYS, ONDEMAND}
    return [i for i in iter_matches(root) if i["kind"] in kinds]


SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb",
                   ".java", ".kt", ".cs", ".php", ".swift", ".toml", ".json"}
SKIP = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__",
        ".next", "target", ".ablation"}


def build_dependencies(root: Path, targets: list[dict]) -> list[str]:
    """Does the repo's own source read its AI layer? A CLI that imports its skill
    markdown at build time breaks the moment those files go missing, and the user
    reads a compile error as an agent regression."""
    names = {Path(t["path"]).name for t in targets}
    names |= {t["path"] for t in targets}
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
            continue
        if any(part in SKIP for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for n in names:
            if n in text:
                hits.append(f"{p.relative_to(root).as_posix()} references {n}")
                break
    return hits


# ---------------------------------------------------------------- one run

# Handed to `claude --settings` by --no-hooks. Flag settings outrank every settings file.
NO_HOOKS_SETTINGS = '{"disableAllHooks": true}'


def run_settings(no_hooks: bool = False, hooks_only: str | None = None,
                 memory_dir: Path | None = None, extra: dict | None = None) -> dict:
    """The one settings object every Claude arm gets through `--settings`.

    `crossSessionInbound: refuse` is unconditional. Every `claude` process, `-p` included,
    binds a peer socket that `disableAllHooks` does not touch, and on a box where other
    sessions broadcast, a message can land mid-run and even replace the final result.
    `autoMemoryDirectory` points both arms at a per-run COPY of the real memory directory:
    both still load the same MEMORY.md, and neither can write to the real one."""
    settings: dict = {}
    if no_hooks:
        settings.update(json.loads(NO_HOOKS_SETTINGS))
    elif hooks_only:
        settings.update(json.loads(Path(hooks_only).read_text(encoding="utf-8")))
    settings["crossSessionInbound"] = "refuse"
    if memory_dir is not None:
        settings["autoMemoryDirectory"] = str(memory_dir)
    settings.update(extra or {})
    return settings


def snapshot_cache_hooks(root: Path, match: str, outdir: Path) -> list[Path]:
    """Run each project SessionStart hook whose command contains `match` ONCE, and keep its output.

    The control arm replays these snapshots with `cat`, so every control run sees the same
    injected text even if the live source (a wiki page, say) changes mid-experiment. Finding no
    hook is an error, not an empty strip: stripping nothing would report the caches as redundant."""
    settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for g in settings.get("hooks", {}).get("SessionStart", [])
                for h in g.get("hooks", []) if match in h.get("command", "")]
    if not commands:
        raise RuntimeError(f"no project SessionStart hook command contains {match!r}; nothing to strip")
    outdir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    event = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    snaps = []
    for n, command in enumerate(commands, 1):
        out = subprocess.run(command, shell=True, cwd=str(root), env=env, input=event,
                             capture_output=True, text=True, timeout=120)
        if out.returncode or not out.stdout.strip():
            raise RuntimeError(f"cache hook {n} gave no output (exit {out.returncode}): {command}")
        f = outdir / f"cache-hook-{n}.txt"
        f.write_text(out.stdout, encoding="utf-8")
        snaps.append(f)
    return snaps


def surface_project_settings(arm: str, snapshots: list[Path]) -> dict:
    """The throwaway worktree's .claude/settings.json when the caches are under test.

    Both arms lose every other project hook and project-enabled plugin (their side effects are
    the hazard --no-hooks exists for); only the control arm replays the cache snapshots.
    Project settings stay a live source, which is what keeps AGENTS.md/CLAUDE.md loading:
    `--setting-sources user` stops them (measured on Opus, claude 2.1.280)."""
    if arm != CONTROL:
        return {}
    return {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": f"cat {shlex.quote(str(f))}"} for f in snapshots]}]}}


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def project_slug(path: Path) -> str:
    """How Claude Code names a project's directory under <config>/projects/."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def scratch_memory(root: Path, tmp: Path) -> Path:
    """A per-run copy of the memory directory the repo's sessions really use.

    A linked worktree shares its main checkout's memory directory, so the source is keyed
    on the main checkout (the common git dir's parent), not on `root` or the run's tree."""
    common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root).strip())
    src = claude_config_dir() / "projects" / project_slug(common.parent) / "memory"
    dst = tmp / "memory"
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.mkdir(parents=True)
    return dst


def cross_session_messages(session_id: str | None) -> int | None:
    """How many peer messages reached this session, read from its own transcript.

    The result text alone misses a message that arrived mid-task and was answered in
    passing. None means the transcript was not found, which is reported, not read as 0.

    Counts DELIVERIES, one per entry: a user turn whose content is a string (arrived idle) or a
    `queued_command` attachment (arrived mid-turn). A substring count flagged clean runs: the tag
    also appears in tool results (an agent that read this file), tool calls, queue bookkeeping and
    the SendMessage tool's own docs -- measured on hub#1618, two of six runs falsely CONTAMINATED."""
    if not session_id:
        return None
    hits = list((claude_config_dir() / "projects").glob(f"*/{session_id}.jsonl"))
    if not hits:
        return None
    n = 0
    for p in hits:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "<cross-session-message" not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if (e.get("type") == "user" and isinstance((e.get("message") or {}).get("content"), str)
                    or e.get("type") == "attachment"
                    and (e.get("attachment") or {}).get("type") == "queued_command"):
                n += 1
    return n


def build_command(model: str | None, runner: str | None,
                  no_hooks: bool = False,
                  allowed_tools: list[str] | None = None,
                  hooks_only: str | None = None,
                  memory_dir: Path | None = None,
                  extra_settings: dict | None = None) -> tuple[list[str] | str, bool]:
    """Returns (command, use_shell). The prompt always arrives on stdin, never in
    argv: Windows caps a command line at 32,767 chars and a real probe task plus
    its context blows through that as a misleading 'file not found'.

    `no_hooks` turns every hook off in BOTH arms. Use it when the repo's hooks act
    on the world (merge, deploy, notify, spend) rather than only guard the session:
    a throwaway run must not do what a real session's Stop hook does. It is also a
    measurement change, so say so in the report: context a hook injects (a
    SessionStart loader, a PreToolUse reminder) is gone from the control arm too.

    `hooks_only` is the other answer to the same hazard, for when hook TEXT is what is under
    test: a settings file whose hooks are the only project or plugin hooks either arm runs.
    `--setting-sources user` drops the project and local settings files, and the plugins they
    enable, so a side-effecting Stop hook never loads while a guard named in the file still
    fires. User-level settings stay, so a hook in ~/.claude/settings.json still runs."""
    if runner:
        return runner, True
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError(
            "`claude` is not on PATH. Install Claude Code, or pass --runner with the "
            "headless command for your agent (it must read the prompt on stdin).")
    cmd = [exe, "-p", "--output-format", "json", "--permission-mode", "acceptEdits"]
    if hooks_only and not no_hooks:
        cmd += ["--setting-sources", "user"]
    cmd += ["--settings", json.dumps(run_settings(no_hooks, hooks_only, memory_dir, extra_settings))]
    if allowed_tools:
        # acceptEdits approves edits and filesystem commands only; under -p every other Bash call
        # is a prompt nobody answers, so an agent cannot run the test it just wrote. Name the
        # commands the task needs instead of bypassing permissions.
        cmd += ["--allowedTools", *allowed_tools]
    if model:
        cmd += ["--model", model]
    return cmd, False


def exclude_results(root: Path, out: Path) -> str | None:
    """Keep the results directory out of `git status` without editing a tracked file.

    Appending to `.gitignore` would dirty the very tree the user asked us never to
    touch, and a shared or protected checkout may forbid that edit outright. The
    repo-local exclude file does the same job untracked. Nothing is written when
    `out` lies outside the repo. Returns the pattern added, or None."""
    try:
        rel = out.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    pattern = f"/{rel.parts[0]}/"
    exclude = Path(git(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
                       root).strip())
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    if pattern in existing.splitlines():
        return None
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(("" if not existing or existing.endswith("\n") else "\n")
                 + "# ablate-ai-layer experiment artifacts\n" + pattern + "\n")
    return pattern


def hide_stripped(wt: Path, removed: list[str]) -> None:
    """Make the stripped files' absence invisible to git in this worktree.

    Without this, the stripped arm's `git status` lists every removed file as deleted
    (an agent reads that, and may restore it), and `git add -A` stages the deletion,
    so the captured diff labels its own arm and reads as the agent deleting the
    rules file. skip-worktree tells git to trust the index for these paths."""
    if removed:
        git(["update-index", "--skip-worktree", "--", *removed], wt)


def apply_variant(wt: Path, patch: Path) -> list[str]:
    """The variant arm: the layer stays whole and one patch changes it -- a shortened message,
    a reworded rule -- so the two arms differ by exactly that edit instead of by the whole layer.

    The patched paths are hidden the same way stripped ones are, for the same reason: otherwise
    `git add -A` stages the patch and the captured diff labels its own arm."""
    git(["apply", str(patch)], wt)
    paths = [l for l in git(["diff", "--name-only"], wt).splitlines() if l]
    # ponytail: skip-worktree hides an agent's OWN later edit to a patched file from the diff
    # too. Patch files the task has no reason to touch (hook scripts, rules), not its sources.
    hide_stripped(wt, paths)
    return paths


def snapshot(root: Path, sha: str, wt: Path, drop: list[str]) -> list[str]:
    """A one-commit repo holding `sha`'s tree minus `drop`, with no history and no refs.

    A linked worktree shares the source repo's objects and refs, so a stripped arm could
    `git show HEAD:AGENTS.md` and read the layer it was stripped of, and any arm could read
    other branches -- both measured in round 2 of hub#1618. The files come through a
    throwaway index, so the source repo's own index is never touched, and every file in the
    tree arrives (`git archive` would honour export-ignore)."""
    wt.mkdir(parents=True)
    env = dict(os.environ, GIT_INDEX_FILE=str(wt.parent / "snapshot.index"))
    subprocess.run(["git", "read-tree", sha], cwd=str(root), env=env, check=True)
    subprocess.run(["git", "--work-tree", str(wt), "checkout-index", "-a"], cwd=str(root),
                   env=env, check=True)
    Path(env["GIT_INDEX_FILE"]).unlink()
    dropped = [p for p in drop if (wt / p).is_file() or (wt / p).is_symlink()]
    for p in dropped:
        (wt / p).unlink()
    git(["init", "-q"], wt)
    git(["add", "-A", "-f"], wt)   # -f: every file came from the tree, ignored patterns or not
    git(["-c", "user.name=ablate", "-c", "user.email=ablate@localhost", "-c", "commit.gpgsign=false",
         "commit", "-q", "--no-verify", "-m", "snapshot"], wt)
    return dropped


def run_one(root: Path, sha: str, arm: str, index: int, prompt: str,
            targets: list[dict], model: str | None, runner: str | None,
            timeout: int, keep: bool, no_hooks: bool = False,
            allowed_tools: list[str] | None = None, hooks_only: str | None = None,
            variant_patch: Path | None = None, strip_surfaces: tuple = (),
            snapshots: list[Path] | None = None) -> dict:
    """One worktree, one agent session, one diff. Fresh worktree per run so runs
    never compound on each other."""
    # The arm is NOT in the path: the agent sees its working directory, and a run that named it in
    # its final message told the blind grader which arm it was in (hub AGENTS.md ablation, round 3).
    tmp = Path(tempfile.mkdtemp(prefix="ablate-run-"))
    wt = tmp / "repo"
    rec: dict = {"arm": arm, "index": index, "removed": [], "ok": False}
    started = time.time()
    try:
        plain_strip = arm == STRIPPED and not strip_surfaces and not variant_patch
        rec["removed"] = snapshot(root, sha, wt, [t["path"] for t in targets] if plain_strip else [])

        extra: dict = {}
        if strip_surfaces:
            # The repo layer stays whole in both arms; only the named outside surfaces move.
            if "caches" in strip_surfaces:
                (wt / ".claude").mkdir(exist_ok=True)
                (wt / ".claude" / "settings.json").write_text(
                    json.dumps(surface_project_settings(arm, snapshots or [])), encoding="utf-8")
                hide_stripped(wt, [".claude/settings.json"])
            if "user-claude-md" in strip_surfaces and arm == STRIPPED:
                extra["claudeMdExcludes"] = [str(claude_config_dir() / "CLAUDE.md")]
            rec["stripped_surfaces"] = list(strip_surfaces) if arm == STRIPPED else []
        elif arm == STRIPPED and variant_patch:
            rec["patched"] = apply_variant(wt, variant_patch)
        elif arm == STRIPPED:
            # The files were left out of the snapshot's only commit, so git never saw them.
            # Drop directories left empty, so nothing looks half-present.
            for t in sorted({str(Path(t["path"]).parent) for t in targets}, reverse=True):
                d = wt / t
                try:
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass

        memory = None if runner else scratch_memory(root, tmp)
        if memory is not None and "memory" in strip_surfaces and arm == STRIPPED:
            (memory / "MEMORY.md").unlink(missing_ok=True)
        cmd, use_shell = build_command(model, runner, no_hooks, allowed_tools, hooks_only, memory,
                                       extra)
        proc = subprocess.run(
            cmd, cwd=str(wt), input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, shell=use_shell)

        rec["exit_code"] = proc.returncode
        rec["stderr_tail"] = (proc.stderr or "")[-600:]
        try:
            payload = json.loads(proc.stdout)
            rec["agent_error"] = bool(payload.get("is_error"))
            rec["result_text"] = str(payload.get("result", ""))
            rec["cost_usd"] = payload.get("total_cost_usd")
            rec["num_turns"] = payload.get("num_turns")
            usage = payload.get("usage") or {}
            rec["output_tokens"] = usage.get("output_tokens")
            rec["session_id"] = payload.get("session_id")
            rec["cross_session_messages"] = cross_session_messages(rec["session_id"])
        except (json.JSONDecodeError, TypeError):
            # A non-Claude runner may print plain text. Not fatal: the diff is
            # the measurement, the transcript is only context for grading.
            rec["result_text"] = proc.stdout or ""
            rec["agent_error"] = proc.returncode != 0

        git(["add", "-A"], wt, check=False)
        rec["diff"] = git(["diff", "--cached"], wt, check=False)
        rec["files_changed"] = [
            l for l in git(["diff", "--cached", "--name-only"], wt, check=False).splitlines() if l]
        contaminated = bool(rec.get("cross_session_messages"))
        rec["ok"] = not rec.get("agent_error") and bool(rec["diff"].strip()) and not contaminated
        if contaminated:
            rec["note"] = (f"CONTAMINATED: {rec['cross_session_messages']} peer message(s) in the "
                           "transcript; discard this run")
        if not rec["diff"].strip():
            rec["note"] = "agent produced no file changes"
    except subprocess.TimeoutExpired:
        rec["note"] = f"timed out after {timeout}s"
    except Exception as exc:  # a broken run must not sink the other five
        rec["note"] = f"{type(exc).__name__}: {exc}"
    finally:
        rec["duration_s"] = round(time.time() - started, 1)
        if keep:
            rec["worktree_kept"] = str(wt)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return rec


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the control and stripped arms of an AI-layer ablation.")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--task-file", required=True,
                    help="file holding the probe-task prompt, reused verbatim by every run")
    ap.add_argument("--runs", type=int, default=2, help="runs PER ARM (default 2)")
    ap.add_argument("--scope", choices=["always", "all"], default="always")
    ap.add_argument("--model", default=None)
    ap.add_argument("--jobs", type=int, default=2, help="concurrent runs (default 2)")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per run")
    ap.add_argument("--runner", default=None,
                    help="shell command for a non-Claude agent; must read the prompt on stdin")
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-worktrees", action="store_true")
    ap.add_argument("--allowed-tools", nargs="+", default=None, metavar="RULE",
                    help="permission rules passed to claude --allowedTools in both arms, e.g. "
                         "'Bash(python3 -m pytest:*)' (ignored with --runner)")
    ap.add_argument("--no-hooks", action="store_true",
                    help="disable every Claude Code hook in both arms (ignored with --runner)")
    ap.add_argument("--hooks-only", default=None, metavar="SETTINGS_JSON",
                    help="run ONLY the hooks in this settings file, in both arms: project and "
                         "local settings and the plugins they enable are not loaded "
                         "(ignored with --runner)")
    ap.add_argument("--variant-patch", default=None, metavar="PATCH",
                    help="the second arm applies this patch to an intact layer instead of "
                         "stripping it, e.g. shortened hook text")
    ap.add_argument("--strip-surface", nargs="+", default=[], choices=SURFACES, metavar="SURFACE",
                    help="strip always-loaded surfaces that live OUTSIDE the repo in the second arm "
                         f"({', '.join(SURFACES)}); the repo layer stays whole in both arms")
    ap.add_argument("--cache-hook-match", default="cache-inject", metavar="TEXT",
                    help="with --strip-surface caches: the project SessionStart hooks whose command "
                         "contains TEXT are the caches (default: cache-inject)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    args = ap.parse_args()
    if args.no_hooks and args.hooks_only:
        ap.error("--no-hooks and --hooks-only both decide which hooks run; pass one")
    strip_surfaces = tuple(dict.fromkeys(args.strip_surface))
    if strip_surfaces and (args.hooks_only or args.variant_patch):
        ap.error("--strip-surface decides what the second arm lacks; it cannot combine with "
                 "--hooks-only or --variant-patch")
    if "caches" in strip_surfaces and args.no_hooks:
        ap.error("--strip-surface caches replays the caches through a hook; --no-hooks would silence "
                 "it in the control arm too (both arms already lose every other project hook)")
    hooks_only = str(Path(args.hooks_only).resolve()) if args.hooks_only else None
    patch = Path(args.variant_patch).resolve() if args.variant_patch else None

    try:
        root = repo_root(Path(args.repo).resolve())
    except Exception:
        print(f"Not a git repository: {Path(args.repo).resolve()}", file=sys.stderr)
        print("Ablation needs git: the experiment runs in worktrees.", file=sys.stderr)
        return 2

    prompt = Path(args.task_file).read_text(encoding="utf-8").strip()
    if not prompt:
        print("The task file is empty. The probe task decides whether this experiment "
              "can detect anything; it cannot be blank.", file=sys.stderr)
        return 2

    targets = [] if (patch or strip_surfaces) else layer_targets(root, args.scope)
    if patch:
        check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=str(root),
                               capture_output=True, text=True)
        if check.returncode:
            print(f"The variant patch does not apply to {root}: {check.stderr.strip()}",
                  file=sys.stderr)
            return 2
    elif not targets and not strip_surfaces:
        print(f"No {args.scope}-scope AI layer artifacts under {root}.")
        print("Nothing to ablate, so there is nothing to test.")
        return 1

    sha = head_sha(root)
    print(f"repo    {root}")
    print(f"base    {sha[:12]}" + ("   (WORKING TREE IS DIRTY: worktrees are built from "
                                   "HEAD, so uncommitted edits are NOT under test)"
                                   if is_dirty(root) else ""))
    if patch:
        print(f"variant {patch} applied in the second arm; the layer is NOT stripped")
    elif strip_surfaces:
        print(f"strip   {', '.join(strip_surfaces)} in the second arm; the repo layer stays whole in both")
        if "caches" in strip_surfaces:
            print(f"        caches = project SessionStart hooks matching {args.cache_hook_match!r}, "
                  "snapshotted once; every other project hook and plugin is off in both arms")
    else:
        print(f"scope   {args.scope} ({len(targets)} files stripped in the stripped arm)")
    for t in targets[:12]:
        print(f"          - {t['path']}")
    if len(targets) > 12:
        print(f"          ... and {len(targets) - 12} more")

    deps = build_dependencies(root, targets)
    if deps:
        print("\nBUILD DEPENDENCY WARNING - this repo's own source reads its AI layer:")
        for d in deps[:5]:
            print(f"          {d}")
        print("        The stripped arm may fail to build. That is a broken experiment,")
        print("        not an agent regression. Consider --scope always.")

    total = args.runs * 2
    print(f"\nplan    {args.runs} control + {args.runs} stripped = {total} agent runs, "
          f"{args.jobs} at a time")
    print(f"        model {args.model or '(session default)'}, timeout {args.timeout}s each")
    if args.runner:
        pass
    elif args.no_hooks:
        print("        hooks OFF in both arms (--no-hooks): hook-injected context is not under test")
    elif hooks_only:
        print(f"        hooks: ONLY {hooks_only} (+ user-level hooks) in both arms")
        print("        NOTE: --setting-sources user also stops the project's AGENTS.md/CLAUDE.md "
              "loading in BOTH arms (measured on Opus, claude 2.1.280)")
    elif "caches" in strip_surfaces:
        print("        hooks: only the cache snapshots, control arm only (+ user-level hooks)")
    else:
        print("        hooks ON: a hook that acts on the world fires in every run (see --no-hooks)")
    if args.allowed_tools and not args.runner:
        print(f"        allowed tools in both arms: {' '.join(args.allowed_tools)}")
    print("        your working tree is never touched; every run is a throwaway worktree")
    if args.dry_run:
        print("\n--dry-run: nothing executed.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else root / ".ablation" / stamp
    out.mkdir(parents=True, exist_ok=True)
    exclude_results(root, out)
    snapshots = (snapshot_cache_hooks(root, args.cache_hook_match, out / "cache-snapshots")
                 if "caches" in strip_surfaces else None)

    jobs = [(arm, i) for arm in (CONTROL, STRIPPED) for i in range(1, args.runs + 1)]
    print(f"\nrunning {total} sessions, writing to {out}\n")
    records: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(run_one, root, sha, arm, i, prompt, targets,
                        args.model, args.runner, args.timeout, args.keep_worktrees,
                        args.no_hooks, args.allowed_tools, hooks_only, patch,
                        strip_surfaces, snapshots): (arm, i)
            for arm, i in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            arm, i = futures[fut]
            rec = fut.result()
            records.append(rec)
            mark = "ok " if rec["ok"] else "!! "
            print(f"  {mark} {arm}/{i}  {rec['duration_s']}s  "
                  f"{len(rec.get('files_changed') or [])} files  {rec.get('note', '')}")

    records.sort(key=lambda r: (r["arm"], r["index"]))
    for rec in records:
        if rec.get("diff"):
            (out / f"{rec['arm']}-{rec['index']}.diff").write_text(rec["diff"], encoding="utf-8")

    summary = {
        "repo": str(root), "base_sha": sha, "scope": args.scope,
        "runs_per_arm": args.runs, "model": args.model,
        "prompt": prompt, "stripped_files": [t["path"] for t in targets],
        "variant_patch": str(patch) if patch else None, "hooks_only": hooks_only,
        "build_dependency_warnings": deps,
        "runs": [{k: v for k, v in r.items() if k != "diff"} for r in records],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "task.md").write_text(prompt, encoding="utf-8")

    good = [r for r in records if r["ok"]]
    costs = [r.get("cost_usd") for r in records if isinstance(r.get("cost_usd"), (int, float))]
    print(f"\n{len(good)}/{total} runs produced changes")
    if costs:
        print(f"cost    ${sum(costs):.2f} across {len(costs)} runs")
    print(f"results {out}")

    by_arm = {a: [r for r in good if r["arm"] == a] for a in (CONTROL, STRIPPED)}
    if not by_arm[CONTROL] or not by_arm[STRIPPED]:
        print("\nAt least one arm produced nothing usable. Do not grade this: an empty "
              "arm is a broken experiment, not a finding. Check stderr_tail in "
              "summary.json, then re-run.")
        return 1
    if len(good) < total:
        print("\nSome runs failed. Grade only the arms that completed, and say so in "
              "the report: unequal run counts weaken every comparison drawn from them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
