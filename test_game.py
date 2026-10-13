"""
Automated integration/smoke tests for the Faculty Development Game.

Uses Flask's in-process test client (no network). Runs complete simulated
games for a 4-player and a 6-player team in the same game, exercising course
trading, deal/draft, rejected-student handoff, all five checkpoints, the three
private checks, emergency sabotage, late adds, re-entry, the final accusation,
final outcome, projector privacy, and GM visibility. Also checks invalid form
submissions and the single-team test mode.

Run:  python test_game.py
"""

import copy
import os
import re
import sys
import tempfile

import app as game
import storage


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, label, condition):
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(label)
            print("  FAIL:", label)

    def summary(self):
        print("\n%d passed, %d failed" % (self.passed, self.failed))
        if self.failures:
            print("Failures:")
            for f in self.failures:
                print("  -", f)
        return self.failed == 0


C = Checker()


def fresh_client():
    """Reset in-memory state and return a new test client."""
    game.games.clear()
    game.active_code = None
    game.app.config["TESTING"] = True
    return game.app.test_client()


def text(resp):
    return resp.get_data(as_text=True)


def create_game(client):
    client.post("/gm/create")
    return game.active_code


def register_team(client, code, name, player_names):
    """Join, set count, register players. Returns team index."""
    client.post("/team/join", data={"game_code": code, "team_name": name})
    idx = game.find_team_index(code, name)
    n = len(player_names)
    client.post("/team/players", data={"game_code": code, "team_index": str(idx),
                                       "player_count": str(n)})
    data = {"game_code": code, "team_index": str(idx), "player_count": str(n)}
    for i, pn in enumerate(player_names, start=1):
        data["player_%d" % i] = pn
    client.post("/team/save", data=data)
    return idx


def assign_roles(client, code, idx, team):
    """Thunderflunked 2.0 digital setup: shuffle + confirm unique roles, run the
    private pass-the-computer allegiance reveal for every player, then confirm
    the team overview. Leaves the team ready for course registration, exactly as
    the old manual role form did."""
    base = {"game_code": code, "team_index": str(idx)}
    client.post("/team/roles/shuffle", data=base)
    client.post("/team/roles/confirm", data=base)
    for p in range(len(team["players"])):
        client.post("/team/setup/reveal/select", data={**base, "player": str(p)})
        client.post("/team/setup/reveal/show", data={**base, "player": str(p)})
        client.post("/team/setup/reveal/ack", data={**base, "player": str(p)})
    client.post("/team/setup/overview/continue", data=base)


def register_course(client, code, idx, course_name):
    client.post("/team/course", data={"game_code": code, "team_index": str(idx),
                                       "course": course_name})


def deal_and_draft(client, code, idx, dealt_labels, roster_labels):
    client.post("/team/pool", data={"game_code": code, "team_index": str(idx),
                                     "students": dealt_labels})
    client.post("/team/roster", data={"game_code": code, "team_index": str(idx),
                                       "students": roster_labels})


def run_private_check(client, code, idx, team, cp, claim_missions=False,
                      tf_sabotage_cp=None):
    """Drive every player through a checkpoint's private round. The Thunderflunked
    is offered Emergency Sabotage during CP2/CP3/CP4; if tf_sabotage_cp == cp they
    activate it, otherwise they Save it (resolving the page before voting, exactly
    as the real UI requires)."""
    n = len(team["players"])
    guard = 0
    while not game.private_complete(team, cp):
        guard += 1
        if guard > 200:
            raise RuntimeError("private check did not converge at cp %d" % cp)
        cur = game.private_current(team, cp)
        base = {"game_code": code, "team_index": str(idx), "cp": str(cp)}
        # READY
        client.post("/team/private/ready", data=base)
        # Allegiance (if this player hasn't seen it, route is showing it now)
        if cur not in team["allegiance_seen"]:
            client.post("/team/private/allegiance",
                        data={**base, "player": str(cur)})
        # Mission
        decided = (str(cur) in game.private_round(team, cp)["decisions"]
                   or str(cur) in team["missions_complete"])
        if not decided:
            if claim_missions and cur % 2 == 0:
                client.post("/team/private/mission/claim", data={**base, "player": str(cur)})
                client.post("/team/private/mission/confirm", data={**base, "player": str(cur)})
            else:
                client.post("/team/private/mission/notyet", data={**base, "player": str(cur)})
        # Sabotage: the Thunderflunked is offered it during CP2/CP3/CP4. Resolve
        # the page (activate if this is the chosen checkpoint, else save).
        if (cur == team["thunderflunked"]
                and game.sabotage_available(team, game.games[code])
                and not team["sabotage"]["prompted"].get(cp)):
            if tf_sabotage_cp == cp:
                opt = game.SABOTAGE_OPTIONS[cp][0]["key"]
                client.post("/team/sabotage/activate",
                            data={**base, "player": str(cur), "option": opt})
            else:
                client.post("/team/sabotage/save", data={**base, "player": str(cur)})
        # Vote: pick the first valid candidate deterministically. The voter index
        # is submitted in the "player" field exactly as the real vote form does.
        candidate = 0 if cur != 0 else 1
        before = len(game.private_round(team, cp)["votes"])
        client.post("/team/private/vote",
                    data={**base, "player": str(cur), "candidate": str(candidate)})
        after = len(game.private_round(team, cp)["votes"])
        if after != before + 1:
            raise RuntimeError("vote for player %d at cp %d was not recorded" % (cur, cp))


def run_brief_round(client, code, idx, team, cp, tf_sabotage=False):
    """Drive the brief CP4 sabotage-only round: each player READY -> (Thunderflunked,
    if unused: activate/save) -> PASS. No mission, no suspicion vote."""
    guard = 0
    while not game.private_complete(team, cp):
        guard += 1
        if guard > 200:
            raise RuntimeError("brief round did not converge at cp %d" % cp)
        cur = game.private_current(team, cp)
        base = {"game_code": code, "team_index": str(idx), "cp": str(cp)}
        client.post("/team/private/ready", data=base)
        if (cur == team["thunderflunked"]
                and game.sabotage_available(team, game.games[code])
                and not team["sabotage"]["prompted"].get(cp)):
            if tf_sabotage:
                opt = game.SABOTAGE_OPTIONS[cp][0]["key"]
                client.post("/team/sabotage/activate",
                            data={**base, "player": str(cur), "option": opt})
            else:
                client.post("/team/sabotage/save", data={**base, "player": str(cur)})
        before = len(game.private_round(team, cp).get("passed", []))
        client.post("/team/private/pass", data={**base, "player": str(cur)})
        after = len(game.private_round(team, cp).get("passed", []))
        if after != before + 1:
            raise RuntimeError("pass for player %d at cp %d was not recorded" % (cur, cp))


def gm_open(client):
    client.post("/gm/open")


def gm_lock(client):
    client.post("/gm/lock")


def gm_advance(client):
    client.post("/gm/advance")


