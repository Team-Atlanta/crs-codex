"""
crs-codex patcher module.

Thin launcher that delegates vulnerability fixing to a swappable AI agent.
The agent (selected via CRS_AGENT env var) handles: bug analysis, code editing,
building (via libCRS), testing (via libCRS), iteration, and final patch
submission (writing .diff to /patches/).

To add a new agent, create a module in agents/ implementing setup() and run().
"""

import importlib
import inspect
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from libCRS.base import DataType
from libCRS.cli.main import init_crs_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("patcher")

# --- Configuration (from oss-crs framework environment variables) ---

SNAPSHOT_IMAGE = os.environ.get("OSS_CRS_SNAPSHOT_IMAGE", "")
TARGET = os.environ.get("OSS_CRS_TARGET", "")
HARNESS = os.environ.get("OSS_CRS_TARGET_HARNESS", "")
LANGUAGE = os.environ.get("FUZZING_LANGUAGE", "c")
SANITIZER = os.environ.get("SANITIZER", "address")
LLM_API_URL = os.environ.get("OSS_CRS_LLM_API_URL", "")
LLM_API_KEY = os.environ.get("OSS_CRS_LLM_API_KEY", "")

# Builder sidecar module name (must match a run_snapshot module in crs.yaml)
BUILDER_MODULE = os.environ.get("BUILDER_MODULE", "inc-builder-asan")
SUBMISSION_FLUSH_WAIT_SECS = int(os.environ.get("SUBMISSION_FLUSH_WAIT_SECS", "12"))

# Agent selection
CRS_AGENT = os.environ.get("CRS_AGENT", "codex")

# No evidence truncation in patcher — the agent manages its own context window.

# Framework directories
WORK_DIR = Path("/work")
PATCHES_DIR = Path("/patches")
POV_DIR = WORK_DIR / "povs"
DIFF_DIR = WORK_DIR / "diffs"
BUG_CANDIDATE_DIR = WORK_DIR / "bug-candidates"

# CRS utils instance (initialized in main())
crs = None


# --- Common infrastructure ---


def _reset_source(source_dir: Path) -> None:
    """Reset source directory to HEAD, cleaning up stale lock files."""
    for lock_file in source_dir.glob(".git/**/*.lock"):
        logger.warning("Removing stale lock file: %s", lock_file)
        lock_file.unlink()

    reset_proc = subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=source_dir, capture_output=True, timeout=60,
    )
    clean_proc = subprocess.run(
        ["git", "clean", "-fd"],
        cwd=source_dir, capture_output=True, timeout=60,
    )
    if reset_proc.returncode != 0:
        stderr = reset_proc.stderr.decode(errors="replace") if isinstance(reset_proc.stderr, bytes) else str(reset_proc.stderr)
        raise RuntimeError(f"git reset failed: {stderr.strip()}")
    if clean_proc.returncode != 0:
        stderr = clean_proc.stderr.decode(errors="replace") if isinstance(clean_proc.stderr, bytes) else str(clean_proc.stderr)
        raise RuntimeError(f"git clean failed: {stderr.strip()}")


def _snapshot_patch_state() -> dict[str, tuple[int, int]]:
    """Capture patch file state by name -> (mtime_ns, size)."""
    state: dict[str, tuple[int, int]] = {}
    for p in PATCHES_DIR.glob("*.diff"):
        try:
            st = p.stat()
        except OSError:
            continue
        state[p.name] = (st.st_mtime_ns, st.st_size)
    return state


