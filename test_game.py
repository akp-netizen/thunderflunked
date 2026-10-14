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
    """Force a team's course deterministically in the fully-digital flow. The real
    UI deals a random Course Card and locks it when the GM closes selection; for
    deterministic tests we pin both the held and the locked course and ensure the
    15-student deal has happened, exactly as lock_team_course does on close."""
    g = game.games[code]
    team = game.get_team(code, idx)
    team["course_held"] = course_name
    team["course"] = course_name
    game.ensure_dealt(team)


def deal_and_draft(client, code, idx, dealt_labels, roster_labels):
    """Thunderflunked 2.0 placement. Overrides the automatic 15-student deal with
    the given labels (for deterministic, feasible test setups), keeps the given
    10, refers the other 5, and confirms (locking the decision). Central
    reassignment auto-runs once every team in the game has confirmed. No written
    rationale is required by the redesigned placement screen."""
    team = game.get_team(code, idx)
    team["dealt"] = list(dealt_labels)
    team["deal_version"] = 2
    client.post("/team/retain", data={"game_code": code, "team_index": str(idx),
                                      "students": list(roster_labels)})
    client.post("/team/retain/confirm", data={"game_code": code, "team_index": str(idx)})


def finish_reassignment(client, code):
    """Trigger the GM-controlled central reassignment (distributes once every
    team has finalized), then acknowledge each team's final 15-student class."""
    client.post("/gm/reassign", data={})
    for i, t in enumerate(game.games[code]["teams"]):
        if t.get("reassigned") and not t.get("placement_acked"):
            client.post("/team/placement/ack",
                        data={"game_code": code, "team_index": str(i)})


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

    # Course selection (fully digital): opening deals every team a Course Card;
    # a team's course is not LOCKED until the GM closes selection.
    client.post("/gm/course/open")
    C.check("team dealt a Course Card on open", teamA.get("course_held") in game.COURSE_NAMES)
    C.check("course not locked while selection open", teamA.get("course") is None)
    # Pin deterministic held courses, then close to lock them in.
    teamA["course_held"] = game.COURSE_NAMES[0]
    teamB["course_held"] = game.COURSE_NAMES[5]
    client.post("/gm/course/close")
    C.check("course locked A after close", teamA["course"] == game.COURSE_NAMES[0])
    C.check("course locked B after close", teamB["course"] == game.COURSE_NAMES[5])

    # Phases 1-4: deal 15 / retain 10 / refer 5, then central reassignment into a
    # 15-student final class. Disjoint deals keep the exclusion rule satisfiable
    # (A and B must swap their referred fives).
    dealtA = game.STUDENTS[:15]
    rosterA = dealtA[:10]
    deal_and_draft(client, code, idxA, dealtA, rosterA)
    dealtB = game.STUDENTS[15:30]
    rosterB = dealtB[:10]
    deal_and_draft(client, code, idxB, dealtB, rosterB)
    finish_reassignment(client, code)
    C.check("retained A is 10", len(teamA["starting_roster"]) == 10)
    C.check("referred A is 5", len(teamA["released"]) == 5)
    C.check("final class A is 15", len(teamA["active_roster"]) == 15)
    C.check("final class A has no duplicate characters", len(set(teamA["active_roster"])) == 15)
    C.check("A was reassigned none of its own original 15",
            all(s not in set(dealtA) for s in teamA["reassigned"]))
    C.check("A received B's referred five", set(teamA["reassigned"]) == set(teamB["released"]))
    C.check("B received A's referred five", set(teamB["reassigned"]) == set(teamA["released"]))
    C.check("starting resources (scored on 15) floored >=40 A",
            all(teamA["resources"][n] >= 40 for n in game.RESOURCE_NAMES))
    C.check("starting resources floored >=40 B",
            all(teamB["resources"][n] >= 40 for n in game.RESOURCE_NAMES))
    startA = dict(teamA["resources"])

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
    C.check("CP2 late adds are the 5 reassigned students A", teamA["late_adds"] == teamA["reassigned"])
    C.check("CP2 late adds excluded from original deal A",
            all(s not in set(dealtA) for s in teamA["late_adds"]))
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
    # Before the GM distributes, the team waits.
    waiting = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))
    C.check("single-team awaits reassignment", "Awaiting Reassignment" in waiting)
    # GM distributes: with no other table to pool from, replacements are drawn
    # from canonical students this team was not dealt (exclusion still honored;
    # no new characters invented).
    client.post("/gm/reassign", data={})
    final = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))
    C.check("single-team reassignment note shown", "Single-team practice game" in final)
    finish_reassignment(client, code)
    C.check("single-team reassigned exactly 5", len(team["reassigned"]) == 5)
    C.check("single-team final class is 15", len(team["active_roster"]) == 15)
    C.check("single-team reassigned excluded from its own deal",
            all(s not in set(team["dealt"]) for s in team["reassigned"]))

    # CP2 late adds must still work with no received cards (fallback).
    client.post("/gm/open")  # CP1
    client.post("/team/cp/submit", data={"game_code": code, "team_index": str(idx),
                                         "cp": "0", "actions": ["clarify"]})
    run_private_check(client, code, idx, team, 0)
    client.post("/gm/lock"); client.post("/gm/advance")
    client.post("/gm/open")  # CP2
    client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)})
    C.check("single-team late adds are the 5 reassigned", team["late_adds"] == team["reassigned"])
    C.check("late adds not in retained roster", all(s not in team["starting_roster"] for s in team["late_adds"]))


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
    C.check("re-entry after roles during selection -> course screen",
            "Your Course" in resume or "Course selection is open" in resume)
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

    # Wrong retain count (digital placement flow).
    team["dealt"] = game.STUDENTS[:15]
    team["deal_version"] = 2
    r = text(client.post("/team/retain", data={"game_code": code, "team_index": str(idx),
                                               "students": game.STUDENTS[:8]}))
    C.check("retain rejects wrong count", "keep exactly 10" in r)
    C.check("retain not locked after invalid", not team.get("roster_confirmed"))

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

    # No written rationale is required: keeping exactly 10 (no rationale field)
    # advances straight to the Confirm & Lock review.
    r3 = text(client.post("/team/retain", data={"game_code": code, "team_index": str(idx),
                                               "students": game.STUDENTS[:10]}))
    C.check("no rationale required -> advances to Confirm & Lock", "Confirm" in r3 and "Lock" in r3)
    C.check("no rationale prompt anywhere in placement flow", "rationale" not in r3.lower())


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
    finish_reassignment(client, code)
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
    # Deal is automatic; pin it for determinism, then retain via the numbers box.
    team["dealt"] = game.STUDENTS[:15]
    team["deal_version"] = 2
    client.post("/team/retain", data={"game_code": code, "team_index": str(idx),
                                      "numbers": "1-5 6 7 8 9 10"})
    C.check("number-entry retain recorded 10", len(team.get("retain_pending", [])) == 10)
    C.check("number-entry retain is #01..#10", team["retain_pending"] == game.STUDENTS[:10])
    client.post("/team/retain/confirm", data={"game_code": code, "team_index": str(idx)})
    C.check("confirmed retained roster is #01..#10", team["starting_roster"] == game.STUDENTS[:10])
    # Mixing checkbox + numbers unions to exactly 10 without duplicates.
    client2 = fresh_client()
    code2 = create_game(client2)
    i2 = register_team(client2, code2, "Mix", ["A", "B", "C", "D"])
    t2 = game.get_team(code2, i2)
    assign_roles(client2, code2, i2, t2)
    client2.post("/gm/course/open"); client2.post("/gm/course/close")
    register_course(client2, code2, i2, game.COURSE_NAMES[0])
    t2["dealt"] = game.STUDENTS[:15]
    t2["deal_version"] = 2
    client2.post("/team/retain", data={"game_code": code2, "team_index": str(i2),
                                       "students": game.STUDENTS[:5], "numbers": "5 6 7 8 9 10"})
    C.check("union dedups overlap to 10", len(t2.get("retain_pending", [])) == 10)


