# Deploying THUNDERFLUNKED 2.0 on PythonAnywhere

This is the **PythonAnywhere** deployment guide (your existing hosting). It is
different from `DEPLOY_RENDER.md` — **ignore the Render/gunicorn instructions
here.** PythonAnywhere runs the app under its own WSGI server; there is no
gunicorn, no `render.yaml`, no managed Postgres required.

Your existing site is `https://akpeters.pythonanywhere.com` and currently runs the
**pre-redesign** version. This guide upgrades it to THUNDERFLUNKED 2.0 **safely,
with a full backup and a one-step rollback, without ever touching the old data.**

---

## The one rule that must not be broken: ONE worker

THUNDERFLUNKED keeps the live game in memory and mirrors it to a database. It is
restored from the database only when the process starts. **This requires exactly
one worker process.** With two or more workers, teams would land on different
in-memory copies and the game would break.

- On PythonAnywhere, set **Web tab → "Number of workers" = 1**.
- Free accounts default to 1 worker (fine). Paid accounts may default higher —
  **you must set it back to 1.**

---

## Why PythonAnywhere is a good fit (and what's different from Render)

- **Persistent disk.** Unlike Render's throwaway dynos, your PythonAnywhere home
  directory is permanent. So a **SQLite file persists across reloads** — no managed
  database needed. (With one worker, SQLite has no concurrency problems.)
- **HTTPS is built in** (`…pythonanywhere.com` is served over HTTPS), so the signed,
  Secure team-session cookies and GM login work correctly.
- **WSGI, not gunicorn.** The app is wired through a WSGI file that imports the Flask
  object. `gunicorn` in `requirements.txt` is only for Render and is never imported
  by the app, so it is harmless here.

---

## Plan / concurrency check (do this first)

A 50–60-person workshop is light load (mostly a poll every few seconds plus short
bursts at each checkpoint). The app itself handles that easily on one worker.

The real limit on PythonAnywhere is the **plan**:
- **Free tier:** one web app only, a daily CPU-seconds quota, and lower priority.
  Usable for *staging/testing*, but a full 75-minute session with 60 people is a
  risk (CPU throttling; only one web app means no separate staging URL).
- **Paid tier (e.g., "Hacker"/"Web developer"):** no CPU-quota throttling for this
  workload, lets you run **multiple web apps** (so you can stage 2.0 at a second URL
  before switching), and lets you set workers explicitly.

**Recommendation:** run the real workshop on a **paid plan with workers = 1**. If
you are on free and don't want to upgrade, the in-place upgrade below still works;
just test it off-hours and warm it right before the session.

---

## STEP 0 — Back up first (nothing below is destructive if you do this)

Open a **Bash console** on PythonAnywhere and run (adjust the project path to match
your account — find it on the **Web tab → "Source code"/"Working directory"**):

```bash
# 1. Note what is live now (write this hash down for rollback):
cd ~/faculty-game        # <-- your actual project directory
git rev-parse HEAD

# 2. Back up the current code state (a branch pointer is enough; code is in git):
git branch backup-pre-2.0 HEAD

# 3. Back up the current database file, if one exists. Find its path from the
#    DATABASE_URL in your WSGI file (Web tab -> WSGI configuration file). If it is
#    sqlite, copy it; if there is none, there is nothing to back up.
cp /home/akpeters/faculty-game/state.db \
   /home/akpeters/faculty-game/state.db.bak-$(date +%Y%m%d-%H%M)   # if it exists
```

> **Key safety choice:** THUNDERFLUNKED 2.0 will be pointed at a **new** database
> file (`state_v2.db`), so your **old database is never written to**. That makes
> rollback trivial and your old data untouchable.

---

## STEP 1 — Get the 2.0 code onto PythonAnywhere

In the same Bash console, in your project directory:

```bash
cd ~/faculty-game
git fetch origin
git checkout redesign/usability
git pull origin redesign/usability
```

Then install/refresh the dependencies into the **virtualenv your web app uses**
(find its path on the Web tab → "Virtualenv"; the example assumes it is active):

```bash
pip install -r requirements.txt
```