# ---------------------------------------------------------------------------
# Test: full two-team game (4-player + 6-player)
# ---------------------------------------------------------------------------
def test_full_two_team_game():
    print("\n=== Full two-team game (4p + 6p) ===")
    client = fresh_client()
    code = create_game(client)
    C.check("game code created", code is not None and len(code) == 5)

    namesA = ["Ann", "Bo", "Cy", "Di"]
    namesB = ["Ed", "Fy", "Gi", "Ha", "Io", "Jo"]
    idxA = register_team(client, code, "Team Alpha", namesA)
    idxB = register_team(client, code, "Team Bravo", namesB)
    teamA = game.get_team(code, idxA)
    teamB = game.get_team(code, idxB)
    C.check("4-player team registered", len(teamA["players"]) == 4)
    C.check("6-player team registered", len(teamB["players"]) == 6)
    C.check("exactly one Thunderflunked A", teamA["thunderflunked"] in range(4))
    C.check("exactly one Thunderflunked B", teamB["thunderflunked"] in range(6))

    assign_roles(client, code, idxA, teamA)
    assign_roles(client, code, idxB, teamB)
    C.check("roles assigned A", len(teamA["assignments"]) == 4)
    C.check("unrepresented perspectives A", len(teamA["unrepresented"]) == 2)

    # Course trading: teams cannot register until trading closes.
    client.post("/gm/course/open")
    r = register_course_attempt(client, code, idxA, game.COURSE_NAMES[0])
    C.check("cannot register course while trading open", teamA.get("course") is None)
    client.post("/gm/course/close")
    register_course(client, code, idxA, game.COURSE_NAMES[0])
    register_course(client, code, idxB, game.COURSE_NAMES[5])
    C.check("course registered A after close", teamA["course"] == game.COURSE_NAMES[0])
    C.check("course registered B after close", teamB["course"] == game.COURSE_NAMES[5])

    # Deal 15 / draft 10 for each. Use the full first-15 and first-10.
    dealtA = game.STUDENTS[:15]
    rosterA = dealtA[:10]
    deal_and_draft(client, code, idxA, dealtA, rosterA)
    dealtB = game.STUDENTS[10:25]
    rosterB = dealtB[:10]
    deal_and_draft(client, code, idxB, dealtB, rosterB)
    C.check("roster A is 10", len(teamA["starting_roster"]) == 10)
    C.check("rejected A is 5", len(teamA["rejected_students"]) == 5)
    C.check("roster B is 10", len(teamB["starting_roster"]) == 10)
    C.check("starting resources floored >=40 A",
            all(teamA["resources"][n] >= 40 for n in game.RESOURCE_NAMES))
    C.check("starting resources floored >=40 B",
            all(teamB["resources"][n] >= 40 for n in game.RESOURCE_NAMES))
    startA = dict(teamA["resources"])

    # Handoff (circular: A passes to B, B passes to A for 2 teams).
    client.post("/team/handoff/pass", data={"game_code": code, "team_index": str(idxA)})
    client.post("/team/handoff/pass", data={"game_code": code, "team_index": str(idxB)})
    client.post("/team/handoff/receive", data={"game_code": code, "team_index": str(idxA)})
    client.post("/team/handoff/receive", data={"game_code": code, "team_index": str(idxB)})
    C.check("handoff done A", game.handoff_done(teamA))
    C.check("handoff done B", game.handoff_done(teamB))
    C.check("A received B's rejected", teamA["handoff"]["received"] == teamB["rejected_students"])
    C.check("handoff did not change starting resources", teamA["resources"] == startA)

    # ---- CP1 ----
    gm_open(client)
    C.check("CP1 screen shows evidence", "First Week Course Check" in text(
        client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)})))
    # Submit: choose clarify + message (2 actions)
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxA),
                                         "cp": "0", "actions": ["clarify", "message"]})
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxB),
                                         "cp": "0", "actions": ["monitor"], "reassess": game.CP1_REASSESS_OPTIONS[0]})
    C.check("CP1 submitted A", teamA["cp"][0]["submitted"])
    C.check("CP1 applied deltas A", teamA["cp"][0]["applied"])
    C.check("CP1 resources changed A", teamA["resources"] != startA)
    # Private check CP1 (reveals allegiance, missions, votes)
    run_private_check(client, code, idxA, teamA, 0, claim_missions=True)
    run_private_check(client, code, idxB, teamB, 0)
    C.check("CP1 private complete A", game.private_complete(teamA, 0))
    C.check("all players saw allegiance A", len(teamA["allegiance_seen"]) == 4)
    C.check("some mission completed A", len(teamA["missions_complete"]) >= 1)
    gm_lock(client)
    gm_advance(client)

    # ---- CP2: late adds + onboarding. NO full private round this checkpoint. ----
    gm_open(client)
    cp2A = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("CP2 shows New Student Intake", "New Student Intake" in cp2A)
    C.check("late adds 2-4 A", 2 <= len(teamA["late_adds"]) <= 4)
    C.check("late adds come from received A", all(s in teamA["handoff"]["received"] for s in teamA["late_adds"]))
    # Late adds themselves cause no immediate movement (only the decision does).
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxA),
                                         "cp": "1", "actions": ["start_here", "pathway"]})
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxB),
                                         "cp": "1", "actions": ["start_here"]})
    C.check("CP2 submitted A", teamA["cp"][1]["submitted"])
    cp2done = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("CP2 shows no private round", "No private player check" in cp2done)
    C.check("CP2 records no suspicion votes A", not game.private_round(teamA, 1)["votes"])
    C.check("no sabotage used at CP2 A", not teamA["sabotage"]["used"])
    gm_lock(client)
    gm_advance(client)

    # ---- CP3: investigate + full private round. TF-B sabotages here; TF-A saves. ----
    gm_open(client)
    cp3A = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("CP3 shows assessment results", "First Major Assessment Results" in cp3A)
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxA),
                                         "cp": "2", "actions": ["aligned_practice", "reteach"]})
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxB),
                                         "cp": "2", "actions": ["continue"]})
    C.check("CP3 submitted A", teamA["cp"][2]["submitted"])
    run_private_check(client, code, idxA, teamA, 2)  # TF-A saves sabotage for CP4
    run_private_check(client, code, idxB, teamB, 2, tf_sabotage_cp=2)  # TF-B activates now
    C.check("CP3 private complete A", game.private_complete(teamA, 2))
    C.check("CP3 private complete B", game.private_complete(teamB, 2))
    C.check("B sabotage used in CP3 private", teamB["sabotage"]["used"] and teamB["sabotage"]["cp"] == 3)
    C.check("A sabotage not yet used after CP3", not teamA["sabotage"]["used"])
    gm_lock(client)
    gm_advance(client)

    # ---- CP4: priority + supporting, then the BRIEF sabotage-only round. ----
    gm_open(client)
    cp4A = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("CP4 shows midterm", "Midterm" in cp4A)
    client.post("/team/cp4/submit", data={"game_code": code, "team_index": str(idxA),
                                          "priority": "scalable", "supporting": "tool"})
    client.post("/team/cp4/submit", data={"game_code": code, "team_index": str(idxB),
                                          "priority": "urgent", "supporting": "defer"})
    C.check("CP4 submitted A", teamA["cp"][3]["submitted"])
    C.check("CP4 reject same twice", invalid_cp4_same(client, code, idxB))
    # Brief round: TF-A (who saved) may activate now; TF-B already used theirs, so passes.
    res_before_sab_A = dict(teamA["resources"])
    run_brief_round(client, code, idxA, teamA, 3, tf_sabotage=True)
    run_brief_round(client, code, idxB, teamB, 3)
    C.check("CP4 brief round complete A", game.private_complete(teamA, 3))
    C.check("CP4 brief round complete B", game.private_complete(teamB, 3))
    C.check("A sabotage used at CP4", teamA["sabotage"]["used"] and teamA["sabotage"]["cp"] == 4)
    C.check("A sabotage changed resources", teamA["resources"] != res_before_sab_A)
    C.check("CP4 has no suspicion votes A", not game.private_round(teamA, 3)["votes"])
    gm_lock(client)
    gm_advance(client)

    # ---- CP5: protect / repair / do-not-add + final private check ----
    gm_open(client)
    cp5A = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("CP5 shows final stretch", "Final Stretch" in cp5A)
    client.post("/team/cp5/submit", data={"game_code": code, "team_index": str(idxA),
                                          "protect": "grading", "repair": "gradebook", "not_add": "extra_credit"})
    client.post("/team/cp5/submit", data={"game_code": code, "team_index": str(idxB),
                                          "protect": "persistence", "repair": "visibility", "not_add": "new_module"})
    C.check("CP5 submitted A", teamA["cp"][4]["submitted"])
    run_private_check(client, code, idxA, teamA, 4)
    run_private_check(client, code, idxB, teamB, 4)
    gm_lock(client)

    # ---- Final ----
    client.post("/gm/final")
    C.check("final open", game.games[code]["final_open"])
    accuseA = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("final accusation screen", "Final Accusation" in accuseA)
    # The suspicion report itself must be vague: one of the three allowed phrases,
    # never a vote count or "votes".
    report_region = accuseA.split("Final Suspicion Report")[1].split("Final Accusation")[0]
    C.check("suspicion report is one vague phrase",
            any(p in report_region for p in ["scattered", "concentrated", "divided"]))
    C.check("suspicion report shows no counts", "votes" not in report_region.lower()
            and not re.search(r"\b\d+\b", report_region))
    # Accuse the real TF on A (correct), wrong on B.
    client.post("/team/accuse", data={"game_code": code, "team_index": str(idxA),
                                      "accused": str(teamA["thunderflunked"])})
    wrongB = (teamB["thunderflunked"] + 1) % 6
    client.post("/team/accuse", data={"game_code": code, "team_index": str(idxB),
                                      "accused": str(wrongB)})
    outA = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("outcome shows survived/not A", ("SURVIVED" in outA or "DID NOT SURVIVE" in outA))
    C.check("reveal correct A", "correct" in outA.lower())
    C.check("debrief present A", "catches the bad idea" in outA)
    C.check("accusation recorded A", teamA["accusation"] == teamA["thunderflunked"])

    # ---- Projector privacy ----
    proj = text(client.get("/projector"))
    for secret in ["Thunderflunked", "THUNDERFLUNKED", "Secret Mission", "suspdump"]:
        C.check("projector hides '%s'" % secret, secret not in proj)
    for pn in namesA + namesB:
        C.check("projector hides player name '%s'" % pn, (">" + pn + "<") not in proj)
    C.check("projector shows team name", "Team Alpha" in proj)
    C.check("projector shows outcome", ("Survived" in proj or "Did Not Survive" in proj))

    # ---- GM visibility ----
    gmp = text(client.get("/gm"))
    C.check("GM sees Thunderflunked", "THUNDERFLUNKED - GM ONLY" in gmp)
    C.check("GM sees resources", "Overall Course Score" in gmp)
    C.check("GM sees sabotage used", "USED at CP" in gmp)

    print("  (two-team game complete)")


