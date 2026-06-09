import requests
import re
import os
import sys
import json
import time
import random
import string
import threading
import argparse
from datetime import datetime

BASE = 'https://questions.aloc.com.ng'
ENV_FILE = '.env'
ACCOUNTS_FILE = 'accounts.json'

TARGET = 100
WORKERS = 10          # concurrent threads — enough to hit 100 well within an hour
RETRY_LIMIT = 3

write_lock = threading.Lock()
counter = {'done': 0, 'failed': 0}


# ── helpers ──────────────────────────────────────────────────────────────────

def rand_str(n=10):
    return ''.join(random.choices(string.ascii_lowercase, k=n))


def get_csrf(session, url):
    r = session.get(url, timeout=15)
    m = re.search(r'name="_token"\s+value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError(f'No CSRF token at {url}')
    return m.group(1)


# ── core actions ─────────────────────────────────────────────────────────────

def signup_account(name, email, password):
    s = requests.Session()
    token = get_csrf(s, f'{BASE}/secure/signup')
    resp = s.post(f'{BASE}/secure/signup', data={
        '_token': token,
        'name': name,
        'email': email,
        'password': password,
        'password_confirmation': password,
    }, timeout=15, allow_redirects=True)

    if '/admin/dashboard' not in resp.url:
        alert = re.search(r'class="[^"]*alert[^"]*"[^>]*>(.*?)</div>', resp.text, re.DOTALL)
        msg = re.sub(r'<[^>]+>', '', alert.group(1)).strip() if alert else 'unknown'
        raise RuntimeError(f'Signup failed: {msg}')
    return s


def login_account(email, password):
    s = requests.Session()
    token = get_csrf(s, f'{BASE}/secure/login')
    resp = s.post(f'{BASE}/secure/login', data={
        '_token': token,
        'email': email,
        'password': password,
    }, timeout=15, allow_redirects=True)

    if '/admin/dashboard' not in resp.url:
        raise RuntimeError('Login failed')
    return s


def fetch_api_key(session):
    r = session.get(f'{BASE}/admin/access-token', timeout=15)
    keys = re.findall(r'(QB-[A-Za-z0-9]+|ALOC-[A-Za-z0-9]+)', r.text)
    if not keys:
        raise RuntimeError('API key not found on access-token page')
    return keys[0]


# ── persistence ───────────────────────────────────────────────────────────────

def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    return []


def save_account(entry):
    """Append one account entry to accounts.json and .env (thread-safe)."""
    with write_lock:
        accounts = load_accounts()
        accounts.append(entry)
        with open(ACCOUNTS_FILE, 'w') as f:
            json.dump(accounts, f, indent=2)

        # find next API_KEY slot in .env
        existing_lines = open(ENV_FILE).readlines() if os.path.exists(ENV_FILE) else []
        existing_keys = [l for l in existing_lines if l.startswith('API_KEY_')]
        n = len(existing_keys) + 1

        with open(ENV_FILE, 'a') as f:
            f.write(f'API_KEY_{n}={entry["api_key"]}\n')
            f.write(f'EMAIL_{n}={entry["email"]}\n')
            f.write(f'PASSWORD_{n}={entry["password"]}\n')


# ── worker ────────────────────────────────────────────────────────────────────

def create_one(index):
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            rand = rand_str(10)
            name = f'User {rand}'
            email = f'aloc{rand}@mailinator.com'
            password = f'Pass{rand_str(8)}'

            session = signup_account(name, email, password)
            api_key = fetch_api_key(session)

            entry = {
                'index': index,
                'name': name,
                'email': email,
                'password': password,
                'api_key': api_key,
                'created_at': datetime.utcnow().isoformat(),
            }
            save_account(entry)

            with write_lock:
                counter['done'] += 1
                print(f'  [{counter["done"]:>3}/{TARGET}] {email} -> {api_key}')
            return

        except Exception as e:
            if attempt < RETRY_LIMIT:
                time.sleep(2 * attempt)
            else:
                with write_lock:
                    counter['failed'] += 1
                    print(f'  [FAIL #{index}] attempt {attempt}: {e}')


# ── main ──────────────────────────────────────────────────────────────────────

def run_bulk(target, workers):
    print(f'[*] Creating {target} accounts with {workers} workers...')
    start = time.time()

    threads = []
    for i in range(1, target + 1):
        t = threading.Thread(target=create_one, args=(i,), daemon=True)
        threads.append(t)

    # launch in batches to avoid hammering the server
    for i in range(0, len(threads), workers):
        batch = threads[i:i + workers]
        for t in batch:
            t.start()
        for t in batch:
            t.join()

    elapsed = time.time() - start
    print(f'\n[+] Done in {elapsed:.1f}s — {counter["done"]} created, {counter["failed"]} failed')
    print(f'[+] Accounts saved to {ACCOUNTS_FILE}')
    print(f'[+] Keys saved to {ENV_FILE}')


def run_single(name, email, password, login_only=False):
    try:
        if login_only:
            session = login_account(email, password)
            print(f'[+] Logged in: {email}')
        else:
            session = signup_account(name, email, password)
            print(f'[+] Signed up: {email}')

        api_key = fetch_api_key(session)
        print(f'[+] API key: {api_key}')

        entry = {
            'name': name, 'email': email, 'password': password,
            'api_key': api_key, 'created_at': datetime.utcnow().isoformat(),
        }
        save_account(entry)
        print(f'[+] Saved to {ACCOUNTS_FILE} and {ENV_FILE}')

    except RuntimeError as e:
        print(f'[-] {e}')
        sys.exit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ALOC signup bot')
    sub = parser.add_subparsers(dest='cmd')

    # bulk: create 100 accounts
    bulk = sub.add_parser('bulk', help='Create 100 accounts (default)')
    bulk.add_argument('--target',  type=int, default=TARGET,  help=f'Number of accounts (default {TARGET})')
    bulk.add_argument('--workers', type=int, default=WORKERS, help=f'Parallel threads (default {WORKERS})')

    # single: one account with your own creds
    single = sub.add_parser('single', help='Sign up / login a single account')
    single.add_argument('--name',       default='', help='Full name')
    single.add_argument('--email',      required=True)
    single.add_argument('--password',   required=True)
    single.add_argument('--login-only', action='store_true')

    args = parser.parse_args()

    if args.cmd == 'single':
        run_single(args.name, args.email, args.password, args.login_only)
    else:
        # default to bulk even with no subcommand
        target  = getattr(args, 'target',  TARGET)
        workers = getattr(args, 'workers', WORKERS)
        run_bulk(target, workers)