def setup_source() -> Path | None:
    """Download source code and locate the project source directory."""
    # Ensure safe.directory is set system-wide so git works regardless of
    # file ownership (downloaded source may have different uid).
    safe_dir_proc = subprocess.run(
        ["git", "config", "--system", "--add", "safe.directory", "*"],
        capture_output=True,
    )
    if safe_dir_proc.returncode != 0:
        fallback_proc = subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", "*"],
            capture_output=True,
        )
        if fallback_proc.returncode != 0:
            logger.warning(
                "Failed to configure git safe.directory in both --system and --global scopes"
            )

    source_dir = WORK_DIR / "src"
    source_dir.mkdir(parents=True, exist_ok=True)

    try:
        crs.download_build_output("src", source_dir)
    except Exception as e:
        logger.error("Failed to download source: %s", e)
        return None

    # Locate the project directory: try "repo/" first, then any subdir with .git.
    project_dir = source_dir / "repo"
    if not project_dir.exists():
        for d in source_dir.iterdir():
            if d.is_dir() and (d / ".git").exists():
                project_dir = d
                break

    # If still no project_dir, use "repo/" or first subdir as fallback.
    if not project_dir.exists():
        subdirs = sorted(
            (d for d in source_dir.iterdir() if d.is_dir()),
            key=lambda p: p.name,
        )
        if subdirs:
            project_dir = subdirs[0]
        else:
            logger.error("No project directory found in %s", source_dir)
            return None

    # Initialize a git repo if the source doesn't have one.
    # The agent needs git to generate patches (git add -A && git diff --cached).
    if not (project_dir / ".git").exists():
        logger.info("No .git found in %s, initializing git repo", project_dir)
        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, timeout=60)
        subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True, timeout=60)
        commit_proc = subprocess.run(
            [
                "git",
                "-c",
                "user.name=crs-codex",
                "-c",
                "user.email=crs-codex@local",
                "commit",
                "-m",
                "initial source",
            ],
            cwd=project_dir, capture_output=True, timeout=60,
        )
        if commit_proc.returncode != 0:
            stderr = (
                commit_proc.stderr.decode(errors="replace")
                if isinstance(commit_proc.stderr, bytes)
                else str(commit_proc.stderr)
            )
            logger.error("Failed to create initial commit: %s", stderr.strip())
            return None

    return project_dir


def wait_for_builder() -> bool:
    """Fail-fast DNS check for the builder sidecar.

    Full health polling is handled internally by ``crs.run_pov()`` /
    ``crs.apply_patch_build()`` (via ``_wait_for_builder_health``), so we
    only verify DNS resolution here to catch configuration errors early.
    """
    try:
        domain = crs.get_service_domain(BUILDER_MODULE)
        logger.info("Builder sidecar '%s' resolved to %s", BUILDER_MODULE, domain)
        return True
    except RuntimeError as e:
        logger.error("Failed to resolve builder domain for '%s': %s",
                      BUILDER_MODULE, e)
        return False


def load_agent(agent_name: str):
    """Dynamically load an agent module from the agents package."""
    module_name = f"agents.{agent_name}"
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        logger.error("Failed to load agent '%s': %s", agent_name, e)
        sys.exit(1)