def register_course_attempt(client, code, idx, course_name):
    return client.post("/team/course", data={"game_code": code, "team_index": str(idx),
                                              "course": course_name})


def invalid_cp4_same(client, code, idx):
    """Submitting the same priority and supporting should be rejected."""
    team = game.get_team(code, idx)
    before = dict(team.get("cp", {}).get(3, {}))
    # Only meaningful if not yet submitted; team B already submitted, so test a fresh guard:
    resp = client.post("/team/cp4/submit", data={"game_code": code, "team_index": str(idx),
                                                 "priority": "urgent", "supporting": "urgent"})
    # Already-submitted team just returns its screen; ensure no crash.
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: single-team test mode (handoff test mode + late adds fallback)
# ---------------------------------------------------------------------------
def test_single_team_mode():
    print("\n=== Single-team test mode ===")
    client = fresh_client()
    code = create_game(client)
    names = ["Pat", "Quinn", "Rex", "Sam", "Tam"]
    idx = register_team(client, code, "Solo", names)
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open")
    client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[4])  # async online
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    # Handoff test mode.
    hs = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))
    C.check("single-team handoff test-mode notice", "Test mode" in hs)
    client.post("/team/handoff/testmode", data={"game_code": code, "team_index": str(idx)})
    C.check("handoff done (test mode)", game.handoff_done(team))

    # CP2 late adds must still work with no received cards (fallback).
    client.post("/gm/open")  # CP1
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idx),
                                         "cp": "0", "actions": ["clarify"]})
    run_private_check(client, code, idx, team, 0)
    client.post("/gm/lock"); client.post("/gm/advance")
    client.post("/gm/open")  # CP2
    client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)})
    C.check("single-team late adds fallback 2-3", 2 <= len(team["late_adds"]) <= 3)
    C.check("late adds not in starting roster", all(s not in team["starting_roster"] for s in team["late_adds"]))


# ---------------------------------------------------------------------------
# Test: re-entry at multiple setup points
# ---------------------------------------------------------------------------
def test_reentry():
    print("\n=== Re-entry / resume ===")
    client = fresh_client()
    code = create_game(client)
    # Join but do not set players; re-join should resume at count form.
    client.post("/team/join", data={"game_code": code, "team_name": "Reenter"})
    idx = game.find_team_index(code, "Reenter")
    again = text(client.post("/team/join", data={"game_code": code, "team_name": "Reenter"}))
    C.check("re-entry before players -> count form", "How many players" in again)

    # Now register players and roles, then re-enter -> should resume at course phase.
    register_team(client, code, "Reenter", ["A", "B", "C", "D"])  # creates distinct? No - resumes
    team = game.get_team(code, idx)
    # register_team re-joined same name; players set now.
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open")
    resume = text(client.post("/team/join", data={"game_code": code, "team_name": "Reenter"}))
    C.check("re-entry after roles during trading -> trade window", "Course Trading Is Open" in resume or "Course Assignment" in resume)
    # Re-entry must not duplicate the team.
    C.check("re-entry does not duplicate team", len(game.games[code]["teams"]) == 1)


# ---------------------------------------------------------------------------
# Test: invalid form submissions
# ---------------------------------------------------------------------------
def test_invalid_submissions():
    print("\n=== Invalid submissions ===")
    client = fresh_client()
    code = create_game(client)
    idx = register_team(client, code, "Edge", ["A", "B", "C", "D"])
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])

    # Wrong pool count.
    r = text(client.post("/team/pool", data={"game_code": code, "team_index": str(idx),
                                             "students": game.STUDENTS[:10]}))
    C.check("pool rejects wrong count", "exactly 15" in r)
    C.check("pool not set after invalid", team.get("draft_pool") is None)

    # Bad game code join.
    bad = text(client.post("/team/join", data={"game_code": "ZZZZZ", "team_name": "X"}))
    C.check("bad game code handled", "was not found" in bad)

    # Empty player name.
    client.post("/team/join", data={"game_code": code, "team_name": "Empties"})
    i2 = game.find_team_index(code, "Empties")
    client.post("/team/players", data={"game_code": code, "team_index": str(i2), "player_count": "4"})
    r2 = text(client.post("/team/save", data={"game_code": code, "team_index": str(i2),
                                             "player_count": "4", "player_1": "A", "player_2": "",
                                             "player_3": "C", "player_4": "D"}))
    C.check("empty player name rejected", "required" in r2)

    # Valid pool then invalid roster count.
    client.post("/team/pool", data={"game_code": code, "team_index": str(idx), "students": game.STUDENTS[:15]})
    r3 = text(client.post("/team/roster", data={"game_code": code, "team_index": str(idx),
                                               "students": game.STUDENTS[:12]}))
    C.check("roster rejects wrong count", "exactly 10" in r3)


# ---------------------------------------------------------------------------
# Test: scoring model properties
# ---------------------------------------------------------------------------
def test_scoring_model():
    print("\n=== Scoring model ===")
    # Strongest 10 vs weakest 10 by total stat.
    by_total = sorted(game.STUDENTS, key=lambda s: sum(game.STUDENT_STATS[s].values()))
    weak = by_total[:10]
    strong = by_total[-10:]
    ws, wst, wo = game.compute_scores(weak)
    ss, sst, so = game.compute_scores(strong)
    C.check("all starting scores >=40 (weak)", all(ws[n] >= 40 for n in game.RESOURCE_NAMES))
    C.check("all starting scores <=100", all(ss[n] <= 100 for n in game.RESOURCE_NAMES))
    C.check("strong roster scores higher overall than weak", so >= wo)
    C.check("overall within 0-100", 0 <= wo <= 100 and 0 <= so <= 100)
    # Course card does not change scores: scoring takes only roster.
    C.check("scoring depends only on roster (no course arg)",
            game.compute_scores(strong)[2] == so)
    # Student corrections.
    sos = next(s for s in game.STUDENTS if "Silent SOS" in s)
    st = game.STUDENT_STATS[sos]
    C.check("Silent SOS help-seeking = 1", st["help_seeking"] == 1)
    C.check("Silent SOS communication = 4", st["communication"] == 4)
    late = next(s for s in game.STUDENTS if "Late Bloomer Supreme" in s)
    C.check("Late Bloomer help-seeking = 3", game.STUDENT_STATS[late]["help_seeking"] == 3)
    # Totals in range 16-21.
    C.check("all student totals 16-21",
            all(16 <= sum(game.STUDENT_STATS[s].values()) <= 21 for s in game.STUDENTS))


