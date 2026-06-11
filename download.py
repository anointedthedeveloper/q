"""
ALOC Question Downloader — commits to GitHub after every round
--------------------------------------------------------------
- Loads API keys from accounts.json, falls back to .env
- Downloads all 24 subjects in parallel
- Every round that finds new questions → git commit + push immediately
- You will see 1000+ commits in the repo as it runs
- Run locally:  python download.py
- In CI:        triggered by GitHub Actions workflow
"""

import requests
import json
import os
import time
import threading
import subprocess
import base64
import ctypes
import logging
from datetime import datetime
from dotenv import dotenv_values

# ── Windows sleep/shutdown prevention ────────────────────────────────────────

def prevent_sleep():
    try:
        ES = 0x80000000 | 0x00000001 | 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(ES)
    except Exception:
        pass

def allow_sleep():
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass

def prevent_shutdown():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        ctypes.windll.user32.ShutdownBlockReasonCreate(hwnd, "Download in progress")
    except Exception:
        pass

def allow_shutdown():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        ctypes.windll.user32.ShutdownBlockReasonDestroy(hwnd)
    except Exception:
        pass

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.json")
ENV_FILE      = os.path.join(SCRIPT_DIR, ".env")

_env = dotenv_values(ENV_FILE)

# ── Error logger ──────────────────────────────────────────────────────────────

LOG_FILE = os.path.join(SCRIPT_DIR, "errors.log")

def _setup_logger():
    logger = logging.getLogger("downloader")
    logger.setLevel(logging.DEBUG)

    # File handler — all errors go here with timestamps
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Console handler — info and above to stdout
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = _setup_logger()

def log_error(msg, exc=None):
    """Log an error to both console and errors.log."""
    full = f"{msg} — {exc}" if exc else msg
    log.error(full)

def log_info(msg):
    log.info(msg)

def log_warning(msg):
    log.warning(msg)


def load_api_keys():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                accounts = json.load(f)
            seen, keys = set(), []
            for acc in accounts:
                k = acc.get("api_key", "").strip()
                if k and k not in seen:
                    keys.append(k)
                    seen.add(k)
            if keys:
                log_info(f"[config] {len(keys)} API keys from accounts.json")
                return keys
        except Exception as e:
            log_error("[config] accounts.json error — falling back to .env", e)
    keys = [v for k, v in sorted(_env.items()) if k.startswith("API_KEY_")]
    log_info(f"[config] {len(keys)} API keys from .env")
    return keys


API_KEYS = load_api_keys()
if not API_KEYS:
    raise RuntimeError("No API keys found — run signup_bot.py first")

HEADERS_LIST = [{"Accept": "application/json", "AccessToken": k} for k in API_KEYS]

# Git config
IN_ACTIONS    = os.environ.get("GITHUB_ACTIONS") == "true"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH") or _env.get("GITHUB_BRANCH", "master")

BASE_URL      = "https://questions.aloc.com.ng/api/v2/q/5"
OUTPUT_DIR    = os.path.join(SCRIPT_DIR, "questions")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "progress.json")

SUBJECTS = [
    "mathematics", "english", "chemistry", "physics", "biology",
    "economics", "government", "englishlit", "geography",
    "commerce", "accounting", "civiledu", "agricultural-science",
    "crk", "irk", "further-mathematics", "computer-studies",
    "home-economics", "yoruba", "igbo", "hausa",
    "currentaffairs", "history", "insurance",
]

MAX_ROUNDS        = 500
NO_NEW_LIMIT      = 40    # consecutive empty rounds before declaring done
RATE_LIMIT        = 58    # requests per key per minute
DELAY             = 0.05  # seconds between rounds

