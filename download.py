"""
ALOC Question Downloader + Auto GitHub Push
--------------------------------------------
- Loads API keys from accounts.json (all of them), falls back to .env
- Downloads all subjects in parallel until exhausted
- After each subject completes, pushes to GitHub via Personal Access Token
- Run:  python download.py
- Env:  GITHUB_TOKEN, GITHUB_REPO (owner/repo), GITHUB_BRANCH in .env
"""

import requests
import json
import os
import time
import threading
import socket
import base64
import ctypes
from dotenv import dotenv_values

# ── Windows sleep/shutdown prevention ────────────────────────────────────────

ES_CONTINUOUS      = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

def prevent_sleep():
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    except Exception:
        pass  # non-Windows — ignore

def allow_sleep():
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
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

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.json")
ENV_FILE      = os.path.join(SCRIPT_DIR, ".env")

_env = dotenv_values(ENV_FILE)

# Load API keys: accounts.json is the primary source
def load_api_keys():
    keys = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                accounts = json.load(f)
            # deduplicate by api_key value
            seen = set()
            for acc in accounts:
                k = acc.get("api_key", "").strip()
                if k and k not in seen:
                    keys.append(k)
                    seen.add(k)
            if keys:
                print(f"[config] loaded {len(keys)} API keys from accounts.json")
                return keys
        except Exception as e:
            print(f"[config] accounts.json read error: {e} — falling back to .env")

    # fallback: read API_KEY_* from .env
    keys = [v for k, v in sorted(_env.items()) if k.startswith("API_KEY_")]
    print(f"[config] loaded {len(keys)} API keys from .env")
    return keys

API_KEYS = load_api_keys()
if not API_KEYS:
    raise RuntimeError("No API keys found in accounts.json or .env — run signup_bot.py first")

HEADERS_LIST = [{"Accept": "application/json", "AccessToken": k} for k in API_KEYS]

# GitHub config — set these in .env
GITHUB_TOKEN  = _env.get("GITHUB_TOKEN", "")
GITHUB_REPO   = _env.get("GITHUB_REPO", "")    # e.g. "yourname/yourrepo"
GITHUB_BRANCH = _env.get("GITHUB_BRANCH", "master")

BASE_URL   = "https://questions.aloc.com.ng/api/v2/q/5"
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "questions")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "progress.json")

SUBJECTS = [
    "mathematics", "english", "chemistry", "physics", "biology",
    "economics", "government", "englishlit", "geography",
    "commerce", "accounting", "civiledu", "agricultural-science",
    "crk", "irk", "further-mathematics", "computer-studies",
    "home-economics", "yoruba", "igbo", "hausa",
    "currentaffairs", "history", "insurance",
]

MAX_ROUNDS   = 500
NO_NEW_LIMIT = 40   # consecutive empty rounds before declaring done
RATE_LIMIT   = 58   # requests per key per minute
DELAY        = 0.05 # seconds between rounds