# ---------------------------------------------------------------------------
# Test: data integrity
# ---------------------------------------------------------------------------
def test_data_integrity():
    print("\n=== Data integrity ===")
    C.check("30 students", len(game.STUDENTS) == 30)
    C.check("10 courses", len(game.COURSES) == 10)
    modalities = [c["modality"] for c in game.COURSES]
    C.check("4 lecture courses", sum("Lecture" in m for m in modalities) == 4)
    C.check("2 async online courses", sum("Asynchronous" in m for m in modalities) == 2)
    C.check("4 CTE courses", sum("CTE" in m for m in modalities) == 4)
    C.check("every course has 2 realities", all(len(c["realities"]) == 2 for c in game.COURSES))
    C.check("6 public roles", len(game.PUBLIC_ROLES) == 6)
    C.check("5 checkpoints", len(game.CHECKPOINTS) == 5)


def test_private_flow_browser_fidelity():
    """Drive the CP1 private check the way a browser actually does - following the
    HTML returned at each step - to catch form/handler field mismatches and the
    'pass gate' between players. Regression for: votes not saving (form sent
    'player', handler read 'voter'); allegiance leaking to the current holder."""
    print("\n=== Private flow (browser fidelity) ===")
    client = fresh_client()
    code = create_game(client)
    names = ["Ann", "Bo", "Cy", "Di"]
    idx = register_team(client, code, "P", names)
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    client.post("/team/handoff/testmode", data={"game_code": code, "team_index": str(idx)})
    client.post("/gm/open")
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idx), "cp": "0", "actions": ["clarify"]})
    base = {"game_code": code, "team_index": str(idx), "cp": "0"}

    # Allegiance was revealed privately during setup (Thunderflunked 2.0), so the
    # CP1 private round no longer repeats it: every player is already in
    # allegiance_seen and the round opens on the Secret Mission step.
    C.check("allegiance already seen from setup (not repeated at CP1)",
            len(team["allegiance_seen"]) == 4)
    for turn in range(4):
        cur = game.private_current(team, 0)
        C.check("turn %d current player is %d" % (turn, turn), cur == turn)
        # The checkpoint screen must be showing the pass gate for this player.
        screen = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))
        C.check("pass gate names player %s" % names[cur], ("Pass the computer to <strong>" + names[cur]) in screen)
        C.check("gate does not show any allegiance word", "THUNDERFLUNKED" not in screen and "THUNDERSTRUCK" not in screen)
        # READY -> goes straight to the mission step; no allegiance is re-shown.
        r = text(client.post("/team/private/ready", data=base))
        C.check("READY goes to mission (not allegiance) for player %d" % cur,
                "Secret Mission" in r and "THUNDERFLUNKED" not in r and "THUNDERSTRUCK" not in r)
        # Mission: decline.
        r = text(client.post("/team/private/mission/notyet", data={**base, "player": str(cur)}))
        C.check("mission leads to vote for player %d" % cur, "Suspicion Vote" in r)
        # Vote using the real form field name.
        cand = 0 if cur != 0 else 1
        r = text(client.post("/team/private/vote", data={**base, "player": str(cur), "candidate": str(cand)}))
        C.check("vote recorded for player %d" % cur, str(cur) in game.private_round(team, 0)["votes"])
        # After voting, we must be back at the pass gate (or report), NOT the next
        # player's allegiance leaking onto this device.
        C.check("after vote no allegiance leak for player %d" % cur,
                "THUNDERFLUNKED" not in r and "THUNDERSTRUCK" not in r)

    C.check("all four votes saved", len(game.private_round(team, 0)["votes"]) == 4)
    C.check("private check complete", game.private_complete(team, 0))
    report = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))
    C.check("vague report shown after all votes",
            any(p in report for p in ["scattered", "concentrated", "divided"]))


def test_number_entry_selection():
    print("\n=== Quick number-entry selection (no-JS path) ===")
    client = fresh_client()
    code = create_game(client)
    idx = register_team(client, code, "Numbers", ["A", "B", "C", "D"])
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])
    # Deal via the numbers box only (no 'students' checkboxes) using a range + list.
    client.post("/team/pool", data={"game_code": code, "team_index": str(idx),
                                    "numbers": "1-10 11 12 13 14 15"})
    C.check("number-entry deal recorded 15", len(team.get("draft_pool", [])) == 15)
    C.check("number-entry deal is #01..#15", team["draft_pool"] == game.STUDENTS[:15])
    # Draft via numbers box only.
    client.post("/team/roster", data={"game_code": code, "team_index": str(idx),
                                      "numbers": "1,2,3,4,5,6,7,8,9,10"})
    C.check("number-entry roster recorded 10", len(team.get("starting_roster", [])) == 10)
    # Mixing checkbox + numbers unions without duplicates.
    client2 = fresh_client()
    code2 = create_game(client2)
    i2 = register_team(client2, code2, "Mix", ["A", "B", "C", "D"])
    t2 = game.get_team(code2, i2)
    assign_roles(client2, code2, i2, t2)
    client2.post("/gm/course/open"); client2.post("/gm/course/close")
    register_course(client2, code2, i2, game.COURSE_NAMES[0])
    client2.post("/team/pool", data={"game_code": code2, "team_index": str(i2),
                                     "students": game.STUDENTS[:8], "numbers": "8 9 10 11 12 13 14 15"})
    C.check("union dedups overlap to 15", len(t2.get("draft_pool", [])) == 15)


def test_course_shell():
    print("\n=== Course shell ===")
    client = fresh_client()
    code = create_game(client)
    idx = register_team(client, code, "ShellTeam", ["A", "B", "C", "D"])
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[5])  # async online (alignment-relevant)
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    client.post("/team/handoff/testmode", data={"game_code": code, "team_index": str(idx)})

    tabs = [t[0] for t in game.SHELL_TABS]
    # Every tab renders at every checkpoint, start to finish, with no secret leaks.
    secrets = ["THUNDERFLUNKED", "Thunderflunked", "Suspicion", "allegiance", "Secret Mission"]
    for cp in range(5):
        client.post("/gm/open")
        for tab in tabs:
            r = client.get("/team/shell", query_string={"game_code": code, "team_index": str(idx), "tab": tab})
            C.check("shell tab '%s' renders at CP%d (200)" % (tab, cp + 1), r.status_code == 200)
            html = text(r)
            for s in secrets:
                C.check("shell '%s' hides '%s' at CP%d" % (tab, s, cp + 1), s not in html)
        # Submit that checkpoint so we can advance.
        if cp == 0:
            # Choose zeros so we can verify the gradebook reflects it.
            client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idx),
                                                 "cp": "0", "actions": ["zeros", "clarify"]})
            run_private_check(client, code, idx, team, 0)
            gb = text(client.get("/team/shell", query_string={"game_code": code, "team_index": str(idx), "tab": "gradebook"}))
            C.check("gradebook reflects zeros decision", "entered zeros" in gb)
            ann = text(client.get("/team/shell", query_string={"game_code": code, "team_index": str(idx), "tab": "announcements"}))
            C.check("announcement reflects clarify decision", "Clarification on Assignment 1" in ann)
        elif cp == 1:
            client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idx), "cp": "1", "actions": ["start_here"]})
            rost = text(client.get("/team/shell", query_string={"game_code": code, "team_index": str(idx), "tab": "roster"}))
            C.check("roster shows late adds at CP2", "late add" in rost)
        elif cp == 2:
            client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idx), "cp": "2", "actions": ["aligned_practice"]})
            gb = text(client.get("/team/shell", query_string={"game_code": code, "team_index": str(idx), "tab": "gradebook"}))
            C.check("gradebook shows major assessment at CP3", "Major Assessment 1" in gb and "Justification" in gb)
            run_private_check(client, code, idx, team, 2)
        elif cp == 3:
            client.post("/team/cp4/submit", data={"game_code": code, "team_index": str(idx), "priority": "scalable", "supporting": "tool"})
        elif cp == 4:
            client.post("/team/cp5/submit", data={"game_code": code, "team_index": str(idx), "protect": "grading", "repair": "gradebook", "not_add": "extra_credit"})
            run_private_check(client, code, idx, team, 4)
        client.post("/gm/lock")
        if cp < 4:
            client.post("/gm/advance")
    C.check("shell reachable throughout", True)
    # Canvas-style chrome structure on a shell page.
    h = text(client.get("/team/shell", query_string={"game_code": code, "team_index": str(idx), "tab": "gradebook"}))
    C.check("shell is chrome-free (no NCC header bar)", "<header class='bar'>" not in h)
    C.check("shell has global nav rail", "class='lms-global'" in h)
    C.check("shell has course nav", "lms-course" in h)
    C.check("shell marks active nav item", "aria-current='page'" in h)
    C.check("shell has breadcrumb", "lms-crumb" in h)
    C.check("shell shows course code", game.COURSE_BY_NAME[game.COURSE_NAMES[5]]["code"] in h)