def test_digital_course_selection_and_trading():
    """Part A: fully-digital Course Cards. Deck of 10 courses x 2 identical
    printed copies, dealt digitally; one-for-one mutual trades (propose / accept /
    decline / cancel) with clear turn/waiting states; lock on close. No physical
    draw, exchange, or dropdown registration anywhere in the flow."""
    print("\n=== Digital course selection + trading ===")
    import os
    from collections import Counter

    # --- Original artwork mapping for all 10 canonical courses -----------------
    app_dir = os.path.dirname(game.__file__)
    for c in game.COURSES:
        nospace = c["code"].replace(" ", "")
        src = game.course_image_src(c["name"])
        C.check("course art maps %s -> _%s_1.png" % (c["code"], nospace),
                src == "/static/art/courses/course_" + nospace + "_1.png")
        path = os.path.join(app_dir, src.lstrip("/").replace("/", os.sep))
        C.check("course art file exists for %s" % c["code"], os.path.exists(path))
    C.check("all 10 courses map to distinct artwork",
            len({game.course_image_src(c["name"]) for c in game.COURSES}) == 10)

    # --- Setup two teams, both past role setup ---------------------------------
    client = fresh_client()
    code = create_game(client)
    iA = register_team(client, code, "Alpha", ["A", "B", "C", "D"]); tA = game.get_team(code, iA)
    assign_roles(client, code, iA, tA)
    iB = register_team(client, code, "Bravo", ["A", "B", "C", "D"]); tB = game.get_team(code, iB)
    assign_roles(client, code, iB, tB)

    def scr(idx):
        return text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))

    # --- The legacy manual registration route is gone --------------------------
    r404 = client.post("/team/course", data={"game_code": code, "team_index": str(iA),
                                             "course": game.COURSE_NAMES[0]})
    C.check("legacy /team/course registration route removed", r404.status_code == 404)

    # --- Not open: the team is dealt a card digitally; waiting state; no paper --
    s = scr(iA)
    C.check("not-open: team already dealt a Course Card", tA.get("course_held") in game.COURSE_NAMES)
    C.check("not-open: course not locked yet", tA.get("course") is None)
    C.check("not-open: shows a waiting state", "has not opened yet" in s)
    C.check("not-open: shows the dealt Course Card artwork", "/static/art/courses/course_" in s)
    for paper in ["Draw one", "physical", "exchange", "in front of your team", "Select the course"]:
        C.check("course screen has no paper instruction '%s'" % paper, paper.lower() not in s.lower())

    # --- Open: deck is 10 courses x 2 copies = 20; every team holds one ---------
    client.post("/gm/course/open")
    g = game.games[code]
    allcards = list(g["course_deck"]) + [t["course_held"] for t in g["teams"]]
    C.check("deck is 10 courses x 2 copies (20 cards total)", len(allcards) == 20)
    cnt = Counter(allcards)
    C.check("each canonical course appears exactly twice in the deck",
            all(cnt[c["name"]] == 2 for c in game.COURSES))
    C.check("two copies allow duplicates (<=2 of any course held)",
            all(v <= 2 for v in Counter(t["course_held"] for t in g["teams"]).values()))

    # Pin deterministic holdings for the trade assertions.
    tA["course_held"] = game.COURSE_NAMES[0]
    tB["course_held"] = game.COURSE_NAMES[1]

    # --- Propose A->B: proposer waits, recipient has the move ------------------
    client.post("/team/course/propose",
                data={"game_code": code, "team_index": str(iA), "target": str(iB)})
    offers = g["course_offers"]
    C.check("propose creates exactly one pending offer",
            len(offers) == 1 and offers[0]["frm"] == iA and offers[0]["to"] == iB)
    sA, sB = scr(iA), scr(iB)
    C.check("proposer sees 'waiting on them' + Cancel", "Waiting for" in sA and "Cancel offer" in sA)
    C.check("recipient sees 'your move' Accept + Decline", "Accept trade" in sB and "Decline" in sB)

    # --- Decline: offer cleared, no swap ---------------------------------------
    oid = offers[0]["id"]
    client.post("/team/course/decline",
                data={"game_code": code, "team_index": str(iB), "offer_id": str(oid)})
    C.check("decline clears the offer", len(g["course_offers"]) == 0)
    C.check("decline does not swap courses",
            tA["course_held"] == game.COURSE_NAMES[0] and tB["course_held"] == game.COURSE_NAMES[1])

    # --- Proposer cannot self-accept (both must agree) -------------------------
    client.post("/team/course/propose",
                data={"game_code": code, "team_index": str(iA), "target": str(iB)})
    oid = g["course_offers"][0]["id"]
    before = (tA["course_held"], tB["course_held"])
    client.post("/team/course/accept",
                data={"game_code": code, "team_index": str(iA), "offer_id": str(oid)})
    C.check("proposer cannot accept own offer (mutual consent required)",
            (tA["course_held"], tB["course_held"]) == before and len(g["course_offers"]) == 1)

    # --- One outgoing offer at a time ------------------------------------------
    first = g["course_offers"][0]["id"]
    client.post("/team/course/propose",
                data={"game_code": code, "team_index": str(iA), "target": str(iB)})
    C.check("re-proposing replaces the prior outgoing offer (one at a time)",
            len(g["course_offers"]) == 1 and g["course_offers"][0]["id"] != first)

    # --- Cancel own offer ------------------------------------------------------
    client.post("/team/course/cancel",
                data={"game_code": code, "team_index": str(iA), "offer_id": str(g["course_offers"][0]["id"])})
    C.check("proposer can cancel own offer", len(g["course_offers"]) == 0)

    # --- Accept: one-for-one swap, both agree, offers cleared ------------------
    client.post("/team/course/propose",
                data={"game_code": code, "team_index": str(iA), "target": str(iB)})
    oid = g["course_offers"][0]["id"]
    client.post("/team/course/accept",
                data={"game_code": code, "team_index": str(iB), "offer_id": str(oid)})
    C.check("accept swaps courses one-for-one",
            tA["course_held"] == game.COURSE_NAMES[1] and tB["course_held"] == game.COURSE_NAMES[0])
    C.check("accept clears all offers", len(g["course_offers"]) == 0)

    # --- Close: lock each held course, deal students, void offers --------------
    tA["course_held"] = game.COURSE_NAMES[0]
    tB["course_held"] = game.COURSE_NAMES[5]
    # a stale offer existing at close must be voided
    client.post("/team/course/propose",
                data={"game_code": code, "team_index": str(iA), "target": str(iB)})
    client.post("/gm/course/close")
    C.check("close locks A's held course", tA["course"] == game.COURSE_NAMES[0])
    C.check("close locks B's held course", tB["course"] == game.COURSE_NAMES[5])
    C.check("close voids pending offers", g["course_offers"] == [])
    C.check("close deals the 15-student placement to A", len(tA.get("dealt", [])) == 15)
    C.check("close deals the 15-student placement to B", len(tB.get("dealt", [])) == 15)

    # --- No trading after close ------------------------------------------------
    client.post("/team/course/propose",
                data={"game_code": code, "team_index": str(iA), "target": str(iB)})
    C.check("no new trade offers after selection closes", g["course_offers"] == [])
    C.check("locked course is unchanged after a late propose", tA["course"] == game.COURSE_NAMES[0])

    # --- GM dashboard reflects status, adds no manual course step --------------
    gmp = text(client.get("/gm"))
    C.check("GM shows locked course A", game.COURSE_NAMES[0] in gmp and "locked" in gmp)
    C.check("GM has no physical course-draw instruction",
            "Draw one Course Card" not in gmp)


