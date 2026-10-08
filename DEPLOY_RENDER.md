# Deploying the Faculty Development Game to Render

This guide takes you from "the game runs on my laptop" to "the game runs on a
public website that anyone can reach, from any network, without my computer
being on." It is written for a non-web-developer. Follow it top to bottom.

You will use three free services:

1. **GitHub** — stores a copy of the game's code online.
2. **Render** — runs the game as a public website.
3. **Render PostgreSQL** — a database that remembers every in-progress game, so
   a server restart never loses a session.

Budget about 30–45 minutes the first time. Future updates take about 2 minutes.

> **A note on cost:** Render's free "Web Service" and free "PostgreSQL" plans are
> enough for this game. Two things to know about the free tier: (a) after ~15
> minutes with no visitors the site "sleeps," and the next visit takes ~30
> seconds to wake up — this is normal, and **no game data is lost** because
> everything lives in the database; (b) a free Render database is deleted after
> 90 days. For a training event that is fine. If you want it to live longer,
> upgrade the database to a paid plan (a few dollars a month).

---

## Part 0 — What's already been prepared for you

Nothing in this section is an action — it is just so you know what the files are.

- `app.py` — the game (unchanged gameplay; deployment plumbing added).
- `storage.py` — saves/loads games to the database.
- `requirements.txt` — the list of software Render installs.
- `.python-version` — tells Render which Python version to use (3.12.7).
- `.gitignore` — keeps your local database and secrets out of GitHub.
- `test_game.py` — the automated tests.

You do **not** need to edit any of these to deploy.

---

## Part 1 — Put the project on GitHub

If your project is already on GitHub, skip to Part 2.

### 1a. Create a free GitHub account

Go to <https://github.com> and sign up if you don't already have an account.

### 1b. Install Git (if you don't have it)

Open PowerShell and type:

```powershell
git --version
```

If you see a version number, Git is installed — continue. If you see an error,
download and install Git from <https://git-scm.com/download/win>, accepting all
the default options, then reopen PowerShell.

### 1c. Create the repository on GitHub

1. Go to <https://github.com/new>.
2. **Repository name:** `faculty-game` (or any name you like).
3. Set it to **Private** (recommended) or Public — either works.
4. Do **not** check "Add a README" or any other boxes.
5. Click **Create repository**.
6. Leave that page open; you'll need the URL it shows (it looks like
   `https://github.com/YOURNAME/faculty-game.git`).

### 1d. Upload your project

In PowerShell, run these commands **one line at a time**, from inside your
project folder. (The commands below `cd` into it for you.)

```powershell
cd C:\Users\ashle\faculty-game
git init
git add .
git commit -m "Faculty game release candidate, prepared for Render"
git branch -M main
git remote add origin https://github.com/YOURNAME/faculty-game.git
git push -u origin main
```

Replace `https://github.com/YOURNAME/faculty-game.git` with the URL from step 1c.

The first `git push` may pop up a window asking you to sign in to GitHub. Sign in
and allow it. When it finishes, refresh your GitHub repository page — you should
see your files (`app.py`, `storage.py`, etc.) listed.

> If `git push` complains that the remote already exists, run
> `git remote set-url origin https://github.com/YOURNAME/faculty-game.git`
> and push again.

---

## Part 2 — Create the database on Render

1. Go to <https://render.com> and sign up (the "Sign up with GitHub" option is
   easiest — it connects the two accounts for you).
2. In the Render dashboard, click **New +** (top right) → **PostgreSQL**.
3. Fill in:
   - **Name:** `faculty-game-db`
   - **Region:** pick the one closest to you (e.g., Ohio for the US Midwest).
     **Remember this choice** — the web service must use the same region.
   - **Plan:** choose **Free**.
4. Click **Create Database**.
5. Wait until the status says **Available** (about a minute).
6. On the database page, scroll to **Connections**. Find **Internal Database
   URL**, click the copy icon, and paste it somewhere temporary (e.g. Notepad).
   It starts with `postgresql://`. You'll need it in Part 3.

> Use the **Internal** URL (not the External one). Internal is faster and does
> not require extra security settings, because the web service and database will
> live in the same Render region.

---

## Part 3 — Create the web service on Render

1. In the Render dashboard, click **New +** → **Web Service**.
2. Choose **Build and deploy from a Git repository**, then click **Next**.
3. Find your `faculty-game` repository in the list and click **Connect**.
   (If you don't see it, click **Configure account** and give Render permission
   to access the repository.)