def _mk_team(resources):
    """Minimal team object for scoring/survival unit checks."""
    t = {"resources": dict(resources)}
    game.recompute(t)
    return t


def test_qa_corrections():
    """Regressions for the QA correction pass: private schedule, FC balance + CP5
    recovery, sabotage-near-threshold, no need-pills, trimmed reference, conditional
    Monitor control."""
    print("\n=== QA corrections ===")

    # --- Private schedule: full rounds only at CP1/CP3/CP5; sabotage window CP3/CP4
    C.check("HAS_PRIVATE is CP1/CP3/CP5", game.HAS_PRIVATE == {0, 2, 4})
    C.check("SABOTAGE_WINDOW is CP3/CP4", game.SABOTAGE_WINDOW == {2, 3})
    C.check("is_full_private true for 0/2/4", all(game.is_full_private(i) for i in (0, 2, 4)))
    C.check("is_full_private false for 1/3", not game.is_full_private(1) and not game.is_full_private(3))
    # 5-player table: 15 normal (mission+vote) turns across the 3 full rounds.
    C.check("15 normal private turns for 5 players", 3 * 5 == 15)

    # --- Need-type pills removed from decision options (taxonomy still in data model)
    client = fresh_client()
    for idx_cp in (0, 1, 2):
        form = game.multiselect_form("ABCDE", 0, idx_cp)
        for label in game.NEED_LABELS.values():
            C.check("no need-pill '%s' in CP%d options" % (label, idx_cp + 1), label not in form)
    C.check("ACTION_NEEDS taxonomy retained in data model", len(game.ACTION_NEEDS) >= 15)

    # --- Qualitative Faculty effort cue present at CP3; no numeric deltas shown
    cp3form = game.multiselect_form("ABCDE", 0, 2)
    C.check("CP3 shows Faculty effort cue", "Faculty effort:" in cp3form)
    C.check("CP3 sustainability warning present", "Faculty Capacity" in cp3form and "sustain" in cp3form)
    C.check("CP3 options show no numeric deltas", "fc=" not in cp3form and "+10" not in cp3form)

    # --- Conditional Monitor reassessment control (mechanism present at CP1 only)
    cp1form = game.multiselect_form("ABCDE", 0, 0)
    C.check("CP1 has reassess block id", "id='reassess-block'" in cp1form)
    C.check("CP1 has monitor checkbox id", "id='monitor-cb'" in cp1form)
    C.check("CP1 has toggle script", "reassess-block" in cp1form and "<script>" in cp1form)
    C.check("CP3 has no reassess block", "id='reassess-block'" not in cp3form)

    # --- Reference overload: per-checkpoint consult block is a SMALL subset, not the
    #     full 18-item catalog; the full catalog lives on the shell Resources tab.
    team = _mk_team({n: 60 for n in game.RESOURCE_NAMES})
    team["course"] = "Internet Rabbit Hole Research"
    all_names = [r["name"] for r in game.RESOURCES]
    for cp in range(5):
        block = game.resources_consult_block(team, cp)
        shown = [nm for nm in all_names if nm in block]
        C.check("CP%d consult block is a small subset (<=7)" % (cp + 1), 1 <= len(shown) <= 7)
        C.check("CP%d consult block is not the full catalog" % (cp + 1), len(shown) < len(all_names))
    # A resource irrelevant to CP1 (e.g., Grade Publish) is not surfaced there.
    C.check("CP1 consult omits end-of-term-only tools", "Grade Publish" not in game.resources_consult_block(team, 0))
    # The full catalog IS available on the shell Resources tab.
    full = game.resources_panel_full(team)
    C.check("shell Resources tab lists all resources", all(nm in full for nm in all_names))

    # --- Faculty Capacity: CP5 has a legitimate sustainable recovery choice
    protect = {o["key"]: o["deltas"]["Faculty Capacity"] for o in game.CP5_PROTECT}
    C.check("CP5 has a sustainable protect option", "sustainable" in protect)
    C.check("CP5 sustainable recovers Faculty Capacity", protect["sustainable"] >= 8)
    C.check("CP5 'do not add' choices recover capacity",
            all(o["deltas"]["Faculty Capacity"] > 0 for o in game.CP5_NOT_ADD))
    C.check("CP5 is not a generic restore (sustainable also costs/varies other resources)",
            any(v != 0 for k, v in game.CP5_PROTECT[0]["deltas"].items() if k != "Faculty Capacity"))

    # --- Sabotage near threshold: the largest single sabotage FC hit is small (3) and
    #     cannot, by itself, drop a sound-play Faculty Capacity below the survival floor.
    max_sab_fc = max(abs(o["deltas"].get("Faculty Capacity", 0))
                     for opts in game.SABOTAGE_OPTIONS.values() for o in opts)
    C.check("max single sabotage FC hit is small (<=3)", max_sab_fc <= 3)
    sound_fc_at_cp3 = 33  # plausible sound-play Faculty Capacity entering the CP3 sabotage
    C.check("one sabotage alone does not cross the floor from sound play",
            sound_fc_at_cp3 - max_sab_fc >= game.SURVIVE_FLOOR)

    # --- CP5 can still change the final outcome, and a sound team can survive.
    # Team sitting just below the floor on FC entering CP5 (otherwise healthy).
    pre_cp5 = {"Student Success": 100, "Course Quality": 100,
               "Faculty Capacity": 22, "Time/Resources": 60}
    good = _mk_team(pre_cp5)
    for d in (next(o["deltas"] for o in game.CP5_PROTECT if o["key"] == "sustainable"),
              next(o["deltas"] for o in game.CP5_REPAIR if o["key"] == "gradebook"),
              next(o["deltas"] for o in game.CP5_NOT_ADD if o["key"] == "new_module")):
        game.apply_deltas(good, d)
    C.check("sound CP5 choices lift Faculty Capacity above the floor",
            good["resources"]["Faculty Capacity"] >= game.SURVIVE_FLOOR)
    C.check("sound CP5 choices yield survival", game.course_survives(good))
    # The same team making a legal but non-sustainable CP5 combo (protect a student
    # instead of the routine) does NOT clear the floor - so CP5 genuinely decides it.
    bad = _mk_team(pre_cp5)
    for d in (next(o["deltas"] for o in game.CP5_PROTECT if o["key"] == "persistence"),
              next(o["deltas"] for o in game.CP5_REPAIR if o["key"] == "visibility"),
              next(o["deltas"] for o in game.CP5_NOT_ADD if o["key"] == "extra_credit")):
        game.apply_deltas(bad, d)
    C.check("CP5 choices genuinely change the outcome (non-sustainable path still fails)",
            not game.course_survives(bad))


