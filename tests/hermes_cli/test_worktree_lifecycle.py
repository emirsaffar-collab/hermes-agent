"""Tests for unified worktree lifecycle: FNV-1a port allocation, metadata, and seeding guardrails."""

from __future__ import annotations

import json
import socket
import stat
import subprocess
import sys
from pathlib import Path

from hermes_cli.worktree_ops import (
    WORKTREE_INCLUDE_MAX_FILE_BYTES,
    WORKTREE_META_FILENAME,
    WORKTREE_PORT_BASE,
    WORKTREE_PORT_SPAN,
    _setup_worktree,
    allocate_worktree_port,
    canonical_worktree_path,
    fnv1a_32,
    preferred_worktree_port,
    read_worktree_meta,
    resolve_worktree_git_dir,
    seed_worktree_includes,
)

# Shared 20-path parity vector mirrored verbatim in
# apps/desktop/electron/git-worktree-ops.test.ts.
FNV1A_PARITY_VECTORS: list[tuple[str, str, int, int]] = [
    ("/", "/", 705468254, 24254),
    ("/repo/.worktrees/feat-a", "/repo/.worktrees/feat-a", 3426949478, 24478),
    ("/repo/.worktrees/feat-a/", "/repo/.worktrees/feat-a", 3426949478, 24478),
    ("/repo//.worktrees/./feat-a", "/repo/.worktrees/feat-a", 3426949478, 24478),
    ("/repo/sub/../.worktrees/feat-b", "/repo/.worktrees/feat-b", 3410171859, 24859),
    ("/workspace/wt-p0-worktree-lifecycle", "/workspace/wt-p0-worktree-lifecycle", 295605217, 24217),
    ("/workspace/wt-p0-inline-diff-review", "/workspace/wt-p0-inline-diff-review", 3861790023, 24023),
    ("/home/dev/my project/.worktrees/branch-1", "/home/dev/my project/.worktrees/branch-1", 2343543345, 24345),
    ("/srv/app/.worktrees/subagent-01234567", "/srv/app/.worktrees/subagent-01234567", 873649153, 24153),
    ("/srv/app/.worktrees/subagent-89abcdef", "/srv/app/.worktrees/subagent-89abcdef", 4001957601, 24601),
    ("/opt/repos/hermes-agent/.worktrees/fix-utf8-åäö", "/opt/repos/hermes-agent/.worktrees/fix-utf8-åäö", 937307044, 24044),
    ("/opt/repos/hermes-agent/.worktrees/日本語-ブランチ", "/opt/repos/hermes-agent/.worktrees/日本語-ブランチ", 688011444, 24444),
    ("/var/lib/ci/workspace-alpha/.worktrees/pr-42", "/var/lib/ci/workspace-alpha/.worktrees/pr-42", 2059532272, 24272),
    ("/var/lib/ci/workspace-beta/.worktrees/pr-42", "/var/lib/ci/workspace-beta/.worktrees/pr-42", 376157556, 24556),
    ("C:\\Users\\dev\\repo\\.worktrees\\feat-win", "C:/Users/dev/repo/.worktrees/feat-win", 4281742425, 24425),
    ("c:/Users/dev/repo/.worktrees/feat-win/", "C:/Users/dev/repo/.worktrees/feat-win", 4281742425, 24425),
    ("D:\\work\\a\\..\\b\\.worktrees\\task-9", "D:/work/b/.worktrees/task-9", 2868306633, 24633),
    ("/a/b/c/d/e/f/g/h/i/j/.worktrees/deep-tree", "/a/b/c/d/e/f/g/h/i/j/.worktrees/deep-tree", 285225627, 24627),
    ("/repo/.worktrees/feat_with_underscores.and.dots-123", "/repo/.worktrees/feat_with_underscores.and.dots-123", 564407567, 24567),
    ("/repo/.worktrees/edge-case@2026+build#7", "/repo/.worktrees/edge-case@2026+build#7", 4151808203, 24203),
]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo_with_worktree(tmp_path: Path, name: str = "wt-a") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    (repo / ".env.example").write_text("API_KEY=changeme\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".env\n.env.*\n!.env.example\n*.local.json\nsecrets/\nnode_modules/\n.venv/\ntarget/\n.next/\ndist/\nbig.bin\n",
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "initial"], repo)
    wt = repo / ".worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-q", str(wt), "-b", f"feat/{name}"], repo)
    return repo, wt