# Parallel requests per round per subject — scale with key count, cap at 30
REQUESTS_PER_ROUND = max(1, min(30, len(API_KEYS) // len(SUBJECTS)))

print(f"[config] {REQUESTS_PER_ROUND} parallel req/round per subject "
      f"| {len(API_KEYS)} keys | {len(API_KEYS) * RATE_LIMIT} req/min capacity")

# ── Rate-limit-aware key picker ───────────────────────────────────────────────

key_lock    = threading.Lock()
current_key = 0
key_counts  = [0] * len(API_KEYS)
key_reset   = [time.time()] * len(API_KEYS)


def pick_key():
    """Round-robin across all keys; waits if all are rate-limited."""
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

        # all keys exhausted — wait for earliest reset
        wait = 60 - (time.time() - min(key_reset))
        print(f"  [rate limit] all keys used — waiting {wait:.0f}s")
        time.sleep(max(wait, 1))
        for i in range(len(API_KEYS)):
            key_counts[i] = 0
            key_reset[i] = time.time()
        key_counts[0] += 1
        return 0, HEADERS_LIST[0]

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def single_request(subject):
    """One API call for a subject; returns list of valid questions."""
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
                time.sleep(min(2 ** attempt, 60))
                continue
        except Exception as e:
            wait = min(2 ** attempt, 60)
            print(f"  [{subject}] request error attempt {attempt+1}/6: {e} — retry in {wait}s")
            time.sleep(wait)
    return []


def parallel_fetch(subject):
    """Fire REQUESTS_PER_ROUND requests concurrently; merge by ID."""
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
    """Load existing questions; returns (dict[id->q], set[ids])."""
    path = os.path.join(OUTPUT_DIR, f"{subject}.json")
    if not os.path.exists(path):
        return {}, set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # filter corrupt entries
        valid = [q for q in data if q.get("id") and q.get("question", "").strip()]
        removed = len(data) - len(valid)
        if removed:
            print(f"  [{subject}] removed {removed} corrupt entries on load")
        seen = {q["id"]: q for q in valid}
        return seen, set(seen.keys())
    except Exception as e:
        print(f"  [{subject}] file corrupt: {e} — starting fresh")
        corrupt = path + ".corrupt"
        try:
            os.replace(path, corrupt)
        except Exception:
            pass
        return {}, set()


def save_subject(subject, seen_dict):
    """Atomic write: sort by id, write to .tmp, then replace."""
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
            print(f"  [progress] save failed: {e}")

# ── GitHub push via API ───────────────────────────────────────────────────────

github_push_lock = threading.Lock()


def github_push_file(subject):
    """
    Push questions/{subject}.json to GitHub using the REST API.
    Requires GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH in .env.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return  # not configured — skip silently

    filepath = os.path.join(OUTPUT_DIR, f"{subject}.json")
    if not os.path.exists(filepath):
        return

    try:
        with open(filepath, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"  [github] read error for {subject}: {e}")
        return

    api_path = f"questions/{subject}.json"
    url      = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{api_path}"
    headers  = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    with github_push_lock:
        # get current SHA (needed for updates)
        sha = None
        try:
            r = requests.get(url, headers=headers,
                             params={"ref": GITHUB_BRANCH}, timeout=15)
            if r.status_code == 200:
                sha = r.json().get("sha")
        except Exception as e:
            print(f"  [github] SHA fetch error for {subject}: {e}")

        # count questions for commit message
        try:
            count = len(json.loads(base64.b64decode(content)))
        except Exception:
            count = "?"

        payload = {
            "message": f"[download] {subject}: {count} questions",
            "content": content,
            "branch":  GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        try:
            r = requests.put(url, headers=headers,
                             json=payload, timeout=60)
            if r.status_code in (200, 201):
                print(f"  [github] pushed {subject}.json ({count} questions)")
            else:
                print(f"  [github] push failed for {subject}: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"  [github] push exception for {subject}: {e}")


def github_push_progress():
    """Push progress.json to GitHub."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return

    if not os.path.exists(PROGRESS_FILE):
        return

    try:
        with open(PROGRESS_FILE, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return

    url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/progress.json"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    with github_push_lock:
        sha = None
        try:
            r = requests.get(url, headers=headers,
                             params={"ref": GITHUB_BRANCH}, timeout=15)
            if r.status_code == 200:
                sha = r.json().get("sha")
        except Exception:
            pass

        payload = {
            "message": f"[download] update progress.json",
            "content": content,
            "branch":  GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        try:
            requests.put(url, headers=headers, json=payload, timeout=30)
        except Exception:
            pass

# ── Verification pass ─────────────────────────────────────────────────────────

def verify_complete(subject, seen_ids):
    """
    Fire 5 more rounds after declaring done.
    Returns True if genuinely exhausted, False if more questions found.
    """
    for _ in range(5):
        data = parallel_fetch(subject)
        new  = [q for q in data if q["id"] not in seen_ids]
        if new:
            return False
    return True

# ── Subject worker ────────────────────────────────────────────────────────────

def process_subject(subject, progress):
    try:
        _run_subject(subject, progress)
    except Exception as e:
        print(f"[{subject}] FATAL: {e}")


def _run_subject(subject, progress):
    print(f"\n[{subject}] starting")
    seen_dict, seen_ids = load_subject(subject)
    total = len(seen_dict)

    # skip if already marked done and has data
    subj_prog = progress.get(subject, {})
    if subj_prog.get("done") and total > 0:
        print(f"[{subject}] already complete ({total} questions) — skipping")
        return

    if total > 0:
        print(f"[{subject}] resuming from {total} questions")

    no_new_streak = 0

    for round_num in range(1, MAX_ROUNDS + 1):
        data = parallel_fetch(subject)
        new  = [q for q in data if q["id"] not in seen_ids]

        if new:
            for q in new:
                seen_ids.add(q["id"])
                seen_dict[q["id"]] = q
            total += len(new)
            no_new_streak = 0
            save_subject(subject, seen_dict)
            print(f"[{subject}] round {round_num}: +{len(new)} (total {total})")
        else:
            no_new_streak += 1
            print(f"[{subject}] round {round_num}: no new ({no_new_streak}/{NO_NEW_LIMIT})")
            if no_new_streak >= NO_NEW_LIMIT:
                break

        with progress_lock:
            progress[subject] = {"done": False, "total": total}
        save_progress(progress)
        time.sleep(DELAY)

    # verification pass
    print(f"[{subject}] verifying...")
    truly_done = verify_complete(subject, seen_ids)

    if not truly_done:
        # API still has questions — keep going
        print(f"[{subject}] verification found more — continuing")
        _run_subject(subject, progress)  # tail-recurse for one more pass
        return

    with progress_lock:
        progress[subject] = {"done": True, "total": total}
    save_progress(progress)

    print(f"[{subject}] COMPLETE: {total} questions — pushing to GitHub...")
    github_push_file(subject)
    github_push_progress()
    print(f"[{subject}] done.")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not GITHUB_TOKEN:
        print("[warning] GITHUB_TOKEN not set in .env — questions will be saved locally only")
    if not GITHUB_REPO:
        print("[warning] GITHUB_REPO not set in .env — e.g. GITHUB_REPO=yourname/yourrepo")

    progress = load_progress()

    prevent_sleep()
    prevent_shutdown()

    print(f"\nDownloading {len(SUBJECTS)} subjects with {len(SUBJECTS)} parallel threads...\n")

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

    # final summary
    print("\n" + "=" * 55)
    print("FINAL RESULTS")
    print("=" * 55)
    for s in SUBJECTS:
        info = progress.get(s, {})
        status = "COMPLETE" if info.get("done") else "INCOMPLETE"
        print(f"  {s:<25} {info.get('total', 0):>6} questions  [{status}]")
    print("=" * 55)
    print("\nAll done!")