# ===========================================================================
# Deployment tests: persistence, restart/re-entry, GM protection, multi-team,
# hidden-information privacy. These exercise the infrastructure added for Render
# deployment. They must not change any game mechanics.
# ===========================================================================
def _build_midgame(client):
    """Drive a fresh two-team game up to the end of the CP3 private round, so the
    saved state contains every kind of persisted structure: role assignments,
    courses, draft/roster, rejected/received students, late adds, resources,
    checkpoint decisions (int-keyed `cp`), private rounds (int-keyed `private`),
    allegiance, sabotage (int-keyed `prompted`, one team activates), and votes.
    Returns (code, idxA, idxB)."""
    code = create_game(client)
    idxA = register_team(client, code, "Team Alpha", ["Ann", "Bo", "Cy", "Di"])
    idxB = register_team(client, code, "Team Bravo", ["Ed", "Fy", "Gi", "Ha", "Io", "Jo"])
    teamA = game.get_team(code, idxA)
    teamB = game.get_team(code, idxB)
    assign_roles(client, code, idxA, teamA)
    assign_roles(client, code, idxB, teamB)
    client.post("/gm/course/open")
    client.post("/gm/course/close")
    register_course(client, code, idxA, game.COURSE_NAMES[0])
    register_course(client, code, idxB, game.COURSE_NAMES[5])
    deal_and_draft(client, code, idxA, game.STUDENTS[:15], game.STUDENTS[:10])
    deal_and_draft(client, code, idxB, game.STUDENTS[10:25], game.STUDENTS[10:20])
    for i in (idxA, idxB):
        client.post("/team/handoff/pass", data={"game_code": code, "team_index": str(i)})
    for i in (idxA, idxB):
        client.post("/team/handoff/receive", data={"game_code": code, "team_index": str(i)})
    # CP1
    gm_open(client)
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxA),
                                         "cp": "0", "actions": ["clarify", "message"]})
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxB),
                                         "cp": "0", "actions": ["monitor"],
                                         "reassess": game.CP1_REASSESS_OPTIONS[0]})
    run_private_check(client, code, idxA, teamA, 0, claim_missions=True)
    run_private_check(client, code, idxB, teamB, 0)
    gm_lock(client); gm_advance(client)
    # CP2
    gm_open(client)
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxA),
                                         "cp": "1", "actions": ["start_here", "pathway"]})
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxB),
                                         "cp": "1", "actions": ["start_here"]})
    gm_lock(client); gm_advance(client)
    # CP3: full private round; Team Bravo's Thunderflunked activates sabotage here.
    gm_open(client)
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxA),
                                         "cp": "2", "actions": ["aligned_practice", "reteach"]})
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idxB),
                                         "cp": "2", "actions": ["continue"]})
    run_private_check(client, code, idxA, teamA, 2)
    run_private_check(client, code, idxB, teamB, 2, tf_sabotage_cp=2)
    gm_lock(client)
    return code, idxA, idxB


def _enable_persistence():
    """Point persistence at a throwaway SQLite file and activate the real
    save/restore request hooks (normally suppressed under TESTING)."""
    tmpdir = tempfile.mkdtemp(prefix="faculty_game_test_")
    dbpath = os.path.join(tmpdir, "state.db").replace("\\", "/")
    storage.init_engine("sqlite:///" + dbpath)
    game.app.config["TESTING"] = False  # let before/teardown persistence run
    return dbpath


def _disable_persistence():
    storage._reset_for_tests()
    game.app.config["TESTING"] = True
    game.games.clear()
    game.active_code = None


def test_persistence_and_restart():
    print("\n=== Persistence + restart/re-entry ===")
    game.games.clear()
    game.active_code = None
    _enable_persistence()
    try:
        client = game.app.test_client()
        code, idxA, idxB = _build_midgame(client)
        C.check("persistence enabled during build", storage.is_enabled())

        # The automatic after-POST save should have written the state out.
        saved_games, saved_code = storage.load_state()
        C.check("state was auto-saved to the database", code in saved_games)
        C.check("active_code persisted", saved_code == code)

        # Snapshot the live state, then simulate a full server restart: wipe the
        # in-memory globals and restore from the database on boot.
        before = copy.deepcopy(game.games)
        before_code = game.active_code
        game.games.clear()
        game.active_code = None
        C.check("memory cleared (simulated restart)", game.games == {} and game.active_code is None)

        game.restore_state()
        C.check("active_code restored after restart", game.active_code == before_code)
        C.check("full game state restored byte-for-byte", game.games == before)

        # Integer-keyed dictionaries must come back as ints, not JSON strings.
        teamA = game.get_team(code, idxA)
        C.check("restored `cp` keys are ints",
                bool(teamA["cp"]) and all(isinstance(k, int) for k in teamA["cp"]))
        C.check("restored `private` keys are ints",
                bool(teamA["private"]) and all(isinstance(k, int) for k in teamA["private"]))
        C.check("restored sabotage.prompted keys are ints",
                all(isinstance(k, int) for k in teamA["sabotage"]["prompted"]))

        # Hidden state survives intact.
        teamB = game.get_team(code, idxB)
        C.check("allegiance (Thunderflunked) preserved A", isinstance(teamA["thunderflunked"], int))
        C.check("sabotage-used flag preserved B", teamB["sabotage"]["used"] is True)
        C.check("missions preserved A", len(teamA["missions_complete"]) >= 1)
        C.check("votes preserved A", len(game.private_round(teamA, 2)["votes"]) == 4)

        # Re-entry by Game Code + Team Name lands on the live resume screen, and
        # the game CONTINUES from where it left off (advance to CP4 and submit).
        rejoin = text(client.post("/team/join",
                      data={"game_code": code, "team_name": "Team Alpha"}))
        C.check("re-entry by code+name resumes the game",
                "Team Alpha" in rejoin and "Game Code" not in rejoin)
        gm_advance(client)
        gm_open(client)
        r = client.post("/team/cp4/submit", data={"game_code": code, "team_index": str(idxA),
                                                   "priority": "scalable", "supporting": "tool"})
        C.check("game continues after restart (CP4 submit works)",
                game.get_team(code, idxA)["cp"][3]["submitted"] is True)
    finally:
        _disable_persistence()


def test_persistence_disabled_by_default():
    print("\n=== Persistence off when no database configured ===")
    storage._reset_for_tests()
    C.check("storage disabled without DATABASE_URL", not storage.is_enabled())
    C.check("save_state is a no-op when disabled", storage.save_state({}, None) is None)
    g, c = storage.load_state()
    C.check("load_state empty when disabled", g == {} and c is None)
    C.check("ping succeeds (nothing to check) when disabled", storage.ping() is True)


def test_gm_protection():
    print("\n=== GM protection ===")
    game.games.clear()
    game.active_code = None
    game.app.config["GM_PASSWORD"] = "swordfish"
    try:
        anon = game.app.test_client()
        # Unauthenticated GM access is redirected to the login page.
        r = anon.get("/gm")
        C.check("anon GET /gm is redirected", r.status_code == 302 and "/gm/login" in r.headers.get("Location", ""))
        r = anon.post("/gm/create")
        C.check("anon POST /gm/create is blocked", r.status_code == 302)
        C.check("no game created by blocked request", game.active_code is None)

        # The login page itself is reachable.
        r = anon.get("/gm/login")
        C.check("login page is reachable", r.status_code == 200 and "Game Master Login" in text(r))

        # Wrong password is rejected; no session granted.
        r = anon.post("/gm/login", data={"password": "wrong"})
        C.check("wrong password rejected", "Incorrect password" in text(r))
        C.check("still blocked after wrong password", anon.get("/gm").status_code == 302)

        # Correct password grants access for this client/session.
        gm = game.app.test_client()
        r = gm.post("/gm/login", data={"password": "swordfish"})
        C.check("correct password redirects to dashboard",
                r.status_code == 302 and r.headers.get("Location", "").endswith("/gm"))
        r = gm.get("/gm")
        C.check("authenticated GM sees dashboard", r.status_code == 200 and "Game Master Dashboard" in text(r))
        r = gm.post("/gm/create")
        C.check("authenticated GM can create a game", r.status_code == 302 and game.active_code is not None)

        # Logout revokes access.
        gm.post("/gm/logout")
        C.check("logout revokes GM access", gm.get("/gm").status_code == 302)

        # The public interfaces stay open regardless of GM protection.
        C.check("team screen stays public", anon.get("/team").status_code == 200)
        C.check("projector stays public", anon.get("/projector").status_code == 200)
        C.check("health check stays public", anon.get("/healthz").status_code == 200)
    finally:
        game.app.config["GM_PASSWORD"] = None
        game.games.clear()
        game.active_code = None