def test_fnv1a_20_path_parity_vector():
    assert len(FNV1A_PARITY_VECTORS) == 20
    for raw_path, expected_canonical, expected_hash, expected_port in FNV1A_PARITY_VECTORS:
        canonical = canonical_worktree_path(raw_path)
        assert canonical == expected_canonical, f"canonical mismatch for {raw_path!r}"
        assert fnv1a_32(canonical) == expected_hash, f"hash mismatch for {raw_path!r}"
        port = preferred_worktree_port(raw_path)
        assert port == expected_port, f"port mismatch for {raw_path!r}"
        assert WORKTREE_PORT_BASE <= port < WORKTREE_PORT_BASE + WORKTREE_PORT_SPAN


def test_allocate_worktree_port_persists_metadata_and_reuses(tmp_path: Path):
    _, wt = _init_repo_with_worktree(tmp_path, "meta-test")
    expected = preferred_worktree_port(str(wt))
    port = allocate_worktree_port(str(wt), probe_bind=False, branch="feat/meta-test", base_ref="main")
    assert port == expected

    git_dir = resolve_worktree_git_dir(wt)
    assert git_dir is not None
    meta_file = git_dir / WORKTREE_META_FILENAME
    assert meta_file.is_file()

    meta = read_worktree_meta(wt)
    assert meta is not None
    assert meta["port"] == expected
    assert meta["branch"] == "feat/meta-test"
    assert meta["baseRef"] == "main"
    assert meta["worktreePath"] == canonical_worktree_path(str(wt))

    # Re-allocating reuses the persisted port cleanly even if probe_bind=False.
    assert allocate_worktree_port(str(wt), probe_bind=False) == expected


def test_allocate_worktree_port_linear_collision_fallback(tmp_path: Path):
    _, wt = _init_repo_with_worktree(tmp_path, "collision")
    preferred = preferred_worktree_port(str(wt))

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", preferred))
        blocker.listen(1)

        allocated = allocate_worktree_port(str(wt), probe_bind=True)
        expected_next = WORKTREE_PORT_BASE + ((preferred - WORKTREE_PORT_BASE + 1) % WORKTREE_PORT_SPAN)
        assert allocated == expected_next

        meta = read_worktree_meta(wt)
        assert meta is not None
        assert meta["port"] == expected_next
    finally:
        blocker.close()


def test_seed_worktree_includes_defaults_to_env_with_0600_and_no_overwrite(tmp_path: Path):
    repo, wt = _init_repo_with_worktree(tmp_path, "default-env")
    (repo / ".env").write_text("SECRET=alpha\n", encoding="utf-8")
    (repo / ".env.local").write_text("LOCAL=beta\n", encoding="utf-8")
    (repo / "ignored.local.json").write_text('{"x": 1}\n', encoding="utf-8")
    # Pre-create .env.local in worktree to verify never-overwrite guarantee.
    (wt / ".env.local").write_text("EXISTING=keep\n", encoding="utf-8")

    seeded = seed_worktree_includes(repo, wt)
    assert seeded == [".env"]
    assert (wt / ".env").read_text(encoding="utf-8") == "SECRET=alpha\n"
    assert (wt / ".env.local").read_text(encoding="utf-8") == "EXISTING=keep\n"
    assert not (wt / "ignored.local.json").exists()

    if sys.platform != "win32":
        mode = stat.S_IMODE((wt / ".env").stat().st_mode)
        assert mode == 0o600