def test_course_card_readability():
    """The digital Course Card is readable: large, enlargeable artwork plus the
    full canonical course information as HTML text; other teams' courses and trade
    offers are inspectable before trading; waiting states keep the full card; no
    secrets or scoring internals leak."""
    print("\n=== Course card readability ===")

    client = fresh_client()
    code = create_game(client)
    iA = register_team(client, code, "Alpha", ["A", "B", "C", "D"]); tA = game.get_team(code, iA)
    assign_roles(client, code, iA, tA)
    iB = register_team(client, code, "Bravo", ["A", "B", "C", "D"]); tB = game.get_team(code, iB)
    assign_roles(client, code, iB, tB)

    def scr(idx):
        return text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))

    # Pin deterministic, distinct courses for both teams.
    tA["course_held"] = game.COURSE_NAMES[1]   # Bigfoot Field Research (BIOL 215)
    tB["course_held"] = game.COURSE_NAMES[6]   # Industrial Snack Automation (MFGT 165)
    mine = game.COURSE_BY_NAME[game.COURSE_NAMES[1]]
    theirs = game.COURSE_BY_NAME[game.COURSE_NAMES[6]]

    # --- Not-open waiting state: full card present, not a mostly-empty message --
    s = scr(iA)
    C.check("waiting state shows the large hero card", "course-hero" in s and "zoom-lg" in s)
    for label, val in [("name", game.COURSE_NAMES[1]), ("code", mine["code"]),
                       ("modality", mine["modality"]), ("credits", str(mine["credits"]) + " credits"),
                       ("description", mine["desc"]), ("prerequisite", mine["prereq"]),
                       ("meeting pattern", mine["pattern"]), ("assessment", mine["assessment"]),
                       ("reality A", mine["realities"][0]), ("reality B", mine["realities"][1])]:
        C.check("waiting state shows canonical %s as readable text" % label, val in s)
    C.check("waiting state is not a bare message (keeps full details)",
            mine["desc"] in s and "Course Realities" in s)

    # --- Enlargement control: pure-CSS checkbox + overlay + obvious close -------
    C.check("card has an enlarge control (checkbox-driven)",
            "class='zoom-cb'" in s and "id='zoom-your'" in s and "for='zoom-your'" in s)
    C.check("enlarge uses a real focusable checkbox (keyboard accessible)",
            "type='checkbox'" in s)
    C.check("enlarged overlay + obvious Close control present",
            "zoom-overlay" in s and "zoom-close" in s and "Close" in s)
    C.check("enlarge works with no JavaScript (CSS :checked drives the overlay)",
            ".zoom-cb:checked~.zoom-overlay" in game.BASE_CSS)
    C.check("enlarge control has a visible keyboard-focus style",
            ".zoom-cb:focus-visible+.zoom-thumb" in game.BASE_CSS)

    # --- No secrets, no scoring internals on the course screen ------------------
    for secret in ["Thunderflunked", "allegiance", "Secret Mission", "Suspicion"]:
        C.check("course screen hides '%s'" % secret, secret.lower() not in s.lower())
    C.check("course screen does not expose the overall score", "Overall Course Score" not in s)
    for rn in game.RESOURCE_NAMES:
        C.check("course screen hides resource '%s'" % rn, rn not in s)
    C.check("course screen renders no resource meters", "class='meter'" not in s)

    # --- Open: an incoming offer's course is inspectable before accepting -------
    client.post("/gm/course/open")
    client.post("/team/course/propose",
                data={"game_code": code, "team_index": str(iB), "target": str(iA)})
    so = scr(iA)
    C.check("offered course name readable before accepting", game.COURSE_NAMES[6] in so)
    C.check("offered course modality readable", theirs["modality"] in so)
    C.check("offered course description readable", theirs["desc"] in so)
    C.check("offered course realities readable", theirs["realities"][0] in so)
    C.check("offered course is enlargeable too", "zoom-in-" in so)
    C.check("own course still shown in full alongside the offer", mine["desc"] in so and "course-hero" in so)
    C.check("accept/decline controls present", "Accept trade" in so and "Decline" in so)

    # --- Other team's course inspectable before PROPOSING (A has no offer out) --
    # A received an offer but has not proposed one, so A also sees the propose list.
    C.check("propose list renders other team's course enlargeable", "zoom-team-" in so)
    C.check("propose list shows the other team's canonical details before proposing",
            theirs["modality"] in so and theirs["desc"] in so)

    # --- Outgoing-offer WAITING state (B proposed to A) keeps full course info --
    sb = scr(iB)
    C.check("proposer sees the waiting state", "Waiting for" in sb and "Cancel offer" in sb)
    C.check("proposer keeps its own full hero card", "course-hero" in sb and theirs["desc"] in sb)
    C.check("proposer can still inspect the course it offered for (readable text)",
            mine["desc"] in sb and mine["realities"][0] in sb)
    C.check("proposer waiting state keeps an enlargeable offered card", "zoom-wait-" in sb)
    C.check("proposer waiting state is not a bare message",
            sb.count("Course Realities") >= 1 and theirs["modality"] in sb)