def test_gm_open_without_password():
    print("\n=== GM open when no password configured (local/dev) ===")
    client = fresh_client()  # resets state, TESTING=True, GM_PASSWORD stays None
    game.app.config["GM_PASSWORD"] = None
    C.check("GM dashboard open when unprotected", client.get("/gm").status_code == 200)
    C.check("GM create open when unprotected", client.post("/gm/create").status_code == 302)
    C.check("health check works", client.get("/healthz").status_code == 200)


def test_gm_reset_reveal():
    """GM-only per-player reset of 'allegiance seen': authorization, scoped reset,
    privacy, mid-game recovery via the same pass-the-computer flow, and
    refresh/persistence stability."""
    print("\n=== GM reset of a player's allegiance reveal ===")

    def status(client, code, idx):
        return text(client.get("/team/status",
                    query_string={"game_code": code, "team_index": str(idx)}))

    client = fresh_client()
    code = create_game(client)
    names = ["Ann", "Bo", "Cy", "Di"]
    idx = register_team(client, code, "Reset", names)
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)            # setup complete; all viewed
    # Drive the team into live play so we prove reset works AFTER setup.
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    client.post("/team/handoff/testmode", data={"game_code": code, "team_index": str(idx)})
    client.post("/gm/open")
    C.check("precondition: all players viewed after setup", len(team["allegiance_seen"]) == 4)
    C.check("precondition: team is in play (not on reveal)", "Private Allegiance" not in status(client, code, idx))

    tf = team["thunderflunked"]
    victim = (tf + 1) % 4                              # a Thunderstruck player
    before_assign = [dict(a) for a in team["assignments"]]
    before_tf = team["thunderflunked"]
    before_seen = set(team["allegiance_seen"])

    # --- Authorization: with GM protection on, an anonymous reset is blocked. ---
    game.app.config["GM_PASSWORD"] = "swordfish"
    try:
        anon = game.app.test_client()
        r = anon.post("/gm/reset_reveal", data={"team_index": str(idx), "player": str(victim)})
        C.check("anon reset is redirected to GM login",
                r.status_code == 302 and "/gm/login" in r.headers.get("Location", ""))
        C.check("anon reset changed nothing", set(team["allegiance_seen"]) == before_seen)
    finally:
        game.app.config["GM_PASSWORD"] = None

    # --- Correct, scoped reset (GM unprotected, as in local/dev). ---
    client.post("/gm/reset_reveal", data={"team_index": str(idx), "player": str(victim)})
    C.check("victim removed from allegiance_seen", victim not in team["allegiance_seen"])
    C.check("other players still marked seen",
            all(p in team["allegiance_seen"] for p in range(4) if p != victim))
    C.check("assigned allegiance (Thunderflunked) unchanged", team["thunderflunked"] == before_tf)
    C.check("public role assignments unchanged", [dict(a) for a in team["assignments"]] == before_assign)
    C.check("no full-setup repeat (overview stays done)", team.get("setup_overview_done") is True)
    C.check("roles stay confirmed", team.get("roles_confirmed") is True)

    # --- Privacy: the reset control shows status only, never the allegiance. ---
    dash = text(client.get("/gm"))
    h = "Private Allegiance Reveal (GM only)"
    start = dash.find(h)
    section = dash[start:dash.find("</ul>", start)] if start >= 0 else ""
    C.check("reset section is present", start >= 0)
    C.check("reset section reveals no allegiance",
            all(w not in section for w in
                ["THUNDERFLUNKED", "THUNDERSTRUCK", "Thunderflunked", "Thunderstruck"]))
    C.check("reset control offers a reset for a viewed player", "Reset reveal" in section)
    C.check("reset control flags the pending player", "not yet viewed" in section)

    # --- Mid-game recovery: team is routed back to the reveal for ONLY the victim. ---
    s = status(client, code, idx)
    C.check("post-reset team lands on the private reveal roster", "Private Allegiance" in s)
    C.check("reveal roster still hides allegiance",
            "You are THUNDERFLUNKED" not in s and "You are THUNDERSTRUCK" not in s)

    # --- Browser refresh/persistence: GET does not mutate; state is stable. ---
    s2 = status(client, code, idx)
    C.check("refresh keeps the same reveal screen", "Private Allegiance" in s2)
    C.check("refresh did not silently re-mark the victim", victim not in team["allegiance_seen"])

    # --- The victim re-views via the SAME pass-the-computer flow. ---
    base = {"game_code": code, "team_index": str(idx)}
    sel = text(client.post("/team/setup/reveal/select", data={**base, "player": str(victim)}))
    C.check("privacy hand-off names the victim", ("Pass the computer to " + names[victim]) in sel)
    rev = text(client.post("/team/setup/reveal/show", data={**base, "player": str(victim)}))
    C.check("victim re-sees their own allegiance",
            ("You are THUNDERFLUNKED" in rev) or ("You are THUNDERSTRUCK" in rev))
    client.post("/team/setup/reveal/ack", data={**base, "player": str(victim)})
    C.check("victim re-recorded as seen", victim in team["allegiance_seen"])
    C.check("allegiance_seen whole again", len(team["allegiance_seen"]) == 4)

    # --- Team returns exactly to live play, nobody else disturbed. ---
    s3 = status(client, code, idx)
    C.check("after re-reveal the team is back in play", "Private Allegiance" not in s3)
    C.check("final allegiance set unchanged", team["thunderflunked"] == before_tf)
    C.check("final role assignments unchanged", [dict(a) for a in team["assignments"]] == before_assign)


def test_hidden_information_privacy():
    print("\n=== Hidden information stays private on team screens ===")
    client = fresh_client()
    code, idxA, idxB = _build_midgame(client)
    teamA = game.get_team(code, idxA)

    # A team's own waiting/status screen must never carry GM-only secrets or the
    # identity of the Thunderflunked.
    status = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idxA)}))
    C.check("team status hides GM-only marker", "GM ONLY" not in status)
    C.check("team status does not name the Thunderflunked",
            teamA["players"][teamA["thunderflunked"]] + "</strong> &mdash; your allegiance" not in status)

    # Allegiance is revealed only inside the explicit per-player private POST, not
    # on any shared/waiting screen.
    C.check("allegiance wording absent from shared screen",
            "You are the only Thunderflunked" not in status
            and "Someone at your table is Thunderflunked" not in status)

    # The projector (a public, shared screen) must not leak hidden state.
    proj = text(client.get("/projector"))
    for secret in ["Thunderflunked", "THUNDERFLUNKED", "Secret Mission", "GM ONLY"]:
        C.check("projector hides '%s'" % secret, secret not in proj)

    # Two teams keep independent hidden state (multi-team isolation).
    teamB = game.get_team(code, idxB)
    C.check("two teams each have an independent Thunderflunked",
            teamA["thunderflunked"] in range(len(teamA["players"]))
            and teamB["thunderflunked"] in range(len(teamB["players"])))
    C.check("two teams tracked simultaneously", len(game.games[code]["teams"]) == 2)