> If `psycopg[binary]` is slow or you prefer to skip it (it is only needed for
> PostgreSQL, which we are not using), you can install the rest without it — the
> app runs fine on SQLite without psycopg. But installing it is harmless.

---

## STEP 2 — Point the WSGI file at 2.0 and set the secrets

On the **Web tab**, open the **WSGI configuration file** link (it edits
`/var/www/akpeters_pythonanywhere_com_wsgi.py`). Replace its contents with the
template in this repo, `wsgi_pythonanywhere_template.py`, adjusting the project
path, and **fill in real secret values directly in that file** (it lives only on
PythonAnywhere's private disk — never in git):

- `SECRET_KEY` — a long random string. Generate one in the Bash console:
  `python3 -c "import secrets; print(secrets.token_hex(32))"` and paste the output.
- `GM_PASSWORD` — a strong password you will type to open `/gm` (e.g.
  `thunder-GM-fall2026-NCC`).
- `DATABASE_URL` — a **new** SQLite file so the old data is untouched, e.g.
  `sqlite:////home/akpeters/faculty-game/state_v2.db` (note the **four** slashes =
  absolute path).

Setting `DATABASE_URL` turns on production mode, which requires `SECRET_KEY` and
enables Secure cookies (correct, because PythonAnywhere serves HTTPS).

---

## STEP 3 — One worker, then Reload

- **Web tab → Number of workers → 1** (verify, set, save).
- Click the big green **Reload** button.

---

## STEP 4 — Verify (use a throwaway test game; do not touch real data)

1. `https://akpeters.pythonanywhere.com/healthz` → shows **`ok`**.
2. `https://akpeters.pythonanywhere.com/` → the game's landing page loads over HTTPS.
3. `https://akpeters.pythonanywhere.com/gm` → a **Game Master Login** page (not the
   dashboard). Log in with your `GM_PASSWORD` → the dashboard appears.
4. Create a **test** game; open `/team` in a second browser and **join** a team;
   confirm the split-screen Canvas workspace loads.
5. **Team security:** from a *third* browser, enter the same code + that team's name
   → it should say "already on another device" (not let you in). On the GM screen,
   "Move team to this device" should recover it.
6. **Projector:** open `/projector` — it loads and shows no secrets.
7. **Persistence across restart:** note the test game code, click **Reload** on the
   Web tab, then reopen `/gm` → the same game code is still there.

---

## Rollback (if anything is wrong) — about 60 seconds

```bash
cd ~/faculty-game
git checkout backup-pre-2.0      # or the hash you wrote down in Step 0
```
Then in the **WSGI file**, change `DATABASE_URL` back to the old database path (or
remove the 2.0 env lines you added), set **workers = 1** if needed, and click
**Reload**. Because 2.0 used a *separate* `state_v2.db`, your original database is
exactly as it was.

---

## Optional: stage 2.0 at a separate URL first (paid accounts only)

If you are on a paid plan, you can run 2.0 **alongside** the old site before
switching:
1. Web tab → **Add a new web app** (paid accounts allow more than one).
2. Point it at a **separate checkout** of this repo on `redesign/usability`, with
   its own virtualenv and its own `state_v2.db`.
3. Use its `…pythonanywhere.com` sub-URL (or a custom domain) to test with real
   people, then make it the primary when you're happy. The old app stays untouched.

On a **free** plan you can have only one web app, so use the in-place upgrade above
(with the backup/rollback), ideally tested off-hours.

---

## Quick reference

| Thing | Value |
|---|---|
| WSGI callable | `from app import app as application` |
| Workers | **1** (required) |
| Python | 3.10+ (the app uses nothing newer; use the latest your account offers) |
| Database | SQLite on the persistent disk, e.g. `sqlite:////home/akpeters/faculty-game/state_v2.db` |
| Required env (set in the WSGI file) | `SECRET_KEY`, `GM_PASSWORD`, `DATABASE_URL` |
| Health check | `/healthz` → `ok` |
| Reload | Web tab → **Reload** (restores games from the DB on boot) |
| GM URL | `/gm` (password) · Team: `/team` · Projector: `/projector` |
| Rollback | `git checkout backup-pre-2.0` + restore old `DATABASE_URL` + Reload |
