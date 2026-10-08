# Faculty Development Game — Run Guide (README)

Northwest Iowa Community College. One-hour faculty-development hidden-role game.

This is a Flask web app with no database and no accounts. Game state lives in
memory and resets whenever the server restarts. That is intentional and fine for
a live session.

---

## 1. What you need

- The facilitator's Windows laptop (this machine) with the project at
  `C:\Users\ashle\faculty-game`.
- One device per **team** (laptop or tablet with a web browser). Teams pass this
  one device around their own table.
- One device for the **projector / room screen** (the facilitator laptop plugged
  into the projector works well — open the projector screen in its own window).
- All devices on the **same Wi-Fi / local network** as the facilitator laptop.
- The printed card decks (see `PRINT_CONTENT.md`).

You can also run a whole game on a single laptop using separate browser tabs if
you are testing or running a very small group.

---

## 2. Start the server (exact command)

Open **PowerShell**, then run:

```powershell
cd C:\Users\ashle\faculty-game
.\venv\Scripts\python.exe app.py
```

Leave that PowerShell window open for the whole session. You will see a line like
`Running on http://0.0.0.0:5000`. The server is now live.

To stop the server, click the PowerShell window and press `Ctrl + C`.

### Optional overrides
- Change the port: `$env:FACULTY_GAME_PORT = "8000"` before the launch command.
- Developer auto-reload (NOT for live play): `$env:FACULTY_GAME_DEBUG = "1"`.

---

## 3. The three URLs

Find the laptop's local network address first:

```powershell
ipconfig | Select-String "IPv4"
```

Use the address that looks like `192.168.x.x` or `10.x.x.x`. Call that
`<LAN-IP>`. Then:

| Screen | On the facilitator laptop | On team / room devices |
|---|---|---|
| Game Master (you) | `http://127.0.0.1:5000/gm` | — (keep this private) |
| Team screen | `http://127.0.0.1:5000/team` | `http://<LAN-IP>:5000/team` |
| Projector / room | `http://127.0.0.1:5000/projector` | `http://<LAN-IP>:5000/projector` |

> The Game Master screen shows secret information (allegiance, sabotage, votes).
> Keep it on your laptop only. Never cast the GM screen to the room.

---

## 4. Recommended browser / tab setup

- **Facilitator laptop:** one browser window on `/gm` (your control panel), and a
  second window on `/projector` dragged onto the projector display.
- **Each team device:** one browser tab on `/team`.
- There is no auto-refresh. Use the on-screen **Refresh** button (or reload the
  page) after you change the room state from the GM screen. This is expected.

---

## 5. Start / reset a game

1. On `/gm`, click **Create New Game**. A 5-character game code appears
   (for example `K7Q2M`). Read it out to the room.
2. Each team opens `/team`, enters the **game code** and a **team name**, and
   registers players.
3. Drive the rest from the GM screen (open/close trading, open/lock/advance
   checkpoints). See `FACILITATOR_RUN_OF_SHOW.md` for the minute-by-minute flow.

**To reset for a new session:** click **Create New Game** again (this starts a
fresh game and makes it the active one), or restart the server (`Ctrl + C`, then
the launch command again) to wipe everything.

---

## 6. Re-entry (a team's device closed or refreshed)

Teams resume automatically. On `/team`, the team re-enters the **same game code
and the exact same team name**. The app returns them to exactly where they left
off — setup step, handoff, current checkpoint, or the final phase. Re-entry never
duplicates a team and never re-does the rejected-student handoff.

Player names and the team name must match what was first entered (team name match
is case-insensitive).

---

## 7. Common troubleshooting

| Symptom | Fix |
|---|---|
| Team device can't reach the site | Confirm it is on the same Wi-Fi and you used the `<LAN-IP>` address, not `127.0.0.1`. Check Windows Firewall is not blocking Python on the private network. |
| "That game code was not found" | The code is wrong, or the server was restarted (which wipes games). Create a new game and share the new code. |
| A team's screen looks stale | Press the **Refresh** button on their screen. The app does not auto-update. |
| A checkpoint won't open for teams | On `/gm`, make sure you clicked **Open Checkpoint**. Teams only see content while the checkpoint is OPEN. |
| A team can't register their course | Course registration is blocked until you **Close Course Trading** on the GM screen. |
| A team is stuck waiting at handoff | The team they receive from must click **Cards Passed** first. Check handoff status on the GM screen. |
| Accidentally created an extra game | The most recently created game is the active one. Just create one more clean game and use that code. |
| Server restarted mid-game | In-memory state is gone. This cannot be recovered; start a new game. Avoid `FACULTY_GAME_DEBUG=1` during live play, which auto-restarts on file changes. |

---

## 8. Files in this package

- `README_RUN_GAME.md` — this file.
- `FACILITATOR_RUN_OF_SHOW.md` — one-hour run sheet with minute marks and script.
- `GAME_RULES.md` — full rules and win conditions.
- `PRINT_CONTENT.md` — final copy for all physical cards (ready for Canva).
- `TEST_PLAN.md` — manual and automated test coverage.