def test_seed_worktree_includes_custom_globs_negation_and_dir_symlink(tmp_path: Path):
    repo, wt = _init_repo_with_worktree(tmp_path, "custom-inc")
    (repo / ".worktreeinclude").write_text(
        "# custom worktree includes\n"
        ".env*\n"
        "*.local.json\n"
        "secrets/**\n"
        "!secrets/skip.txt\n"
        ".venv\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text("A=1\n", encoding="utf-8")
    (repo / "config.local.json").write_text('{"ok": true}\n', encoding="utf-8")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "token.txt").write_text("tok\n", encoding="utf-8")
    (repo / "secrets" / "skip.txt").write_text("nope\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker.txt").write_text("venv\n", encoding="utf-8")

    seeded = seed_worktree_includes(repo, wt)
    assert ".env" in seeded
    assert "config.local.json" in seeded
    assert "secrets/token.txt" in seeded
    assert "secrets/skip.txt" not in seeded
    assert ".venv" in seeded
    assert (wt / ".venv").is_symlink() or (sys.platform == "win32" and (wt / ".venv").is_dir())
    assert (wt / "secrets" / "token.txt").read_text(encoding="utf-8") == "tok\n"
    assert not (wt / "secrets" / "skip.txt").exists()


def test_seed_worktree_includes_security_and_size_guardrails(tmp_path: Path):
    repo, wt = _init_repo_with_worktree(tmp_path, "guardrails")

    # 1. Symlink escaping main_root via default .env* pattern
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("TOP_SECRET=1\n", encoding="utf-8")
    (repo / ".env.escape").symlink_to(outside)

    # 2. File > 10 MB
    huge = repo / ".env.huge"
    with huge.open("wb") as fh:
        fh.seek(WORKTREE_INCLUDE_MAX_FILE_BYTES + 1)
        fh.write(b"x")

    # 3. Heavy directories skipped by default even if they contain .env
    for heavy in ("node_modules", ".venv", "target", ".next", "dist"):
        d = repo / heavy
        d.mkdir()
        (d / ".env").write_text(f"HEAVY={heavy}\n", encoding="utf-8")

    # 4. Valid small .env
    (repo / ".env").write_text("OK=1\n", encoding="utf-8")

    seeded = seed_worktree_includes(repo, wt)
    assert seeded == [".env"]
    assert not (wt / ".env.escape").exists()
    assert not (wt / ".env.huge").exists()
    for heavy in ("node_modules", ".venv", "target", ".next", "dist"):
        assert not (wt / heavy / ".env").exists()


def test_seed_worktree_includes_allows_heavy_dir_when_explicitly_named(tmp_path: Path):
    repo, wt = _init_repo_with_worktree(tmp_path, "explicit-heavy")
    (repo / ".worktreeinclude").write_text("dist/.env\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / ".env").write_text("DIST_ENV=1\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / ".env").write_text("NM_ENV=1\n", encoding="utf-8")

    seeded = seed_worktree_includes(repo, wt)
    assert seeded == ["dist/.env"]
    assert (wt / "dist" / ".env").read_text(encoding="utf-8") == "DIST_ENV=1\n"
    assert not (wt / "node_modules" / ".env").exists()


def test_setup_worktree_returns_port_and_seeded_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    (repo / ".env").write_text("TOKEN=123\n", encoding="utf-8")

    info = _setup_worktree(repo_root=str(repo), sync_base=False, name="setup-wt")
    assert info is not None
    assert WORKTREE_PORT_BASE <= info["port"] < WORKTREE_PORT_BASE + WORKTREE_PORT_SPAN
    assert info["seeded_files"] == [".env"]
    wt = Path(info["path"])
    assert (wt / ".env").read_text(encoding="utf-8") == "TOKEN=123\n"
    meta = read_worktree_meta(wt)
    assert meta is not None
    assert meta["port"] == info["port"]
    git_dir = resolve_worktree_git_dir(wt)
    assert git_dir is not None
    assert json.loads((git_dir / WORKTREE_META_FILENAME).read_text(encoding="utf-8"))["port"] == info["port"]
