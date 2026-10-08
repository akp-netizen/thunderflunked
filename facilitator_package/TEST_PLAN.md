# Test Plan

Two layers: an **automated suite** (`test_game.py`, Flask in-process test client,
no network) and **manual procedures** for a facilitator to verify a live room.

## Automated suite

Run from the project root:

```powershell
cd C:\Users\ashle\faculty-game
.\venv\Scripts\python.exe test_game.py
```

Expected final line: `100 passed, 0 failed` (exit code 0). The suite runs a
complete simulated game and covers:

| Area | What is verified |
|---|---|
| Data integrity | 30 students; 10 courses (4 lecture / 2 async / 4 CTE); every course has 2 realities; 6 roles; 5 checkpoints. |
| Scoring model | Starting resources floored at 40 and capped at 100; strong roster scores at or above weak roster; scoring depends only on the roster (course has no effect); student corrections (Silent SOS help-seeking=1, communication=4; Late Bloomer help-seeking=3); all student totals 16-21. |
| Two-team full game | 4-player + 6-player team in one game, played start to finish. |
| Allegiance | Exactly one Thunderflunked per team; every player privately sees allegiance. |
| Roles | Assignment and Unrepresented Perspectives. |
| Course trading | Cannot register a course while trading is OPEN; can register after CLOSE. |
| Deal / draft | Exactly 15 dealt, 10 roster, 5 rejected. |
| Handoff | Circular pass; receiver gets the sender's exact rejected set; starting resources unchanged by handoff. |
| CP1-CP5 | Each checkpoint's evidence renders; decisions submit once and apply resource deltas once. |
| Late adds (CP2) | 2-4 join, drawn from the received cards; they do not move resources on arrival. |
| Private checks | Run to completion at every checkpoint; votes recorded (incl. a browser-fidelity test that drives the real pages); mission claims recorded; each player sees their own allegiance; no leak between players. |
| Emergency Sabotage | Offered inside the Thunderflunked's private turn at CP2/CP3; used at most once; Save For Later works. |
| CP4 structure | Priority + supporting; same option twice is handled. |
| Final | Accusation recorded; outcome shows Survived/Did Not Survive; reveal and debrief present. |
| Projector privacy | No allegiance, mission, suspicion, vote, or player name leaks; team name and outcome shown. |
| GM visibility | GM sees Thunderflunked, resources, and sabotage use. |
| Re-entry | Resume at the correct step; no duplicate team. |
| Invalid input | Wrong deal/roster counts, bad game code, empty player name all rejected without crashing. |
| Single-team mode | Handoff test mode; late-add fallback when there are no received cards. |

## Manual test checklist (one facilitator, one laptop, extra browser tabs)

Use two browser windows for two teams plus a projector tab. This mirrors the live
flow without needing other devices.

1. **Smoke test / single team** - Create game; join one team (4 players); assign
   roles; open+close trading; register a course; deal 15 / draft 10; confirm
   Starting Conditions show meters and a roster-pattern note; complete handoff
   test mode; play CP1 through the outcome. Confirm no error pages.
2. **Two-team test** - Join a second team (6 players) in the same game before
   trading. Confirm both appear on the GM screen and projector, and that the
   handoff names each team's send/receive partner correctly.
3. **4-player and 6-player** - Covered by teams above; confirm private checks run
   the right number of player turns (4 and 6).
4. **Course trading** - With trading OPEN, confirm a team's screen says trading is
   open and will not register a course; after CLOSE, confirm it registers.
5. **Draft** - Try to submit 14 and 16 dealt cards and 9 and 11 roster cards;
   confirm each is rejected with a clear message.
6. **Handoff** - Confirm a receiver cannot confirm before the sender passes; GM
   screen shows passed/received status.
7. **Re-entry** - Mid-setup and mid-checkpoint, close the team tab and re-join
   with the same code + team name; confirm it resumes in place and does not create
   a second team or re-run the handoff.
8. **CP1** - Confirm the syllabus-vs-Canvas due-date detail appears; submit up to
   3 actions; confirm the narrative reflects the choices.
9. **CP2 late adds** - Confirm 2-4 students join with full cards shown; confirm
   resources do not jump on arrival (only the onboarding decision moves them).
10. **CP3** - Confirm multiple-cause evidence (engagement + alignment); submit.
11. **CP4** - Confirm priority + supporting selection; if you first drive a
    resource Critical, confirm the "outreach to everyone" option disappears.
12. **CP5** - Confirm Protect / Repair / Do-Not-Add selection.
13. **Private checks (every checkpoint)** - After each of CP1-CP5 confirm the
    pass-the-device flow: each player sees their own allegiance once, a mission
    Claim/Not Yet, then a Suspicion Vote; the device returns to the pass gate
    between players; a vague report shows at the end. Confirm a player's vote
    actually advances to the next player (regression for the vote-not-saving bug).
14. **Emergency Sabotage** - As the Thunderflunked, confirm the Activate / Save For
    Later option appears inside your private turn at CP2, CP3, or CP4, that it can
    be used only once total, and that Save For Later continues your turn normally.
15. **Final accusation / outcome** - Proceed to final; accuse; confirm the reveal,
    Survived/Did Not Survive, final resources, recap, and debrief.
16. **Projector privacy** - Throughout, confirm the projector never shows
    allegiance, missions, votes, suspicion, sabotage, or player names.
17. **GM visibility** - Confirm the GM screen shows allegiance, current resources,
    checkpoint decisions, private-check progress, sabotage, accusation, outcome.
18. **Invalid submissions** - Enter a bad game code and an empty player name;
    confirm friendly error pages, not crashes.

## Notes

- State is in memory; restarting the server resets everything. Do not run with
  `FACULTY_GAME_DEBUG=1` during live play (the reloader would restart and wipe
  state).
- There is no auto-refresh; use each screen's Refresh button after GM actions.