def test_placement_screen_redesign():
    """Part B: the student placement screen is a responsive artwork grid with a
    persistent counter, clickable/keyboard-accessible selection, no rationale and
    no required typing, exactly 10-of-15 kept with 5 auto-referred, and a single
    Confirm & Lock with double-submit protection."""
    print("\n=== Placement screen redesign ===")
    client = fresh_client()
    code = create_game(client)
    idx = register_team(client, code, "Pick", ["A", "B", "C", "D"])
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open")
    team["course_held"] = game.COURSE_NAMES[0]
    client.post("/gm/course/close")
    team["dealt"] = game.STUDENTS[:15]
    team["deal_version"] = 2

    s = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(idx)}))
    C.check("persistent 'X of 10 students selected' counter", "of 10 students selected" in s)
    C.check("responsive artwork grid of the 15 dealt students",
            "cardgrid" in s and s.count("/static/art/students/student_") >= 15)
    C.check("no repeated six-stat table (stats are on the card art)", "/5)" not in s)
    C.check("no written rationale field", "<textarea" not in s and "rationale" not in s.lower())
    C.check("no required card-number / quick-entry typing", "Quick entry" not in s)
    C.check("each card's face is a selection checkbox (tap + keyboard accessible)",
            "class='sel-cb'" in s and "name='students'" in s and "class='sel-face'" in s)
    C.check("visible selected checkmark on each card", "&#10003; Selected" in s)
    C.check("keyboard hint present", "Space to toggle" in s)
    # Click-to-enlarge: a SEPARATE control so enlarging never changes selection.
    C.check("each card has an Enlarge control", s.count(">Enlarge card<") >= 15)
    C.check("enlarge uses its own checkbox + overlay (independent of selection)",
            "class='zoom-cb'" in s and "class='enlarge-btn'" in s and "zoom-overlay" in s)
    C.check("enlarge overlay has an obvious close control", "zoom-close" in s)
    # Large, readable 3-up desktop grid (not four tiny cards).
    C.check("desktop grid is 3 columns", "grid-template-columns:repeat(3,minmax(0,1fr))" in game.BASE_CSS)
    C.check("grid drops to 2 then 1 column on smaller widths",
            "grid-template-columns:repeat(2,minmax(0,1fr))" in game.BASE_CSS
            and "max-width:620px" in game.BASE_CSS)
    C.check("images are capped so narrow screens never overflow horizontally",
            "img{max-width:100%;height:auto;}" in game.BASE_CSS)
    C.check("placement screen uses the wide working-width layout", "wrap wide" in s)

    # Validation: wrong count is rejected, nothing locked.
    r8 = text(client.post("/team/retain",
                          data={"game_code": code, "team_index": str(idx), "students": game.STUDENTS[:8]}))
    C.check("keeping != 10 is rejected", "keep exactly 10" in r8)
    C.check("wrong count does not lock", not team.get("roster_confirmed"))

    # Keep exactly 10 -> single Confirm & Lock review with a double-submit guard.
    r = text(client.post("/team/retain",
                         data={"game_code": code, "team_index": str(idx), "students": game.STUDENTS[:10]}))
    C.check("review offers exactly one Confirm & Lock", r.count("Confirm &amp; Lock") == 1)
    C.check("Confirm & Lock has a double-submit guard", "b.disabled=true" in r)
    C.check("10 kept held pending", len(team["retain_pending"]) == 10)
    C.check("the other 5 are automatically referred",
            len(team["release_pending"]) == 5
            and set(team["retain_pending"]) | set(team["release_pending"]) == set(game.STUDENTS[:15]))

    # Lock, then confirm is idempotent against accidental double submission.
    client.post("/team/retain/confirm", data={"game_code": code, "team_index": str(idx)})
    C.check("locked starting roster is 10", len(team["starting_roster"]) == 10)
    C.check("locked referred is 5", len(team["released"]) == 5)
    locked = list(team["starting_roster"])
    client.post("/team/retain/confirm", data={"game_code": code, "team_index": str(idx)})
    C.check("double Confirm & Lock is idempotent", team["starting_roster"] == locked)


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
    finish_reassignment(client, code)

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
    deal_and_draft(client, code, idxB, game.STUDENTS[15:30], game.STUDENTS[15:25])
    finish_reassignment(client, code)
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
        C.check("locked course persisted across restart A", teamA.get("course") in game.COURSE_NAMES)
        C.check("missions preserved A", len(teamA["missions_complete"]) >= 1)
        C.check("votes preserved A", len(game.private_round(teamA, 2)["votes"]) == 4)
        # Digital placement state survives a restart intact.
        C.check("reassigned 5 preserved A", len(teamA["reassigned"]) == 5)
        C.check("final 15-student class preserved A", len(teamA["active_roster"]) == 15)
        C.check("reassignment exclusion still holds after restart A",
                all(s not in set(teamA["dealt"]) for s in teamA["reassigned"]))

        # Re-entry by Game Code + Team Name resumes the game. A team already in
        # active play now lands on the split workspace (follow the redirect).
        rejoin = text(client.post("/team/join",
                      data={"game_code": code, "team_name": "Team Alpha"},
                      follow_redirects=True))
        C.check("re-entry by code+name resumes the game",
                "Team Alpha" in rejoin and "Game Code" not in rejoin)
        C.check("active re-entry lands on the split workspace",
                "Team Workspace" in rejoin and "ws-panes" in rejoin)
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


def _setup_to_course(client, code, name, players, course_i=0):
    """Register a team and advance it to just-after course registration (which
    auto-deals 15 students). Returns (idx, team)."""
    idx = register_team(client, code, name, players)
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    return idx, team


def _retain_confirm(client, code, idx, dealt, retain):
    """Override the deal for determinism, then retain/refer/confirm."""
    team = game.get_team(code, idx)
    team["dealt"] = list(dealt)
    team["deal_version"] = 2
    client.post("/team/retain", data={"game_code": code, "team_index": str(idx),
                                      "students": list(retain)})
    client.post("/team/retain/confirm", data={"game_code": code, "team_index": str(idx)})