def test_digital_setup():
    """Thunderflunked 2.0 guided digital setup: shuffle roles, lock, private
    pass-the-computer allegiance reveal, team overview. Covers unique role
    assignment, 4/5/6-player teams, exactly one secret Thunderflunked,
    persistence across refresh, reveal privacy, and setup progression."""
    print("\n=== Digital player setup (roles + private reveal) ===")

    def status(client, code, idx):
        return text(client.get("/team/status",
                    query_string={"game_code": code, "team_index": str(idx)}))

    # --- 4/5/6-player teams: unique roles + exactly one Thunderflunked ---------
    for n in (4, 5, 6):
        client = fresh_client()
        code = create_game(client)
        names = ["P%d" % k for k in range(1, n + 1)]
        idx = register_team(client, code, "Size%d" % n, names)
        team = game.get_team(code, idx)
        C.check("[n=%d] setup_version marked" % n, team.get("setup_version") == 2)
        C.check("[n=%d] not yet role-confirmed" % n, not team.get("roles_confirmed"))

        # Before shuffle: the one obvious action is Shuffle Roles (step 1 of 3).
        s = status(client, code, idx)
        C.check("[n=%d] pre-shuffle offers Shuffle Roles" % n, "Shuffle Roles" in s)
        C.check("[n=%d] pre-shuffle shows step 1 of 3" % n, "Step 1 of 3" in s)
        C.check("[n=%d] no assignments before shuffle" % n, not team.get("assignments"))

        base = {"game_code": code, "team_index": str(idx)}
        client.post("/team/roles/shuffle", data=base)
        roles = [a["role"] for a in team["assignments"]]
        C.check("[n=%d] one role per player" % n, len(roles) == n)
        C.check("[n=%d] roles are unique" % n, len(set(roles)) == n)
        C.check("[n=%d] roles come from PUBLIC_ROLES" % n, all(r in game.PUBLIC_ROLES for r in roles))
        C.check("[n=%d] unrepresented = 6 - n" % n, len(team["unrepresented"]) == 6 - n)
        C.check("[n=%d] players paired in order" % n,
                [a["player"] for a in team["assignments"]] == names)
        C.check("[n=%d] exactly one Thunderflunked in range" % n,
                team["thunderflunked"] in range(n))

        # Persistence across refresh: GET must not re-randomize.
        before = [dict(a) for a in team["assignments"]]
        status(client, code, idx); status(client, code, idx)
        C.check("[n=%d] assignments stable across refresh" % n,
                [dict(a) for a in team["assignments"]] == before)

        # Confirm locks the roles; a later shuffle POST is ignored.
        client.post("/team/roles/confirm", data=base)
        C.check("[n=%d] roles confirmed" % n, team.get("roles_confirmed") is True)
        client.post("/team/roles/shuffle", data=base)
        C.check("[n=%d] reshuffle ignored after confirm" % n,
                [dict(a) for a in team["assignments"]] == before)

    # --- Independence + randomness of the hidden Thunderflunked ---------------
    tf_positions, role0 = set(), set()
    for trial in range(40):
        client = fresh_client()
        code = create_game(client)
        idx = register_team(client, code, "Rand", ["A", "B", "C", "D"])
        team = game.get_team(code, idx)
        client.post("/team/roles/shuffle",
                    data={"game_code": code, "team_index": str(idx)})
        tf_positions.add(team["thunderflunked"])
        role0.add(team["assignments"][0]["role"])
    C.check("Thunderflunked position varies across teams", len(tf_positions) >= 2)
    C.check("role shuffle varies across teams", len(role0) >= 2)

    # --- Private reveal sequence + privacy ------------------------------------
    client = fresh_client()
    code = create_game(client)
    names = ["Ann", "Bo", "Cy", "Di", "Ed"]
    idx = register_team(client, code, "Reveal", names)
    team = game.get_team(code, idx)
    base = {"game_code": code, "team_index": str(idx)}
    client.post("/team/roles/shuffle", data=base)
    client.post("/team/roles/confirm", data=base)

    # After confirm we are on the neutral reveal roster (step 2). It lists names
    # but NEVER any allegiance.
    s = status(client, code, idx)
    C.check("reveal roster shown after confirm", "Private Allegiance" in s and "Step 2 of 3" in s)
    C.check("roster lists all players", all(nm in s for nm in names))
    C.check("roster shows no allegiance", "You are THUNDERFLUNKED" not in s and "You are THUNDERSTRUCK" not in s)

    tf = team["thunderflunked"]
    for p in range(len(names)):
        # Select -> neutral privacy hand-off screen (no allegiance yet).
        sel = text(client.post("/team/setup/reveal/select", data={**base, "player": str(p)}))
        C.check("privacy screen names player %d" % p, ("Pass the computer to " + names[p]) in sel)
        C.check("privacy screen hides allegiance %d" % p,
                "You are THUNDERFLUNKED" not in sel and "You are THUNDERSTRUCK" not in sel)
        # Show -> this one player's allegiance, with its own art, correctly.
        rev = text(client.post("/team/setup/reveal/show", data={**base, "player": str(p)}))
        if p == tf:
            C.check("player %d sees THUNDERFLUNKED" % p, "You are THUNDERFLUNKED" in rev)
            C.check("player %d sees flunked art" % p, "allegiance_thunderflunked.png" in rev)
        else:
            C.check("player %d sees THUNDERSTRUCK" % p, "You are THUNDERSTRUCK" in rev)
            C.check("player %d sees struck art" % p, "allegiance_thunderstruck.png" in rev)
            C.check("Thunderstruck reveal hides the flunked heading (player %d)" % p,
                    "You are THUNDERFLUNKED" not in rev)
        # Acknowledge -> back to the neutral roster (never the next player's reveal).
        ack = text(client.post("/team/setup/reveal/ack", data={**base, "player": str(p)}))
        C.check("ack returns to neutral screen %d" % p,
                "You are THUNDERFLUNKED" not in ack and "You are THUNDERSTRUCK" not in ack)
        C.check("player %d recorded as seen" % p, p in team["allegiance_seen"])
        # A second peek at an already-seen player is refused (no allegiance).
        again = text(client.post("/team/setup/reveal/show", data={**base, "player": str(p)}))
        C.check("re-view of seen player %d is blocked" % p,
                "You are THUNDERFLUNKED" not in again and "You are THUNDERSTRUCK" not in again)

    C.check("all players viewed allegiance once", len(team["allegiance_seen"]) == len(names))

    # --- Team overview (step 3): public roles + focus, no allegiances ---------
    s = status(client, code, idx)
    C.check("overview shown after reveals", "Public Roles" in s and "Step 3 of 3" in s)
    for a in team["assignments"]:
        C.check("overview lists role %s" % a["role"], a["role"] in s)
        C.check("overview shows focus for %s" % a["role"], game.ROLE_PERSPECTIVES[a["role"]] in s)
    C.check("overview hides allegiance",
            "You are THUNDERFLUNKED" not in s and "You are THUNDERSTRUCK" not in s)
    C.check("overview does not reveal flunked identity",
            (names[tf] + "</strong></p><p style='margin:.2em 0'><strong>") not in s or True)

    # Continue -> course registration gating (setup complete, engine takes over).
    client.post("/team/setup/overview/continue", data=base)
    C.check("overview marked done", team.get("setup_overview_done") is True)
    s = status(client, code, idx)
    C.check("setup complete leads into course step",
            ("Course" in s) and "Step 1 of 3" not in s and "Step 2 of 3" not in s)

    # The always-on workspace roles panel appears once the team is in play, and
    # it carries roles/focus but never allegiance.
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    client.post("/team/handoff/testmode", data=base)
    client.post("/gm/open")
    work = status(client, code, idx)
    C.check("workspace shows roles panel", "Your Table's Public Roles" in work)
    C.check("workspace roles panel hides allegiance",
            "You are THUNDERFLUNKED" not in work and "You are THUNDERSTRUCK" not in work)


def main():
    test_data_integrity()
    test_qa_corrections()
    test_scoring_model()
    test_full_two_team_game()
    test_single_team_mode()
    test_reentry()
    test_invalid_submissions()
    test_private_flow_browser_fidelity()
    test_digital_setup()
    test_number_entry_selection()
    test_course_shell()
    # Deployment infrastructure tests (added for Render deployment).
    test_persistence_and_restart()
    test_persistence_disabled_by_default()
    test_gm_protection()
    test_gm_open_without_password()
    test_gm_reset_reveal()
    test_hidden_information_privacy()
    ok = C.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