REQUESTS_PER_ROUND = max(1, min(30, len(API_KEYS) // len(SUBJECTS)))

log_info(f"[config] {REQUESTS_PER_ROUND} parallel req/round per subject "
         f"| {len(API_KEYS)} keys | {len(API_KEYS) * RATE_LIMIT} req/min capacity")

# ── Rate-limit-aware key picker ───────────────────────────────────────────────

key_lock    = threading.Lock()
current_key = 0
key_counts  = [0] * len(API_KEYS)
key_reset   = [time.time()] * len(API_KEYS)


def pick_key():
    global current_key
    with key_lock:
        now = time.time()
        for i in range(len(API_KEYS)):
            if now - key_reset[i] >= 60:
                key_counts[i] = 0
                key_reset[i] = now
        for i in range(len(API_KEYS)):
            idx = (current_key + i) % len(API_KEYS)
            if key_counts[idx] < RATE_LIMIT:
                key_counts[idx] += 1
                current_key = (idx + 1) % len(API_KEYS)
                return idx, HEADERS_LIST[idx]
        wait = 60 - (time.time() - min(key_reset))
        log_info(f"  [rate limit] waiting {wait:.0f}s")
        time.sleep(max(wait, 1))
        for i in range(len(API_KEYS)):
            key_counts[i] = 0
            key_reset[i] = time.time()
        key_counts[0] += 1
        return 0, HEADERS_LIST[0]

# ── HTTP fetch ────────────────────────────────────────────────────────────────

def single_request(subject):
    idx, headers = pick_key()
    for attempt in range(6):
        try:
            r = requests.get(BASE_URL, headers=headers,
                             params={"subject": subject}, timeout=30)
            if r.status_code == 429:
                with key_lock:
                    key_counts[idx] = RATE_LIMIT
                return []
            if r.status_code == 200:
                data = r.json().get("data", [])
                if isinstance(data, dict):
                    data = [data]
                return [q for q in data if q.get("id") and q.get("question")]
            if r.status_code >= 500:
                log_error(f"[{subject}] HTTP {r.status_code} on attempt {attempt+1}/6")
                time.sleep(min(2 ** attempt, 60))
                continue
        except Exception as e:
            wait = min(2 ** attempt, 60)
            log_error(f"[{subject}] request error attempt {attempt+1}/6 — retry in {wait}s", e)
            time.sleep(wait)
    return []


def parallel_fetch(subject):
    results = [[] for _ in range(REQUESTS_PER_ROUND)]

    def fetch(i):
        results[i] = single_request(subject)

    threads = [threading.Thread(target=fetch, args=(i,)) for i in range(REQUESTS_PER_ROUND)]
    for t in threads: t.start()
    for t in threads: t.join()

    merged = {}
    for batch in results:
        for q in batch:
            merged[q["id"]] = q
    return list(merged.values())

# ── File I/O ──────────────────────────────────────────────────────────────────

file_locks     = {}
file_lock_meta = threading.Lock()
progress_lock  = threading.Lock()


def get_file_lock(subject):
    with file_lock_meta:
        if subject not in file_locks:
            file_locks[subject] = threading.Lock()
        return file_locks[subject]


def load_subject(subject):
    path = os.path.join(OUTPUT_DIR, f"{subject}.json")
    if not os.path.exists(path):
        return {}, set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        valid = [q for q in data if q.get("id") and q.get("question", "").strip()]
        removed = len(data) - len(valid)
        if removed:
            log_warning(f"[{subject}] removed {removed} corrupt entries on load")
        seen = {q["id"]: q for q in valid}
        return seen, set(seen.keys())
    except Exception as e:
        log_error(f"[{subject}] file corrupt — starting fresh", e)
        try:
            os.replace(path, path + ".corrupt")
        except Exception as re:
            log_error(f"[{subject}] could not rename corrupt file", re)
        return {}, set()


def save_subject(subject, seen_dict):
    path = os.path.join(OUTPUT_DIR, f"{subject}.json")
    tmp  = path + ".tmp"
    merged = sorted(seen_dict.values(), key=lambda q: int(q.get("id", 0)))
    with get_file_lock(subject):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_progress(progress):
    with progress_lock:
        tmp = PROGRESS_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(progress, f, indent=2)
            os.replace(tmp, PROGRESS_FILE)
        except Exception as e:
            log_error("[progress] save failed", e)

# ── Git commit + push ─────────────────────────────────────────────────────────

git_lock = threading.Lock()


def git_run(args):
    """Run a git command, return (stdout, stderr, returncode)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def git_commit_and_push(subject, new_count, total):
    """
    Stage questions/{subject}.json + progress.json and push a commit.
    Serialised with git_lock so parallel subject threads don't race.
    """
    with git_lock:
        # pull first to avoid diverged history
        git_run(["pull", "--rebase", "--autostash", "origin", GITHUB_BRANCH])

        # stage the changed files
        git_run(["add",
                 os.path.join("questions", f"{subject}.json"),
                 "progress.json"])

        # check there's actually something staged
        _, _, rc = git_run(["diff", "--cached", "--quiet"])
        if rc == 0:
            return  # nothing changed — skip

        msg = f"[{subject}] +{new_count} questions (total {total})"
        _, err, rc = git_run(["commit", "-m", msg])
        if rc != 0:
            log_error(f"[git] commit failed: {err}")
            return

        # push with retry
        for attempt in range(3):
            _, err, rc = git_run(["push", "origin", GITHUB_BRANCH])
            if rc == 0:
                log_info(f"  [git] committed: {msg}")
                return
            # another thread may have pushed first — rebase and retry
            git_run(["pull", "--rebase", "--autostash", "origin", GITHUB_BRANCH])
            time.sleep(2 * (attempt + 1))

        log_error(f"[git] push failed after 3 attempts: {err}")

# ── Verification pass ─────────────────────────────────────────────────────────

# Subjects we know have very few questions — don't require a minimum count
KNOWN_SPARSE = {"irk", "currentaffairs", "history"}

# Minimum plausible question count for a subject to be considered truly done.
# Subjects below this threshold will NOT be marked done even if the API stops
# returning new items — they need more rounds.
MIN_QUESTIONS_TO_MARK_DONE = 200


def verify_complete(subject, seen_ids, total):
    """
    Returns True only if:
    1. The subject is in KNOWN_SPARSE  OR  has at least MIN_QUESTIONS_TO_MARK_DONE
    2. Five consecutive parallel fetches return zero new IDs
    """
    if subject not in KNOWN_SPARSE and total < MIN_QUESTIONS_TO_MARK_DONE:
        log_warning(f"[{subject}] only {total} questions — too few to mark done, continuing...")
        return False

    for attempt in range(5):
        data = parallel_fetch(subject)
        if any(q["id"] not in seen_ids for q in data):
            log_info(f"  [{subject}] verify attempt {attempt+1}: found new questions — not done yet")
            return False
    return True

# ── Subject worker ────────────────────────────────────────────────────────────

def process_subject(subject, progress):
    try:
        _run_subject(subject, progress)
    except Exception as e:
        log_error(f"[{subject}] FATAL error", e)


def _run_subject(subject, progress):
    log_info(f"\n[{subject}] starting")
    seen_dict, seen_ids = load_subject(subject)
    total = len(seen_dict)

    subj_prog = progress.get(subject, {})
    is_done   = subj_prog.get("done", False)
    is_sparse = subject in KNOWN_SPARSE

    # Skip only if marked done AND has enough questions (or is a known sparse subject)
    if is_done and (is_sparse or total >= MIN_QUESTIONS_TO_MARK_DONE):
        log_info(f"[{subject}] already complete ({total} questions) — skipping")
        return

    # Was marked done but has suspiciously few questions — re-run
    if is_done and total < MIN_QUESTIONS_TO_MARK_DONE and not is_sparse:
        log_warning(f"[{subject}] was marked done but only has {total} questions — re-checking")

    if total > 0:
        log_info(f"[{subject}] resuming from {total} questions")

    no_new_streak = 0

    for round_num in range(1, MAX_ROUNDS + 1):
        data = parallel_fetch(subject)
        new  = [q for q in data if q["id"] not in seen_ids]

        if new:
            for q in new:
                seen_ids.add(q["id"])
                seen_dict[q["id"]] = q
            total        += len(new)
            no_new_streak = 0

            # save to disk
            save_subject(subject, seen_dict)

            # update progress
            with progress_lock:
                progress[subject] = {"done": False, "total": total}
            save_progress(progress)

            log_info(f"[{subject}] round {round_num}: +{len(new)} (total {total})")

            # commit every round that has new questions
            git_commit_and_push(subject, len(new), total)

        else:
            no_new_streak += 1
            log_info(f"[{subject}] round {round_num}: no new ({no_new_streak}/{NO_NEW_LIMIT})")
            if no_new_streak >= NO_NEW_LIMIT:
                break

        time.sleep(DELAY)

    # verification pass — keep going if API still has more
    log_info(f"[{subject}] verifying...")
    if not verify_complete(subject, seen_ids, total):
        log_info(f"[{subject}] more found — running another pass")
        _run_subject(subject, progress)
        return

    # mark done
    with progress_lock:
        progress[subject] = {"done": True, "total": total}
    save_progress(progress)

    # final commit for this subject
    git_commit_and_push(subject, 0, total)
    log_info(f"[{subject}] COMPLETE: {total} questions")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    log_info(f"\n{'='*55}")
    log_info(f"Starting download — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_info(f"Errors will be saved to: {LOG_FILE}")
    log_info(f"{'='*55}\n")

    # configure git identity — hardcoded so commits count as contributions
    git_run(["config", "user.name",  "anointedthedeveloper"])
    git_run(["config", "user.email", "anointedthedeveloper@gmail.com"])

    progress = load_progress()

    prevent_sleep()
    prevent_shutdown()

    log_info(f"\nDownloading {len(SUBJECTS)} subjects — committing every round...\n")

    threads = [
        threading.Thread(target=process_subject, args=(s, progress), daemon=True)
        for s in SUBJECTS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allow_sleep()
    allow_shutdown()

    log_info("\n" + "=" * 55)
    log_info("FINAL RESULTS")
    log_info("=" * 55)
    for s in SUBJECTS:
        info   = progress.get(s, {})
        status = "COMPLETE" if info.get("done") else "INCOMPLETE"
        log_info(f"  {s:<25} {info.get('total', 0):>6} questions  [{status}]")
    log_info("=" * 55)
    log_info(f"\nAll done! Check {LOG_FILE} for any errors.")