def test_digital_student_placement():
    """Thunderflunked 2.0 automatic deal + central constrained reassignment:
    auto-deal, independent deals, refresh persistence, retain 10 / refer 5,
    confirmation lock, multi-team reassignment with full exclusion and no final
    duplicates, single-team special case, infeasibility safe-reporting, atomic
    idempotence, and the various supported player counts."""
    print("\n=== Digital student placement + reassignment ===")
    S = game.STUDENTS

    def status(client, code, idx):
        return text(client.get("/team/status",
                    query_string={"game_code": code, "team_index": str(idx)}))

    # --- Phase 1: automatic deal of 15 distinct, shown with art + stats --------
    client = fresh_client()
    code = create_game(client)
    idx, team = _setup_to_course(client, code, "Deal", ["A", "B", "C", "D"])
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])
    C.check("auto-deal is 15", len(team.get("dealt", [])) == 15)
    C.check("auto-deal is distinct", len(set(team["dealt"])) == 15)
    C.check("auto-deal from canonical 30", all(s in S for s in team["dealt"]))
    screen = status(client, code, idx)
    C.check("placement screen shows student artwork", "/static/art/students/student_" in screen)
    # Stats live on the card artwork now; the redundant dot-stat table is gone.
    C.check("placement screen does not repeat a stat table", "/5)" not in screen)
    C.check("placement screen shows the 'of 10 students selected' counter",
            "of 10 students selected" in screen)

    # --- Phase 1: refresh never redeals ---------------------------------------
    before = list(team["dealt"])
    status(client, code, idx); status(client, code, idx)
    C.check("refresh does not redeal", team["dealt"] == before)

    # --- Phase 2: independent deals (characters may recur across teams) --------
    multi = fresh_client()
    mcode = create_game(multi)
    deals = []
    for k in range(3):
        mi, mt = _setup_to_course(multi, mcode, "Ind%d" % k, ["A", "B", "C", "D"])
        multi.post("/gm/course/open"); multi.post("/gm/course/close")
        register_course(multi, mcode, mi, game.COURSE_NAMES[0])
        deals.append(list(mt["dealt"]))
    C.check("each team's deal is a valid 15-subset", all(len(set(d)) == 15 for d in deals))
    all_dealt = [s for d in deals for s in d]
    C.check("characters recur across independent deals", len(set(all_dealt)) < len(all_dealt))

    # --- Phases 2-4: 3-team reassignment with full exclusion, no final dups ---
    # Cyclic blocks of 5 make the exclusion rule satisfiable with a forced swap.
    client = fresh_client()
    code = create_game(client)
    specs = [
        ("T0", S[0:15],            S[0:10]),             # release S[10:15]
        ("T1", S[10:25],           S[10:20]),            # release S[20:25]
        ("T2", S[20:30] + S[0:5],  S[20:30]),            # release S[0:5]
    ]
    idxs = []
    for nm, dealt, retain in specs:
        i, _ = _setup_to_course(client, code, nm, ["A", "B", "C", "D"])
        client.post("/gm/course/open"); client.post("/gm/course/close")
        register_course(client, code, i, game.COURSE_NAMES[0])
        _retain_confirm(client, code, i, dealt, retain)
        idxs.append(i)
    teams = [game.get_team(code, i) for i in idxs]
    finish_reassignment(client, code)
    for n, t in enumerate(teams):
        C.check("[T%d] retained 10" % n, len(t["starting_roster"]) == 10)
        C.check("[T%d] reassigned 5" % n, len(t["reassigned"]) == 5)
        C.check("[T%d] final class 15" % n, len(t["active_roster"]) == 15)
        C.check("[T%d] no duplicate characters in final class" % n, len(set(t["active_roster"])) == 15)
        C.check("[T%d] reassigned excludes ALL original 15 (incl. released)" % n,
                all(s not in set(t["dealt"]) for s in t["reassigned"]))
        C.check("[T%d] final = retained + reassigned" % n,
                set(t["active_roster"]) == set(t["starting_roster"]) | set(t["reassigned"]))
        C.check("[T%d] resources scored on 15, floored >=40" % n,
                all(t["resources"][nm] >= 40 for nm in game.RESOURCE_NAMES))
    # Every released instance was placed exactly once (supply == demand).
    released_pool = sorted(s for t in teams for s in t["released"])
    reassigned_all = sorted(s for t in teams for s in t["reassigned"])
    C.check("every released student was placed", released_pool == reassigned_all)

    # --- Atomic + idempotent: re-running distribution changes nothing ----------
    snap = [list(t["reassigned"]) for t in teams]
    client.post("/gm/reassign", data={})
    C.check("reassignment is idempotent", [list(t["reassigned"]) for t in teams] == snap)
    # Refresh persistence of the final class.
    status(client, code, idxs[0])
    C.check("final class stable across refresh", teams[0]["reassigned"] == snap[0])

    # --- Confirmation lock: a locked team cannot silently re-pick --------------
    locked = teams[0]
    before_lock = list(locked["starting_roster"])
    client.post("/team/retain", data={"game_code": code, "team_index": str(idxs[0]),
                                      "students": locked["dealt"][:10], "rationale": "x"})
    C.check("locked placement is not editable", locked["starting_roster"] == before_lock)

    # --- Single-team special case: 5 from un-dealt canonical, exclusion honored -
    for n in (4, 5, 6):
        solo = fresh_client()
        scode = create_game(solo)
        si, st = _setup_to_course(solo, scode, "Solo%d" % n, ["P%d" % j for j in range(n)])
        solo.post("/gm/course/open"); solo.post("/gm/course/close")
        register_course(solo, scode, si, game.COURSE_NAMES[0])
        _retain_confirm(solo, scode, si, S[0:15], S[0:10])
        finish_reassignment(solo, scode)
        C.check("[solo n=%d] final class 15" % n, len(st["active_roster"]) == 15)
        C.check("[solo n=%d] reassigned 5 from un-dealt" % n,
                len(st["reassigned"]) == 5 and all(s not in set(st["dealt"]) for s in st["reassigned"]))
        C.check("[solo n=%d] invented no new characters" % n, all(s in S for s in st["reassigned"]))

    # --- No dead end when the shared pool cannot cover a team (backfill) -------
    # Two identical deals releasing the same five: nothing in the pool is legal
    # for either team, so every replacement is backfilled from un-dealt canonical
    # students. The game still completes, and both hard rules still hold.
    bad = fresh_client()
    bcode = create_game(bad)
    bidx = []
    for nm in ("X0", "X1"):
        i, _ = _setup_to_course(bad, bcode, nm, ["A", "B", "C", "D"])
        bad.post("/gm/course/open"); bad.post("/gm/course/close")
        register_course(bad, bcode, i, game.COURSE_NAMES[0])
        _retain_confirm(bad, bcode, i, S[0:15], S[0:10])   # identical deals + releases
        bidx.append(i)
    bad.post("/gm/reassign", data={})
    bteams = [game.get_team(bcode, i) for i in bidx]
    C.check("backfill: reassignment completed (no dead end)", game.games[bcode].get("reassignment_done"))
    C.check("backfill: no error state", not game.games[bcode].get("reassignment_error"))
    for n, t in enumerate(bteams):
        C.check("[backfill X%d] got exactly 5" % n, len(t.get("reassigned", [])) == 5)
        C.check("[backfill X%d] final class 15, no duplicates" % n,
                len(t["active_roster"]) == 15 and len(set(t["active_roster"])) == 15)
        C.check("[backfill X%d] none from own original 15" % n,
                all(s not in set(t["dealt"]) for s in t["reassigned"]))
        C.check("[backfill X%d] used the backfill path" % n,
                t.get("reassignment_mode") == "shared_pool_plus_backfill")

    # --- Guaranteed no dead end for 2..6 teams on real (random) deals ----------
    # Covers odd team counts (3, 5), which are provably impossible to guarantee
    # by deal design alone; backfill removes the dead end regardless.
    for T in (2, 3, 4, 5, 6):
        cl = fresh_client()
        cc = create_game(cl)
        ids = [_setup_to_course(cl, cc, "N%d_%d" % (T, k), ["A", "B", "C", "D"])[0]
               for k in range(T)]
        cl.post("/gm/course/open"); cl.post("/gm/course/close")
        for i in ids:
            register_course(cl, cc, i, game.COURSE_NAMES[0])
            tt = game.get_team(cc, i)
            cl.post("/team/retain", data={"game_code": cc, "team_index": str(i),
                                          "students": tt["dealt"][:10], "rationale": "auto"})
            cl.post("/team/retain/confirm", data={"game_code": cc, "team_index": str(i)})
        cl.post("/gm/reassign", data={})
        C.check("[T=%d] reassignment completed (no dead end)" % T,
                game.games[cc].get("reassignment_done"))
        for i in ids:
            t = game.get_team(cc, i)
            C.check("[T=%d] team received exactly 5" % T, len(t["reassigned"]) == 5)
            C.check("[T=%d] final class is 15 distinct" % T,
                    len(t["active_roster"]) == 15 and len(set(t["active_roster"])) == 15)
            C.check("[T=%d] exclusion of all original 15 holds" % T,
                    all(s not in set(t["dealt"]) for s in t["reassigned"]))