4. Fill in the settings:

   | Setting | Value |
   |---|---|
   | **Name** | `faculty-game` (this becomes part of your web address) |
   | **Region** | **the same region you picked for the database** |
   | **Branch** | `main` |
   | **Runtime / Language** | Python (Render detects this automatically) |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT` |
   | **Instance Type** | **Free** |

   > **Important:** the start command uses `--workers 1` on purpose. The game
   > keeps the live session in memory and mirrors it to the database. One worker
   > with multiple threads handles a full classroom of devices at once; using
   > more than one worker would split the game across separate memories. Do not
   > change `--workers 1`.

5. **Do not click Create yet.** First add the environment variables (Part 4).

---

## Part 4 — Environment variables (the secrets)

Still on the web service setup page, find the **Environment Variables** section
(on some screens it's under **Advanced**). Click **Add Environment Variable**
once per row below:

| Key | Value |
|---|---|
| `DATABASE_URL` | *(paste the Internal Database URL you copied in Part 2, step 6)* |
| `SECRET_KEY` | *(a long random string — see below)* |
| `GM_PASSWORD` | *(a password you choose for the Game Master screen)* |

**How to create a SECRET_KEY:** it just needs to be a long, random, hard-to-guess
string. Either make one up by mashing the keyboard (40+ characters), or run this
in PowerShell and copy the output:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

**GM_PASSWORD** is the password you (and only you) will type to open the Game
Master dashboard during the session. Choose something you'll remember but others
won't guess, e.g. `thunder-GM-fall2026`.

> **Never put these values in the code or in GitHub.** They live only here, in
> Render's environment variables. That is exactly what keeps them secret.

Now click **Create Web Service**.

---

## Part 5 — First deployment

Render now installs the software and starts the game. Watch the **Logs** tab.

- You'll see it install the requirements, then a line like
  `Booting worker` / `Listening at: http://0.0.0.0:10000`.
- When the top of the page shows a green **Live** badge, it's running.
- The first build takes a few minutes. This is normal.

Your public address is shown near the top of the page, something like:

```
https://faculty-game.onrender.com
```

This is the link you'll share. (Yours will use the name you chose.)

---

## Part 6 — Verify the deployment

Open your `https://…onrender.com` address and check each of these:

1. **Health check** — visit `https://YOUR-ADDRESS/healthz`.
   You should see the word **`ok`**. (If you see `database unavailable`,
   double-check the `DATABASE_URL` variable in Part 4.)
2. **Home page** — visit `https://YOUR-ADDRESS/`. The game's landing page loads.
3. **Team screen** — visit `https://YOUR-ADDRESS/team`. The "Join Game" form
   loads. (This is public, as intended.)
4. **GM is protected** — visit `https://YOUR-ADDRESS/gm`. You should be sent to
   a **Game Master Login** page, *not* straight into the dashboard. This proves
   strangers can't control your game.
5. **GM login works** — type your `GM_PASSWORD`. You should land on the Game
   Master Dashboard.
6. **Persistence** — on the GM dashboard, click **Create New Game** and note the
   game code. Then, in Render, open the web service's **Settings** and click
   **Manual Deploy → Restart service** (or just wait for it to sleep and wake).
   After it restarts, log back into `/gm` — **the same game code is still
   there.** That is the database doing its job.

If all six pass, you're deployed. 🎉

---

## Part 7 — Running the game day-of

### How you (the Game Master) get in

1. Go to `https://YOUR-ADDRESS/gm`.
2. Enter your `GM_PASSWORD`.
3. Click **Create New Game**. A short **game code** appears (e.g. `K7P2M`).
4. Read that code out to the room.
5. Optional: open `https://YOUR-ADDRESS/projector` on the room's big screen.
   The projector screen is public and never shows hidden roles.
6. When you're done, click **Log Out** on the dashboard.

### How teams join

Each team uses **one device** (laptop/tablet at their table). They:

1. Go to `https://YOUR-ADDRESS/team`.
2. Enter the **game code** you announced and their **team name**.
3. Follow the on-screen setup.

**Re-entry:** if a team's device refreshes, loses Wi-Fi, or closes the tab, they
simply go back to `/team` and enter the **same game code + same team name**. They
land exactly where they left off. Nothing is lost.

---

## Part 8 — Deploying future updates

Whenever the game's code changes (by you or someone helping you), publish the
update like this, from PowerShell:

```powershell
cd C:\Users\ashle\faculty-game
git add .
git commit -m "Describe what changed"
git push
```

That's it. Render notices the push and automatically rebuilds and redeploys
within a few minutes. You can watch progress on the web service's **Logs** tab.
In-progress games stored in the database are preserved across the update.

> If you ever change the `GM_PASSWORD`, `SECRET_KEY`, or `DATABASE_URL`, do it in
> Render under the web service's **Environment** tab, then click **Save Changes**
> (Render redeploys automatically). Changing `SECRET_KEY` logs out any current GM
> session, which is harmless — just log in again.

---

## Quick reference card

| Thing | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT` |
| Health check path | `/healthz` |
| Required env vars | `DATABASE_URL`, `SECRET_KEY`, `GM_PASSWORD` |
| Game Master URL | `https://YOUR-ADDRESS/gm` |
| Team join URL | `https://YOUR-ADDRESS/team` |
| Projector URL | `https://YOUR-ADDRESS/projector` |
| Publish an update | `git add .` → `git commit -m "…"` → `git push` |

---

## Running it locally (unchanged)

Deploying to Render does **not** change how you test on your own computer. Local
play still runs fully in memory with no database and no password, exactly as
before:

```powershell
cd C:\Users\ashle\faculty-game
venv\Scripts\python app.py
```

Then open <http://localhost:5000>. To run the automated tests:

```powershell
venv\Scripts\python test_game.py
```