def process_inputs(
    pov_paths: list[Path],
    source_dir: Path,
    agent,
    bug_candidate_paths: list[Path],
    ref_diff: str | None = None,
) -> bool:
    """Process available inputs in a single agent session.

    Inputs can include any combination of:
    - POV files
    - Bug-candidate files (e.g., SARIF or other static report formats)
    - Reference diff (delta context)
    """
    try:
        _reset_source(source_dir)
    except Exception as e:
        logger.error("Failed to reset source before agent run: %s", e)
        return False

    agent_work_dir = WORK_DIR / "agent"
    agent_work_dir.mkdir(parents=True, exist_ok=True)

    existing_patches = _snapshot_patch_state()
    run_result = False

    run_sig = inspect.signature(agent.run)
    if "bug_candidates" in run_sig.parameters:
        run_kwargs = {
            "source_dir": source_dir,
            "povs": pov_paths,
            "bug_candidates": bug_candidate_paths,
            "harness": HARNESS,
            "patches_dir": PATCHES_DIR,
            "work_dir": agent_work_dir,
        }
        optional_kwargs = {
            "language": LANGUAGE,
            "sanitizer": SANITIZER,
            "builder": BUILDER_MODULE,
            "ref_diff": ref_diff,
        }
        for key, value in optional_kwargs.items():
            if key in run_sig.parameters:
                run_kwargs[key] = value
        run_result = bool(
            agent.run(**run_kwargs)
        )
    else:
        # Backward compatibility for custom agents using the old interface.
        old_kwargs = {}
        if "language" in run_sig.parameters:
            old_kwargs["language"] = LANGUAGE
        if "sanitizer" in run_sig.parameters:
            old_kwargs["sanitizer"] = SANITIZER
        if "builder" in run_sig.parameters:
            old_kwargs["builder"] = BUILDER_MODULE
        if "ref_diff" in run_sig.parameters:
            old_kwargs["ref_diff"] = ref_diff
        run_result = bool(
            agent.run(
                source_dir,
                pov_paths,
                HARNESS,
                PATCHES_DIR,
                agent_work_dir,
                **old_kwargs,
            )
        )

    try:
        _reset_source(source_dir)
    except Exception as e:
        logger.error("Failed to reset source after agent run: %s", e)
        return False

    current_patches = _snapshot_patch_state()
    changed_patch_names = sorted(
        name
        for name, state in current_patches.items()
        if existing_patches.get(name) != state
    )
    if changed_patch_names:
        if len(changed_patch_names) > 1:
            logger.warning(
                "Multiple changed patch files detected (%d): %s. Each file in %s is auto-submitted.",
                len(changed_patch_names), changed_patch_names, PATCHES_DIR,
            )
        logger.warning(
            "Submission is final: detected patch file(s) %s in %s. Submitted patches cannot be edited or resubmitted.",
            changed_patch_names, PATCHES_DIR,
        )
        logger.info("Updated/new patch produced: %s", changed_patch_names)
        return True

    if run_result:
        logger.warning(
            "Agent reported success but no new patch file was created in %s",
            PATCHES_DIR,
        )
    logger.warning("Agent did not produce a patch")
    return False


# --- Main loop ---