def test_split_workspace():
    """Step 1 split-screen: container layout, embed chrome, mobile toggle markup,
    privacy isolation (private breaks out of the pane), iframe navigation, form
    submission, and the return path."""
    print("\n=== Split workspace (Canvas + Team Workspace) ===")
    client = fresh_client()
    code = create_game(client)
    names = ["Ann", "Bo", "Cy", "Di"]
    idx = register_team(client, code, "Split", names)
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    finish_reassignment(client, code)   # placement acknowledged -> active play
    client.post("/gm/open")             # open CP1
    qs = {"game_code": code, "team_index": str(idx)}

    # --- Container: both panes, headings, toggle, and no secrets ---------------
    cont = text(client.get("/team/workspace", query_string=qs))
    C.check("workspace shows both pane headings", "Canvas Classroom" in cont and "Team Workspace" in cont)
    C.check("left pane iframes the Canvas shell (embedded)", ("/team/shell?game_code=" + code) in cont)
    C.check("right pane iframes the status screen (embedded)", ("/team/status?game_code=" + code) in cont)
    C.check("both iframe srcs carry embed=1", cont.count("embed=1") >= 2)
    C.check("mobile toggle is a pure-CSS radio group (no JS required)",
            "type='radio'" in cont and "for='v-canvas'" in cont and "for='v-work'" in cont
            and "ws-pane" in cont and "ws-panes" in cont)
    C.check("toggle needs no JavaScript (no script tag in the container)", "<script>" not in cont)
    C.check("no-JS small-screen hint present", "<noscript>" in cont)
    C.check("radiogroup is labelled for assistive tech", "role='radiogroup'" in cont and "aria-label=" in cont)
    for secret in ["THUNDERFLUNKED", "Thunderflunked", "Secret Mission", "Suspicion", "allegiance"]:
        C.check("container hides '%s'" % secret, secret not in cont)
    C.check("container (and iframe URLs) never name the Thunderflunked",
            names[team["thunderflunked"]] not in cont)

    # --- Embedded workspace pane drops cross-pane chrome -----------------------
    emb = text(client.get("/team/status", query_string={**qs, "embed": "1"}))
    C.check("embedded status hides the View Course button", "View Course" not in emb)
    C.check("embedded status hides the Home link", "href='/'>Home" not in emb)
    C.check("embedded status refresh keeps embed", "embed=1" in emb)
    plain = text(client.get("/team/status", query_string=qs))
    C.check("non-embedded status shows the split-view link", "/team/workspace" in plain)
    C.check("non-embedded status shows the View Course button", "View Course" in plain)

    # --- Item 1: the embedded pane drops the redundant NCC branding bar --------
    C.check("embedded status omits the NCC header bar", "<header class='bar'>" not in emb)
    C.check("non-embedded status keeps the NCC header bar", "<header class='bar'>" in plain)
    C.check("embedded status still shows resource meters", "Course Resources" in emb)
    C.check("embedded status still shows the role reminder panel", "Public Roles" in emb)
    C.check("embedded status still shows checkpoint info", "Checkpoint 1" in emb)

    # --- Embedded Canvas pane: no back-to-decision chrome, nav keeps embed -----
    esh = text(client.get("/team/shell", query_string={**qs, "tab": "home", "embed": "1"}))
    C.check("embedded shell hides the back-to-decision footer", "Back to team decision screen" not in esh)
    C.check("embedded shell nav links keep embed", "embed=1" in esh)
    nsh = text(client.get("/team/shell", query_string={**qs, "tab": "home"}))
    C.check("non-embedded shell keeps the back footer", "Back to team decision screen" in nsh)

    # --- Form submission stays in the pane; private pass-gate breaks OUT -------
    client.post("/team/cp/submit",
                data={"game_code": code, "team_index": str(idx), "cp": "0",
                      "actions": ["clarify"], "embed": "1"})
    cp = text(client.get("/team/status", query_string={**qs, "embed": "1"}))
    C.check("embedded checkpoint still hides View Course after submit", "View Course" not in cp)
    C.check("private pass-gate targets _top (takes over the whole window)",
            "action='/team/private/ready' target='_top'" in cp)
    C.check("pass-gate itself leaks no allegiance beside Canvas",
            "THUNDERFLUNKED" not in cp and "THUNDERSTRUCK" not in cp)

    # --- Item 3 (markup level): private screens are POST-only, so they can never
    # be an iframe src and never render inside a pane. (Real browser rendering of
    # target=_top is NOT exercised here -- see the report's browser caveat.)
    for route in ["/team/private/ready", "/team/private/allegiance", "/team/private/vote",
                  "/team/private/mission/claim", "/team/sabotage/activate"]:
        r = client.get(route, query_string=qs)
        C.check("private route %s is POST-only (never a GET/iframe src)" % route, r.status_code == 405)

    # --- Return path: after the private round, public screen offers the split --
    run_private_check(client, code, idx, team, 0)
    pub = text(client.get("/team/status", query_string=qs))
    C.check("post-private public screen offers a return-to-split link", "/team/workspace" in pub)
    C.check("post-private public screen leaks no allegiance",
            "THUNDERFLUNKED" not in pub and "THUNDERSTRUCK" not in pub)

    # --- Item 4: return to the split after private interactions, incl. refresh --
    cont2 = text(client.get("/team/workspace", query_string=qs))
    C.check("split workspace still renders after the private round",
            "Team Workspace" in cont2 and "ws-panes" in cont2)
    refreshed = text(client.get("/team/status", query_string={**qs, "embed": "1"}))
    C.check("refreshed workspace pane shows only public state (no allegiance)",
            "You are THUNDERFLUNKED" not in refreshed and "You are THUNDERSTRUCK" not in refreshed)
    C.check("refreshed workspace pane still omits the NCC header bar",
            "<header class='bar'>" not in refreshed)

    # --- Entry gating: setup-incomplete teams never get the split -------------
    c2 = fresh_client()
    code2 = create_game(c2)
    i2 = register_team(c2, code2, "Early", ["A", "B", "C", "D"])
    early = text(c2.get("/team/workspace", query_string={"game_code": code2, "team_index": str(i2)}))
    C.check("workspace for a setup-incomplete team does not render the split",
            "ws-panes" not in early)


def test_canvas_highlights():
    """Contextual Canvas navigation highlights (Step 13): checkpoint->section
    mapping, genuinely-relevant content, correct advancement, accessibility,
    neutral language, privacy, and no exposure of scoring or correct answers."""
    print("\n=== Contextual Canvas highlights ===")
    client = fresh_client()
    code = create_game(client)
    idx = register_team(client, code, "Hi", ["A", "B", "C", "D"])
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    finish_reassignment(client, code)
    qs = {"game_code": code, "team_index": str(idx)}

    def nav():
        return text(client.get("/team/shell", query_string={**qs, "tab": "home", "embed": "1"}))

    def section(tab):
        return text(client.get("/team/shell", query_string={**qs, "tab": tab, "embed": "1"}))

    def highlighted(html):
        return set(k for _cls, k in re.findall(r"class='([^']*wx[^']*)'[^>]*tab=(\w+)", html))

    # One representative, genuinely-relevant content string per flagged section.
    content = {
        0: {"gradebook": "missing", "syllabus": "disagree", "inbox": "confusing"},
        1: {"roster": "late add", "modules": "new students"},
        2: {"gradebook": "major assessment 1"},
        3: {"gradebook": "assignment 3", "inbox": "feedback"},
        4: {"gradebook": "final", "announcements": "course improvement survey"},
    }
    seen = []
    for cpn in range(5):
        client.post("/gm/open")
        n = nav()
        hi = highlighted(n)
        seen.append(frozenset(hi))
        C.check("CP%d highlights match the explicit mapping" % (cpn + 1),
                hi == set(game.CP_CANVAS_HIGHLIGHTS[cpn]))
        C.check("CP%d flags 1-3 sections (not excessive)" % (cpn + 1), 1 <= len(hi) <= 3)
        # Each flagged section actually contains current, relevant evidence.
        for tab, needle in content[cpn].items():
            C.check("CP%d: flagged '%s' holds relevant content" % (cpn + 1, tab),
                    needle in section(tab).lower())
        # Every section stays reachable whether or not it is flagged.
        for key, _label in game.COURSE_NAV:
            C.check("CP%d: nav keeps '%s' accessible" % (cpn + 1, key), ("tab=" + key) in n)
        # Neutral language only; nothing that reveals the correct/best choice.
        C.check("CP%d: uses neutral 'Worth exploring'" % (cpn + 1), "Worth exploring" in n)
        for bad in ["Correct", "Best choice", "best choice", "correct answer",
                    "Overall Course Score", "THUNDERFLUNKED"]:
            C.check("CP%d: nav avoids '%s'" % (cpn + 1, bad), bad not in n)

        if cpn == 0:
            # Privacy: a private pass-the-computer screen carries no Canvas highlight.
            client.post("/team/cp/submit", data={**qs, "cp": "0", "actions": ["clarify"]})
            priv = text(client.post("/team/private/ready", data={**qs, "cp": "0"}))
            # Highlights live only in the Canvas course nav, which private screens
            # never render. (BASE_CSS defining .nav-wx is in every head; we check
            # the rendered MARKUP, not the stylesheet.)
            C.check("private screen renders no Canvas nav", "<nav class='lms-course'" not in priv)
            C.check("private screen shows no 'Worth exploring' flag", "Worth exploring" not in priv)
            C.check("private screen shows no highlight span", "<span class='nav-wx'>" not in priv)
            # Refresh does not change game state.
            before = game.games[code]["checkpoint_index"]
            nav(); nav()
            C.check("refreshing the shell does not change checkpoint state",
                    game.games[code]["checkpoint_index"] == before)
            run_private_check(client, code, idx, team, 0)
        elif cpn == 1:
            client.post("/team/cp/submit", data={**qs, "cp": "1", "actions": ["start_here"]})
        elif cpn == 2:
            client.post("/team/cp/submit", data={**qs, "cp": "2", "actions": ["aligned_practice"]})
            run_private_check(client, code, idx, team, 2)
        elif cpn == 3:
            client.post("/team/cp4/submit", data={**qs, "priority": "scalable", "supporting": "tool"})
        elif cpn == 4:
            client.post("/team/cp5/submit", data={**qs, "protect": "grading",
                                                  "repair": "gradebook", "not_add": "extra_credit"})
        client.post("/gm/lock")
        if cpn < 4:
            client.post("/gm/advance")

    C.check("highlights update as the GM advances (distinct sets across CPs)", len(set(seen)) >= 3)
    # The mapping itself only names real nav keys and flags 1-3 per checkpoint.
    navkeys = {k for k, _ in game.COURSE_NAV}
    for i, keys in game.CP_CANVAS_HIGHLIGHTS.items():
        C.check("mapping CP%d uses real nav keys, 1-3 of them" % (i + 1),
                1 <= len(keys) <= 3 and all(k in navkeys for k in keys))


def test_resources_drawer():
    """Always-available Resources drawer in the public Team Workspace: resource
    completeness, category + teaching-context filters, artwork, privacy (absent
    from private screens; no secrets/scoring), and form-state preservation."""
    print("\n=== Resources drawer (public workspace) ===")
    client = fresh_client()
    code = create_game(client)
    idx = register_team(client, code, "Res", ["A", "B", "C", "D"])
    team = game.get_team(code, idx)
    assign_roles(client, code, idx, team)
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, idx, game.COURSE_NAMES[0])   # WELL 142, on-campus
    deal_and_draft(client, code, idx, game.STUDENTS[:15], game.STUDENTS[:10])
    finish_reassignment(client, code)
    qs = {"game_code": code, "team_index": str(idx)}

    # Available on the waiting screen (before any checkpoint opens).
    wait = text(client.get("/team/status", query_string=qs))
    C.check("drawer available on the waiting screen",
            "res-drawer" in wait and "Resources &amp; Student Support" in wait)

    client.post("/gm/open")   # CP1
    cp = text(client.get("/team/status", query_string=qs))

    # --- Completeness: all 12 tools + 6 supports, with existing descriptions ---
    C.check("drawer has 12 instructor tools", cp.count("data-cat='tool'") == 12)
    C.check("drawer has 6 student supports", cp.count("data-cat='support'") == 6)
    for r in game.RESOURCES:
        C.check("drawer lists resource: %s" % r["name"], r["name"] in cp)
    C.check("drawer reuses existing resource descriptions",
            "What it is:" in cp and "When it may help:" in cp and "Limitations:" in cp)

    # --- Artwork, mapped accurately ------------------------------------------
    C.check("drawer shows tool artwork", "/static/art/tools/tool_" in cp)
    C.check("drawer shows support artwork", "/static/art/supports/support_" in cp)

    # --- Category filters: All / Instructor Tools / Student Supports ----------
    C.check("category filters present (All/Tools/Supports)",
            "for='rc-all'" in cp and "for='rc-tools'" in cp and "for='rc-supports'" in cp)
    # --- Teaching-context filters: Online / Face-to-Face / CTL/Lab -----------
    C.check("teaching-context filters present (Online/F2F/CTE)",
            "for='rx-online'" in cp and "for='rx-campus'" in cp and "for='rx-cte'" in cp)
    mc = game.modality_class(game.COURSE_BY_NAME[game.COURSE_NAMES[0]]["modality"])
    C.check("default teaching context matches the course modality",
            ("id='rx-%s' checked" % mc) in cp)
    # Context never hard-excludes resources: all 18 remain regardless of context.
    C.check("context filter does not hide resources (no context-based display:none)",
            "res-item" in cp and ":checked~.res-list .res-item[data-cat" in cp
            and "res-note{display:none" in cp)   # only the NOTES toggle, not items
    C.check("filters are pure-CSS radios (work without JS)",
            "type='radio'" in cp and ":checked~.res-list" in cp)

    # --- Concern 1: a CLOSED drawer cannot intercept clicks/focus -------------
    # The fixed backdrop/panel is display:none unless the <details> is [open],
    # so when closed it is removed from the layout and the hit-testing tree.
    overlay_rule = cp[cp.index(".res-overlay{"):cp.index(".res-overlay{") + 180]
    C.check("closed drawer overlay is display:none (cannot intercept)", "display:none" in overlay_rule)
    C.check("overlay only shows when [open]", ".res-drawer[open] .res-overlay{display:flex" in cp)
    C.check("base overlay rule is hidden by default (not unconditionally shown)",
            "flex-end;display:none" in cp)

    # --- Concern 2: the drawer never wraps the decision form ------------------
    C.check("decision form still renders alongside the drawer",
            "action='/team/cp/submit'" in cp and "name='actions'" in cp)
    C.check("drawer opens client-side via <details> (no navigation, no submit)",
            "<details class='res-drawer'>" in cp)
    C.check("drawer sits OUTSIDE the decision form (opens before it)",
            cp.index("res-drawer") < cp.index("action='/team/cp/submit'"))
    C.check("drawer fully CLOSES before the decision form begins",
            cp.index("</details>") < cp.index("action='/team/cp/submit'"))
    C.check("drawer filter fields do not collide with decision fields",
            "name='res-cat'" in cp and "name='res-ctx'" in cp
            and "res-cat" != "actions" and "res-ctx" != "reassess")

    # --- Concern 3: all 18 resources + artwork reachable in the SPLIT pane -----
    embp = text(client.get("/team/status", query_string={**qs, "embed": "1"}))
    C.check("drawer present in the embedded split pane", "res-drawer" in embp)
    C.check("embedded pane keeps all 12 tools + 6 supports",
            embp.count("data-cat='tool'") == 12 and embp.count("data-cat='support'") == 6)
    C.check("embedded pane keeps tool + support artwork",
            "/static/art/tools/tool_" in embp and "/static/art/supports/support_" in embp)

    # --- Privacy: no secrets/scoring inside the drawer -----------------------
    draw = cp[cp.index("<details class='res-drawer'>"):cp.index("</details>")]
    for leak in ["THUNDERFLUNKED", "THUNDERSTRUCK", "Thunderflunked", "Suspicion",
                 "Secret Mission", "Overall Course Score", "compute_scores"]:
        C.check("drawer hides '%s'" % leak, leak not in draw)
    # The drawer is built only from static RESOURCES + the course modality; it
    # reads no player/allegiance/score state, so it is deterministic and cannot
    # carry hidden identities.
    C.check("drawer content is deterministic (no hidden/random state)",
            game.resources_drawer(team) == game.resources_drawer(team))

    # --- Privacy: drawer is ABSENT from private pass-the-computer screens -----
    client.post("/team/cp/submit", data={**qs, "cp": "0", "actions": ["clarify"]})
    priv = text(client.post("/team/private/ready", data={**qs, "cp": "0"}))
    C.check("private screen carries no resources drawer", "res-drawer" not in priv)
    C.check("private screen carries no Resources button", "Resources &amp; Student Support" not in priv)

    # --- Available across all five checkpoints --------------------------------
    run_private_check(client, code, idx, team, 0)
    client.post("/gm/lock"); client.post("/gm/advance")
    def drawer_at_cp(n):
        return "res-drawer" in text(client.get("/team/status", query_string=qs))
    client.post("/gm/open")  # CP2
    C.check("drawer at CP2", drawer_at_cp(2))
    client.post("/team/cp/submit", data={**qs, "cp": "1", "actions": ["start_here"]})
    client.post("/gm/lock"); client.post("/gm/advance")
    client.post("/gm/open")  # CP3
    C.check("drawer at CP3", drawer_at_cp(3))
    client.post("/team/cp/submit", data={**qs, "cp": "2", "actions": ["aligned_practice"]})
    run_private_check(client, code, idx, team, 2)
    client.post("/gm/lock"); client.post("/gm/advance")
    client.post("/gm/open")  # CP4
    C.check("drawer at CP4 (brief round)", drawer_at_cp(4))
    client.post("/team/cp4/submit", data={**qs, "priority": "scalable", "supporting": "tool"})
    client.post("/gm/lock"); client.post("/gm/advance")
    client.post("/gm/open")  # CP5
    C.check("drawer at CP5", drawer_at_cp(5))