def main():
    logger.info(
        "Starting patcher: target=%s harness=%s agent=%s snapshot=%s",
        TARGET, HARNESS, CRS_AGENT, SNAPSHOT_IMAGE or "(none)",
    )

    if not SNAPSHOT_IMAGE:
        logger.error("OSS_CRS_SNAPSHOT_IMAGE is not set.")
        logger.error("Declare snapshot: true in target_build_phase and run_snapshot: true in crs_run_phase (crs.yaml).")
        sys.exit(1)

    global crs
    crs = init_crs_utils()

    # Register patch submission directory (daemon thread — blocks forever).
    PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(
        target=crs.register_submit_dir,
        args=(DataType.PATCH, PATCHES_DIR),
        daemon=True,
    ).start()
    logger.info("Patch submission watcher started")

    # Fetch POV files (one-shot — all POVs are present before container starts).
    pov_files_fetched = crs.fetch(DataType.POV, POV_DIR)
    logger.info("Fetched %d POV file(s) into %s", len(pov_files_fetched), POV_DIR)

    # Fetch diff files for delta mode (one-shot, optional).
    try:
        diff_files_fetched = crs.fetch(DataType.DIFF, DIFF_DIR)
        if diff_files_fetched:
            logger.info("Fetched %d diff file(s) into %s", len(diff_files_fetched), DIFF_DIR)
    except Exception as e:
        logger.warning("Diff fetch failed: %s — delta mode diffs unavailable", e)

    # Fetch bug-candidate reports (one-shot, optional).
    try:
        bug_files_fetched = crs.fetch(DataType.BUG_CANDIDATE, BUG_CANDIDATE_DIR)
        if bug_files_fetched:
            logger.info(
                "Fetched %d bug-candidate file(s) into %s",
                len(bug_files_fetched),
                BUG_CANDIDATE_DIR,
            )
    except Exception as e:
        logger.warning("Bug-candidate fetch failed: %s — static findings unavailable", e)

    # Register Codex home (including logs) as shared dir for post-run analysis.
    # register-shared-dir creates a symlink, so the path must not exist beforehand.
    # Preserve existing Codex home and restore it if registration fails.
    codex_home = Path.home() / ".codex"
    codex_home_backup = codex_home.with_name(".codex.pre-crs-backup")
    had_existing_codex_home = codex_home.exists() or codex_home.is_symlink()
    if codex_home_backup.exists() or codex_home_backup.is_symlink():
        if codex_home_backup.is_symlink() or codex_home_backup.is_file():
            codex_home_backup.unlink()
        else:
            shutil.rmtree(codex_home_backup)
    if had_existing_codex_home:
        codex_home.rename(codex_home_backup)

    try:
        crs.register_shared_dir(codex_home, "codex-home")
        logger.info("Codex home shared at %s", codex_home)
        if codex_home_backup.exists() or codex_home_backup.is_symlink():
            logger.info("Preserved previous Codex home backup at %s", codex_home_backup)
    except Exception as e:
        logger.warning("Failed to register codex-home shared dir: %s", e)
        if codex_home.exists() or codex_home.is_symlink():
            if codex_home.is_symlink() or codex_home.is_file():
                codex_home.unlink()
            else:
                shutil.rmtree(codex_home)
        if codex_home_backup.exists() or codex_home_backup.is_symlink():
            codex_home_backup.rename(codex_home)
            logger.info("Restored previous Codex home from backup")
        else:
            codex_home.mkdir(parents=True, exist_ok=True)

    source_dir = setup_source()
    if source_dir is None:
        logger.error("Failed to set up source directory")
        sys.exit(1)

    logger.info("Source directory: %s", source_dir)

    # Load and configure agent
    agent = load_agent(CRS_AGENT)
    agent.setup(source_dir, {
        "llm_api_url": LLM_API_URL,
        "llm_api_key": LLM_API_KEY,
        "codex_home": str(codex_home),
    })

    # Inputs were fetched above — scan available artifacts.
    pov_files = sorted(f for f in POV_DIR.rglob("*") if f.is_file() and not f.name.startswith("."))
    bug_candidate_files = sorted(
        f for f in BUG_CANDIDATE_DIR.rglob("*") if f.is_file() and not f.name.startswith(".")
    )

    if pov_files:
        logger.info("Found %d POV(s): %s", len(pov_files), [p.name for p in pov_files])
    else:
        logger.info("No POV files found in %s", POV_DIR)

    if bug_candidate_files:
        logger.info(
            "Found %d bug-candidate file(s): %s",
            len(bug_candidate_files),
            [p.name for p in bug_candidate_files],
        )
    else:
        logger.info("No bug-candidate files found in %s", BUG_CANDIDATE_DIR)

    # Read reference diff if available (delta mode).
    ref_diff = None
    ref_diff_path = DIFF_DIR / "ref.diff"
    if DIFF_DIR.exists() and ref_diff_path.is_file():
        ref_diff = ref_diff_path.read_text()
        logger.info("Reference diff found (%d chars)", len(ref_diff))
    else:
        logger.info("No reference diff found in %s", DIFF_DIR)

    if not pov_files and not bug_candidate_files and not ref_diff:
        logger.warning(
            "No actionable input found (POV, bug-candidate, or diff). Nothing to do."
        )
        sys.exit(0)

    if not wait_for_builder():
        logger.warning(
            "Builder sidecar unavailable at startup. Continuing run; "
            "agent may still fail build/test commands."
        )

    if process_inputs(
        pov_files,
        source_dir,
        agent,
        bug_candidate_files,
        ref_diff=ref_diff,
    ):
        # Wait for the submission daemon to flush (batch_time=10s) before exiting.
        logger.info("Patch submitted. Waiting for daemon to flush...")
        time.sleep(SUBMISSION_FLUSH_WAIT_SECS)


if __name__ == "__main__":
    main()