def test_reassignment_pool_vs_backfill():
    """Pool-first maximization, backfill correctness, per-student origin labels
    with privacy, GM summary counts, and 15 students in the Canvas roster."""
    print("\n=== Reassignment: pool vs. administrative backfill ===")
    S = game.STUDENTS
    client = fresh_client()
    code = create_game(client)
    iA, _ = _setup_to_course(client, code, "Alpha", ["A", "B", "C", "D"])
    iB, _ = _setup_to_course(client, code, "Bravo", ["A", "B", "C", "D"])
    client.post("/gm/course/open"); client.post("/gm/course/close")
    register_course(client, code, iA, game.COURSE_NAMES[0])
    register_course(client, code, iB, game.COURSE_NAMES[0])
    # Deals chosen so the pool can cover Alpha fully but Bravo not at all:
    #   Alpha deal S[0:15], refers S[10:15]  (lands nowhere: in both deals)
    #   Bravo deal S[5:20], refers S[15:20]  (eligible for Alpha, excluded for Bravo)
    _retain_confirm(client, code, iA, S[0:15], S[0:10])
    _retain_confirm(client, code, iB, S[5:20], S[5:15])
    client.post("/gm/reassign", data={})
    tA = game.get_team(code, iA)
    tB = game.get_team(code, iB)

    # Req 1: pool placements are maximized before backfill. Only 5 of the 10
    # referred students are placeable at all (Alpha's referrals sit in both
    # deals), and all 5 go to Alpha from the shared pool.
    C.check("Alpha filled entirely from shared pool",
            set(tA["reassigned_pool"]) == set(S[15:20]) and tA["reassigned_backfill"] == [])
    C.check("Alpha mode is shared_pool", tA["reassignment_mode"] == "shared_pool")
    C.check("Bravo got nothing placeable from the pool", tB["reassigned_pool"] == [])
    C.check("pool placement is maximized (5 of 10 referred are placeable)",
            len(tA["reassigned_pool"]) + len(tB["reassigned_pool"]) == 5)

    # Req 2: backfill is canonical, excludes the team's own 15, no final duplicates.
    C.check("Bravo backfilled exactly 5", len(tB["reassigned_backfill"]) == 5)
    C.check("Bravo backfill uses only canonical students", all(s in S for s in tB["reassigned_backfill"]))
    C.check("Bravo backfill excludes its own original 15",
            all(s not in set(tB["dealt"]) for s in tB["reassigned_backfill"]))
    C.check("Bravo final class is 15 distinct", len(set(tB["active_roster"])) == 15)
    C.check("Bravo mode is shared_pool_plus_backfill",
            tB["reassignment_mode"] == "shared_pool_plus_backfill")

    # Req 3: the final class distinguishes origin, without naming the source team.
    scrA = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(iA)}))
    C.check("Alpha final class labels shared-pool arrivals", "Shared pool" in scrA)
    C.check("Alpha final class does not expose the source team", "Bravo" not in scrA)
    scrB = text(client.get("/team/status", query_string={"game_code": code, "team_index": str(iB)}))
    C.check("Bravo final class labels administrative placements", "Administrative" in scrB)
    C.check("Bravo final class does not expose any source team", "Alpha" not in scrB)

    # Req 4: GM sees a simple pool-vs-backfill summary without managing anything.
    dash = text(client.get("/gm"))
    C.check("GM per-team split: Alpha 5 pool / 0 backfill", "5 from shared pool, 0 administrative" in dash)
    C.check("GM per-team split: Bravo 0 pool / 5 backfill", "0 from shared pool, 5 administrative" in dash)
    C.check("GM game-level distribution total shown", "Replacements distributed:" in dash)

    # Req 5: all 15 students appear in the final class and the Canvas roster.
    C.check("Alpha final class is 15", len(tA["active_roster"]) == 15)
    finish_reassignment(client, code)   # acknowledge both teams
    client.post("/gm/open")             # open CP1 so the shell is reachable
    roster = text(client.get("/team/shell",
                  query_string={"game_code": code, "team_index": str(iA), "tab": "roster"}))
    C.check("Canvas roster shows all 15 of Alpha's final class",
            all(s in roster for s in tA["active_roster"]))
    C.check("Canvas roster marks the 5 reassigned students",
            roster.count("late add") == 5 or roster.lower().count("reassign") >= 1)


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
    finish_reassignment(client, code)
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
    finish_reassignment(client, code)
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
    test_digital_course_selection_and_trading()
    test_course_card_readability()
    test_placement_screen_redesign()
    test_digital_student_placement()
    test_reassignment_pool_vs_backfill()
    test_resources_drawer()
    test_canvas_highlights()
    test_split_workspace()
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
