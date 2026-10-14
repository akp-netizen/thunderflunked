"""
Northwest Iowa Community College - Faculty Development Game
Release Candidate 1.

A one-hour tabletop/computer hybrid game. Faculty teams guide a fictional
college course through one semester (five checkpoints), practicing good course
design process. A hidden-role layer (Thunderstruck vs. one Thunderflunked per
table) reinforces that a reasonable-sounding idea can still be a poor course
decision, and that good process - not spotting the villain - is what catches it.

Architecture (intentionally simple and reliable):
  * Flask, in-memory state (resets on restart). No database, no auth, no JS.
  * Three public screens: Game Master (/gm), Team (/team), Projector (/projector).
  * Short game codes; multiple teams; 4-6 players each; safe re-entry by
    Game Code + Team Name.

State lives in `games[code]` (game-wide: teams, checkpoint, trading, final flag)
and each team dict (players, roles, course, roster, resources, checkpoint
decisions, private checks, allegiance, sabotage, accusation).
"""

import json
import os
import random
import threading
import time

from flask import Flask, request, redirect, url_for, session, jsonify
from markupsafe import escape

import storage

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Deployment configuration (environment-driven; no secrets in source).
# ---------------------------------------------------------------------------
# PRODUCTION is on whenever a database is configured (i.e. on Render). It
# tightens cookie security and requires a real SECRET_KEY.
_DATABASE_URL = os.environ.get("DATABASE_URL")
app.config["PRODUCTION"] = bool(_DATABASE_URL) or os.environ.get("FACULTY_GAME_PRODUCTION") == "1"

# Session signing key. Required in production (set SECRET_KEY on Render). For
# local development we fall back to a random per-process key so the app still
# runs; the only consequence is that GM logins do not survive a local restart.
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    if app.config["PRODUCTION"]:
        raise RuntimeError(
            "SECRET_KEY environment variable is required in production. "
            "Set it on your Render service (see DEPLOY_RENDER.md).")
    _secret = os.urandom(32).hex()
app.config["SECRET_KEY"] = _secret

# GM password gate. When set, the Game Master screens require login. When unset
# (local development and the automated test suite), GM screens stay open exactly
# as in the original prototype, so nothing about local play or testing changes.
app.config["GM_PASSWORD"] = os.environ.get("GM_PASSWORD")

# Secure session cookies. Only mark cookies Secure in production, otherwise the
# browser would refuse to send them over plain http during local development.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(app.config["PRODUCTION"]),
)

# A single lock serializes request handling. The original game logic mutates
# deeply nested in-memory dicts and was never written to be thread-safe; with a
# threaded server we must not let two requests interleave mid-mutation. The game
# is low-throughput (one room, a handful of teams) so this has no practical cost.
_state_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Live game state. Still the same in-memory dicts the game was built and tested
# against; persistence (below) snapshots them to a database so a server restart
# on Render does not lose an in-progress game.
# ---------------------------------------------------------------------------
games = {}
active_code = None


# ---------------------------------------------------------------------------
# Persistence: snapshot the whole in-memory state to the database after each
# change, and restore it on boot. See storage.py for the rationale.
# ---------------------------------------------------------------------------
def _persistence_active():
    """Persistence runs only when a database is configured and we are not in the
    in-memory test harness (the test suite manipulates globals directly)."""
    return storage.is_enabled() and not app.config.get("TESTING")


def _normalize_loaded_state(loaded_games):
    """JSON turns every object key into a string, but a few of the game's
    dictionaries are keyed by integer checkpoint index. Convert just those keys
    back to ints so restored games behave identically to live ones. All other
    dictionaries (votes/decisions keyed by player-index strings, etc.) are left
    exactly as stored."""
    for game in loaded_games.values():
        for team in game.get("teams", []):
            if isinstance(team.get("private"), dict):
                team["private"] = {int(k): v for k, v in team["private"].items()}
            if isinstance(team.get("cp"), dict):
                team["cp"] = {int(k): v for k, v in team["cp"].items()}
            sab = team.get("sabotage")
            if isinstance(sab, dict) and isinstance(sab.get("prompted"), dict):
                sab["prompted"] = {int(k): v for k, v in sab["prompted"].items()}
    return loaded_games


def save_state():
    """Persist the current in-memory state. Safe to call unconditionally."""
    if _persistence_active():
        storage.save_state(games, active_code)


def restore_state():
    """Load any previously saved game back into memory on boot."""
    global games, active_code
    if not _persistence_active():
        return
    loaded_games, loaded_code = storage.load_state()
    games = _normalize_loaded_state(loaded_games)
    active_code = loaded_code


# ---------------------------------------------------------------------------
# Request lifecycle: serialize handling with the state lock, then persist any
# changes made by mutating (POST) requests.
# ---------------------------------------------------------------------------
@app.before_request
def _acquire_state_lock():
    _state_lock.acquire()


@app.teardown_request
def _release_state_lock(exc):
    try:
        if exc is None and request.method not in ("GET", "HEAD", "OPTIONS"):
            save_state()
    finally:
        try:
            _state_lock.release()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Game Master access control. When GM_PASSWORD is set, every /gm* screen except
# the login page requires an authenticated session. When it is unset (local dev
# and tests) the screens stay open, preserving the original workflow.
# ---------------------------------------------------------------------------
@app.before_request
def _require_gm_login():
    path = request.path
    if not (path == "/gm" or path.startswith("/gm/")):
        return None
    if path in ("/gm/login", "/gm/logout"):
        return None
    if not app.config.get("GM_PASSWORD"):
        return None  # GM protection not configured (local/dev/test): open.
    if session.get("is_gm"):
        return None
    return redirect(url_for("gm_login"))


@app.route("/gm/login", methods=["GET", "POST"])
def gm_login():
    configured = app.config.get("GM_PASSWORD")
    if not configured:
        # Nothing to protect; send the GM straight to the dashboard.
        return redirect(url_for("gm"))
    error = ""
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if supplied == configured:
            session["is_gm"] = True
            session.permanent = False
            return redirect(url_for("gm"))
        error = "<p class='err'>Incorrect password.</p>"
    body = (
        "<div class='card'><h2>Game Master Login</h2>" + error +
        "<form method='post' action='/gm/login'>"
        "<p><label>Password<br><input type='password' name='password' autocomplete='off'></label></p>"
        "<p><button type='submit'>Log In</button></p></form></div>"
        "<p class='footer'><a href='/'>Home</a></p>")
    return page("Game Master Login", body, "gm")


@app.route("/gm/logout", methods=["POST", "GET"])
def gm_logout():
    session.pop("is_gm", None)
    return redirect(url_for("gm_login"))


# ---------------------------------------------------------------------------
# Health check (used by Render and for manual verification). No auth, no state.
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    try:
        storage.ping()
    except Exception:
        return "database unavailable", 503
    return "ok", 200


# Characters used for game codes. Ambiguous ones (0, O, 1, I) are left out.
CODE_CHARACTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Teams are only allowed these player counts.
ALLOWED_PLAYER_COUNTS = [4, 5, 6]

# How many cards a team is dealt, how many make the starting roster.
DEALT_COUNT = 15
ROSTER_COUNT = 10


def e(value):
    """Escape user-provided text for safe HTML display."""
    return str(escape(value))


# ---------------------------------------------------------------------------
# Public Roles - perspectives, not powers.
# ---------------------------------------------------------------------------
PUBLIC_ROLES = [
    "Student Whisperer",
    "Learning Police",
    "Burnout Bouncer",
    "Receipts Department",
    "Efficiency Gremlin",
    "Professional Buzzkill",
]

ROLE_PERSPECTIVES = {
    "Student Whisperer": "Watch for the student experience: engagement, belonging, and who may be quietly struggling.",
    "Learning Police": "Watch for alignment: do the activities, practice, and assessments actually match the stated learning?",
    "Burnout Bouncer": "Watch faculty capacity: is the plan sustainable for a real human instructor?",
    "Receipts Department": "Watch the evidence: what does the data actually show before we decide what it means?",
    "Efficiency Gremlin": "Watch for scale: is there a reusable, lower-effort way to solve this for everyone?",
    "Professional Buzzkill": "Watch for consequences: what does this decision cost us later that it does not cost us now?",
}


def role_image_src(role):
    """Static path to the printed Role Card art for a public role (e.g.
    "Student Whisperer" -> /static/art/roles/role_student_whisperer.png)."""
    return "/static/art/roles/role_" + role.lower().replace(" ", "_") + ".png"


def allegiance_image_src(is_thunderflunked):
    """Static path to the printed Allegiance Card art."""
    name = "thunderflunked" if is_thunderflunked else "thunderstruck"
    return "/static/art/allegiance/allegiance_" + name + ".png"

# ---------------------------------------------------------------------------
# The 30 Student Cards.
# Visible stats (1-5 each): Readiness, Motivation, Communication,
# Self-Management, Adaptability, Help-Seeking. Totals run ~16-20.
# No visible total is shown to faculty. Stats drive starting conditions and
# which students the authored checkpoint evidence features.
# ---------------------------------------------------------------------------
# Key order used everywhere: readiness, motivation, communication,
# self_management, adaptability, help_seeking.
STUDENT_DATA = [
    {"n": 1, "name": "Captain Calendar",
     "stats": (3, 5, 3, 1, 3, 3),
     "context": "Recent high-school graduate. Works weekend shifts.",
     "strengths": ["Enthusiastic", "Notices upcoming work", "Wants to do well"],
     "challenges": ["Plans more than executes", "Underestimates task time"]},
    {"n": 2, "name": "Deadline Dodge",
     "stats": (2, 5, 4, 1, 4, 2),
     "context": "Lives with roommates and works variable evening shifts.",
     "strengths": ["Energetic", "Personable", "Rebounds quickly"],
     "challenges": ["Starts late", "Relies on urgency", "Inconsistent routines"]},
    {"n": 3, "name": "Professor Already-Knows-It",
     "stats": (5, 3, 2, 4, 1, 3),
     "context": "Returning student with significant prior experience related to the subject.",
     "strengths": ["Strong background knowledge", "Independent", "Efficient"],
     "challenges": ["Dislikes changing established methods", "May disengage from material that feels familiar"]},
    {"n": 4, "name": "Human Question Mark",
     "stats": (3, 2, 5, 4, 3, 1),
     "context": "First semester at college and still learning how college courses operate.",
     "strengths": ["Communicative", "Careful", "Follows established routines"],
     "challenges": ["Hesitates to admit confusion directly", "Can overthink ambiguous directions"]},
    {"n": 5, "name": "Checklist Chad",
     "stats": (2, 4, 1, 5, 3, 3),
     "context": "Balances school with a predictable part-time job.",
     "strengths": ["Organized", "Dependable", "Follows processes"],
     "challenges": ["Rarely speaks up spontaneously", "Can focus on completion over purpose"]},
    {"n": 6, "name": "Pivot McGee",
     "stats": (4, 1, 3, 2, 5, 3),
     "context": "Career changer returning after several years.",
     "strengths": ["Learns quickly", "Flexible", "Comfortable changing approach"],
     "challenges": ["Motivation fluctuates", "Routines are still developing"]},
    {"n": 7, "name": "Late Bloomer Supreme",
     "stats": (1, 4, 3, 2, 4, 3),
     "context": "Entered college after time away.",
     "strengths": ["Persistent", "Receptive to support", "Willing to adjust"],
     "challenges": ["Foundations are rebuilding", "Takes time to establish routines"]},
    {"n": 8, "name": "Silent SOS",
     "stats": (3, 2, 4, 3, 3, 1),
     "context": "Commuter with a consistent weekly schedule.",
     "strengths": ["Communicates well when approached", "Uses support effectively once connected"],
     "challenges": ["May struggle to initiate when stuck", "Can wait too long for someone else to notice"]},
    {"n": 9, "name": "AP Everything",
     "stats": (5, 2, 1, 3, 4, 3),
     "context": "Has substantial previous academic credit.",
     "strengths": ["Academically prepared", "Learns quickly", "Handles change"],
     "challenges": ["Very quiet", "Participation can be difficult to read"]},
    {"n": 10, "name": "Group Chat Mayor",
     "stats": (2, 3, 5, 2, 1, 5),
     "context": "Highly connected with classmates and active in several campus activities.",
     "strengths": ["Builds relationships", "Asks for help", "Keeps peers connected"],
     "challenges": ["Change frustrates them", "Competing commitments can disrupt routines"]},
    {"n": 11, "name": "Locked-In Larry",
     "stats": (3, 4, 2, 4, 1, 4),
     "context": "Adult learner with a carefully structured weekly routine.",
     "strengths": ["Persistent", "Organized", "Seeks assistance when needed"],
     "challenges": ["Reserved communicator", "Unexpected changes are difficult"]},
    {"n": 12, "name": "Reinvention Rita",
     "stats": (1, 3, 4, 5, 2, 3),
     "context": "Returning student starting a new career after years in the workforce.",
     "strengths": ["Highly organized", "Communicates clearly", "Reliable"],
     "challenges": ["Academic foundations are rusty", "Needs time with unfamiliar approaches"]},
    {"n": 13, "name": "Chaos With a Planner",
     "stats": (3, 3, 2, 2, 4, 4),
     "context": "Full course load with an extremely detailed planner.",
     "strengths": ["Flexible", "Willing to ask", "Motivated by visible progress"],
     "challenges": ["Planner is not always execution", "Loses track when tasks overlap"]},
    {"n": 14, "name": "Natural Talent Nancy",
     "stats": (5, 1, 3, 4, 2, 3),
     "context": "Has prior informal experience related to the subject.",
     "strengths": ["Strong initial skill base", "Independent", "Dependable"],
     "challenges": ["Motivation drops when work feels repetitive", "May resist unfamiliar approaches"]},
    {"n": 15, "name": "Office Hours Celebrity",
     "stats": (2, 3, 5, 2, 3, 4),
     "context": "Lives close to campus and spends a lot of time there.",
     "strengths": ["Talks through ideas", "Engaged", "Comfortable connecting"],
     "challenges": ["May seek reassurance without identifying the help actually needed"]},
    {"n": 16, "name": "Rubric Enthusiast",
     "stats": (4, 3, 1, 5, 2, 2),
     "context": "Has succeeded in structured settings and prefers clear expectations.",
     "strengths": ["Prepared", "Exceptionally organized", "Consistent"],
     "challenges": ["Reluctant to speak", "Ambiguity slows progress"]},
    {"n": 17, "name": "Fresh Start Freddie",
     "stats": (1, 5, 3, 4, 3, 2),
     "context": "Beginning college after a previous path did not work out.",
     "strengths": ["Highly motivated", "Dependable", "Willing to work"],
     "challenges": ["Skills need development", "Reluctant to request assistance"]},
    {"n": 18, "name": "Improvisation Station",
     "stats": (3, 2, 4, 1, 5, 3),
     "context": "Works irregular hours and adjusts plans quickly.",
     "strengths": ["Flexible", "Communicative", "Resourceful"],
     "challenges": ["Routine maintenance is difficult", "Long-range planning is inconsistent"]},
    {"n": 19, "name": "Quietly Crushing It",
     "stats": (5, 3, 3, 2, 4, 1),
     "context": "Has previous coursework and a small, predictable outside workload.",
     "strengths": ["Academically strong", "Adaptable", "Independent"],
     "challenges": ["Procrastinates when confident", "Rarely signals problems"]},
    {"n": 20, "name": "Talk-It-Out Taylor",
     "stats": (2, 1, 5, 4, 3, 3),
     "context": "Returning student who learns best through conversation.",
     "strengths": ["Expressive", "Dependable", "Collaborative"],
     "challenges": ["Motivation is uneven", "May process aloud before attempting independently"]},
    {"n": 21, "name": "Backup Plan Bailey",
     "stats": (3, 5, 3, 1, 3, 3),
     "context": "Parent with a coordinated household and school schedule.",
     "strengths": ["Determined", "Anticipates obstacles", "Thinks ahead"],
     "challenges": ["Plans collapse when priorities collide", "May revise the plan instead of starting"]},
    {"n": 22, "name": "Plot Twist Pat",
     "stats": (4, 1, 3, 2, 5, 3),
     "context": "Recent graduate exploring a very different field.",
     "strengths": ["Adaptable", "Academically capable", "Curious"],
     "challenges": ["Motivation depends on relevance", "Routines are inconsistent"]},
    {"n": 23, "name": "Steady-ish Eddie",
     "stats": (3, 4, 2, 4, 1, 4),
     "context": "Works full-time on a consistent schedule.",
     "strengths": ["Reliable", "Persistent", "Comfortable seeking support"],
     "challenges": ["Prefers predictability", "Communicates selectively"]},
    {"n": 24, "name": "Middle-of-the-Road Morgan",
     "stats": (3, 3, 3, 3, 4, 3),
     "context": "Traditional-age student with a moderate outside schedule.",
     "strengths": ["Flexible", "Reasonably consistent", "Works well with others"],
     "challenges": ["Few extreme strengths", "Can blend in"]},
    {"n": 25, "name": "Surprisingly Prepared Sam",
     "stats": (3, 3, 3, 3, 3, 4),
     "context": "Expected college to be overwhelming and built a support routine before classes.",
     "strengths": ["Uses help", "Steady", "Reflective"],
     "challenges": ["Confidence lags performance", "May seek confirmation"]},
    {"n": 26, "name": "Low-Drama Logan",
     "stats": (4, 3, 3, 3, 3, 2),
     "context": "Returning student with prior college experience and a predictable schedule.",
     "strengths": ["Prepared", "Steady", "Self-directed"],
     "challenges": ["Can become invisible", "May wait too long to communicate"]},
    {"n": 27, "name": "Selectively Organized",
     "stats": (4, 4, 2, 3, 3, 2),
     "context": "Has several recurring outside responsibilities.",
     "strengths": ["Prepared", "Motivated", "Has systems for priorities"],
     "challenges": ["Lower-priority tasks disappear", "Reluctant to ask"]},
    {"n": 28, "name": "Ask-Me-Anything Avery",
     "stats": (2, 3, 4, 4, 2, 3),
     "context": "Newer student who is comfortable talking with faculty and peers.",
     "strengths": ["Communicative", "Organized", "Willing to engage"],
     "challenges": ["Preparation varies", "Unexpected changes take time"]},
    {"n": 29, "name": "Plan B Parker",
     "stats": (3, 2, 3, 4, 4, 2),
     "context": "Adult learner managing work projects and changing schedules.",
     "strengths": ["Organized", "Adaptable", "Calm under change"],
     "challenges": ["Motivation is selective", "Tends to solve problems alone"]},
    {"n": 30, "name": "Motivated-ish Micah",
     "stats": (3, 4, 2, 2, 3, 4),
     "context": "Has a clear career goal and a busy but manageable outside schedule.",
     "strengths": ["Goal-oriented", "Seeks support", "Keeps moving after setbacks"],
     "challenges": ["Daily execution is inconsistent", "Communication can be brief or delayed"]},
]

STAT_KEYS = ["readiness", "motivation", "communication",
             "self_management", "adaptability", "help_seeking"]
STAT_LABELS = {
    "readiness": "Readiness",
    "motivation": "Motivation",
    "communication": "Communication",
    "self_management": "Self-Management",
    "adaptability": "Adaptability",
    "help_seeking": "Help-Seeking",
}

# Card label format: "#07 Late Bloomer Supreme". These labels are the values
# used in checkboxes and stored on rosters, so they must stay stable.
STUDENTS = []
STUDENT_STATS = {}
STUDENT_INFO = {}
for _s in STUDENT_DATA:
    _label = "#%02d %s" % (_s["n"], _s["name"])
    STUDENTS.append(_label)
    STUDENT_STATS[_label] = dict(zip(STAT_KEYS, _s["stats"]))
    STUDENT_INFO[_label] = _s

# ---------------------------------------------------------------------------
# Resources (0-100). Status bands. Starting floor is 40.
# ---------------------------------------------------------------------------
RESOURCE_NAMES = [
    "Student Success",
    "Course Quality",
    "Faculty Capacity",
    "Time/Resources",
]

RESOURCE_BLURB = {
    "Student Success": "Students' current ability to engage, persist, respond to difficulty, and use support.",
    "Course Quality": "Health of the learning environment: clarity, alignment, accessibility, meaningful practice, feedback, consistency.",
    "Faculty Capacity": "Instructor human bandwidth: attention, judgment, communication, follow-up, cognitive and emotional load.",
    "Time/Resources": "Structural bandwidth: scalable routines, available supports, predictable processes, efficient interventions, time.",
}

# How each resource reads the roster as a classroom system. Weights are spread
# so no single visible stat is a dominant drafting strategy.
RESOURCE_WEIGHTS = {
    "Student Success": {"readiness": 1.0, "motivation": 1.5, "adaptability": 1.0,
                        "help_seeking": 1.0, "communication": 0.5},
    "Course Quality": {"communication": 1.0, "adaptability": 1.0,
                       "readiness": 1.0, "self_management": 1.0},
    "Faculty Capacity": {"help_seeking": 1.5, "self_management": 1.0,
                         "communication": 1.0, "motivation": 0.5},
    "Time/Resources": {"self_management": 1.5, "help_seeking": 1.0,
                       "motivation": 1.0, "adaptability": 0.5},
}

RESOURCE_FLOOR = 40  # starting resources may never begin below this


def resource_status(value):
    if value < 30:
        return "Critical"
    if value < 60:
        return "Strained"
    return "Normal"


def _resource_from_roster(roster, weights):
    """Score one resource from a 10-student roster as a classroom system.

    Not a simple average: low relevant-stat clusters apply escalating pressure,
    stacking high stats hits diminishing returns, and complementary strengths
    give a small coverage bonus. Hidden from faculty. Floored at the start.
    """
    per = []
    wsum = sum(weights.values())
    for s in roster:
        st = STUDENT_STATS[s]
        val = sum(st[k] * w for k, w in weights.items()) / wsum  # 1..5
        per.append(val)
    m = sum(per) / len(per)
    base = 25 + (m - 1) / 4 * 70            # m=1->25, m=3->60, m=5->95
    if base > 65:                           # diminishing returns on the high side
        base = 65 + (base - 65) * 0.6
    lows = sum(1 for v in per if v <= 2.25)  # clustered weakness
    penalty = (lows - 1) * 6 if lows >= 2 else 0
    highs = sum(1 for v in per if v >= 4.2)  # complementary strengths (capped)
    bonus = min(highs, 3) * 2
    score = base - penalty + bonus
    return max(RESOURCE_FLOOR, min(100, round(score)))


def _geomean(scores):
    product = 1.0
    for name in RESOURCE_NAMES:
        product *= max(1, scores[name])
    return round(product ** (1.0 / len(RESOURCE_NAMES)))


def compute_scores(roster):
    """Return (scores, statuses, overall) for a 10-student roster."""
    scores = {name: _resource_from_roster(roster, RESOURCE_WEIGHTS[name])
              for name in RESOURCE_NAMES}
    statuses = {name: resource_status(scores[name]) for name in RESOURCE_NAMES}
    overall = _geomean(scores)
    return scores, statuses, overall


def recompute(team):
    """Recompute statuses + overall from the team's current resources."""
    res = team["resources"]
    team["statuses"] = {name: resource_status(res[name]) for name in RESOURCE_NAMES}
    team["overall"] = _geomean(res)


def apply_deltas(team, deltas):
    """Apply a {resource: delta} dict to current resources, clamp 0-100."""
    res = team["resources"]
    for name, delta in deltas.items():
        res[name] = max(0, min(100, res[name] + delta))
    recompute(team)


def roster_interpretation(roster):
    """One short, non-blaming roster-pattern sentence (never a per-student penalty)."""
    avg = {}
    for k in STAT_KEYS:
        avg[k] = sum(STUDENT_STATS[s][k] for s in roster) / len(roster)
    weakest = min(STAT_KEYS, key=lambda k: avg[k])
    lines = {
        "readiness": "This group includes several students whose preparation varies, so early foundational content and worked examples may matter more than usual.",
        "motivation": "This group includes several students whose drive may dip, so visible progress, encouragement, and early wins may help this particular class.",
        "communication": "This group includes several students who may not naturally volunteer information, so proactive, structured check-ins may serve this class well.",
        "self_management": "This group includes several students who may benefit from clear structure, predictable routines, and reminders rather than open-ended deadlines.",
        "adaptability": "This group includes several students who may need early notice and a little extra support whenever expectations or approaches change.",
        "help_seeking": "This group contains several students who may benefit from proactive faculty contact rather than initiating support themselves.",
    }
    return lines[weakest]


STARTING_PREAMBLE = (
    "Your roster does not determine whether your course succeeds. It does affect "
    "what kinds of systems, communication, flexibility, and support this particular "
    "group may need from the course."
)

# ---------------------------------------------------------------------------
# Course Cards (10 unique courses, two physical copies each). Straight-faced.
# Course Realities are neutral structural facts; they do NOT change starting
# scores, but may change how later actions read. They are not announced as
# "the one that matters" until consequence or evidence reveals it.
# ---------------------------------------------------------------------------
COURSES = [
    {"name": "Advanced Competitive Napping",
     "code": "WELL 142", "credits": 3, "modality": "Lecture (on campus)",
     "desc": "A performance-oriented study of restorative rest as a competitive discipline, covering sleep staging, recovery theory, and timed napping technique.",
     "pattern": "Two 75-minute meetings per week; weekly lab logs.",
     "assessment": "Weekly logs, two skills demonstrations, a final timed event.",
     "prereq": "None.",
     "realities": [
         "Attendance-dependent: much of the graded practice happens live in the room.",
         "Skills demonstrations are scheduled on fixed dates that are difficult to move."]},
    {"name": "Bigfoot Field Research",
     "code": "BIOL 215", "credits": 4, "modality": "CTE / hands-on (field + lab)",
     "desc": "Field methodology for documenting elusive large mammals, including transect design, evidence chain-of-custody, and observational reliability.",
     "pattern": "One lecture plus one 3-hour field lab per week.",
     "assessment": "Field notebooks, two lab practicals, a research poster.",
     "prereq": "Recommended: an introductory lab science.",
     "realities": [
         "Field labs run rain or shine on a fixed weekly window.",
         "Equipment is shared, so late-arriving students need an onboarding slot."]},
    {"name": "Professional Excuse Engineering",
     "code": "COMM 108", "credits": 3, "modality": "Lecture (on campus)",
     "desc": "A rhetoric course examining the structure, credibility, and ethics of explanation and justification in professional settings.",
     "pattern": "Two 75-minute meetings per week; discussion-based.",
     "assessment": "Discussion posts, two papers, a final portfolio.",
     "prereq": "Composition I.",
     "realities": [
         "Heavily writing- and feedback-dependent; grading load is front-loaded.",
         "Participation credit assumes students are present in discussion."]},
    {"name": "Advanced Drama Management",
     "code": "THEA 230", "credits": 3, "modality": "Lecture (on campus)",
     "desc": "Analysis and de-escalation of interpersonal conflict dynamics, with applied scenario work and reflective practice.",
     "pattern": "Two 75-minute meetings per week; scenario workshops.",
     "assessment": "Scenario performances, peer feedback, a reflective journal.",
     "prereq": "None.",
     "realities": [
         "Much of the learning is collaborative and peer-dependent.",
         "Scenario work assumes stable groups formed early in the term."]},
    {"name": "Internet Rabbit Hole Research",
     "code": "INFO 120", "credits": 3, "modality": "Asynchronous online",
     "desc": "Information literacy for deep, self-directed inquiry: source evaluation, search strategy, and avoiding unproductive tangents.",
     "pattern": "Fully online; weekly modules with no fixed meeting time.",
     "assessment": "Weekly module quizzes, an annotated source set, a final report.",
     "prereq": "None.",
     "realities": [
         "No fixed meetings, so start-here structure and navigation carry the course.",
         "Self-paced modules reward students who self-manage and punish those who drift."]},
    {"name": "Paranormal Evidence Analysis",
     "code": "FSCI 240", "credits": 4, "modality": "Asynchronous online",
     "desc": "Applied analytical methods for evaluating ambiguous evidence, emphasizing standards of proof, bias, and justification.",
     "pattern": "Fully online; modules plus two proctored analytical exams.",
     "assessment": "Module assignments and two major analytical assessments.",
     "prereq": "Recommended: a statistics or logic course.",
     "realities": [
         "Major assessments reward higher-order justification, not just identification.",
         "Online delivery means practice activities must carry the alignment work."]},
    {"name": "Industrial Snack Automation",
     "code": "MFGT 165", "credits": 4, "modality": "CTE / hands-on (shop)",
     "desc": "Design, operation, and troubleshooting of automated small-batch food production lines, with a safety and quality emphasis.",
     "pattern": "One lecture plus one 3-hour shop lab per week.",
     "assessment": "Shop competencies, safety checkoff, a line-build project.",
     "prereq": "Shop safety orientation.",
     "realities": [
         "Competency checkoffs are sequential; later skills require earlier ones.",
         "Lab stations are limited, so onboarding a late student takes a scheduled slot."]},
    {"name": "Cryptid Habitat Construction",
     "code": "CONS 180", "credits": 4, "modality": "CTE / hands-on (shop)",
     "desc": "Applied construction techniques for specialized enclosures, covering materials, load, tolerances, and site planning.",
     "pattern": "One lecture plus one 3-hour build lab per week.",
     "assessment": "Build competencies, a materials estimate, a capstone enclosure.",
     "prereq": "None.",
     "realities": [
         "The capstone depends on foundational competencies completed in order.",
         "Build teams are formed early and are disrupted by late changes."]},
    {"name": "Duct Tape Structural Engineering",
     "code": "ENGR 155", "credits": 3, "modality": "CTE / hands-on (lab)",
     "desc": "Rapid-prototyping engineering using adhesive-tensioned structures, emphasizing iteration, testing, and failure analysis.",
     "pattern": "One lecture plus one 2-hour lab per week.",
     "assessment": "Iterative build logs, two load tests, a design review.",
     "prereq": "Recommended: an introductory design or math course.",
     "realities": [
         "Grades reward documented iteration, not just a working final build.",
         "Load tests happen on fixed dates with shared equipment."]},
    {"name": "Applied Apocalypse Preparation",
     "code": "EMGT 110", "credits": 3, "modality": "Lecture (on campus)",
     "desc": "Planning and logistics for large-scale disruption, covering risk assessment, prioritization, and resource allocation under constraint.",
     "pattern": "Two 75-minute meetings per week; case-based.",
     "assessment": "Case analyses, a tabletop exercise, a final plan.",
     "prereq": "None.",
     "realities": [
         "The course explicitly rewards prioritization under limited resources.",
         "The tabletop exercise assumes students have kept up with prior cases."]},
]
COURSE_NAMES = [c["name"] for c in COURSES]
COURSE_BY_NAME = {c["name"]: c for c in COURSES}


# ---------------------------------------------------------------------------
# Resource & tool reference (verified against nwicc.edu, Oct 2026).
# Two categories faculty must learn to tell apart:
#   COURSE TOOL    - things faculty do INSIDE the course (Canvas) to manage the
#                    learning environment, see concerns, communicate, and assess.
#   STUDENT SUPPORT- real NCC services faculty CONNECT a student with when the
#                    evidence shows the student would benefit from outside help.
# Each card carries the need type(s) it addresses so the game can teach faculty to
# distinguish a support need from a course-design, communication, advising, or
# accessibility need. NCC-specific facts (e.g. Brainfuse 15 hrs/yr, the Panorama
# Accessibility tool name) are verified NCC information for this build.
# ---------------------------------------------------------------------------
# Need taxonomy (shown to faculty as plain labels).
NEED_LABELS = {
    "support": "Student support need",
    "course_design": "Course design / instruction need",
    "communication": "Communication need",
    "advising": "Advising / persistence concern",
    "accessibility": "Accessibility need",
}

TOOL = "COURSE TOOL"
SUPPORT = "STUDENT SUPPORT"

RESOURCES = [
    # ---- COURSE TOOLS (inside Canvas / the course) ------------------------
    {"name": "Dropout Detective", "category": TOOL,
     "needs": ["advising"],
     "what": "Early-alert system that helps faculty identify and document a student "
             "persistence concern and route it to the student's advisor.",
     "when": "A student shows a persistence-risk pattern (disengagement, missed work, "
             "stopped logging in) that advising should know about. It is NOT tutoring.",
     "how": "Canvas left navigation → Dropout Detective → select the student → "
            "Notes → write a note to the advisor → activate the alert. The note is "
            "instructor-to-advisor communication, not a student-facing message.",
     "limits": "Flags and documents concern and activates an advisor; it does not itself "
               "teach, tutor, or fix the course. One alert per genuinely at-risk student — "
               "not a substitute for a class-wide fix."},
    {"name": "Canvas Grades / Entering Zeros", "category": TOOL,
     "needs": ["course_design"],
     "what": "The gradebook. Entering zeros for missing work (instead of blank cells) so "
             "grades reflect reality.",
     "when": "Early, and at end of term, so students see the true impact of missing work and "
             "the gradebook is accurate.",
     "how": "Gradebook → enter 0 for missing submissions; record last date of attendance "
            "where required.",
     "limits": "Accuracy is not outreach — an accurate zero still needs a message if you "
               "want the student to act."},
    {"name": "Message Students Who…", "category": TOOL,
     "needs": ["communication"],
     "what": "Send a targeted message from the gradebook to a filtered group (e.g. students "
             "who have not submitted an item).",
     "when": "Fast, targeted outreach without emailing the whole class.",
     "how": "Gradebook → assignment column menu → Message Students Who… → pick "
            "the condition.",
     "limits": "Relies on student follow-through; it is a nudge, not a guarantee."},
    {"name": "Announcements / Scheduled Announcements", "category": TOOL,
     "needs": ["communication"],
     "what": "Class announcements, which can be written ahead and set to post on chosen dates.",
     "when": "Whole-class communication; spreading communication load and keeping timing "
             "consistent.",
     "how": "Announcements → compose → (optionally) set a future post date.",
     "limits": "Whole-class by design — not the tool for one student's individual issue."},
    {"name": "Canvas Modules / Start Here", "category": TOOL,
     "needs": ["course_design"],
     "what": "Content organized into modules, with requirements/prerequisites and a clear "
             "Start Here path.",
     "when": "Creating a sensible order through content and onboarding late-adds.",
     "how": "Modules → build a Start Here module; set requirements/prerequisites.",
     "limits": "Structure helps students who engage; it does not by itself reach a student who "
               "has stopped showing up."},
    {"name": "Rubrics", "category": TOOL,
     "needs": ["course_design"],
     "what": "A criteria-based rubric attached to an assignment so expectations and scoring "
             "are explicit.",
     "when": "Students are unclear what \"good\" looks like, or grading needs to be consistent "
             "and defensible.",
     "how": "Assignment → + Rubric → define criteria and point values.",
     "limits": "Clarifies expectations; does not close a skill gap on its own."},
    {"name": "SpeedGrader / Reusable Feedback", "category": TOOL,
     "needs": ["course_design", "communication"],
     "what": "Grade in one place and reuse common feedback (comment library) across "
             "submissions.",
     "when": "Many students share the same issue; cutting grading time.",
     "how": "SpeedGrader → comment → save to / reuse from the comment library.",
     "limits": "Feedback only helps if students can find it — point them to where it lives."},
    {"name": "Course Analytics / Access Report", "category": TOOL,
     "needs": ["advising", "course_design"],
     "what": "Activity and access data showing who is engaging with the course and content.",
     "when": "Investigating whether a problem is engagement vs. a shared design gap.",
     "how": "Course Analytics / a student's Access Report in People.",
     "limits": "Shows what happened, not why — pair it with the student's actual work."},
    {"name": "Panorama Accessibility", "category": TOOL,
     "needs": ["accessibility", "course_design"],
     "what": "Checks course documents, pages, and media for accessibility (headings, alt text, "
             "contrast, captions) and helps remediate them.",
     "when": "Making sure all students have equitable access to the materials.",
     "how": "Open the Panorama accessibility report in the course and work through flagged items.",
     "limits": "Addresses material access; a documented individual need still routes to "
               "Accessibility / Accommodation Services (a STUDENT SUPPORT)."},
    {"name": "Simple Syllabus", "category": TOOL,
     "needs": ["course_design"],
     "what": "A consistent, up-to-date syllabus of record.",
     "when": "Keeping the syllabus and the live course (due dates, schedule) in agreement — "
             "the CP1 syllabus-vs-Canvas mismatch is exactly this.",
     "how": "Maintain the syllabus of record; reconcile Canvas due dates to it.",
     "limits": "Fixes the course inconsistency, not a student who already acted on the wrong date."},
    {"name": "Grade Publish", "category": TOOL,
     "needs": ["course_design"],
     "what": "Posting/publishing final grades through the approved end-of-term process.",
     "when": "The Final Stretch; confirm accuracy first.",
     "how": "Follow the approved Grade Publish process at end of term.",
     "limits": "Final and visible — verify accuracy before publishing."},
    {"name": "Course Improvement Survey", "category": TOOL,
     "needs": ["course_design"],
     "what": "End-of-term student feedback on the course.",
     "when": "The Final Stretch, as part of finishing accurately and improving next term.",
     "how": "Deploy the Course Improvement Survey near end of term.",
     "limits": "Informs future terms, not the current cohort."},

    # ---- STUDENT SUPPORTS (real NCC services) -----------------------------
    {"name": "Learning Center (Student Success Center)", "category": SUPPORT,
     "needs": ["support"],
     "what": "NCC's academic-support center in Building A: free tutoring (drop-in, one-to-one, "
             "or small group) in most program areas, plus a computer lab, study rooms, and "
             "testing/proctoring.",
     "when": "A student needs academic help with course content. A natural first option for "
             "on-campus students — and online students may use it too.",
     "how": "Refer the student to the Learning Center in Building A; 712-324-5061 / "
            "learncenter@nwicc.edu. No appointment required for drop-in tutoring.",
     "limits": "In-person location (Building A); best fit when a student can get to campus. "
               "Tutoring supports the student — it does not fix an unclear assignment or a "
               "course-design gap.",
     "modality_note": "Especially natural for on-campus courses; still open to online students."},
    {"name": "Brainfuse Online Tutoring", "category": SUPPORT,
     "needs": ["support"],
     "what": "Free online tutoring NCC students can reach any time through the sidebar link in "
             "any Canvas course.",
     "when": "A student needs academic help and in-person hours or location are a barrier — "
             "especially accessible for online students, but available to others as well.",
     "how": "Click the Brainfuse link in the sidebar of any Canvas course. No appointment needed.",
     "limits": "NCC students receive 15 hours of Brainfuse tutoring per year — it is not "
               "unlimited; it supplements, not replaces, course instruction.",
     "modality_note": "Especially accessible for online students; available to others where "
                      "appropriate."},
    {"name": "TRiO Student Support Services", "category": SUPPORT,
     "needs": ["advising", "support"],
     "what": "A federally funded program offering eligible students academic advising, career "
             "and personal counseling, financial-aid advising, professional and peer tutoring by "
             "appointment, transfer assistance, and Lunch & Learn workshops — all free.",
     "when": "A student with broader persistence/support needs who is ELIGIBLE would benefit from "
             "wraparound support, not just one tutoring session.",
     "how": "Encourage an eligible student to apply (TRiO application on NCC's Academic Support "
            "page). Services are free to those who qualify.",
     "limits": "Eligibility required — a student must be a first-generation college student, "
               "meet federal income guidelines, OR have a documented disability, and be a U.S. "
               "citizen/resident. The program serves a limited number of students. Not every "
               "student qualifies — do not assume universal access.",
     "modality_note": "Eligibility- and application-based, independent of course modality."},
    {"name": "Accessibility / Accommodation Services", "category": SUPPORT,
     "needs": ["accessibility"],
     "what": "NCC's coordination of accommodations and support for students with a documented "
             "disability.",
     "when": "The evidence points to an access need (not an effort or content-help need).",
     "how": "Direct the student to apply for accommodations (Application for Accommodation for "
            "Disability and records-release paperwork) through NCC's accommodation process.",
     "limits": "Requires documentation and the student's request; accommodations are arranged "
               "through the process, not granted ad hoc in the course."},
    {"name": "Academic Advising", "category": SUPPORT,
     "needs": ["advising"],
     "what": "Each student's assigned advisor, who helps with persistence, course selection, "
             "staying on track, and connecting to NCC resources. Advising is required each "
             "semester.",
     "when": "A persistence/path concern bigger than one assignment — and the destination of "
             "a Dropout Detective alert.",
     "how": "Loop in the student's advisor (directly or via a Dropout Detective note/alert).",
     "limits": "Advisors guide persistence and pathway — they are not the content tutor for "
               "your course."},
    {"name": "NCC Library", "category": SUPPORT,
     "needs": ["support", "course_design"],
     "what": "The NCC Library, which supports all of the college's programs with research help "
             "and resources.",
     "when": "A student (or an assignment) needs research, source, or information-literacy help.",
     "how": "Point students to the Library's research help and resources.",
     "limits": "Research/resource support, not general subject tutoring."},
]
RESOURCES_BY_NAME = {r["name"]: r for r in RESOURCES}
COURSE_TOOLS = [r for r in RESOURCES if r["category"] == TOOL]
STUDENT_SUPPORTS = [r for r in RESOURCES if r["category"] == SUPPORT]


def _resource_image_map():
    """Map each resource NAME to its committed artwork under static/art/, but only
    when the file actually exists (so a resource with no accurate artwork simply
    shows none). Built once at import."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "art")
    out = {}
    for r in RESOURCES:
        pre = "tool" if r["category"] == TOOL else "support"
        folder = "tools" if pre == "tool" else "supports"
        slug = "_".join("".join(c if c.isalnum() else " " for c in r["name"].lower()).split())
        fn = pre + "_" + slug + ".png"
        if os.path.exists(os.path.join(base, folder, fn)):
            out[r["name"]] = "/static/art/" + folder + "/" + fn
    return out


RESOURCE_IMAGE = _resource_image_map()


def modality_class(modality):
    """Collapse a course's modality string into one of: online, campus, cte."""
    m = (modality or "").lower()
    if "online" in m:
        return "online"
    if "cte" in m or "hands-on" in m or "shop" in m or "lab" in m or "field" in m:
        return "cte"
    return "campus"


def support_context_note(team):
    """Modality-aware guidance for the team's own course. Thin wrapper over
    support_context_note_for()."""
    return support_context_note_for(
        modality_class(COURSE_BY_NAME.get(team.get("course"), {}).get("modality")))


def support_context_note_for(mc):
    """Modality-aware guidance for choosing a STUDENT SUPPORT, for a given
    modality_class ('online' / 'cte' / 'campus'). Deliberately NOT a lookup
    table: it frames which options are most accessible while keeping several
    valid, and reminds faculty to match the need before referring anyone. Reused
    by the shell Resources tab and the workspace Resources drawer (same text)."""
    if mc == "online":
        return ("For this asynchronous online course, <strong>Brainfuse online tutoring</strong> "
                "(sidebar link in Canvas) is usually the most accessible tutoring option — "
                "remember the 15-hours-per-year limit. The <strong>Learning Center (Student Success "
                "Center)</strong> is still open to online students, and <strong>TRiO</strong> may "
                "fit an eligible student. First confirm the need is academic support, not an unclear "
                "assignment.")
    if mc == "cte":
        return ("This is a hands-on / CTE course, so tutoring is not automatically the answer. Check "
                "the evidence first: a competency gap may call for additional guided practice, "
                "clarified directions, or an accessibility accommodation rather than tutoring. If "
                "academic support is warranted, the <strong>Learning Center (Student Success Center)</strong> "
                "and <strong>Brainfuse</strong> (15 hrs/yr) are both available; <strong>TRiO</strong> "
                "may fit an eligible student with broader needs.")
    return ("For this on-campus course, the <strong>Learning Center (Student Success Center)</strong> "
            "in Building A is a natural in-person option; <strong>Brainfuse online tutoring</strong> "
            "(15 hrs/yr) is available too, and <strong>TRiO</strong> may fit an eligible student. "
            "First confirm the need is academic support rather than a course-design or communication "
            "issue.")


# ---------------------------------------------------------------------------
# Checkpoints.
# ---------------------------------------------------------------------------
CHECKPOINTS = [
    "First Week",
    "Early Semester",
    "First Major Assessment",
    "Midterm",
    "Final Stretch",
]
# Full private player check (mission claim + suspicion vote) happens ONLY after
# CP1, CP3, CP5 (indices 0, 2, 4). CP2 has no private round. CP4 (index 3) has a
# brief, privacy-preserving pass-the-device round whose ONLY purpose is the
# Thunderflunked's Emergency Sabotage opportunity (no mission, no suspicion vote).
HAS_PRIVATE = {0, 2, 4}
# Emergency Sabotage is available during CP3 (index 2, inside the full private
# round) and CP4 (index 3, inside the brief round), offered in the Thunderflunked's
# own private turn. It is one-time and expires after CP4.
SABOTAGE_WINDOW = {2, 3}


# GM pacing plan: cumulative target minute-marks per phase for the locked
# private-round schedule (full private after CP1/CP3/CP5; CP2 none; CP4 brief
# sabotage-only) targeting ~60 min. Used only for the GM's on-screen pacing
# panel; it does not affect gameplay.
PACING = [
    ("setup", "Setup / roles / course selection / student placement", 0, 12),
    ("cp1", "Checkpoint 1: First Week (+ full private)", 12, 21),
    ("cp2", "Checkpoint 2: Early Semester (no private)", 21, 28),
    ("cp3", "Checkpoint 3: First Major Assessment (+ full private)", 28, 39),
    ("cp4", "Checkpoint 4: Midterm (brief sabotage only)", 39, 47),
    ("cp5", "Checkpoint 5: Final Stretch (+ full private)", 47, 57),
    ("final", "Final accusation / reveal / debrief", 57, 64),
]


def current_phase_key(game):
    if game.get("final_open"):
        return "final"
    idx = game["checkpoint_index"]
    if idx == 0 and not game["opened_once"]:
        return "setup"
    return "cp%d" % (idx + 1)


# ---------------------------------------------------------------------------
# Live-update signatures + per-team status (used by the GM Command Center, team
# waiting screens, and the projector). Polling only -- the global state lock
# serializes requests, so a long-lived SSE stream would block the whole app.
# None of these expose secret allegiance, private votes, mission, or sabotage
# content; they report only public-appropriate phase/progress information.
# ---------------------------------------------------------------------------
def game_rev(game):
    """A compact signature of the game-wide phase. Changes exactly when the GM
    advances the room (checkpoint open/lock/advance/final, course phase,
    reassignment). The GM dashboard reloads on change; otherwise it patches
    cards in place."""
    return "|".join(str(p) for p in [
        game.get("checkpoint_index", 0), int(bool(game.get("checkpoint_open"))),
        int(bool(game.get("opened_once"))), int(bool(game.get("final_open"))),
        game.get("course_trading", "closed"), int(bool(game.get("reassignment_done"))),
        len(game.get("teams", [])),
        # Teaching-moment stage of the current checkpoint: changes make the
        # projector and team screens reload (into the discussion/strategies view).
        str(game.get("teaching", {}).get(str(game.get("checkpoint_index", 0))))])


def team_rev(game, i):
    """A signature of everything one team's WAITING screen depends on. A waiting
    team reloads when this changes (e.g. the GM opens a checkpoint, a trade
    arrives, reassignment distributes). Never includes private content."""
    t = game["teams"][i]
    offers = game.get("course_offers", [])
    n_off = sum(1 for o in offers if i in (o["frm"], o["to"]))
    return game_rev(game) + "#" + "|".join(str(p) for p in [
        t.get("course") or "", t.get("course_held") or "",
        int(bool(t.get("roster_confirmed"))), int(t.get("reassigned") is not None),
        int(bool(t.get("placement_acked"))), n_off,
        len(t.get("players", [])), int(bool(t.get("roles_confirmed"))),
        len(t.get("allegiance_seen", [])), int(bool(t.get("setup_overview_done")))])


def projector_rev(game):
    """Projector reloads on any phase change, pause toggle, completion progress,
    plus final-outcome changes."""
    prog = room_progress(game)
    base = game_rev(game) + "~" + str(int(bool(game.get("paused")))) + "~" + str(prog["done"])
    if game.get("final_open"):
        base += "#" + ",".join(str(t.get("accusation")) + ":" + str(t.get("overall"))
                               for t in game.get("teams", []))
    # A hallway-rumor announce/dismiss must reload the projector (full takeover and
    # its removal), so fold the rumor signature in here (not into game_rev).
    return base + "~" + rumors_sig(game)


def help_active(team):
    return bool(team.get("help", {}).get("active"))


# ---------------------------------------------------------------------------
# GM-guided teaching moments (Batch 4). Each checkpoint has an explicit stage
# the GM controls: None (not begun) -> "discussion" (question on projector) ->
# "revealed" (Canvas strategies on projector). A separate per-checkpoint
# "dismissed" flag lets the GM quiet the readiness prompt (Keep Waiting) without
# starting anything. All state is plain JSON (string keys) so it persists and
# restores. Nothing instructional appears automatically -- only the GM advances
# a stage.
# ---------------------------------------------------------------------------
def in_checkpoint(game):
    """True while the room is inside one of the five checkpoints (not setup,
    not the final)."""
    return bool(game.get("opened_once")) and not game.get("final_open")


def teaching_stage(game, idx):
    return game.get("teaching", {}).get(str(idx))        # None | "discussion" | "revealed"


def _set_teaching_stage(game, idx, stage):
    game.setdefault("teaching", {})[str(idx)] = stage


def teaching_dismissed(game, idx):
    return bool(game.get("teaching_dismissed", {}).get(str(idx)))


def teaching_available(game):
    """The readiness prompt is worth showing: we're in a checkpoint, discussion
    has not begun, and every team has finished the decision + any private round."""
    if not in_checkpoint(game):
        return False
    idx = game["checkpoint_index"]
    if teaching_stage(game, idx) is not None:
        return False
    return room_progress(game)["all_ready"]


def teaching_active(game):
    """Discussion has begun (question shown) and/or strategies revealed for the
    current checkpoint."""
    return in_checkpoint(game) and teaching_stage(game, game["checkpoint_index"]) in ("discussion", "revealed")


def teaching_revealed_current(game):
    return in_checkpoint(game) and teaching_stage(game, game["checkpoint_index"]) == "revealed"


def teach_sig(game):
    """A GM-dashboard reload trigger for teaching availability/stage/dismiss
    changes that game_rev does not capture (game_rev omits live readiness). The
    GM page reloads when this changes so the right facilitation controls appear."""
    idx = game.get("checkpoint_index", 0)
    return "|".join(str(p) for p in [
        idx, teaching_stage(game, idx), int(teaching_available(game)),
        int(teaching_dismissed(game, idx)), int(bool(game.get("final_open"))),
        int(bool(game.get("opened_once"))),
        # Fold in rumor state so the dashboard reloads when a team's sabotage queues
        # a rumor, or when the GM announces/dismisses one.
        rumors_sig(game)])


def _private_progress(game, team, idx):
    """How many players have finished the current private round (count only, no
    content). Full rounds count votes; the CP4 brief round counts passes."""
    rnd = team.get("private", {}).get(idx, {})
    done = len(rnd.get("votes", {})) if idx in HAS_PRIVATE else len(rnd.get("passed", []))
    total = len(team.get("players", []))
    return done, total


def team_live_status(game, i, team):
    """Public-appropriate 'what is this team doing right now' summary for the GM
    Command Center card and the live payload. Returns phase/activity/state/
    progress/course/overall. `state` is working | waiting | finished."""
    teams = game["teams"]
    course = team.get("course") or team.get("course_held") or ""
    overall = team.get("overall")
    idx = game.get("checkpoint_index", 0)

    def d(phase, activity, state, progress=""):
        return {"phase": phase, "activity": activity, "state": state,
                "progress": progress, "course": course, "overall": overall}

    if game.get("final_open"):
        if team.get("outcome"):
            return d("Final", "Finished", "finished")
        if team.get("accusation") is not None:
            return d("Final", "Accusation in — awaiting outcome", "waiting")
        return d("Final", "Making the final accusation", "working")

    # Setup (before any checkpoint has been opened).
    if idx == 0 and not game.get("opened_once"):
        if not team.get("players"):
            return d("Setup", "Registering players", "working")
        if team.get("setup_version") == 2 and not team.get("roles_confirmed"):
            return d("Setup", "Assigning roles", "working")
        if (team.get("setup_version") == 2
                and len(team.get("allegiance_seen", [])) < len(team["players"])):
            n, tot = len(team.get("allegiance_seen", [])), len(team["players"])
            return d("Setup", "Private allegiance reveal", "working", "%d/%d viewed" % (n, tot))
        if team.get("setup_version") == 2 and not team.get("setup_overview_done"):
            return d("Setup", "Reviewing team overview", "working")
        if not team.get("course"):
            ph = game.get("course_trading", "closed")
            if ph == "not_open":
                return d("Course selection", "Waiting for course selection to open", "waiting")
            if ph == "open":
                return d("Course selection", "Reviewing / trading course", "working")
            return d("Course selection", "Finalizing course", "working")
        if team.get("deal_version") == 2:
            if not team.get("roster_confirmed"):
                return d("Placement", "Choosing 10 of 15 students", "working")
            if not team.get("reassigned"):
                return d("Placement", "Finalized — waiting for other teams", "waiting")
            if not team.get("placement_acked"):
                return d("Placement", "Reviewing the final class", "working")
        return d("Ready", "Set up — waiting for Checkpoint 1", "waiting")

    # Checkpoint phase.
    cpname = "Checkpoint %d" % (idx + 1)
    if game.get("checkpoint_open"):
        if not team.get("cp", {}).get(idx, {}).get("submitted"):
            return d(cpname, "Deciding the checkpoint", "working")
        # Submitted, but a required private round may still be in progress -- the
        # team is not finished yet (keeps the card consistent with completion).
        if _cp_has_private(idx) and not private_complete(team, idx):
            done, tot = _private_progress(game, team, idx)
            return d(cpname, "Private player check", "working", "%d/%d done" % (done, tot))
        return d(cpname, "Decision submitted — waiting", "waiting")
    if not game.get("opened_once"):
        return d(cpname, "Waiting for %s to open" % cpname, "waiting")
    if (idx in HAS_PRIVATE or idx == 3) and not private_complete(team, idx):
        done, tot = _private_progress(game, team, idx)
        return d(cpname, "Private player check in progress", "working", "%d/%d done" % (done, tot))
    return d(cpname, "Decisions locked — waiting for the next step", "waiting")


# ---------------------------------------------------------------------------
# Completion detection + room progress (Batch 3). These read existing state
# only; they invent no new mandatory step. "Done" means the team has finished
# everything required for the CURRENT GM-gated activity -- the signal the GM
# needs before opening/advancing the next checkpoint.
# ---------------------------------------------------------------------------
def _cp_has_private(idx):
    """CP1/CP3/CP5 run a full private round; CP4 runs the brief pass round;
    CP2 has none."""
    return idx in HAS_PRIVATE or idx == 3


def team_activity_done(game, i, team):
    """True when this team has completed the current GM-gated activity."""
    if game.get("final_open"):
        return team.get("accusation") is not None
    idx = game.get("checkpoint_index", 0)
    if idx == 0 and not game.get("opened_once"):
        # Setup / course / placement (before Checkpoint 1).
        ph = game.get("course_trading", "closed")
        if ph in ("not_open", "open"):
            # Course selection/trading has no mandatory action; a team is "ready"
            # for the GM to close once it is holding a dealt course.
            return bool(team.get("course_held"))
        # Course closed -> student placement; done when the final class is acked.
        return team_setup_complete(team)
    # Checkpoint phase.
    if not game.get("checkpoint_open"):
        # Between checkpoints (locked): nothing left to do unless this cp's
        # private round was left unfinished when it was locked.
        if _cp_has_private(idx) and not private_complete(team, idx):
            return False
        return True
    # Open checkpoint: decision submitted AND the private round finished (if any).
    rec = team.get("cp", {}).get(idx, {})
    if not rec.get("submitted"):
        return False
    if _cp_has_private(idx) and not private_complete(team, idx):
        return False
    return True


def current_activity_label(game):
    """Short public name for the current GM-gated activity."""
    if game.get("final_open"):
        return "Final: accusations & outcomes"
    idx = game.get("checkpoint_index", 0)
    if idx == 0 and not game.get("opened_once"):
        ph = game.get("course_trading", "closed")
        if ph == "not_open":
            return "Setup (course not open yet)"
        if ph == "open":
            return "Course selection & trading"
        return "Student placement"
    return "Checkpoint %d: %s" % (idx + 1, CHECKPOINTS[idx])


def next_gm_action(game, all_ready=False):
    """(label, note) for the single next GM step. Teaching-aware inside a
    checkpoint so the GM always knows what to do next. `all_ready` is passed in
    (room_progress computes it) to avoid recomputing / recursion."""
    if game.get("final_open"):
        return ("Game complete", "Teams are viewing their outcomes.")
    idx = game.get("checkpoint_index", 0)
    if idx == 0 and not game.get("opened_once"):
        ph = game.get("course_trading", "closed")
        if ph == "not_open":
            return ("Open course selection", "Deals each team a course and lets teams trade.")
        if ph == "open":
            return ("Close course selection", "Locks each team's course and starts student placement.")
        return ("Open Checkpoint 1", "Begins the first checkpoint when placement is done.")
    # Inside a checkpoint: the guided teaching sequence drives the next step.
    stage = teaching_stage(game, idx)
    if stage == "revealed":
        if idx < len(CHECKPOINTS) - 1:
            return ("Advance to Checkpoint %d" % (idx + 2), "The teaching moment is done — move the room on.")
        return ("Proceed to Final", "The teaching moment is done — open the final accusation.")
    if stage == "discussion":
        return ("Reveal Canvas strategies", "Shows the practical Canvas strategies on the projector.")
    if all_ready:
        return ("Begin the Checkpoint %d discussion" % (idx + 1),
                "Locks decisions and shows the discussion question on the projector.")
    if game.get("checkpoint_open"):
        return ("Teams are deciding", "The teaching moment unlocks when every team has finished.")
    return ("Begin the Checkpoint %d discussion" % (idx + 1),
            "Decisions are locked — start the discussion when you're ready.")


def room_progress(game):
    """Aggregate completion for the current activity. Secret-free."""
    teams = game.get("teams", [])
    done_flags = [team_activity_done(game, i, t) for i, t in enumerate(teams)]
    working = [t["name"] for t, f in zip(teams, done_flags) if not f]
    helpers = [t["name"] for t in teams if help_active(t)]
    done = sum(1 for f in done_flags if f)
    all_ready = bool(teams) and done == len(teams)
    label, note = next_gm_action(game, all_ready)
    return {"done": done, "total": len(teams), "working": working, "help": helpers,
            "all_ready": all_ready,
            "activity": current_activity_label(game),
            "next_label": label, "next_note": note,
            "paused": bool(game.get("paused"))}


def projector_instruction(game):
    """Large, public participant instruction for the projector. Never reveals a
    correct decision, a scoring consequence, private information, or a Canvas
    teaching takeaway -- only what participants should do right now."""
    if game.get("final_open"):
        return ""
    idx = game.get("checkpoint_index", 0)
    if idx == 0 and not game.get("opened_once"):
        ph = game.get("course_trading", "closed")
        if ph == "not_open":
            return "Your course has been dealt. Review it on your team screen while we get started."
        if ph == "open":
            return ("On your team screen, review your course and propose a one-for-one trade if you want "
                    "one. Both teams must agree.")
        return "On your team screen, keep 10 of your 15 students, then confirm."
    if game.get("checkpoint_open"):
        return "On your team screen, read the situation and make your team's decision."
    return "This checkpoint is closed. Please wait for the Game Master."


def make_game_code(length=5):
    return "".join(random.choice(CODE_CHARACTERS) for _ in range(length))


def get_team(game_code, team_index):
    if game_code not in games:
        return None
    teams = games[game_code]["teams"]
    if team_index < 0 or team_index >= len(teams):
        return None
    return teams[team_index]


def find_team_index(game_code, team_name):
    if game_code not in games:
        return None
    target = team_name.strip().lower()
    for i, team in enumerate(games[game_code]["teams"]):
        if team["name"].strip().lower() == target:
            return i
    return None


# ===========================================================================
# Shared page layout (CSS, accessibility, NCC brand). No JS, no external assets.
# ===========================================================================
NCC_RED = "#A4343A"
NCC_GRAY = "#676767"

BASE_CSS = """
:root{--red:#A4343A;--gray:#676767;--ink:#1f2224;--bg:#f4f1ec;--card:#ffffff;
      --line:#d9d4cc;--good:#2e7d4f;--warn:#b26a00;--bad:#9e2b25;}
*{box-sizing:border-box;}
/* Base readability: 17px root so rem-based sizing scales up app-wide. */
html{font-size:17px;}
/* Images never force horizontal overflow on narrow screens. */
img{max-width:100%;height:auto;}
body{margin:0;font-family:"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
     color:var(--ink);background:var(--bg);line-height:1.55;font-size:1rem;}
/* Content width tiers. Default stays a readable measure; 'wide' is for
   card/working screens that need desktop space. GM and projector get their own
   wider frames (set below) regardless of the per-screen width. */
.wrap{max-width:1040px;margin:0 auto;padding:22px 24px;}
.wrap.wide{max-width:1380px;}
header.bar{background:var(--red);color:#fff;padding:16px 24px;}
header.bar .wrap{padding:0;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}
header.bar h1{font-size:1.4rem;margin:0;}
header.bar .tag{font-size:.95rem;text-transform:uppercase;letter-spacing:.08em;opacity:.92;}
h1,h2,h3,h4{color:var(--ink);line-height:1.25;}
h1{font-size:1.95rem;margin:.2em 0 .5em;}
h2{font-size:1.5rem;border-bottom:3px solid var(--red);padding-bottom:5px;margin-top:1.3em;}
h3{font-size:1.22rem;}
h4{font-size:1.06rem;}
a{color:var(--red);}
/* Comfortable interactive controls (labels/buttons ~18-20px). */
input[type=text],input[type=number],textarea,select{font-size:1.02rem;padding:9px 11px;
  border:1px solid var(--line);border-radius:6px;max-width:100%;}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px 18px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.04);}
.evidence{border-left:5px solid var(--gray);}
.notice{background:#fff7e6;border:1px solid #f0d9a6;border-radius:8px;padding:14px 16px;margin:14px 0;font-size:1.05rem;}
.btn,button{background:var(--red);color:#fff;border:0;border-radius:8px;
      padding:12px 22px;font-size:1.1rem;cursor:pointer;text-decoration:none;display:inline-block;line-height:1.2;}
.btn.secondary,button.secondary{background:#fff;color:var(--red);border:2px solid var(--red);}
button:focus,a:focus,input:focus,select:focus{outline:3px solid #000;outline-offset:2px;}
label{display:block;margin:.5em 0;font-size:1.02rem;}
.choice{border:1px solid var(--line);border-radius:8px;padding:13px 15px;margin:10px 0;background:#fff;font-size:1.04rem;}
.choice small{color:var(--gray);display:block;margin-top:5px;font-size:.95rem;}
/* GM runs on a desktop; projector is read across a room -- give each more room. */
body.gm .wrap{max-width:1440px;}
/* Small-screen guard: tighten chrome so the header never overflows. */
@media(max-width:560px){
  .wrap{padding:16px 15px;}
  header.bar{padding:14px 16px;}
  header.bar h1{font-size:1.15rem;}
  h1{font-size:1.6rem;} h2{font-size:1.3rem;}
}
.meter{margin:10px 0;}
.meter .top{display:flex;justify-content:space-between;font-weight:600;}
.meter .track{background:#e6e1d8;border-radius:6px;height:16px;overflow:hidden;border:1px solid var(--line);}
.meter .fill{height:100%;}
.status-Normal{color:var(--good);} .status-Strained{color:var(--warn);} .status-Critical{color:var(--bad);}
.fill-Normal{background:var(--good);} .fill-Strained{background:var(--warn);} .fill-Critical{background:var(--bad);}
table{border-collapse:collapse;width:100%;margin:10px 0;}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;}
th{background:#efe9e0;}
.err{color:#fff;background:var(--bad);padding:10px 12px;border-radius:8px;}
.muted{color:var(--gray);}
.pill{display:inline-block;background:#efe9e0;border-radius:20px;padding:3px 12px;font-size:.95rem;}
.resource{border-left:5px solid var(--line);}
.resource.support{border-left-color:var(--red);}
.resource.tool{border-left-color:var(--gray);}
.resource h3{margin-top:0;}
details.card summary{cursor:pointer;font-size:1rem;}
details.card[open] summary{margin-bottom:8px;}
.private{background:#2a2d31;color:#fff;}
.private .wrap{min-height:40vh;}
.private h1,.private h2,.private h3{color:#fff;}
.private .card{background:#3a3f45;border-color:#555;color:#fff;}
.big{font-size:1.55rem;}
.center{text-align:center;}
.footer{margin-top:26px;font-size:1rem;}
ul.clean{list-style:none;padding-left:0;}
ul.clean li{padding:4px 0;border-bottom:1px solid var(--line);}
.tilegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:12px 0;}
.tile{position:relative;border:2px solid var(--line);border-radius:10px;padding:12px 12px 10px;background:#fff;cursor:pointer;display:block;}
.tile .num{font-weight:700;font-size:1.05rem;color:var(--red);}
.tile .nm{display:block;font-weight:600;margin-top:2px;}
.tile .ctx{display:block;color:var(--gray);font-size:.78rem;margin-top:6px;}
.tile input{position:absolute;opacity:0;width:1px;height:1px;}
.tile .check{position:absolute;top:8px;right:10px;font-size:.7rem;font-weight:700;color:#fff;background:var(--red);border-radius:10px;padding:1px 7px;opacity:0;}
.tile.sel,.tile:has(input:checked){border-color:var(--red);background:#faecec;box-shadow:0 0 0 2px var(--red) inset;}
.tile.sel .check,.tile:has(input:checked) .check{opacity:1;}
.tile:has(input:focus){outline:3px solid #000;outline-offset:2px;}
.selbar{position:sticky;top:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:10px 0;z-index:5;}
.selbar.ok{border-color:var(--good);}
#selcount{font-weight:700;}
#selcount.count-ok{color:var(--good);} #selcount.count-no{color:var(--warn);}
.selbar label{display:inline;}
.selbar input[type=text]{width:220px;max-width:60vw;}
/* Artwork-based Student Card tiles -- large and readable, 3-up on desktop,
   2-up on small laptops, 1-up on narrow. The card art carries the name + all six
   stats, so no stat table is repeated beside it. */
.cardgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin:16px 0;}
@media(max-width:1100px){.cardgrid{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media(max-width:620px){.cardgrid{grid-template-columns:minmax(0,1fr);}}
.student-tile{position:relative;border:2px solid var(--line);border-radius:12px;padding:8px;background:#fff;min-width:0;}
.sel-cb{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden;clip:rect(0 0 0 0);}
.sel-face{display:block;cursor:pointer;border-radius:8px;margin:0;}
.sel-face img,.scard img{width:100%;height:auto;display:block;border-radius:8px;}
.sel-face img{transition:opacity .12s;opacity:.9;}
.student-tile:has(.sel-cb:checked){border-color:var(--red);box-shadow:0 0 0 3px var(--red) inset;background:#faecec;}
.student-tile:has(.sel-cb:checked) .sel-face img{opacity:1;}
.sel-cb:focus-visible+.sel-face{outline:3px solid #000;outline-offset:2px;}
.student-tile .check{position:absolute;top:16px;left:16px;font-size:1rem;font-weight:700;color:#fff;
  background:var(--red);border-radius:10px;padding:5px 13px;opacity:0;pointer-events:none;z-index:2;}
.student-tile:has(.sel-cb:checked) .check{opacity:1;}
.enlarge-btn{position:absolute;top:16px;right:16px;z-index:3;background:rgba(255,255,255,.96);
  border:1px solid var(--line);border-radius:8px;padding:6px 13px;font-size:.98rem;font-weight:600;
  color:var(--ink);cursor:zoom-in;}
.zoom-cb:focus-visible+.enlarge-btn{outline:3px solid #000;outline-offset:2px;}
.scard{position:relative;border:1px solid var(--line);border-radius:10px;padding:6px;background:#fff;}
.scard-badge{position:absolute;top:10px;left:10px;background:var(--red);color:#fff;font-weight:700;}
/* Persistent selection counter for the placement screen */
.selcount-bar{position:sticky;top:0;z-index:5;background:var(--card);border:2px solid var(--line);
  border-radius:10px;padding:12px 16px;margin:12px 0;font-size:1.15rem;font-weight:700;
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
.selcount-bar.ok{border-color:var(--good);background:#eef7f1;}
.selcount-bar .n{color:var(--warn);}
.selcount-bar.ok .n{color:var(--good);}
/* Course Cards + trading (fully in-app) */
/* Your-course hero: large readable artwork beside full HTML course details */
.course-hero{display:flex;gap:22px;flex-wrap:wrap;align-items:flex-start;}
.course-hero-art{flex:0 0 auto;max-width:100%;}
.course-hero-text{flex:1 1 320px;min-width:0;}
.course-name{margin-top:0;}
.course-meta{color:var(--gray);font-weight:600;margin:.2em 0 .7em;}
.course-realities{margin:.3em 0 0;padding-left:1.2em;}
.course-realities li{margin:.25em 0;}
.trade-row{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;border:1px solid var(--line);
  border-radius:10px;padding:14px;margin:12px 0;background:#fff;}
.trade-art{flex:0 0 auto;max-width:100%;}
.trade-row .tr-body{flex:1 1 280px;min-width:0;}
.trade-row form{display:inline;}
.offer-in{border-left:5px solid var(--good);}
.offer-wait{border-left:5px solid var(--warn);}
/* Click/tap-to-enlarge Course Card (pure CSS; keyboard accessible, no JS needed) */
.zoom{display:inline-block;max-width:100%;}
.zoom-cb{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden;clip:rect(0 0 0 0);}
.zoom-thumb{display:inline-block;cursor:zoom-in;margin:0;text-align:center;}
.zoom-thumb img{width:100%;height:auto;display:block;border-radius:10px;border:1px solid var(--line);}
.zoom-lg .zoom-thumb img{max-width:min(440px,100%);}
.zoom-md .zoom-thumb img{max-width:min(260px,100%);}
.zoom-hint{display:block;font-size:.95rem;color:var(--gray);margin-top:6px;}
.zoom-cb:focus-visible+.zoom-thumb{outline:3px solid #000;outline-offset:2px;}
.zoom-cb:focus-visible+.zoom-thumb .zoom-hint{text-decoration:underline;}
.zoom-overlay{display:none;}
.zoom-cb:checked~.zoom-overlay{display:flex;position:fixed;inset:0;z-index:60;cursor:zoom-out;
  background:rgba(0,0,0,.88);padding:16px;flex-direction:column;align-items:center;overflow:auto;}
.zoom-overlay img{max-width:min(96vw,780px);width:100%;height:auto;border-radius:8px;margin:auto;}
.zoom-close{align-self:center;position:sticky;top:0;margin:0 0 12px;background:#fff;color:var(--ink);
  border-radius:8px;padding:9px 20px;font-weight:700;cursor:pointer;}
/* Projector / room screen: large type meant to be read from the back of a room. */
body.projector .wrap{max-width:1680px;}
body.projector{font-size:1.4rem;}
body.projector header.bar h1{font-size:1.9rem;}
body.projector header.bar .tag{font-size:1.15rem;}
body.projector h1{font-size:2.7rem;}
body.projector h2{font-size:2.2rem;}
body.projector h3{font-size:1.7rem;}
body.projector .card{padding:22px 28px;margin:18px 0;}
body.projector table{font-size:1.3rem;}
body.projector th,body.projector td{padding:13px 18px;}
body.projector .big{font-size:2.1rem;}
body.projector .footer{font-size:1.15rem;}
body.projector .muted{color:#555;}
/* GM Command Center */
.cc-top{position:sticky;top:0;z-index:20;background:#2a2f36;color:#fff;border-radius:12px;
  padding:14px 18px;margin:8px 0 14px;display:flex;align-items:center;justify-content:space-between;
  gap:14px;flex-wrap:wrap;box-shadow:0 2px 8px rgba(0,0,0,.18);}
.cc-id{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
.cc-id-code{font-size:1.5rem;font-weight:800;letter-spacing:.04em;}
.cc-helpcount{background:var(--bad);color:#fff;font-weight:800;border-radius:20px;padding:4px 14px;font-size:1rem;}
.cc-top .btn,.cc-top button{padding:9px 16px;font-size:1rem;}
.cc-top .btn.secondary,.cc-top button.secondary{background:#3a414a;color:#fff;border:1px solid #5a636e;}
.cc-deck{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin:6px 0 10px;}
.cc-deck .card{margin:0;}
.cc-deck .card h2{margin-top:0;font-size:1.25rem;}
.cc-details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;margin:10px 0;}
.cc-details>summary{cursor:pointer;font-weight:700;font-size:1.05rem;padding:4px 0;}
.cc-details .card{margin:10px 0 0;border:0;box-shadow:none;padding:0;}
.cc-alert{background:#fcebe9;border:2px solid var(--bad);border-left:8px solid var(--bad);border-radius:10px;
  padding:12px 16px;margin:10px 0 14px;font-size:1.1rem;display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
.cc-alert strong{color:var(--bad);font-size:1.15rem;}
.cc-alert-team{display:inline-flex;align-items:center;gap:7px;background:#fff;border:1px solid var(--line);
  border-radius:20px;padding:3px 6px 3px 12px;}
.cc-alert-team a{font-weight:700;text-decoration:none;}
.cc-alert-team button{padding:5px 12px;font-size:.92rem;}
.cc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin:12px 0;}
.cc-card{display:block;position:relative;background:var(--card);border:1px solid var(--line);
  border-left:7px solid var(--gray);border-radius:12px;padding:14px 16px 12px;text-decoration:none;color:var(--ink);
  box-shadow:0 1px 3px rgba(0,0,0,.06);transition:box-shadow .12s,transform .12s;}
.cc-card:hover,.cc-card:focus-visible{box-shadow:0 4px 14px rgba(0,0,0,.14);transform:translateY(-2px);outline:none;}
.cc-card:focus-visible{outline:3px solid #000;outline-offset:2px;}
.cc-name{font-size:1.3rem;font-weight:800;line-height:1.2;}
.cc-course{color:var(--gray);font-weight:600;margin:2px 0 8px;}
.cc-chip{display:inline-block;background:#edf0f2;color:#2d3b45;border-radius:20px;padding:3px 12px;
  font-size:.92rem;font-weight:700;}
.cc-activity{margin:9px 0 0;font-size:1.05rem;}
.cc-prog{color:var(--gray);font-weight:600;}
.cc-foot{display:flex;align-items:center;justify-content:space-between;margin-top:11px;gap:10px;flex-wrap:wrap;}
.cc-state{font-weight:800;text-transform:uppercase;letter-spacing:.04em;font-size:.9rem;padding:3px 10px;
  border-radius:6px;background:#eef0f2;color:#2d3b45;}
.cc-overall{font-weight:700;color:var(--ink);}
.cc-open{display:block;margin-top:10px;color:var(--red);font-weight:700;font-size:.95rem;}
.cc-help-badge{display:none;position:absolute;top:-10px;right:14px;background:var(--bad);color:#fff;
  font-weight:800;border-radius:16px;padding:4px 14px;font-size:.92rem;box-shadow:0 2px 6px rgba(0,0,0,.25);}
/* Status colors (text label + border + chip, never color alone) */
.cc-card.state-working{border-left-color:#2f6fed;}
.cc-card.state-working .cc-state{background:#e5edff;color:#1b47a8;}
.cc-card.state-waiting{border-left-color:var(--warn);}
.cc-card.state-waiting .cc-state{background:#fbeccd;color:#8a5200;}
.cc-card.state-finished{border-left-color:var(--good);}
.cc-card.state-finished .cc-state{background:#e0f1e7;color:#1d6b40;}
.cc-card.state-help{border-color:var(--bad);border-left-color:var(--bad);background:#fdf1f0;
  box-shadow:0 0 0 2px var(--bad) inset;}
.cc-card.state-help .cc-state{background:var(--bad);color:#fff;}
.cc-card.state-help .cc-help-badge{display:inline-block;}
/* Detail modal (pure CSS :target; no JS needed to open/close) */
.cc-modal{display:none;position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.6);padding:16px;overflow:auto;}
.cc-modal:target{display:block;}
.cc-modal-inner{max-width:920px;margin:24px auto;background:var(--card);border-radius:12px;
  padding:18px 22px 22px;box-shadow:0 10px 40px rgba(0,0,0,.4);}
.cc-modal-inner .card{border:1px solid var(--line);}
.cc-modal-close{display:inline-block;float:right;background:#fff;color:var(--ink);border:2px solid var(--line);
  border-radius:8px;padding:8px 16px;font-weight:700;text-decoration:none;position:sticky;top:0;}
@media(max-width:560px){.cc-top{position:static;}.pausebanner{position:static;}}
/* Progression panel (Command Center hero) */
.cc-prog{background:var(--card);border:1px solid var(--line);border-left:7px solid #2f6fed;border-radius:12px;
  padding:16px 20px;margin:10px 0 14px;display:flex;gap:18px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;}
.cc-prog.ready{border-left-color:var(--good);background:#eef7f1;}
.cc-prog-main{flex:1 1 320px;min-width:0;}
.cc-prog-activity{font-size:1.15rem;margin-bottom:6px;}
.cc-readycount{font-size:1.55rem;font-weight:800;margin:4px 0;}
.cc-readycount #cc-ready-n{color:#1b47a8;}
.cc-working{color:var(--gray);margin:2px 0 6px;}
.cc-allready{display:none;font-size:1.65rem;font-weight:800;color:var(--good);margin:4px 0;}
.cc-prog.ready .cc-allready{display:block;}
.cc-prog.ready .cc-readycount,.cc-prog.ready .cc-working{display:none;}
.cc-prog-help a{color:var(--bad);font-weight:700;}
.cc-next{margin:6px 0 0;}
.cc-prog-actions{flex:0 0 auto;}
.cc-paused-tag{background:var(--warn);color:#fff;font-weight:800;border-radius:6px;padding:2px 10px;font-size:.9rem;}
/* Team pause banner (fixed, non-intrusive; toggled by the live poll) */
.pausebanner{position:fixed;top:0;left:0;right:0;z-index:80;background:var(--warn);color:#fff;
  text-align:center;font-weight:800;font-size:1.1rem;padding:12px 16px;box-shadow:0 2px 10px rgba(0,0,0,.25);}
.teamwait{border-left:6px solid var(--good);}
body.projector .cc-allready{color:var(--good);}
/* GM teaching-moment block */
.cc-teach{border:1px solid var(--line);border-left:8px solid var(--gray);border-radius:12px;
  padding:16px 20px;margin:12px 0;background:var(--card);}
.cc-teach h3{margin:0 0 6px;font-size:1.3rem;}
.cc-teach p{margin:.3em 0;}
.cc-teach button{padding:12px 22px;font-size:1.12rem;}
.cc-teach.avail{border-left-color:var(--good);background:#eef7f1;box-shadow:0 0 0 2px var(--good) inset;}
.cc-teach.avail h3{color:#1d6b40;font-size:1.5rem;}
.cc-teach.discussion{border-left-color:#2f6fed;background:#eef3ff;}
.cc-teach.discussion h3{color:#1b47a8;}
.cc-teach.revealed{border-left-color:var(--good);background:#eef7f1;}
.cc-teach.revealed h3{color:#1d6b40;}
.cc-teach.dismissed,.cc-teach.pending{border-left-color:var(--line);}
.cc-notes{background:#fff;border:1px solid var(--line);border-left:5px solid var(--warn);border-radius:10px;
  padding:8px 16px;margin:8px 0 14px;}
.cc-notes>summary{cursor:pointer;font-weight:700;color:#8a5200;padding:4px 0;}
.cc-notes-body{padding:4px 0 8px;}
.cc-notes-body ul{margin:.3em 0 .6em;padding-left:1.2em;}
.cc-notes-body li{margin:.25em 0;}
/* Projector: teaching question + strategies */
.proj-teach-head{font-size:1.5rem;color:var(--red);font-weight:800;letter-spacing:.02em;margin-bottom:10px;}
.proj-question{text-align:center;padding:40px 48px;}
.proj-q-label{font-size:1.6rem;text-transform:uppercase;letter-spacing:.14em;color:var(--gray);margin-bottom:18px;}
.proj-q-text{font-size:2.9rem;line-height:1.3;font-weight:700;color:var(--ink);max-width:22ch;margin:0 auto;}
.proj-strats{padding:28px 36px;}
.proj-strats-title{font-size:2.3rem;margin:0 0 20px;}
.proj-strat-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;}
.proj-strat{border:1px solid var(--line);border-left:7px solid var(--red);border-radius:12px;
  padding:18px 22px;background:#fff;}
.proj-strat-do{font-size:1.55rem;font-weight:800;line-height:1.25;margin-bottom:8px;}
.proj-strat-where{font-size:1.2rem;color:var(--gray);margin-bottom:8px;}
.proj-strat-solves{font-size:1.25rem;color:#1d6b40;font-weight:600;}
@media(max-width:1100px){.proj-strat-grid{grid-template-columns:1fr;}.proj-q-text{font-size:2.1rem;}}
/* GM Hallway Rumors block */
.cc-rumors{border:1px solid var(--line);border-left:8px solid var(--red);border-radius:12px;
  padding:16px 20px;margin:12px 0;background:#fff7f6;}
.cc-rumors h2{margin:0 0 4px;font-size:1.3rem;color:var(--red);}
.cc-rumors h3{margin:.4em 0 .3em;font-size:1.1rem;}
.cc-rumor-live{border:1px solid var(--red);border-radius:10px;padding:10px 14px;margin:8px 0;background:#fff;}
.cc-rumor-pending{border:1px dashed var(--line);border-radius:10px;padding:10px 14px;margin:8px 0;background:#fff;}
.cc-rumor-public{font-style:italic;font-size:1.05rem;line-height:1.4;margin:.3em 0;}
.cc-rumors button{padding:10px 20px;font-size:1.05rem;}
/* Projector: hallway rumor takeover */
.proj-rumor{padding:48px 56px;text-align:center;}
.proj-rumor-kicker{font-size:1.9rem;font-weight:800;letter-spacing:.06em;color:var(--red);
  text-transform:uppercase;margin-bottom:28px;}
.proj-rumor-text{font-size:2.5rem;line-height:1.4;font-weight:600;color:var(--ink);max-width:26ch;margin:0 auto;}
@media(max-width:1100px){.proj-rumor-text{font-size:1.9rem;}.proj-rumor-kicker{font-size:1.4rem;}}
/* Canvas-style course shell */
body.shell{background:#fff;}
.lms{display:flex;min-height:100vh;background:#fff;color:#2d3b45;}
.lms-global{background:#394B58;width:84px;flex:0 0 84px;display:flex;flex-direction:column;align-items:stretch;padding:8px 0;}
.lms-global .gi{color:#fff;opacity:.9;text-align:center;font-size:.6rem;line-height:1.1;padding:9px 2px;}
.lms-global .gi svg{display:block;margin:0 auto 3px;width:22px;height:22px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;}
.lms-course{flex:0 0 200px;border-right:1px solid #c7cdd1;padding:16px 0;background:#fff;}
.lms-course .ch{font-weight:700;padding:0 16px 12px;color:#A4343A;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;}
.lms-course a{display:block;padding:9px 16px;color:#394B58;text-decoration:none;border-left:4px solid transparent;}
.lms-course a:hover{background:#f2f4f5;}
.lms-course a.active{color:#A4343A;font-weight:700;border-left-color:#A4343A;}
.lms-course a.wx{background:#faf3f3;}
.lms-course a.wx::before{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;background:#A4343A;margin-right:7px;vertical-align:middle;}
.nav-wx{display:block;font-size:.72rem;font-weight:600;color:#A4343A;margin:1px 0 0 15px;}
.lms-content{flex:1;padding:18px 26px;min-width:0;}
.lms-crumb{color:#A4343A;font-size:.85rem;margin-bottom:6px;}
.lms-crumb a{color:#A4343A;text-decoration:none;}
.lms-h1{font-size:1.7rem;margin:0 0 16px;font-weight:600;color:#2d3b45;}
.lms .card{box-shadow:none;border-color:#e1e4e6;}
.lms .card h2{border-bottom:1px solid #e1e4e6;}
.lms table th{background:#f5f5f5;}
.lms-content a.btn{text-decoration:none;}
@media (max-width:760px){
 .lms{flex-direction:column;}
 .lms-global{flex-direction:row;width:100%;flex:0 0 auto;overflow-x:auto;}
 .lms-global .gi{flex:0 0 auto;}
 .lms-course{flex:0 0 auto;border-right:0;border-bottom:1px solid #c7cdd1;display:flex;flex-wrap:wrap;padding:8px;}
 .lms-course .ch{width:100%;padding-bottom:6px;}
 .lms-course a{border-left:0;border-bottom:3px solid transparent;}
 .lms-course a.active{border-left:0;border-bottom-color:#A4343A;}
}
"""


def page(title, body, screen="team", header_tag="Faculty Development Game", bare=False, width=""):
    body_class = {"team": "", "projector": "projector", "gm": "gm",
                  "private": "private", "landing": "", "shell": "shell"}.get(screen, "")
    # 'wide' gives card/working screens (student + course selection) desktop width.
    wrapcls = "wrap wide" if width == "wide" else "wrap"
    head = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>" + e(title) + "</title><style>" + BASE_CSS + "</style></head>"
        "<body class='" + body_class + "'>"
    )
    if bare:
        # No standard NCC chrome: the caller supplies its own full-viewport layout
        # (used by the Canvas-style course shell).
        return head + body + "</body></html>"
    # Inside a split pane the container already shows the NCC heading and the
    # "Team Workspace" label, so drop the redundant branding bar (keeping the
    # content wrap and all checkpoint info, meters, roles, and controls). This
    # never applies to standalone/legacy pages or to the full-screen private
    # screens, which are not embedded.
    header = ("" if _embedded() else
              "<header class='bar'><div class='" + wrapcls + "'><h1>Northwest Iowa Community College</h1>"
              "<span class='tag'>" + e(header_tag) + "</span></div></header>")
    return head + header + "<main class='" + wrapcls + "'>" + body + "</main></body></html>"


def resource_meters(scores, statuses):
    html = "<div class='card'><h3>Course Resources</h3>"
    for name in RESOURCE_NAMES:
        val = scores[name]
        st = statuses[name]
        html += (
            "<div class='meter'>"
            "<div class='top'><span>" + name + "</span>"
            "<span class='status-" + st + "'>" + str(val) + " / 100 &mdash; " + st + "</span></div>"
            "<div class='track'><div class='fill fill-" + st + "' style='width:" + str(val) + "%'></div></div>"
            "<div class='muted' style='font-size:.85rem'>" + RESOURCE_BLURB[name] + "</div>"
            "</div>"
        )
    html += "</div>"
    return html


def scores_table(scores, statuses):
    rows = ""
    for name in RESOURCE_NAMES:
        rows += ("<tr><td>" + name + "</td><td>" + str(scores[name])
                 + "</td><td class='status-" + statuses[name] + "'>" + statuses[name] + "</td></tr>")
    return ("<table><tr><th>Resource</th><th>Score</th><th>Status</th></tr>"
            + rows + "</table>")


def student_list_html(labels):
    if not labels:
        return "<p class='muted'>(none)</p>"
    return "<ul class='clean'>" + "".join("<li>" + e(s) + "</li>" for s in labels) + "</ul>"


def student_image_src(label):
    """Static path to the printed Student Card art (label '#NN Name')."""
    return "/static/art/students/student_%02d.png" % int(label[1:3])


def student_stat_table(label):
    """The six visible stats as a dot table (shared by all student readouts)."""
    st = STUDENT_STATS[label]
    return "<table>" + "".join(
        "<tr><td>" + STAT_LABELS[k] + "</td><td>" + ("&#9679;" * st[k]) + ("&#9675;" * (5 - st[k]))
        + " <span class='muted'>(" + str(st[k]) + "/5)</span></td></tr>"
        for k in STAT_KEYS
    ) + "</table>"


def student_image_tag(label, width=150):
    return ("<img src='" + student_image_src(label) + "' alt='" + e(label)
            + " Student Card' style='width:100%;max-width:" + str(width)
            + "px;height:auto;border-radius:8px;display:block'>")


def course_image_src(course_name):
    """Static path to the original Course Card art for a canonical course. The two
    printed copies (course_<CODE>_1.png / _2.png) are identical, so the digital
    card always uses copy 1. Course code 'BIOL 215' -> 'course_BIOL215_1.png'."""
    code = COURSE_BY_NAME[course_name]["code"].replace(" ", "")
    return "/static/art/courses/course_" + code + "_1.png"


def student_card_html(label):
    """A physical-card-style readout: original artwork, name, all six stats,
    context, strengths, challenges."""
    info = STUDENT_INFO[label]
    strengths = "".join("<li>" + e(x) + "</li>" for x in info["strengths"])
    challenges = "".join("<li>" + e(x) + "</li>" for x in info["challenges"])
    return (
        "<div class='card'><h4>" + e(label) + "</h4>"
        + student_image_tag(label, 150)
        + "<p class='muted'>" + e(info["context"]) + "</p>"
        + student_stat_table(label)
        + "<p><strong>Strengths:</strong></p><ul>" + strengths + "</ul>"
        "<p><strong>Challenges:</strong></p><ul>" + challenges + "</ul></div>"
    )


def student_mini_card(label, selectable=False, checked=False, badge=""):
    """Compact Student-Card tile built from the original card artwork, which
    already prints the student's name, number, and all six stats legibly -- so we
    never repeat a stat table beside it. `badge` (e.g. 'Retained'/'Refer') labels
    the card's game state; when `selectable`, the whole card is a keyboard- and
    tap-accessible checkbox with a visible selected state and checkmark."""
    num = int(label[1:3])
    src = e(student_image_src(label))
    alt = e(label) + " Student Card (shows name, number, and all six stats)"
    img = "<img src='" + src + "' alt='" + alt + "' loading='lazy'>"
    if selectable:
        ck = " checked" if checked else ""
        sid = "sel-%02d" % num
        zid = "szoom-%02d" % num
        # Two independent controls in one tile: the card face is the SELECT
        # checkbox (tap/click/Space to keep the student); a separate corner button
        # opens a full-size enlarge overlay without touching the selection state.
        return (
            "<div class='tile student-tile'>"
            "<input type='checkbox' class='sel-cb' name='students' id='" + sid + "' value='"
            + e(label) + "' data-num='" + str(num) + "'" + ck + ">"
            "<label for='" + sid + "' class='sel-face'>" + img
            + "<span class='check' aria-hidden='true'>&#10003; Selected</span></label>"
            "<input type='checkbox' class='zoom-cb' id='" + zid + "'>"
            "<label for='" + zid + "' class='enlarge-btn'>Enlarge card</label>"
            "<label for='" + zid + "' class='zoom-overlay'>"
            "<span class='zoom-close'>Close &#10005;</span>"
            "<img src='" + src + "' alt='" + alt + ", enlarged'></label>"
            "</div>")
    badge_html = ("<span class='pill scard-badge'>" + e(badge) + "</span>") if badge else ""
    return "<div class='scard'>" + img + badge_html + "</div>"


# ===========================================================================
# Home
# ===========================================================================
@app.route("/")
def home():
    body = (
        "<div class='card'><p>Choose a screen to open:</p>"
        "<p><a class='btn' href='/gm'>Game Master Dashboard</a></p>"
        "<p><a class='btn secondary' href='/team'>Team Screen</a></p>"
        "<p><a class='btn secondary' href='/projector'>Projector / Room Screen</a></p>"
        "</div>"
    )
    return page("Faculty Development Game", body, "landing")


# ===========================================================================
# Game Master Dashboard
# ===========================================================================
def course_control_html(game):
    phase = game.get("course_trading", "closed")
    if phase == "not_open":
        return ("<div class='card'><h2>Course Selection</h2><p><strong>COURSE SELECTION: NOT OPEN</strong></p>"
                "<p class='muted'>Opening deals each team a Course Card and lets teams propose one-for-one "
                "trades on their own screens.</p>"
                "<form method='post' action='/gm/course/open'><button type='submit'>Open Course Selection</button></form></div>")
    if phase == "open":
        return ("<div class='card'><h2>Course Selection</h2><p><strong>COURSE SELECTION: OPEN</strong></p>"
                "<p class='muted'>Teams may trade one-for-one in the app. Closing locks each team's held "
                "course and begins student placement.</p>"
                "<form method='post' action='/gm/course/close'><button type='submit'>Close Course Selection</button></form></div>")
    return "<div class='card'><h2>Course Selection</h2><p><strong>COURSE SELECTION: CLOSED</strong></p></div>"


def game_control_html(code, game):
    index = game["checkpoint_index"]
    number = index + 1
    name = CHECKPOINTS[index]
    is_open = game["checkpoint_open"]
    opened_once = game["opened_once"]
    final = game.get("final_open")

    if final:
        status = "FINAL ACCUSATION & OUTCOME"
    elif is_open:
        status = "OPEN (teams can see the current checkpoint)"
    elif opened_once:
        status = "LOCKED (teams waiting after this checkpoint)"
    else:
        status = "LOCKED (not yet opened)"

    buttons = ""
    if not final:
        if not is_open:
            buttons += ("<form method='post' action='/gm/open' style='display:inline'>"
                        "<button type='submit'>Open Checkpoint</button></form> ")
        if is_open:
            buttons += ("<form method='post' action='/gm/lock' style='display:inline'>"
                        "<button type='submit'>Lock Checkpoint</button></form> ")
        can_advance = (not is_open) and opened_once and (index < len(CHECKPOINTS) - 1)
        if can_advance:
            buttons += ("<form method='post' action='/gm/advance' style='display:inline'>"
                        "<button type='submit'>Advance to Next Checkpoint</button></form> ")
        if (not is_open) and opened_once and index == len(CHECKPOINTS) - 1:
            buttons += ("<form method='post' action='/gm/final' style='display:inline'>"
                        "<button type='submit'>Proceed to Final Accusation &amp; Outcome</button></form>")

    return (
        "<div class='card'><h2>Game Control</h2>"
        "<p><strong>Current checkpoint:</strong> " + str(number) + " of "
        + str(len(CHECKPOINTS)) + " &mdash; " + name + "</p>"
        "<p><strong>Status:</strong> " + status + "</p>"
        "<p>" + buttons + "</p></div>"
    )


def reassignment_control_html(code, game):
    """GM readiness view + controls for central student reassignment (Phase 3)."""
    teams = game.get("teams", [])
    if not any(t.get("deal_version") == 2 for t in teams):
        return ""   # student placement has not begun
    rows = ""
    for t in teams:
        if t.get("reassigned"):
            st = "reassignment complete"
        elif t.get("roster_confirmed"):
            st = "finalized (retain 10 / refer 5)"
        elif t.get("deal_version") == 2:
            st = "choosing"
        else:
            st = "earlier setup"
        rows += "<li>" + e(t["name"]) + " &mdash; " + st + "</li>"
    if game.get("reassignment_done"):
        tp = sum(len(t.get("reassigned_pool", [])) for t in teams)
        tb = sum(len(t.get("reassigned_backfill", [])) for t in teams)
        control = ("<p class='muted'>Replacements distributed: <strong>" + str(tp)
                   + "</strong> from the shared pool, <strong>" + str(tb)
                   + "</strong> administrative backfill. Each team now has 15 students.</p>")
    elif game.get("reassignment_error"):
        control = ("<p class='err'>" + e(game["reassignment_error"]) + "</p>"
                   "<form method='post' action='/gm/reopen_placement'>"
                   "<button type='submit'>Reopen Placement Decisions</button></form>")
    elif reassignment_ready(game):
        control = ("<p>All teams are finalized. Distribute the pooled students.</p>"
                   "<form method='post' action='/gm/reassign'>"
                   "<button type='submit'>Distribute Replacements</button></form>")
    else:
        control = "<p class='muted'>Waiting for every team to finalize before replacements are distributed.</p>"
    return ("<div class='card'><h2>Student Reassignment</h2>"
            "<ul class='clean'>" + rows + "</ul>" + control + "</div>")


def gm_team_block(code, game, team_i, team):
    teams = game["teams"]
    n_teams = len(teams)
    out = "<div class='card'><h3>" + e(team["name"]) + "</h3>"

    # Players / roles / allegiance (GM only).
    assignments = team.get("assignments", [])
    if assignments:
        out += "<p><strong>Players &amp; Roles:</strong></p><ul>"
        for idx, a in enumerate(assignments):
            tf = " <strong style='color:#A4343A'>[THUNDERFLUNKED - GM ONLY]</strong>" if team.get("thunderflunked") == idx else ""
            out += "<li>" + e(a["player"]) + " &mdash; " + a["role"] + tf + "</li>"
        out += "</ul>"
        if team.get("unrepresented"):
            out += ("<p class='muted'>Unrepresented Perspectives: "
                    + ", ".join(team["unrepresented"]) + "</p>")
    elif team.get("players"):
        out += "<p><strong>Players:</strong> " + ", ".join(e(p) for p in team["players"]) + "</p>"
    else:
        out += "<p class='muted'>(no players registered yet)</p>"

    # Private allegiance reveal status + per-player reset (GM only). Shows only
    # whether each player has viewed their reveal yet -- never which allegiance.
    # Resetting makes that one player privately re-view on the team device;
    # it changes nobody's allegiance, role, or setup progress.
    if team.get("setup_version") == 2 and team.get("roles_confirmed") and team.get("players"):
        seen = set(team.get("allegiance_seen", []))
        items = ""
        for pi, pname in enumerate(team["players"]):
            if pi in seen:
                items += ("<li>" + e(pname) + " &mdash; <span class='muted'>viewed</span> "
                          "<form method='post' action='/gm/reset_reveal' style='display:inline'>"
                          "<input type='hidden' name='team_index' value='" + str(team_i) + "'>"
                          "<input type='hidden' name='player' value='" + str(pi) + "'>"
                          "<button class='secondary' type='submit'>Reset reveal</button></form></li>")
            else:
                items += ("<li>" + e(pname) + " &mdash; <span class='status-Strained'>not yet viewed</span></li>")
        out += ("<p><strong>Private Allegiance Reveal (GM only):</strong> reset a player only if they "
                "acknowledged without reading. They will privately view it again on the team device; "
                "no allegiance or role changes.</p><ul class='clean'>" + items + "</ul>")

    if team.get("course"):
        out += "<p><strong>Course:</strong> " + e(team["course"]) + " <span class='muted'>(locked)</span></p>"
    elif team.get("course_held"):
        phase = game.get("course_trading", "closed")
        tag = "held, selection open" if phase == "open" else "dealt"
        out += "<p><strong>Course:</strong> " + e(team["course_held"]) + " <span class='muted'>(" + tag + ")</span></p>"
        pend = [o for o in game.get("course_offers", []) if team_i in (o["frm"], o["to"])]
        if pend:
            out += "<p class='muted'>Pending trade offers: " + str(len(pend)) + "</p>"
    else:
        out += "<p><strong>Course:</strong> (not dealt yet)</p>"

    if team.get("deal_version") == 2:
        # Digital placement (2.0).
        if team.get("roster_confirmed"):
            out += ("<p><strong>Retained (10):</strong> " + ", ".join(e(s) for s in team.get("starting_roster", [])) + "</p>"
                    "<p><strong>Referred (5):</strong> " + ", ".join(e(s) for s in team.get("released", [])) + "</p>")
        if team.get("reassigned"):
            n_pool = len(team.get("reassigned_pool", []))
            n_back = len(team.get("reassigned_backfill", []))
            out += ("<p><strong>Reassigned in (5):</strong> " + str(n_pool) + " from shared pool, "
                    + str(n_back) + " administrative backfill</p>"
                    "<p class='muted'>" + ", ".join(e(s) for s in team["reassigned"]) + "</p>"
                    "<p><strong>Final class (15):</strong> " + ", ".join(e(s) for s in team.get("active_roster", [])) + "</p>")
    else:
        if team.get("starting_roster"):
            out += "<p><strong>Starting Roster:</strong> " + ", ".join(e(s) for s in team["starting_roster"]) + "</p>"
            out += "<p><strong>Rejected:</strong> " + ", ".join(e(s) for s in team.get("rejected_students", [])) + "</p>"
        if team.get("late_adds"):
            out += "<p><strong>Active Late Adds:</strong> " + ", ".join(e(s) for s in team["late_adds"]) + "</p>"

    if team.get("resources"):
        out += "<p><strong>Current Resources:</strong></p>" + scores_table(team["resources"], team["statuses"])
        out += "<p><strong>Overall Course Score: " + str(team["overall"]) + "</strong></p>"

    # Placement/handoff status.
    if team.get("deal_version") == 2:
        if team.get("reassigned"):
            place = "reassignment complete"
        elif team.get("roster_confirmed"):
            place = "finalized &mdash; waiting for other teams"
        elif team.get("dealt"):
            place = "choosing (retain 10 / refer 5)"
        else:
            place = "not started"
        out += "<p><strong>Placement review:</strong> " + place + "</p>"
    else:
        h = team.get("handoff", {})
        rej = ", ".join(team.get("rejected_students", [])) or "(none)"
        if n_teams <= 1:
            out += ("<p><strong>Rejected Handoff (single-team test mode):</strong> "
                    + ("complete" if handoff_done(team) else "not confirmed")
                    + "<br>Rejected: " + rej + "</p>")
        else:
            dest = teams[(team_i + 1) % n_teams]["name"]
            src = teams[(team_i - 1) % n_teams]["name"]
            out += ("<p><strong>Rejected Handoff:</strong><br>"
                    "Passed to " + e(dest) + ": " + ("Yes" if h.get("pass_confirmed") else "No") + "<br>"
                    "Received from " + e(src) + ": " + ("Yes" if h.get("receive_confirmed") else "No")
                    + " (" + (", ".join(h.get("received", [])) or "none") + ")</p>")

    # Checkpoint decisions.
    cp = team.get("cp", {})
    out += "<p><strong>Checkpoint decisions:</strong></p><ul>"
    for i in range(len(CHECKPOINTS)):
        d = cp.get(i)
        if d and d.get("submitted"):
            out += "<li>CP" + str(i + 1) + ": " + e("; ".join(d.get("summary", []))) + "</li>"
        else:
            out += "<li>CP" + str(i + 1) + ": (not submitted)</li>"
    out += "</ul>"

    # Private checks (CP1/3/5) - GM only.
    out += "<p><strong>Private Player Checks (GM only):</strong></p><ul>"
    for i in sorted(HAS_PRIVATE):
        rnd = team.get("private", {}).get(i, {})
        votes = rnd.get("votes", {})
        complete = len(votes) == len(team.get("players", [])) and team.get("players")
        tally = {}
        for v in votes.values():
            nm = team["players"][int(v)]
            tally[nm] = tally.get(nm, 0) + 1
        totals = ", ".join(nm + ": " + str(c) for nm, c in tally.items()) or "(none)"
        out += ("<li>CP" + str(i + 1) + ": " + ("complete" if complete else "in progress / not started")
                + " &mdash; vote totals: " + e(totals) + "</li>")
    out += "</ul>"
    out += "<p><strong>Missions completed:</strong> " + str(len(team.get("missions_complete", []))) + "</p>"

    # Sabotage (GM only).
    sab = team.get("sabotage", {})
    if sab.get("used"):
        out += ("<p><strong>Emergency Sabotage (GM only):</strong> USED at CP"
                + str(sab.get("cp", "?") ) + " &mdash; " + e(sab.get("label", "")) + "</p>")
    else:
        out += "<p><strong>Emergency Sabotage (GM only):</strong> not used</p>"

    # Accusation / outcome.
    if team.get("accusation") is not None:
        accused = team["players"][team["accusation"]]
        correct = team["accusation"] == team.get("thunderflunked")
        out += ("<p><strong>Final Accusation (GM only):</strong> " + e(accused)
                + " &mdash; " + ("CORRECT" if correct else "incorrect") + "</p>")
    if team.get("outcome"):
        out += "<p><strong>Outcome:</strong> " + team["outcome"] + "</p>"

    out += "</div>"
    return out


def pacing_panel(code, game):
    start = game.get("timer_start")
    phase_key = current_phase_key(game)
    rows = ""
    for key, label, a, b in PACING:
        here = (key == phase_key)
        mark = str(a) + "-" + str(b) + " min"
        rows += ("<tr" + (" style='background:#faecec;font-weight:700'" if here else "") + ">"
                 "<td>" + ("&rarr; " if here else "") + label + "</td><td>" + mark + "</td></tr>")
    schedule = ("<table><tr><th>Phase</th><th>Target</th></tr>" + rows + "</table>")

    if not start:
        return ("<div class='card'><h2>Session Pacing</h2>"
                "<p>Start the clock when you kick off with the room. Target is ~60 min; this plan "
                "allows ~75 for the full private round after every checkpoint. You talk fast, so "
                "<strong>ahead of target is good.</strong></p>"
                "<form method='post' action='/gm/timer/start'><button type='submit'>Start Session Timer</button></form>"
                + schedule + "</div>")

    elapsed = int(time.time() - start)
    win = next((p for p in PACING if p[0] == phase_key), PACING[0])
    _, plabel, a, b = win
    emin = elapsed // 60
    if emin < a:
        status, cls = "Ahead of plan", "status-Normal"
    elif emin <= b:
        status, cls = "On pace", "status-Normal"
    elif emin <= b + 3:
        status, cls = "Slightly behind", "status-Strained"
    else:
        status, cls = "Behind - consider a cut", "status-Critical"

    clock = ("<p class='big'>Elapsed: <span id='sessclock'>00:00</span></p>"
             "<p>Current phase: <strong>" + plabel + "</strong> (target " + str(a) + "-" + str(b) + " min)<br>"
             "<span class='" + cls + "'><strong>" + status + "</strong></span></p>")
    js = ("<script>(function(){var el=document.getElementById('sessclock');if(!el)return;"
          "var s=" + str(elapsed) + ";function f(){var m=Math.floor(s/60),x=s%60;"
          "el.textContent=(m<10?'0':'')+m+':'+(x<10?'0':'')+x;s++;}f();setInterval(f,1000);})();</script>")
    reset = ("<form method='post' action='/gm/timer/reset' style='display:inline'>"
             "<button class='secondary' type='submit'>Reset Timer</button></form>")
    return ("<div class='card'><h2>Session Pacing</h2>" + clock
            + "<p>Refresh to update the pace status. " + reset + "</p>" + schedule + js + "</div>")


@app.route("/gm/timer/start", methods=["POST"])
def gm_timer_start():
    if active_code is not None:
        games[active_code]["timer_start"] = time.time()
    return redirect(url_for("gm"))


@app.route("/gm/timer/reset", methods=["POST"])
def gm_timer_reset():
    if active_code is not None:
        games[active_code]["timer_start"] = None
    return redirect(url_for("gm"))


# ---------------------------------------------------------------------------
# Live update + Request GM Help endpoints (controlled polling).
# ---------------------------------------------------------------------------
@app.route("/live/game")
def live_game():
    """Public, secret-free poll target. Returns a revision string that changes
    when a waiting team's screen (or the projector) should reload. Scoped to the
    given game (and team, if supplied)."""
    game_code = request.args.get("game_code", "").strip().upper()
    if game_code not in games:
        return jsonify({"rev": ""})
    game = games[game_code]
    paused = bool(game.get("paused"))
    ti = request.args.get("team_index", "")
    if ti.isdigit() and get_team(game_code, int(ti)) is not None:
        team = get_team(game_code, int(ti))
        # `rev` drives waiting-screen reloads; `help` and `paused` let the help
        # control and the pause banner sync WITHOUT a reload (so no inputs or
        # private actions are ever disturbed).
        return jsonify({"rev": team_rev(game, int(ti)), "help": help_active(team),
                        "paused": paused})
    return jsonify({"rev": projector_rev(game), "paused": paused})


@app.route("/gm/live")
def gm_live():
    """GM-only live payload (behind the /gm login guard). Public-appropriate
    per-team status + help flags for in-place card patching -- never allegiance,
    votes, missions, or sabotage."""
    if active_code is None:
        return jsonify({"rev": "", "teams": [], "help_names": []})
    game = games[active_code]
    teams = []
    for i, t in enumerate(game["teams"]):
        s = team_live_status(game, i, t)
        teams.append({"i": i, "name": t["name"], "course": s["course"],
                      "phase": s["phase"], "activity": s["activity"],
                      "state": ("help" if help_active(t) else s["state"]),
                      "base_state": s["state"], "progress": s["progress"],
                      "overall": s["overall"], "help": help_active(t)})
    # Per-team completion of the current activity (drives the live card "done" tick).
    for row, t in zip(teams, game["teams"]):
        row["done"] = team_activity_done(game, row["i"], t)
    help_names = [t["name"] for t in game["teams"] if help_active(t)]
    # `teach_sig` lets the dashboard reload when teaching availability/stage/dismiss
    # changes (so the right facilitation controls appear). No notes/question/
    # strategy text is ever sent in this payload.
    return jsonify({"rev": game_rev(game), "teams": teams, "help_names": help_names,
                    "progress": room_progress(game), "teach_sig": teach_sig(game)})


def _help_response(game_code, ti, team):
    """AJAX callers (the fetch-based help button) get JSON so the current screen
    and any unsent inputs are left untouched; no-JS form posts re-render the
    team's current screen in place."""
    if request.headers.get("X-Requested-With") == "fetch" or request.form.get("ajax") == "1":
        return jsonify({"ok": True, "active": help_active(team)})
    return resume_screen(game_code, ti, team)


@app.route("/team/help", methods=["POST"])
def team_help():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False}), 400
        return error_page("Something went wrong finding your team. Please start again.")
    ti = int(team_index)
    team = get_team(game_code, ti)
    team["help"] = {"active": True, "since": time.time()}
    return _help_response(game_code, ti, team)


@app.route("/team/help/cancel", methods=["POST"])
def team_help_cancel():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False}), 400
        return error_page("Something went wrong finding your team. Please start again.")
    ti = int(team_index)
    team = get_team(game_code, ti)
    team["help"] = {"active": False}
    return _help_response(game_code, ti, team)


@app.route("/gm/help/resolve", methods=["POST"])
def gm_help_resolve():
    if active_code is None:
        return redirect(url_for("gm"))
    team_index = request.form.get("team_index", "")
    if team_index.isdigit():
        team = get_team(active_code, int(team_index))
        if team is not None:
            team["help"] = {"active": False}
    return redirect(url_for("gm"))


@app.route("/gm/pause", methods=["POST"])
def gm_pause():
    """Pause the room for instructional discussion. Teams see a banner without
    any navigation or input loss; private actions are never interrupted. This
    changes no game state beyond the flag."""
    if active_code is not None:
        games[active_code]["paused"] = True
    return redirect(url_for("gm"))


@app.route("/gm/resume", methods=["POST"])
def gm_resume():
    if active_code is not None:
        games[active_code]["paused"] = False
    return redirect(url_for("gm"))


# --- Hallway rumor controls (GM decides when the room hears a rumor) ---------
@app.route("/gm/rumor/announce", methods=["POST"])
def gm_rumor_announce():
    """Move the oldest queued rumor onto the projector (full takeover). Idempotent:
    does nothing if one is already on screen or the queue is empty. Announcing only
    displays wording -- it never applies or re-applies any game effect."""
    if active_code is None:
        return redirect(url_for("gm"))
    r = rumors_state(games[active_code])
    if r.get("active") is None and r["queue"]:
        nxt = r["queue"].pop(0)
        r["active"] = nxt
        r["announced"].append(nxt)
    return redirect(url_for("gm"))


@app.route("/gm/rumor/dismiss", methods=["POST"])
def gm_rumor_dismiss():
    """Clear the on-screen rumor; the projector automatically restores whatever it
    was showing before (the stage is derived from game state)."""
    if active_code is None:
        return redirect(url_for("gm"))
    rumors_state(games[active_code])["active"] = None
    return redirect(url_for("gm"))


def hallway_rumors_block(game):
    """GM-only Hallway Rumors control. Shows the pending count, the exact public
    wording the room will see, the private cause (the GM is authorized to see it),
    an Announce action, a Dismiss/Return control while one is live, and a tally of
    what has already been announced. Renders nothing when there is no rumor at all."""
    r = _rumors_read(game)
    active, pending, announced = r["active"], r["queue"], r["announced"]
    if not active and not pending and not announced:
        return ""
    parts = []
    if active:
        parts.append(
            "<div class='cc-rumor-live'><h3>On the projector now</h3>"
            "<p class='cc-rumor-public'>&ldquo;" + e(rumor_public_text(active)) + "&rdquo;</p>"
            "<p class='muted'>Private cause (GM only): " + e(active.get("cause", "")) + "</p>"
            "<form method='post' action='/gm/rumor/dismiss' style='display:inline'>"
            "<button type='submit'>Dismiss &mdash; return the projector</button></form></div>")
    if pending:
        nxt = pending[0]
        more = ("<p class='muted'>" + str(len(pending) - 1) + " more waiting after this one.</p>"
                if len(pending) > 1 else "")
        if active:
            action = "<p class='muted'>Finish the current announcement before reading the next one.</p>"
        else:
            action = ("<form method='post' action='/gm/rumor/announce' style='display:inline'>"
                      "<button type='submit'>Announce Rumor</button></form>")
        parts.append(
            "<div class='cc-rumor-pending'><h3>" + str(len(pending)) + " hallway rumor"
            + ("" if len(pending) == 1 else "s") + " ready to announce</h3>"
            "<p class='muted'>Preview &mdash; exactly what the room will see:</p>"
            "<p class='cc-rumor-public'>&ldquo;" + e(rumor_public_text(nxt)) + "&rdquo;</p>"
            "<p class='muted'>Private cause (GM only): " + e(nxt.get("cause", "")) + "</p>"
            + more + action + "</div>")
    if announced:
        parts.append("<p class='muted'>Announced so far this game: <strong>" + str(len(announced))
                     + "</strong>.</p>")
    return ("<div class='cc-rumors' id='cc-rumors'><h2>Hallway Rumors</h2>"
            "<p class='muted'>Private to you. Nothing reaches the room until you announce it.</p>"
            + "".join(parts) + "</div>")


# --- GM-guided teaching moment controls (idempotent, double-click safe) ------
@app.route("/gm/teaching/begin", methods=["POST"])
def gm_teaching_begin():
    """Begin Discussion: safely lock the checkpoint (close decisions) and show the
    discussion question on the projector. Requires all teams finished unless the
    GM explicitly overrides. Idempotent -- a second click (or a double-submit)
    leaves the stage at 'discussion'."""
    if active_code is not None:
        game = games[active_code]
        if in_checkpoint(game):
            idx = game["checkpoint_index"]
            if teaching_stage(game, idx) is None:
                prog = room_progress(game)
                if not prog["all_ready"] and request.form.get("confirm") != "1":
                    return gm_confirm_page(
                        "/gm/teaching/begin",
                        "Begin the Checkpoint %d discussion" % (idx + 1),
                        prog["working"])
                game["checkpoint_open"] = False      # safe lock: decisions close
                _set_teaching_stage(game, idx, "discussion")
    return redirect(url_for("gm"))


@app.route("/gm/teaching/reveal", methods=["POST"])
def gm_teaching_reveal():
    """Reveal Canvas Strategies on the projector. Never automatic / never timed --
    only this explicit GM action moves 'discussion' -> 'revealed'. Idempotent."""
    if active_code is not None:
        game = games[active_code]
        if in_checkpoint(game) and teaching_stage(game, game["checkpoint_index"]) == "discussion":
            _set_teaching_stage(game, game["checkpoint_index"], "revealed")
    return redirect(url_for("gm"))


@app.route("/gm/teaching/dismiss", methods=["POST"])
def gm_teaching_dismiss():
    """Keep Waiting: quiet the 'teaching moment available' prompt for the current
    readiness WITHOUT starting discussion, locking, advancing, or altering any
    team's work. The GM can still Begin Discussion later."""
    if active_code is not None:
        game = games[active_code]
        if in_checkpoint(game) and teaching_stage(game, game["checkpoint_index"]) is None:
            game.setdefault("teaching_dismissed", {})[str(game["checkpoint_index"])] = True
    return redirect(url_for("gm"))


_STATE_LABEL = {"working": "Working", "waiting": "Waiting",
                "finished": "Finished", "help": "NEEDS HELP"}


def gm_team_card(game, i, team):
    """Compact, clickable, secret-free team card for the Command Center grid.
    Clicking opens the full GM-authorized detail modal (#team-i)."""
    s = team_live_status(game, i, team)
    state = "help" if help_active(team) else s["state"]
    course = e(s["course"]) if s["course"] else "<span class='muted'>no course yet</span>"
    prog = (" <span class='cc-prog'>(" + e(s["progress"]) + ")</span>") if s["progress"] else ""
    overall = ("Score " + str(s["overall"])) if s["overall"] is not None else ""
    return (
        "<a class='cc-card state-" + state + "' id='cc-card-" + str(i) + "' href='#team-" + str(i) + "'>"
        "<span class='cc-help-badge'>Needs help</span>"
        "<div class='cc-name'>" + e(team["name"]) + "</div>"
        "<div class='cc-course'>" + course + "</div>"
        "<div class='cc-chip'>" + e(s["phase"]) + "</div>"
        "<div class='cc-activity'>" + e(s["activity"]) + prog + "</div>"
        "<div class='cc-foot'><span class='cc-state'>" + _STATE_LABEL[state] + "</span>"
        "<span class='cc-overall'>" + overall + "</span></div>"
        "<span class='cc-open'>Open team details &rarr;</span>"
        "</a>")


def gm_team_modal(code, game, i, team):
    """Pure-CSS (:target) detail modal reusing the existing GM-authorized
    gm_team_block. Opening/closing needs no JavaScript; GM action forms inside
    post and redirect back to /gm as before."""
    resolve = ""
    if help_active(team):
        resolve = ("<form method='post' action='/gm/help/resolve' style='margin:0 0 12px'>"
                   "<input type='hidden' name='team_index' value='" + str(i) + "'>"
                   "<button type='submit'>Mark help resolved</button></form>")
    return ("<div class='cc-modal' id='team-" + str(i) + "'>"
            "<div class='cc-modal-inner'>"
            "<a class='cc-modal-close' href='#' aria-label='Close team details'>Close &#10005;</a>"
            + resolve + gm_team_block(code, game, i, team)
            + "</div></div>")


def gm_help_alert(game):
    """Prominent help summary strip. Always present (hidden when empty) so the
    live poller can show/hide it. Resolve buttons post and reload."""
    items = [(i, t) for i, t in enumerate(game["teams"]) if help_active(t)]
    inner = ""
    if items:
        n = len(items)
        rows = "".join(
            "<span class='cc-alert-team'><a href='#team-" + str(i) + "'>" + e(t["name"]) + "</a> "
            "<form method='post' action='/gm/help/resolve' style='display:inline'>"
            "<input type='hidden' name='team_index' value='" + str(i) + "'>"
            "<button class='secondary' type='submit'>Resolve</button></form></span>"
            for i, t in items)
        inner = "<strong>" + str(n) + " team" + ("s" if n > 1 else "") + " need help</strong> " + rows
    style = "" if items else " style='display:none'"
    return "<div class='cc-alert' id='cc-alert' role='alert'" + style + ">" + inner + "</div>"


def progression_panel(code, game):
    """The hero of the Command Center: current activity, how many teams are ready,
    who is still working, help count, the next GM action, and pause/resume. All
    fields have stable ids so the live poller patches them (textContent only -- no
    innerHTML, so team names can never inject markup)."""
    prog = room_progress(game)
    if prog["paused"]:
        pausebtn = ("<form method='post' action='/gm/resume' style='display:inline'>"
                    "<button type='submit'>Resume play</button></form>")
    else:
        pausebtn = ("<form method='post' action='/gm/pause' style='display:inline'>"
                    "<button class='secondary' type='submit'>Pause room for discussion</button></form>")
    pausetag = ("<span class='cc-paused-tag' id='cc-paused-tag'"
                + ("" if prog["paused"] else " style='display:none'") + ">PAUSED</span>")
    helpn = len(prog["help"])
    ready_cls = " ready" if prog["all_ready"] else ""
    working_txt = (", ".join(prog["working"])) or "none"
    return (
        "<div class='cc-prog" + ready_cls + "' id='cc-prog'>"
        "<div class='cc-prog-main'>"
        "<div class='cc-prog-activity'>Current activity: <strong id='cc-activity'>"
        + e(prog["activity"]) + "</strong> " + pausetag + "</div>"
        "<p class='cc-allready'>&#10003; All teams are ready</p>"
        "<p class='cc-readycount'><span id='cc-ready-n'>" + str(prog["done"]) + "</span> of "
        "<span id='cc-total-n'>" + str(prog["total"]) + "</span> teams ready</p>"
        "<p class='cc-working'>Still working: <span id='cc-working'>" + e(working_txt) + "</span></p>"
        "<p class='cc-prog-help' id='cc-prog-help'" + ("" if helpn else " style='display:none'") + ">"
        "<a href='#cc-alert'><span id='cc-prog-help-n'>" + str(helpn) + "</span> team(s) need help</a></p>"
        "<p class='cc-next'><strong>Next GM action:</strong> <span id='cc-next-label'>"
        + e(prog["next_label"]) + "</span> <span class='muted'>&mdash; <span id='cc-next-note'>"
        + e(prog["next_note"]) + "</span></span></p>"
        "</div><div class='cc-prog-actions'>" + pausebtn + "</div></div>")


def gm_live_script(game):
    """Poll /gm/live every 3s: reload if the game phase changed (controls differ),
    otherwise patch each card's state/activity/score AND the progression panel in
    place, keeping the help counter current. Never reloads for mere team-status/
    help changes, so an open detail modal or scroll position is preserved. Fails
    quiet on dropped connection."""
    cur = game_rev(game)
    tsig = teach_sig(game)
    return (
        "<script>(function(){var cur=" + json.dumps(cur) + ";var tsig=" + json.dumps(tsig) + ";"
        "var LBL={working:'Working',waiting:'Waiting',finished:'Finished',help:'NEEDS HELP'};"
        "function esc(s){return String(s).replace(/[&<>]/g,function(c){"
        "return c==='&'?'&amp;':c==='<'?'&lt;':'&gt;';});}"
        "function set(id,v){var el=document.getElementById(id);if(el)el.textContent=v;}"
        "function tick(){fetch('/gm/live',{headers:{'X-Requested-With':'fetch'}})"
        ".then(function(r){return r.json();}).then(function(j){if(!j)return;"
        # Reload when the phase OR the teaching availability/stage changes so the
        # facilitation controls (Begin / Reveal / Advance) stay correct.
        "if((j.rev&&j.rev!==cur)||(typeof j.teach_sig==='string'&&j.teach_sig!==tsig)){location.reload();return;}var hn=0;"
        "(j.teams||[]).forEach(function(t){if(t.help)hn++;var c=document.getElementById('cc-card-'+t.i);if(!c)return;"
        "c.className='cc-card state-'+t.state;"
        "var a=c.querySelector('.cc-activity');if(a)a.innerHTML=esc(t.activity)+"
        "(t.progress?(\" <span class='cc-prog'>(\"+esc(t.progress)+\")</span>\"):'');"
        "var st=c.querySelector('.cc-state');if(st)st.textContent=LBL[t.state]||t.state;"
        "var ov=c.querySelector('.cc-overall');if(ov)ov.textContent=(t.overall!=null?('Score '+t.overall):'');"
        "var ch=c.querySelector('.cc-chip');if(ch)ch.textContent=esc(t.phase);});"
        "var hc=document.getElementById('cc-helpcount');"
        "if(hc){if(hn>0){hc.style.display='';hc.textContent=hn+' need help';}else{hc.style.display='none';}}"
        "var p=j.progress;if(p){set('cc-activity',p.activity);set('cc-ready-n',p.done);"
        "set('cc-total-n',p.total);set('cc-working',p.working.length?p.working.join(', '):'none');"
        "set('cc-next-label',p.next_label);set('cc-next-note',p.next_note);"
        "var pw=document.getElementById('cc-prog');if(pw)pw.className='cc-prog'+(p.all_ready?' ready':'');"
        "var ph=document.getElementById('cc-prog-help');if(ph){if(p.help&&p.help.length){ph.style.display='';"
        "set('cc-prog-help-n',p.help.length);}else{ph.style.display='none';}}"
        "var ptag=document.getElementById('cc-paused-tag');if(ptag)ptag.style.display=p.paused?'':'none';}"
        "}).catch(function(){});}setInterval(tick,3000);})();</script>")


def facilitator_notes(idx):
    """GM-only facilitator notes + a preview of the projector question/strategies,
    collapsed by default. NEVER rendered on a team screen, the projector, or any
    live payload."""
    t = TEACHING.get(idx)
    if not t:
        return ""
    n = t["notes"]
    follow = "".join("<li>" + e(q) + "</li>" for q in n["followups"])
    strat = "".join("<li><strong>" + e(s["do"]) + "</strong> "
                    "<span class='muted'>" + e(s["where"]) + "</span> &mdash; " + e(s["solves"]) + "</li>"
                    for s in t["strategies"])
    return ("<details class='cc-notes'><summary>Facilitator notes (GM only)</summary>"
            "<div class='cc-notes-body'>"
            "<p class='muted'>Private to you &mdash; never shown on the projector or team screens. "
            "Suggested time: <strong>" + e(n["time"]) + "</strong>.</p>"
            "<p><strong>Objective:</strong> " + e(n["objective"]) + "</p>"
            "<p><strong>Opener:</strong> &ldquo;" + e(n["opener"]) + "&rdquo;</p>"
            "<p><strong>Follow-up angles:</strong></p><ul>" + follow + "</ul>"
            "<p><strong>Discussion question (appears on the projector at Begin):</strong><br>"
            + e(t["question"]) + "</p>"
            "<p><strong>Canvas strategies (appear on the projector at Reveal):</strong></p><ul>"
            + strat + "</ul>"
            "<p><strong>Tools:</strong> " + e(n["tools"]) + "</p>"
            "<p><strong>Why it matters:</strong> " + e(n["why"]) + "</p>"
            "<p><strong>Transition:</strong> " + e(n["transition"]) + "</p>"
            "</div></details>")


def teaching_block(game):
    """The GM's guided teaching-moment control for the CURRENT checkpoint: the
    stage-appropriate prompt + the single clear next action, plus the GM-only
    facilitator notes. Rendered server-side for the current stage; the live poller
    reloads the dashboard when availability/stage changes (teach_sig)."""
    if not in_checkpoint(game):
        return ""
    idx = game["checkpoint_index"]
    if idx not in TEACHING:
        return ""
    stage = teaching_stage(game, idx)
    cpn = idx + 1
    cpname = e(CHECKPOINTS[idx])
    begin = ("<form method='post' action='/gm/teaching/begin' style='display:inline'>"
             "<button type='submit'>Begin Discussion</button></form>")
    reveal = ("<form method='post' action='/gm/teaching/reveal' style='display:inline'>"
              "<button type='submit'>Reveal Canvas Strategies</button></form>")
    keepwait = ("<form method='post' action='/gm/teaching/dismiss' style='display:inline'>"
                "<button class='secondary' type='submit'>Keep Waiting</button></form>")
    if stage == "revealed":
        if idx < len(CHECKPOINTS) - 1:
            nxt_label, nxt_url = "Advance to Checkpoint %d" % (cpn + 1), "/gm/advance"
        else:
            nxt_label, nxt_url = "Proceed to Final", "/gm/final"
        inner = ("<h3>&#10003; Discussion complete &mdash; ready to continue</h3>"
                 "<p>The Canvas strategies are on the projector. When you're ready:</p>"
                 "<form method='post' action='" + nxt_url + "' style='display:inline'>"
                 "<button type='submit'>" + nxt_label + "</button></form>")
        cls = "revealed"
    elif stage == "discussion":
        inner = ("<h3>Discussion in progress</h3>"
                 "<p>The projector is showing the discussion question; teams are looking at the projector.</p>"
                 "<p class='muted'>Strategies appear only when you press Reveal &mdash; never automatically or on a timer.</p>"
                 + reveal)
        cls = "discussion"
    elif teaching_available(game) and not teaching_dismissed(game, idx):
        inner = ("<h3>&#9733; All teams ready &mdash; Teaching moment available</h3>"
                 "<p>Checkpoint " + str(cpn) + ": " + cpname + ". Begin the discussion to lock decisions "
                 "and show the question on the projector.</p>" + begin + " " + keepwait)
        cls = "avail"
    elif teaching_dismissed(game, idx):
        inner = ("<h3>Teaching moment available</h3>"
                 "<p>Checkpoint " + str(cpn) + ": " + cpname + ". Begin the discussion whenever you're ready.</p>"
                 + begin)
        cls = "dismissed"
    else:
        inner = ("<h3>Teaching moment</h3><p class='muted'>Unlocks for Checkpoint " + str(cpn)
                 + " once every team has finished its decision and private round.</p>")
        cls = "pending"
    return ("<div class='cc-teach " + cls + "' id='cc-teach'>" + inner + "</div>" + facilitator_notes(idx))


@app.route("/gm")
def gm():
    if active_code is None:
        body = ("<div class='card'><h2>Game Master Command Center</h2>"
                "<p>No active game yet. Create one to begin.</p>"
                "<form method='post' action='/gm/create'><button type='submit'>Create New Game</button></form></div>")
        return page("Game Master Command Center", body, "gm")

    game = games[active_code]
    teams = game["teams"]
    logout = ""
    if app.config.get("GM_PASSWORD") and session.get("is_gm"):
        logout = ("<form method='post' action='/gm/logout' style='display:inline'>"
                  "<button class='secondary' type='submit'>Log Out</button></form> ")

    # Pinned header: identity + room-wide actions, always visible.
    header = (
        "<div class='cc-top'>"
        "<div class='cc-id'><span class='cc-id-code'>Game " + active_code + "</span>"
        "<span class='cc-helpcount' id='cc-helpcount' style='display:none'></span></div>"
        "<div class='cc-id-actions'>"
        "<a class='btn secondary' href='/gm'>Refresh</a> "
        "<a class='btn secondary' href='/projector' target='_blank' rel='noopener'>Projector</a> "
        + logout +
        "<form method='post' action='/gm/create' style='display:inline'>"
        "<button class='secondary' type='submit'>New Game</button></form>"
        "</div></div>")

    # Control deck: the critical phase controls stay near the top; the full pacing
    # schedule is secondary (collapsed).
    controls = ("<div class='cc-deck'>"
                + course_control_html(game) + game_control_html(active_code, game)
                + reassignment_control_html(active_code, game) + "</div>")
    pacing = ("<details class='cc-details'><summary>Session pacing &amp; timer</summary>"
              + pacing_panel(active_code, game) + "</details>")

    # Team cards grid + detail modals.
    if teams:
        cards = "<div class='cc-grid'>" + "".join(gm_team_card(game, i, t) for i, t in enumerate(teams)) + "</div>"
        modals = "".join(gm_team_modal(active_code, game, i, t) for i, t in enumerate(teams))
        grid = "<h2>Teams (" + str(len(teams)) + ")</h2>" + cards + modals
    else:
        grid = "<div class='card'><p>No teams have joined yet. Share the game code above.</p></div>"

    body = (header + progression_panel(active_code, game) + teaching_block(game)
            + hallway_rumors_block(game)
            + gm_help_alert(game) + controls + pacing + grid
            + "<p class='footer'><a href='/'>Home</a></p>"
            + gm_live_script(game))
    return page("Game Master Command Center", body, "gm")


def gm_confirm_page(action_url, action_label, incomplete):
    """Shown when the GM tries to end an activity before every team is finished.
    Preserves the authorized override: an explicit confirm re-posts the same
    action. Opening a checkpoint is never gated (that starts an activity)."""
    lst = "".join("<li>" + e(n) + "</li>" for n in incomplete)
    body = ("<div class='card'><h2>Not all teams are finished</h2>"
            "<p class='err'><strong>" + str(len(incomplete)) + "</strong> team(s) have not finished "
            "the current activity:</p><ul>" + lst + "</ul>"
            "<p>You can wait for them (the dashboard updates live as they finish), or override and "
            "continue now &mdash; overriding may cut a team's work short.</p>"
            "<form method='post' action='" + action_url + "' style='display:inline'>"
            "<input type='hidden' name='confirm' value='1'>"
            "<button type='submit'>" + e(action_label) + " anyway (override)</button></form> "
            "<a class='btn secondary' href='/gm'>Cancel &mdash; keep waiting</a></div>")
    return page("Confirm: " + action_label, body, "gm")


def gm_teaching_skip_confirm_page(action_url, action_label, idx):
    """Shown when the GM tries to move past a checkpoint without completing its
    teaching moment (discussion + reveal). A teaching moment is never skipped
    silently; bypassing it takes a deliberate, explicit override."""
    stage = teaching_stage(games[active_code], idx)
    where = ("you have not begun the discussion yet" if stage is None
             else "you began the discussion but have not revealed the Canvas strategies")
    body = ("<div class='card'><h2>Teaching moment not finished</h2>"
            "<p class='err'>For <strong>Checkpoint " + str(idx + 1) + ": " + e(CHECKPOINTS[idx])
            + "</strong>, " + where + ".</p>"
            "<p>The teaching moment is the point of the round. You can go back and run it, or skip it "
            "deliberately this time.</p>"
            "<form method='post' action='" + action_url + "' style='display:inline'>"
            "<input type='hidden' name='confirm' value='1'>"
            "<input type='hidden' name='skip_teaching' value='1'>"
            "<button type='submit'>Skip the teaching moment &amp; " + e(action_label) + "</button></form> "
            "<a class='btn secondary' href='/gm'>Cancel &mdash; go back</a></div>")
    return page("Confirm: skip teaching moment", body, "gm")


@app.route("/gm/open", methods=["POST"])
def gm_open():
    if active_code is not None and not games[active_code].get("final_open"):
        games[active_code]["checkpoint_open"] = True
        games[active_code]["opened_once"] = True
    return redirect(url_for("gm"))


@app.route("/gm/lock", methods=["POST"])
def gm_lock():
    if active_code is not None:
        game = games[active_code]
        if game.get("checkpoint_open"):
            prog = room_progress(game)
            if not prog["all_ready"] and request.form.get("confirm") != "1":
                return gm_confirm_page("/gm/lock",
                                       "Lock Checkpoint %d" % (game["checkpoint_index"] + 1),
                                       prog["working"])
            game["checkpoint_open"] = False
    return redirect(url_for("gm"))


@app.route("/gm/reassign", methods=["POST"])
def gm_reassign():
    """GM-triggered central reassignment (also auto-runs when the last team
    finalizes). Idempotent: a no-op once reassignment is done."""
    if active_code is not None:
        attempt_reassignment(active_code)
    return redirect(url_for("gm"))


@app.route("/gm/reopen_placement", methods=["POST"])
def gm_reopen_placement():
    """Safe recovery when no valid reassignment exists: unlock every team's
    retain/refer decision so they can release a different five. Never undoes a
    completed distribution."""
    if active_code is not None:
        game = games[active_code]
        if not game.get("reassignment_done"):
            for t in game["teams"]:
                if t.get("deal_version") == 2:
                    t["roster_confirmed"] = False
                    t.pop("retain_pending", None)
                    t.pop("release_pending", None)
            game["reassignment_error"] = None
    return redirect(url_for("gm"))


@app.route("/gm/reset_reveal", methods=["POST"])
def gm_reset_reveal():
    """GM-only: clear ONE player's 'allegiance seen' flag so they privately view
    their reveal again (e.g. they acknowledged without reading). This endpoint is
    under /gm/, so the standard GM login requirement applies. It touches only
    team["allegiance_seen"] -- never the assigned allegiance, the public role, or
    any other player. The v2 setup gate in resume_screen then routes just that
    player back through the same pass-the-computer reveal, even mid-game, without
    repeating setup for the team or restarting the game."""
    if active_code is None:
        return redirect(url_for("gm"))
    team_index = request.form.get("team_index", "")
    player = request.form.get("player", "")
    if not team_index.isdigit():
        return redirect(url_for("gm"))
    team = get_team(active_code, int(team_index))
    if team is None:
        return redirect(url_for("gm"))
    if player.isdigit() and int(player) < len(team.get("players", [])):
        seen = team.get("allegiance_seen", [])
        if int(player) in seen:
            seen.remove(int(player))
    return redirect(url_for("gm"))


@app.route("/gm/advance", methods=["POST"])
def gm_advance():
    if active_code is not None:
        game = games[active_code]
        index = game["checkpoint_index"]
        if (not game["checkpoint_open"] and game["opened_once"]
                and index < len(CHECKPOINTS) - 1):
            prog = room_progress(game)
            if not prog["all_ready"] and request.form.get("confirm") != "1":
                return gm_confirm_page("/gm/advance",
                                       "Advance to Checkpoint %d" % (index + 2),
                                       prog["working"])
            # Never silently skip the teaching moment: bypassing it needs an
            # explicit override (reuses the same confirm=1 token as the page).
            if teaching_stage(game, index) != "revealed" and request.form.get("confirm") != "1":
                return gm_teaching_skip_confirm_page(
                    "/gm/advance", "Advance to Checkpoint %d" % (index + 2), index)
            game["checkpoint_index"] = index + 1
            game["checkpoint_open"] = False
            game["opened_once"] = False
    return redirect(url_for("gm"))


@app.route("/gm/final", methods=["POST"])
def gm_final():
    if active_code is not None:
        game = games[active_code]
        if (not game["checkpoint_open"] and game["opened_once"]
                and game["checkpoint_index"] == len(CHECKPOINTS) - 1):
            prog = room_progress(game)
            if not prog["all_ready"] and request.form.get("confirm") != "1":
                return gm_confirm_page("/gm/final", "Proceed to Final", prog["working"])
            if teaching_stage(game, game["checkpoint_index"]) != "revealed" and request.form.get("confirm") != "1":
                return gm_teaching_skip_confirm_page(
                    "/gm/final", "Proceed to Final", game["checkpoint_index"])
            game["final_open"] = True
            game["checkpoint_open"] = False
    return redirect(url_for("gm"))


@app.route("/gm/course/open", methods=["POST"])
def gm_course_open():
    if active_code is not None and games[active_code].get("course_trading") == "not_open":
        game = games[active_code]
        game["course_trading"] = "open"
        # Deal every team its Course Card now, so the GM and teams can see all
        # holdings and trading can begin. Idempotent; late joiners deal on view.
        for t in game["teams"]:
            ensure_course_dealt(game, t)
    return redirect(url_for("gm"))


@app.route("/gm/course/close", methods=["POST"])
def gm_course_close():
    if active_code is not None and games[active_code].get("course_trading") == "open":
        game = games[active_code]
        game["course_trading"] = "closed"
        # Lock each team's held course as its course for the semester and enter
        # the digital student-placement flow. Pending trade offers are void.
        game["course_offers"] = []
        for t in game["teams"]:
            lock_team_course(game, t)
    return redirect(url_for("gm"))


@app.route("/gm/create", methods=["POST"])
def create_game():
    global active_code
    code = make_game_code()
    games[code] = {
        "teams": [],
        "checkpoint_index": 0,
        "checkpoint_open": False,
        "opened_once": False,
        "course_trading": "not_open",
        "final_open": False,
        "timer_start": None,   # epoch seconds when the GM starts the session clock
    }
    active_code = code
    return redirect(url_for("gm"))


# ===========================================================================
# Team Screen - join
# ===========================================================================
@app.route("/team")
def team():
    body = (
        "<div class='card'><form method='post' action='/team/join'>"
        "<p><label>Game Code<br><input type='text' name='game_code' autocomplete='off'></label></p>"
        "<p><label>Team Name<br><input type='text' name='team_name' autocomplete='off'></label></p>"
        "<p><button type='submit'>Join Game</button></p></form></div>"
        "<p class='footer'><a href='/'>Home</a></p>"
    )
    return page("Team Screen", body)


def error_page(message):
    body = ("<div class='card'><p class='err'>" + e(message) + "</p>"
            "<p><a class='btn secondary' href='/team'>Try again</a> <a href='/'>Home</a></p></div>")
    return page("Team Screen", body)


@app.route("/team/join", methods=["POST"])
def team_join():
    game_code = request.form.get("game_code", "").strip().upper()
    team_name = request.form.get("team_name", "").strip()

    if game_code not in games:
        return error_page("That game code was not found. Please check the code and try again.")
    if team_name == "":
        return error_page("Please enter a team name.")

    existing_index = find_team_index(game_code, team_name)
    if existing_index is not None:
        team = get_team(game_code, existing_index)
        # A team already in active play rejoins into the split workspace; teams
        # still in setup resume their full-page setup flow.
        if team_setup_complete(team):
            return redirect(workspace_url(game_code, existing_index))
        return resume_screen(game_code, existing_index, team)

    games[game_code]["teams"].append({"name": team_name, "players": []})
    team_index = len(games[game_code]["teams"]) - 1
    return count_form(game_code, team_index, team_name)


def count_form(game_code, team_index, team_name):
    options = ""
    for count in ALLOWED_PLAYER_COUNTS:
        options += ("<label><input type='radio' name='player_count' value='" + str(count)
                    + "'> " + str(count) + " players</label>")
    body = (
        "<div class='card'><p>You are on team <strong>" + e(team_name) + "</strong> in game "
        + game_code + ".</p><h2>How many players are at your table?</h2>"
        "<form method='post' action='/team/players'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        + options + "<p><button type='submit'>Next</button></p></form></div>"
    )
    return page("Team Setup", body + help_widget(game_code, team_index, get_team(game_code, team_index)))


def resume_screen(game_code, team_index, team):
    """Route a team to the correct screen based on how far setup got, and on
    the game's synchronized phase. Reads state only; never mutates the team."""
    game = games[game_code]
    if not team.get("players"):
        return count_form(game_code, team_index, team["name"])
    if team.get("setup_version") == 2:
        # Guided digital setup: shuffle roles -> private allegiance reveal ->
        # team overview. Each step is gated on its own persisted flag so a page
        # refresh always lands on the same screen and never re-randomizes.
        if not team.get("roles_confirmed"):
            return roles_setup_screen(game_code, team_index, team)
        if len(team.get("allegiance_seen", [])) < len(team["players"]):
            return reveal_roster_screen(game_code, team_index, team)
        if not team.get("setup_overview_done"):
            return setup_overview_screen(game_code, team_index, team)
    elif not team.get("assignments"):
        return role_form(game_code, team_index, team)
    if not team.get("course"):
        return course_trade_screen(game_code, team_index, team)
    if team.get("deal_version") == 2:
        # Thunderflunked 2.0 digital placement: auto-deal 15 -> retain 10/refer 5
        # -> central reassignment -> final 15-student class.
        if not team.get("roster_confirmed"):
            return roster_decision_screen(game_code, team_index, team)
        if not team.get("placement_acked"):
            return reassignment_screen(game_code, team_index, team)
    else:
        # Legacy manual deal/draft/table-handoff flow (pre-2.0 saved games).
        if not team.get("draft_pool"):
            return pool_form(game_code, team_index, team)
        if not team.get("starting_roster"):
            return roster_form(game_code, team_index, team)
        if not handoff_done(team):
            return handoff_screen(game_code, team_index, team)
    if game.get("final_open"):
        return final_screen(game_code, team_index, team)
    return status_screen(game_code, team_index, team)


@app.route("/team/players", methods=["POST"])
def team_players():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    player_count = request.form.get("player_count", "")

    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    if not player_count.isdigit() or int(player_count) not in ALLOWED_PLAYER_COUNTS:
        return error_page("Please choose 4, 5, or 6 players.")

    count = int(player_count)
    team = get_team(game_code, int(team_index))
    name_fields = ""
    for i in range(1, count + 1):
        name_fields += ("<p><label>Player " + str(i) + " name<br>"
                        "<input type='text' name='player_" + str(i) + "' autocomplete='off'></label></p>")
    body = (
        "<div class='card'><h2>Register players for " + e(team["name"]) + "</h2>"
        "<form method='post' action='/team/save'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + team_index + "'>"
        "<input type='hidden' name='player_count' value='" + str(count) + "'>"
        + name_fields + "<p><button type='submit'>Register Players</button></p></form></div>"
    )
    return page("Team Setup", body + help_widget(game_code, int(team_index), team))


@app.route("/team/save", methods=["POST"])
def team_save():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    player_count = request.form.get("player_count", "")

    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    if not player_count.isdigit() or int(player_count) not in ALLOWED_PLAYER_COUNTS:
        return error_page("Please choose 4, 5, or 6 players.")

    count = int(player_count)
    names = []
    for i in range(1, count + 1):
        name = request.form.get("player_" + str(i), "").strip()
        if name == "":
            return error_page("Every player name is required. Please go back and fill them all in.")
        names.append(name)

    team = get_team(game_code, int(team_index))
    team["players"] = names
    # Assign allegiance privately now: exactly one Thunderflunked per table.
    # Revealed to each player privately during their first private turn.
    if "thunderflunked" not in team:
        team["thunderflunked"] = random.randrange(count)
        team["allegiance_seen"] = []
        team["missions_complete"] = []
        team["sabotage"] = {"used": False, "cp": None, "label": None, "prompted": {}}
        team["private"] = {}
    # Thunderflunked 2.0: guided digital setup replaces the physical Role Cards
    # and the in-person Allegiance Card deal. setup_version marks a team that
    # goes through the shuffle -> private reveal -> overview sequence. Legacy
    # teams (no setup_version) keep the original manual role_form path.
    team["setup_version"] = 2
    team.setdefault("roles_confirmed", False)

    return resume_screen(game_code, int(team_index), team)


def role_option_tags(selected=""):
    tags = "<option value=''>-- choose a role --</option>"
    for role in PUBLIC_ROLES:
        chosen = " selected" if role == selected else ""
        tags += "<option value='" + role + "'" + chosen + ">" + role + "</option>"
    return tags


def role_form(game_code, team_index, team, error_message="", previous=None):
    if previous is None:
        previous = {}
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    perspectives = "".join(
        "<li><strong>" + r + ":</strong> " + ROLE_PERSPECTIVES[r] + "</li>" for r in PUBLIC_ROLES
    )
    rows = ""
    for i, player in enumerate(team["players"], start=1):
        selected = previous.get("role_for_" + str(i), "")
        rows += ("<p><label>" + e(player) + "<br><select name='role_for_" + str(i) + "'>"
                 + role_option_tags(selected) + "</select></label></p>")
    body = (
        "<div class='card'><h2>Public Roles</h2>"
        "<p>Your Role is the perspective you are responsible for bringing into team "
        "decisions. It does not give you special powers. Use it to challenge the group "
        "when that part of the course is being overlooked.</p>"
        "<ul>" + perspectives + "</ul></div>"
        "<div class='card'><h3>Record the physical Role Card each player drew</h3>"
        "<p>Each role can only be used once.</p>" + error_html +
        "<form method='post' action='/team/roles'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        + rows + "<p><button type='submit'>Save Role Assignments</button></p></form></div>"
    )
    return page("Public Roles", body)


@app.route("/team/roles", methods=["POST"])
def team_roles():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")

    team = get_team(game_code, int(team_index))
    names = team["players"]
    chosen = {}
    picked_roles = []
    for i in range(1, len(names) + 1):
        chosen["role_for_" + str(i)] = request.form.get("role_for_" + str(i), "")
        picked_roles.append(chosen["role_for_" + str(i)])

    if any(role == "" for role in picked_roles):
        return role_form(game_code, int(team_index), team, "Every player must be given a role.", chosen)
    if any(role not in PUBLIC_ROLES for role in picked_roles):
        return role_form(game_code, int(team_index), team, "One of the selected roles is not valid.", chosen)
    if len(set(picked_roles)) != len(picked_roles):
        return role_form(game_code, int(team_index), team,
                         "The same role cannot be given to more than one player.", chosen)

    team["assignments"] = [{"role": r, "player": p} for r, p in zip(picked_roles, names)]
    team["unrepresented"] = [r for r in PUBLIC_ROLES if r not in picked_roles]
    return resume_screen(game_code, int(team_index), team)


# ===========================================================================
# Thunderflunked 2.0 digital setup
#   Step 1  Shuffle Roles   (unique public role per player, confirm to lock)
#   Step 2  Private Reveal  (pass-the-computer allegiance reveal, one at a time)
#   Step 3  Team Overview   (public roles + focus; never allegiances)
# These screens replace the physical Role Cards and the in-person Allegiance
# deal. They produce the same team["assignments"] / team["allegiance_seen"]
# the existing engine already consumes, so checkpoints, missions, sabotage, and
# suspicion voting are untouched.
# ===========================================================================
def setup_progress(step):
    """Small 'Step N of 3' indicator for the guided setup screens."""
    labels = ["Assign Roles", "Private Allegiance", "Team Overview"]
    cells = ""
    for i, label in enumerate(labels, start=1):
        state = "done" if i < step else ("now" if i == step else "todo")
        mark = "&#10003; " if state == "done" else (str(i) + ". ")
        style = ("font-weight:700;" if state == "now"
                 else ("opacity:.55;" if state == "todo" else "opacity:.8;"))
        cells += "<span style='margin-right:1.2em;" + style + "'>" + mark + label + "</span>"
    return ("<p class='muted' style='font-size:.9rem;margin-bottom:.4em'>Team Setup &mdash; Step "
            + str(step) + " of 3</p><p style='margin-top:0'>" + cells + "</p>")


def role_card_html(role, player=None, show_image=True, img_width=200):
    """Card-style readout of one public role: art, name, focus, optional player."""
    img = ("<img src='" + role_image_src(role) + "' alt='" + e(role)
           + " Role Card' style='width:100%;max-width:" + str(img_width)
           + "px;height:auto;border-radius:8px;display:block;margin:0 auto .5em'>") if show_image else ""
    who = ("<p style='margin:.2em 0'><strong>" + e(player) + "</strong></p>") if player else ""
    return ("<div class='card' style='display:inline-block;vertical-align:top;max-width:260px;margin:.4em'>"
            + img + who
            + "<p style='margin:.2em 0'><strong>" + e(role) + "</strong></p>"
            + "<p class='muted' style='font-size:.9rem'>" + ROLE_PERSPECTIVES[role] + "</p></div>")


def _setup_hidden(game_code, team_index):
    return ("<input type='hidden' name='game_code' value='" + game_code + "'>"
            "<input type='hidden' name='team_index' value='" + str(team_index) + "'>")


def roles_setup_screen(game_code, team_index, team):
    """Step 1. Shuffle unique public roles, review, and confirm to lock."""
    hidden = _setup_hidden(game_code, team_index)
    perspectives = "".join(
        "<li><strong>" + r + ":</strong> " + ROLE_PERSPECTIVES[r] + "</li>" for r in PUBLIC_ROLES
    )
    intro = (
        "<div class='card'>" + setup_progress(1) + "<h2>Assign Public Roles</h2>"
        "<p>Your Role is the perspective you are responsible for bringing into team "
        "decisions. It is not a special power &mdash; use it to speak up when that part of "
        "the course is being overlooked. Each role is given to one person.</p>"
        "<ul>" + perspectives + "</ul></div>"
    )
    if not team.get("assignments"):
        # Not shuffled yet: one obvious action.
        body = intro + (
            "<div class='card'><p>When everyone is registered, shuffle to deal each player a role.</p>"
            "<form method='post' action='/team/roles/shuffle'>" + hidden
            + "<button type='submit'>Shuffle Roles</button></form></div>"
        )
        return page("Assign Roles - " + team["name"], body + help_widget(game_code, team_index, team))

    # Shuffled but not confirmed: show the deal, allow reshuffle or confirm.
    cards = "".join(role_card_html(a["role"], a["player"]) for a in team["assignments"])
    body = intro + (
        "<div class='card'><h3>Roles dealt</h3>"
        "<div style='text-align:center'>" + cards + "</div>"
        "<p class='muted'>Review the assignments. You can reshuffle until you confirm.</p>"
        "<form method='post' action='/team/roles/confirm' style='display:inline'>" + hidden
        + "<button type='submit'>Confirm Roles</button></form> "
        "<form method='post' action='/team/roles/shuffle' style='display:inline'>" + hidden
        + "<button class='secondary' type='submit'>Reshuffle</button></form></div>"
    )
    return page("Assign Roles - " + team["name"], body + help_widget(game_code, team_index, team))


@app.route("/team/roles/shuffle", methods=["POST"])
def team_roles_shuffle():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    # Once roles are confirmed, ignore any further shuffle (e.g. a stale tab or
    # back button) so a confirmed assignment can never be silently replaced.
    if team.get("roles_confirmed"):
        return resume_screen(game_code, int(team_index), team)
    names = team.get("players", [])
    if not names:
        return error_page("Something went wrong finding your team. Please start again.")
    roles = random.sample(PUBLIC_ROLES, len(names))
    team["assignments"] = [{"role": r, "player": p} for r, p in zip(roles, names)]
    team["unrepresented"] = [r for r in PUBLIC_ROLES if r not in roles]
    return roles_setup_screen(game_code, int(team_index), team)


@app.route("/team/roles/confirm", methods=["POST"])
def team_roles_confirm():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if not team.get("assignments"):
        # Nothing to confirm yet; send them back to shuffle.
        return roles_setup_screen(game_code, int(team_index), team)
    # Deal each player their digital Secret Mission now that roles are fixed
    # (idempotent -- re-confirming never re-rolls).
    ensure_missions(team)
    team["roles_confirmed"] = True
    return resume_screen(game_code, int(team_index), team)


# ---------------------------------------------------------------------------
# Step 2. Private pass-the-computer allegiance reveal.
#   roster  -> select name -> neutral privacy screen -> reveal (with art) ->
#   acknowledge -> back to neutral roster. Exactly one Thunderflunked per team
#   was assigned at registration (team["thunderflunked"]); everyone else is
#   Thunderstruck. Allegiance is never listed, never placed in a GET/URL, and
#   only ever rendered for the single acting player.
# ---------------------------------------------------------------------------
def reveal_roster_screen(game_code, team_index, team):
    hidden = _setup_hidden(game_code, team_index)
    seen = set(team.get("allegiance_seen", []))
    rows = ""
    for i, pname in enumerate(team["players"]):
        if i in seen:
            rows += ("<li style='margin:.3em 0'>&#10003; <strong>" + e(pname)
                     + "</strong> <span class='muted'>&mdash; done</span></li>")
        else:
            rows += ("<li style='margin:.3em 0'><form method='post' "
                     "action='/team/setup/reveal/select' style='display:inline'>" + hidden
                     + "<input type='hidden' name='player' value='" + str(i) + "'>"
                     "<button type='submit'>" + e(pname) + "</button></li>")
    remaining = len(team["players"]) - len(seen)
    body = ("<div class='card'>" + setup_progress(2) + "<h2>Private Allegiance</h2>"
            "<p>One at a time, each player views their secret allegiance on this shared "
            "computer. Pick your name, look while others look away, then pass it on.</p>"
            "<p class='muted'>" + str(len(seen)) + " of " + str(len(team["players"]))
            + " viewed &middot; " + str(remaining) + " to go.</p>"
            "<ul class='clean'>" + rows + "</ul></div>")
    return page("Private Allegiance - " + team["name"], body, "private")


@app.route("/team/setup/reveal/select", methods=["POST"])
def team_setup_reveal_select():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    player = request.form.get("player", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if not (team.get("setup_version") == 2 and team.get("roles_confirmed")):
        return resume_screen(game_code, int(team_index), team)
    if not player.isdigit() or int(player) >= len(team["players"]):
        return reveal_roster_screen(game_code, int(team_index), team)
    if int(player) in set(team.get("allegiance_seen", [])):
        # Already viewed; do not allow a second look during setup.
        return reveal_roster_screen(game_code, int(team_index), team)
    # Neutral privacy hand-off screen. No allegiance data here.
    hidden = _setup_hidden(game_code, team_index)
    pname = team["players"][int(player)]
    body = ("<div class='card'><h2>Pass the computer to " + e(pname) + "</h2>"
            "<p>Everyone else, please look away.</p>"
            "<p><strong>" + e(pname) + "</strong>, when you are ready and no one else can see "
            "the screen, reveal your allegiance.</p>"
            "<form method='post' action='/team/setup/reveal/show' style='display:inline'>" + hidden
            + "<input type='hidden' name='player' value='" + str(int(player)) + "'>"
            "<button type='submit'>Reveal My Allegiance</button></form> "
            "<form method='post' action='/team/setup/reveal/back' style='display:inline'>" + hidden
            + "<button class='secondary' type='submit'>Not me &mdash; go back</button></form></div>")
    return page("Private Allegiance - " + team["name"], body, "private")


@app.route("/team/setup/reveal/show", methods=["POST"])
def team_setup_reveal_show():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    player = request.form.get("player", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if not (team.get("setup_version") == 2 and team.get("roles_confirmed")):
        return resume_screen(game_code, int(team_index), team)
    if not player.isdigit() or int(player) >= len(team["players"]):
        return reveal_roster_screen(game_code, int(team_index), team)
    if int(player) in set(team.get("allegiance_seen", [])):
        return reveal_roster_screen(game_code, int(team_index), team)
    p = int(player)
    is_tf = (p == team["thunderflunked"])
    hidden = _setup_hidden(game_code, team_index)
    img = ("<img src='" + allegiance_image_src(is_tf) + "' alt='Allegiance Card' "
           "style='width:100%;max-width:300px;height:auto;border-radius:10px;"
           "display:block;margin:.4em auto'>")
    if is_tf:
        text_html = ("<h3>You are THUNDERFLUNKED</h3>"
                     "<p>You're here to make sure they don't get the students and course through the semester.</p>"
                     "<p>You are the only Thunderflunked person at this table. Sabotage mainly through persuasion: "
                     "push reasonable ideas too far, favor individual fixes over scalable ones, spend capacity on "
                     "low-priority issues. Occasionally support good ideas to stay unsuspected.</p>")
    else:
        text_html = ("<h3>You are THUNDERSTRUCK</h3>"
                     "<p>You're here to get your students and course through the semester.</p>"
                     "<p>Someone at your table is Thunderflunked. You do not know who.</p>")
    # Deal (if needed) and reveal this player's Secret Mission on the same private
    # screen, so one pass-the-laptop handoff covers both allegiance and mission.
    ensure_missions(team)
    mission_html = mission_reveal_card(player_mission(team, p))
    body = ("<div class='card'><h2>" + e(team["players"][p]) + " &mdash; keep this secret</h2>"
            "<div class='card'>" + img + text_html + "</div>"
            + mission_html +
            "<form method='post' action='/team/setup/reveal/ack'>" + hidden
            + "<input type='hidden' name='player' value='" + str(p) + "'>"
            "<button type='submit'>I have seen it &mdash; hide and pass on</button></form></div>")
    return page("Private Allegiance - " + team["name"], body, "private")


@app.route("/team/setup/reveal/ack", methods=["POST"])
def team_setup_reveal_ack():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    player = request.form.get("player", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if player.isdigit() and int(player) < len(team["players"]):
        if int(player) not in team["allegiance_seen"]:
            team["allegiance_seen"].append(int(player))
    # Always return to the neutral roster (never another player's reveal).
    return resume_screen(game_code, int(team_index), team)


@app.route("/team/setup/reveal/back", methods=["POST"])
def team_setup_reveal_back():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    return resume_screen(game_code, int(team_index), team)


# ---------------------------------------------------------------------------
# Step 3. Team overview: public roles + focus only. No allegiances.
# ---------------------------------------------------------------------------
def setup_overview_screen(game_code, team_index, team):
    hidden = _setup_hidden(game_code, team_index)
    cards = "".join(role_card_html(a["role"], a["player"], img_width=160)
                    for a in team.get("assignments", []))
    body = ("<div class='card'>" + setup_progress(3) + "<h2>Your Table's Public Roles</h2>"
            "<p>Everyone has privately seen their allegiance. Here is who is watching which "
            "perspective during the semester. (Allegiances stay secret.)</p>"
            "<div style='text-align:center'>" + cards + "</div>"
            "<form method='post' action='/team/setup/overview/continue'>" + hidden
            + "<button type='submit'>Continue to Course Registration</button></form></div>")
    return page("Team Overview - " + team["name"], body + help_widget(game_code, team_index, team))


@app.route("/team/setup/overview/continue", methods=["POST"])
def team_setup_overview_continue():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    team["setup_overview_done"] = True
    return resume_screen(game_code, int(team_index), team)


def team_roles_panel(team):
    """Compact, always-available readout of the table's public roles and focus
    for the team workspace. Public information only; never shows allegiance."""
    assignments = team.get("assignments")
    if not assignments:
        return ""
    rows = "".join(
        "<li><strong>" + e(a["player"]) + "</strong> &mdash; " + a["role"]
        + ": <span class='muted'>" + ROLE_PERSPECTIVES[a["role"]] + "</span></li>"
        for a in assignments
    )
    return ("<details class='card'><summary><strong>Your Table's Public Roles &amp; Focus</strong></summary>"
            "<ul class='clean'>" + rows + "</ul></details>")


# ---------------------------------------------------------------------------
# Digital Course Cards: deal, one-for-one trade, and lock-on-close.
#
# A faithful digital version of the original paper flow. Each team is dealt one
# Course Card from a deck of the 10 canonical courses with two copies each (so
# two teams can hold the same course, exactly as the printed deck allowed). While
# the GM has course selection OPEN, any two teams may swap their held courses
# one-for-one -- but only if BOTH agree: one team proposes, the other accepts.
# When the GM CLOSES selection, every team's held course locks in as its course
# for the semester (see gm_course_close). The software now records the deal and
# every trade end to end; nothing is drawn, exchanged, or registered on paper.
# ---------------------------------------------------------------------------
def ensure_course_dealt(game, team):
    """Deal this team one Course Card from the shared deck (10 courses x 2 copies),
    once. Idempotent: a refresh never re-deals. If the deck is somehow exhausted
    (more than 20 teams), fall back to a random course so a live game never
    dead-ends."""
    if team.get("course_held"):
        return team["course_held"]
    deck = game.get("course_deck")
    if deck is None:
        deck = [c["name"] for c in COURSES] * 2
        random.shuffle(deck)
        game["course_deck"] = deck
    team["course_held"] = deck.pop() if deck else random.choice(COURSE_NAMES)
    return team["course_held"]


def lock_team_course(game, team):
    """Lock a team's held course as its course for the semester and enter the
    digital student-placement flow. Idempotent."""
    if team.get("course"):
        return
    team["course"] = ensure_course_dealt(game, team)
    ensure_dealt(team)


def _course_offers(game):
    return game.setdefault("course_offers", [])


def _cancel_offers_touching(game, *indices):
    idx = set(indices)
    game["course_offers"] = [o for o in _course_offers(game)
                             if o["frm"] not in idx and o["to"] not in idx]


def _course_hidden(game_code, team_index, name, value):
    return ("<input type='hidden' name='game_code' value='" + game_code + "'>"
            "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
            "<input type='hidden' name='" + name + "' value='" + e(str(value)) + "'>")


def course_details_html(course_name, compact=False):
    """All faculty-facing canonical course information rendered as readable HTML
    text (never relying on the artwork being legible). Compact mode drops the
    prerequisite/pattern/assessment lines for trade rows but keeps the facts that
    drive a trade decision. No hidden scoring is ever exposed -- COURSES carries
    none; resource weighting lives elsewhere and is not read here."""
    c = COURSE_BY_NAME[course_name]
    realities = "".join("<li>" + e(r) + "</li>" for r in c["realities"])
    meta = (e(c["code"]) + " &middot; " + e(c["modality"]) + " &middot; "
            + str(c["credits"]) + " credits")
    parts = ["<h3 class='course-name'>" + e(course_name) + "</h3>",
             "<p class='course-meta'>" + meta + "</p>",
             "<p>" + e(c["desc"]) + "</p>"]
    if not compact:
        parts.append("<p><strong>Prerequisite:</strong> " + e(c["prereq"]) + "</p>")
        parts.append("<p><strong>Meeting / learning pattern:</strong> " + e(c["pattern"]) + "</p>")
        parts.append("<p><strong>Assessment pattern:</strong> " + e(c["assessment"]) + "</p>")
    parts.append("<p><strong>Course Realities</strong> &mdash; neutral structural facts that can "
                 "shape how a later decision plays out:</p>"
                 "<ul class='course-realities'>" + realities + "</ul>")
    return "".join(parts)


def course_figure(course_name, zoom_id, size="md"):
    """Click/tap-to-enlarge Course Card artwork. Pure CSS (checkbox hack): the
    hidden checkbox is the focusable control (Space toggles), its label is the
    tappable thumbnail, and a second label is the full-screen overlay with an
    obvious Close control. Works with JavaScript disabled."""
    src = e(course_image_src(course_name))
    thumb_alt = e(course_name) + " Course Card - activate to enlarge"
    big_alt = e(course_name) + " Course Card, enlarged"
    return (
        "<span class='zoom zoom-" + size + "'>"
        "<input type='checkbox' id='" + zoom_id + "' class='zoom-cb'>"
        "<label for='" + zoom_id + "' class='zoom-thumb'>"
        "<img src='" + src + "' alt='" + thumb_alt + "' loading='lazy'>"
        "<span class='zoom-hint'>Tap or press Space to enlarge</span></label>"
        "<label for='" + zoom_id + "' class='zoom-overlay'>"
        "<span class='zoom-close'>Close &#10005;</span>"
        "<img src='" + src + "' alt='" + big_alt + "'></label>"
        "</span>")


def course_hero(course_name):
    """The team's own course as the visual focus: large enlargeable artwork beside
    the complete course information as readable HTML text."""
    return ("<div class='card'><h2>Your Course</h2>"
            "<div class='course-hero'>"
            "<div class='course-hero-art'>" + course_figure(course_name, "zoom-your", "lg") + "</div>"
            "<div class='course-hero-text'>" + course_details_html(course_name) + "</div>"
            "</div></div>")


def course_trade_screen(game_code, team_index, team):
    """The whole digital course-selection/trading experience on one screen:
    shows the team's dealt Course Card, a clear waiting state when selection is
    not open or already closed, and -- while open -- one-for-one trade controls
    with explicit 'your move' / 'waiting on them' states."""
    game = games[game_code]
    phase = game.get("course_trading", "closed")

    # Selection already closed: lock whatever this team holds and move on. Normal
    # games lock every team at close; this covers a team that arrives afterward.
    if phase == "closed":
        lock_team_course(game, team)
        return resume_screen(game_code, team_index, team)

    ensure_course_dealt(game, team)
    refresh = team_footer(game_code, team_index)
    # The full course card (large artwork + complete readable details) is the focus
    # of every state, including the waiting states below.
    hero = course_hero(team["course_held"])

    if phase == "not_open":
        body = (hero
                + "<div class='card'><p>This is the course your team has been dealt &mdash; review it "
                "in full above while you wait.</p>"
                "<p class='muted'>Course selection has not opened yet. When the Game Master opens it, "
                "you will be able to propose a one-for-one trade with another team.</p></div>")
        return page("Your Course - " + team["name"],
                    body + refresh + live_reload_script(game_code, team_index), width="wide")

    # phase == "open": trade controls.
    teams = game["teams"]
    offers = _course_offers(game)
    my_outgoing = next((o for o in offers if o["frm"] == team_index), None)
    incoming = [o for o in offers if o["to"] == team_index]
    sections = [hero]

    # Incoming offers to respond to (your move). The offered course is shown at a
    # readable size with its full-text details so faculty can inspect before
    # accepting.
    if incoming:
        rows = ""
        for o in incoming:
            frm = teams[o["frm"]]
            rows += (
                "<div class='trade-row offer-in'>"
                "<div class='trade-art'>"
                + course_figure(frm["course_held"], "zoom-in-" + str(o["id"]), "md")
                + "</div><div class='tr-body'>"
                "<p><strong>" + e(frm["name"]) + "</strong> offers you this course:</p>"
                + course_details_html(frm["course_held"], compact=True)
                + "<p class='muted'>If you accept, you give up your current course, <strong>"
                + e(team["course_held"]) + "</strong>.</p>"
                "<form method='post' action='/team/course/accept'>"
                + _course_hidden(game_code, team_index, "offer_id", o["id"])
                + "<button type='submit'>Accept trade</button></form> "
                "<form method='post' action='/team/course/decline'>"
                + _course_hidden(game_code, team_index, "offer_id", o["id"])
                + "<button class='secondary' type='submit'>Decline</button></form></div></div>")
        sections.append("<div class='card'><h3>Trade offers for you</h3>"
                        "<p class='muted'>Your move: inspect the offered course, then accept to swap "
                        "or decline.</p>" + rows + "</div>")

    # Your own outgoing offer (waiting on them) OR the list of teams to offer to.
    if my_outgoing is not None:
        dest = teams[my_outgoing["to"]]
        sections.append(
            "<div class='card'><h3>Trade proposed</h3>"
            "<div class='trade-row offer-wait'>"
            "<div class='trade-art'>"
            + course_figure(dest["course_held"], "zoom-wait-" + str(my_outgoing["id"]), "md")
            + "</div><div class='tr-body'>"
            "<p>You offered your <strong>" + e(team["course_held"]) + "</strong> for <strong>"
            + e(dest["name"]) + "</strong>'s course below:</p>"
            + course_details_html(dest["course_held"], compact=True)
            + "<p class='muted'>Waiting for " + e(dest["name"]) + " to accept or decline.</p>"
            "<form method='post' action='/team/course/cancel'>"
            + _course_hidden(game_code, team_index, "offer_id", my_outgoing["id"])
            + "<button class='secondary' type='submit'>Cancel offer</button></form></div></div></div>")
    else:
        others = [(i, t) for i, t in enumerate(teams) if i != team_index and t.get("course_held")]
        if others:
            rows = ""
            for i, t in others:
                rows += (
                    "<div class='trade-row'>"
                    "<div class='trade-art'>"
                    + course_figure(t["course_held"], "zoom-team-" + str(i), "md")
                    + "</div><div class='tr-body'>"
                    "<p class='muted'>Held by <strong>" + e(t["name"]) + "</strong></p>"
                    + course_details_html(t["course_held"], compact=True)
                    + "<form method='post' action='/team/course/propose'>"
                    + _course_hidden(game_code, team_index, "target", i)
                    + "<button type='submit'>Propose trade</button></form></div></div>")
            sections.append("<div class='card'><h3>Trade with another team</h3>"
                            "<p>Inspect another team's course below, then propose a one-for-one swap. "
                            "The other team must accept for the trade to happen. You can only have one "
                            "offer out at a time.</p>" + rows + "</div>")
        else:
            sections.append("<div class='card'><h3>Trade with another team</h3>"
                            "<p class='muted'>No other team has a course yet. You can keep the course "
                            "you were dealt (shown in full above).</p></div>")

    sections.append("<div class='card'><p class='muted'>Course selection is open. When the Game Master "
                    "closes it, the course you currently hold becomes your course for the semester.</p></div>")
    return page("Course Selection - " + team["name"],
                "".join(sections) + refresh + live_reload_script(game_code, team_index), width="wide")


@app.route("/team/course/propose", methods=["POST"])
def team_course_propose():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    target = request.form.get("target", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    ti = int(team_index)
    game = games[game_code]
    team = get_team(game_code, ti)
    teams = game["teams"]
    if (game.get("course_trading") == "open" and not team.get("course")
            and target.isdigit()):
        tj = int(target)
        if (0 <= tj < len(teams) and tj != ti
                and teams[tj].get("course_held") and team.get("course_held")):
            offers = _course_offers(game)
            # One outgoing offer at a time: replace any prior offer from this team.
            offers[:] = [o for o in offers if o["frm"] != ti]
            oid = game.get("offer_seq", 0) + 1
            game["offer_seq"] = oid
            offers.append({"id": oid, "frm": ti, "to": tj})
    return resume_screen(game_code, ti, team)


@app.route("/team/course/accept", methods=["POST"])
def team_course_accept():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    offer_id = request.form.get("offer_id", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    ti = int(team_index)
    game = games[game_code]
    team = get_team(game_code, ti)
    if game.get("course_trading") == "open" and offer_id.isdigit():
        o = next((x for x in _course_offers(game)
                  if x["id"] == int(offer_id) and x["to"] == ti), None)
        if o is not None:
            a, b = o["frm"], ti
            teams = game["teams"]
            teams[a]["course_held"], teams[b]["course_held"] = (
                teams[b]["course_held"], teams[a]["course_held"])
            # Both teams' holdings changed; any offer touching either is now stale.
            _cancel_offers_touching(game, a, b)
    return resume_screen(game_code, ti, team)


@app.route("/team/course/decline", methods=["POST"])
def team_course_decline():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    offer_id = request.form.get("offer_id", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    ti = int(team_index)
    game = games[game_code]
    team = get_team(game_code, ti)
    if offer_id.isdigit():
        game["course_offers"] = [x for x in _course_offers(game)
                                 if not (x["id"] == int(offer_id) and x["to"] == ti)]
    return resume_screen(game_code, ti, team)


@app.route("/team/course/cancel", methods=["POST"])
def team_course_cancel():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    offer_id = request.form.get("offer_id", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    ti = int(team_index)
    game = games[game_code]
    team = get_team(game_code, ti)
    if offer_id.isdigit():
        game["course_offers"] = [x for x in _course_offers(game)
                                 if not (x["id"] == int(offer_id) and x["frm"] == ti)]
    return resume_screen(game_code, ti, team)


# ---------------------------------------------------------------------------
# Student deal (15) and draft (10)
# ---------------------------------------------------------------------------
def parse_numbers(text, allowed):
    """Parse free text like '1 3 7, 12-15' into the matching student labels,
    restricted to `allowed` (labels formatted '#NN Name'), preserving order."""
    import re
    nums = set()
    for token in re.findall(r"\d+(?:-\d+)?", text or ""):
        if "-" in token:
            a, b = token.split("-")
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            nums.update(range(a, b + 1))
        else:
            nums.add(int(token))
    return [lab for lab in allowed if int(lab[1:3]) in nums]


def selected_from_form(allowed):
    """Combine checkbox selections and the Quick-entry number box into an
    ordered, de-duplicated list restricted to `allowed`. Lets number entry work
    even with JavaScript disabled."""
    chosen = set(request.form.getlist("students")) | set(parse_numbers(request.form.get("numbers", ""), allowed))
    return [lab for lab in allowed if lab in chosen]


def tile_grid(options, checked):
    if checked is None:
        checked = set()
    html = "<div class='tilegrid'>"
    for lab in options:
        num = int(lab[1:3])
        name = lab[4:]
        ck = " checked" if lab in checked else ""
        html += ("<label class='tile'><input type='checkbox' name='students' value='" + e(lab)
                 + "' data-num='" + str(num) + "'" + ck + ">"
                 "<span class='check'>Selected</span>"
                 "<span class='num'>#" + ("%02d" % num) + "</span>"
                 "<span class='nm'>" + e(name) + "</span>"
                 "<span class='ctx'>" + e(STUDENT_INFO[lab]["context"]) + "</span></label>")
    return html + "</div>"


def selection_bar(target):
    return ("<div class='selbar' id='selbar'>"
            "<span id='selcount'>Selected 0 of " + str(target) + "</span> "
            "<label>Quick entry (type card numbers, e.g. 1 3 7 or 12-15): "
            "<input type='text' id='numbers' name='numbers' autocomplete='off'></label> "
            "<button type='button' class='secondary' id='selclear'>Clear</button></div>")


def selection_script(target):
    return (
        "<script>(function(){var f=document.getElementById('selform');if(!f)return;"
        "var t=" + str(target) + ";var boxes=f.querySelectorAll('input[name=\"students\"]');"
        "var c=document.getElementById('selcount');var bar=document.getElementById('selbar');"
        "var nm=document.getElementById('numbers');"
        "function up(){var n=0;boxes.forEach(function(b){var l=b.closest('label');"
        "if(l)l.classList.toggle('sel',b.checked);if(b.checked)n++;});"
        "c.textContent='Selected '+n+' of '+t;c.className=(n===t)?'count-ok':'count-no';"
        "bar.className='selbar'+((n===t)?' ok':'');}"
        "boxes.forEach(function(b){b.addEventListener('change',up);});"
        "if(nm){nm.addEventListener('input',function(){var s={};"
        "(nm.value.match(/\\d+(?:-\\d+)?/g)||[]).forEach(function(k){"
        "if(k.indexOf('-')>-1){var p=k.split('-');var a=+p[0],b=+p[1];if(a>b){var z=a;a=b;b=z;}"
        "for(var i=a;i<=b;i++)s[i]=1;}else s[+k]=1;});"
        "boxes.forEach(function(b){b.checked=!!s[+b.getAttribute('data-num')];});up();});}"
        "var cl=document.getElementById('selclear');"
        "if(cl)cl.addEventListener('click',function(){boxes.forEach(function(b){b.checked=false;});"
        "if(nm)nm.value='';up();});up();})();</script>")


def pool_form(game_code, team_index, team, error_message="", checked=None):
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    body = (
        "<div class='card'><h2>Step 1: Register your dealt Student Cards</h2>"
        "<p>Course: <strong>" + e(team["course"]) + "</strong></p>"
        "<p>Record the <strong>" + str(DEALT_COUNT) + "</strong> students matching the physical cards your "
        "table dealt itself. Fastest: type the card numbers in Quick entry. You can also tap the cards.</p>"
        + error_html +
        "<form method='post' action='/team/pool' id='selform'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        + selection_bar(DEALT_COUNT)
        + tile_grid(STUDENTS, checked)
        + "<p><button type='submit'>Save Dealt Cards</button></p></form>"
        + selection_script(DEALT_COUNT) + "</div>"
    )
    return page("Student Deal", body)


@app.route("/team/pool", methods=["POST"])
def team_pool():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")

    team = get_team(game_code, int(team_index))
    selected = selected_from_form(STUDENTS)
    if len(selected) != DEALT_COUNT:
        return pool_form(game_code, int(team_index), team,
                         "Please record exactly " + str(DEALT_COUNT) + ". You have "
                         + str(len(selected)) + ".", set(selected))
    team["draft_pool"] = selected
    return roster_form(game_code, int(team_index), team)


def roster_form(game_code, team_index, team, error_message="", checked=None):
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    pool = team.get("draft_pool", [])
    body = (
        "<div class='card'><h2>Step 2: Draft your starting roster</h2>"
        "<p>These are the " + str(DEALT_COUNT) + " cards you registered. Choose exactly "
        + "<strong>" + str(ROSTER_COUNT) + "</strong> for your starting roster. The other "
        + str(DEALT_COUNT - ROSTER_COUNT) + " become Rejected Students. "
        "Type the card numbers to keep, or tap the cards.</p>" + error_html +
        "<form method='post' action='/team/roster' id='selform'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        + selection_bar(ROSTER_COUNT)
        + tile_grid(pool, checked)
        + "<p><button type='submit'>Save Starting Roster</button></p></form>"
        + selection_script(ROSTER_COUNT) + "</div>"
    )
    return page("Student Draft", body)


@app.route("/team/roster", methods=["POST"])
def team_roster():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")

    team = get_team(game_code, int(team_index))
    pool = team.get("draft_pool", [])
    selected = selected_from_form(pool)
    if len(selected) != ROSTER_COUNT:
        return roster_form(game_code, int(team_index), team,
                           "Please choose exactly " + str(ROSTER_COUNT) + ". You have "
                           + str(len(selected)) + ".", set(selected))

    roster = [s for s in pool if s in selected]
    rejected = [s for s in pool if s not in selected]
    team["starting_roster"] = roster
    team["active_roster"] = list(roster)  # late adds will extend this at CP2
    team["rejected_students"] = rejected

    scores, statuses, overall = compute_scores(roster)
    team["resources"] = dict(scores)
    team["start_resources"] = dict(scores)
    team["statuses"] = statuses
    team["overall"] = overall
    team["start_overall"] = overall

    body = (
        "<div class='card'><h2>Starting Conditions for " + e(team["name"]) + "</h2>"
        "<div class='notice'><p>" + STARTING_PREAMBLE + "</p>"
        "<p><strong>Your roster pattern:</strong> " + roster_interpretation(roster) + "</p></div></div>"
        + resource_meters(scores, statuses)
        + "<div class='card'><p><strong>Overall Course Score: " + str(overall) + "</strong></p></div>"
        "<div class='card'><h3>Starting Roster (" + str(len(roster)) + ")</h3>" + student_list_html(roster)
        + "<h3>Rejected Students (" + str(len(rejected)) + ")</h3>" + student_list_html(rejected)
        + "<p>Roster locked. Continue to the next setup step.</p>"
        + "<p><a class='btn' href='" + status_url(game_code, int(team_index)) + "'>Continue</a></p></div>"
    )
    return page("Starting Conditions", body)


@app.route("/team/status")
def team_status():
    game_code = request.args.get("game_code", "").strip().upper()
    team_index = request.args.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    return resume_screen(game_code, int(team_index), team)


def status_url(game_code, team_index, embed=False):
    url = "/team/status?game_code=" + game_code + "&team_index=" + str(team_index)
    return url + "&embed=1" if embed else url


def workspace_url(game_code, team_index):
    return "/team/workspace?game_code=" + game_code + "&team_index=" + str(team_index)


def _embedded():
    """True when a workspace screen is being rendered inside a /team/workspace
    split pane (the iframes pass embed=1, and public POST forms echo it). This is
    layout-only: it changes navigation chrome, never game logic or state."""
    try:
        return request.values.get("embed") == "1"
    except RuntimeError:
        # Called outside a request (e.g. a unit test rendering a form directly).
        return False


def _embed_hidden():
    """A hidden embed field so a public POST (e.g. a checkpoint decision) keeps
    the re-rendered screen inside its split pane. Omitted for private forms,
    which deliberately break out to the full window."""
    return "<input type='hidden' name='embed' value='1'>" if _embedded() else ""


def team_setup_complete(team):
    """True once a team is in active gameplay (where resume_screen falls through
    to the status/checkpoint screen). Used only to decide navigation chrome."""
    if not team.get("course"):
        return False
    if team.get("deal_version") == 2:
        return bool(team.get("placement_acked"))
    return bool(team.get("starting_roster")) and handoff_done(team)


def help_script():
    """Progressive enhancement for the help control: submit via fetch so toggling
    help never reloads the page or disturbs an in-progress selection. With no
    fetch support the forms post normally and the server re-renders the screen."""
    return (
        "<script>(function(){var box=document.getElementById('helpbox');if(!box)return;"
        "function wire(f){if(!f)return;f.addEventListener('submit',function(ev){"
        "if(!window.fetch)return;ev.preventDefault();"
        "fetch(f.action,{method:'POST',body:new FormData(f),headers:{'X-Requested-With':'fetch'}})"
        ".then(function(r){return r.json();}).then(function(j){if(j&&j.ok){"
        "box.classList.toggle('on',!!j.active);}}).catch(function(){});});}"
        "wire(box.querySelector('.help-idle form'));wire(box.querySelector('.help-active-wrap form'));"
        # Sync the help state (e.g. the GM resolved it) without reloading, so the
        # team's current screen and any in-progress selection are never disturbed.
        "var g=box.getAttribute('data-game'),t=box.getAttribute('data-team');"
        "if(window.fetch&&g&&t){var u='/live/game?game_code='+encodeURIComponent(g)+'&team_index='+encodeURIComponent(t);"
        "setInterval(function(){fetch(u,{headers:{'X-Requested-With':'fetch'}}).then(function(r){return r.json();})"
        ".then(function(j){if(j){if(typeof j.help==='boolean'){box.classList.toggle('on',j.help);}"
        "var pb=document.getElementById('pausebanner');if(pb&&typeof j.paused==='boolean'){"
        "pb.style.display=j.paused?'':'none';}}}).catch(function(){});},5000);}"
        "})();</script>")


def help_widget(game_code, team_index, team):
    """A 'Request GM Help' control for PUBLIC team screens (setup, course
    selection, roster selection, checkpoint play, waiting). Both states render so
    fetch can toggle without a reload. Deliberately never placed on private
    allegiance / mission / vote / sabotage screens."""
    hid = ("<input type='hidden' name='game_code' value='" + game_code + "'>"
           "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
           "<input type='hidden' name='ajax' value='1'>")
    idle = ("<div class='help-idle'><form method='post' action='/team/help'>" + hid
            + "<button type='submit' class='help-req-btn'>Request GM Help</button></form></div>")
    onblk = ("<div class='help-active-wrap'><span class='help-badge'>Help requested</span>"
             "<span class='muted'> The Game Master has been notified.</span> "
             "<form method='post' action='/team/help/cancel' style='display:inline'>" + hid
             + "<button type='submit' class='secondary'>Cancel request</button></form></div>")
    paused = bool(games.get(game_code, {}).get("paused"))
    banner = ("<div class='pausebanner' id='pausebanner'" + ("" if paused else " style='display:none'")
              + " role='status'>Paused for discussion &mdash; please pause your work and listen to the "
              "Game Master.</div>")
    return (banner + "<div class='helpbox" + (" on" if help_active(team) else "") + "' id='helpbox'"
            " data-game='" + game_code + "' data-team='" + str(team_index) + "'>"
            + idle + onblk + "</div>" + help_script())


def live_reload_script(game_code, team_index):
    """Reload a WAITING team screen when its team_rev changes (GM opened a
    checkpoint, a trade arrived, reassignment ran). Only injected on screens with
    no unsent inputs; never on the placement selection or private screens.
    Degrades silently on a dropped connection."""
    cur = team_rev(games[game_code], team_index)
    url = "/live/game?game_code=" + game_code + "&team_index=" + str(team_index)
    return ("<script>(function(){var cur=" + json.dumps(cur) + ";"
            "function tick(){fetch(" + json.dumps(url) + ",{headers:{'X-Requested-With':'fetch'}})"
            ".then(function(r){return r.json();}).then(function(j){"
            "if(j&&typeof j.rev==='string'&&j.rev&&j.rev!==cur){location.reload();}})"
            ".catch(function(){});}setInterval(tick,4000);})();</script>")


def team_waiting_note(game):
    """Concise, participant-friendly room-progress line. Public-appropriate
    counts only -- never other teams' choices or any private information."""
    prog = room_progress(game)
    if prog["all_ready"]:
        inner = ("<p class='big'>All teams are ready.</p>"
                 "<p>Waiting for the Game Master to begin the next step.</p>")
    else:
        inner = ("<p class='big'>Your team is finished.</p>"
                 "<p>Waiting for other teams &mdash; " + str(prog["done"]) + " of "
                 + str(prog["total"]) + " ready.</p>")
    return "<div class='card center teamwait'>" + inner + "</div>"


def team_waiting_tail(game_code, team_index, team):
    """team_waiting_note plus live auto-advance, appended once a team has finished
    the current activity. Reloads only when the GM moves the room forward
    (team_rev change). Call ONLY on a screen with no unsent inputs. Never crosses a
    checkpoint boundary by itself -- it just reloads into whatever screen
    resume_screen then selects."""
    return team_waiting_note(games[game_code]) + live_reload_script(game_code, team_index)


def checkpoint_done_tail(game_code, team_index, team):
    """team_waiting_tail for the open-checkpoint screen, but only once this team
    has fully finished (decision submitted AND any private round complete), so the
    auto-reload is never added while a private turn or a decision form is live."""
    if team_activity_done(games[game_code], team_index, team):
        return team_waiting_tail(game_code, team_index, team)
    return ""


def team_footer(game_code, team_index):
    note = "<em>You cannot advance yourself. The Game Master controls the room.</em><br>"
    team = get_team(game_code, team_index)
    help_html = help_widget(game_code, team_index, team) if team is not None else ""
    if _embedded():
        # Inside a split pane: refresh reloads this pane only (keep embed); the
        # container supplies Home/navigation, so we add nothing that could load
        # the wrong pane.
        return (help_html + "<p class='footer'>" + note + "<a class='btn secondary' href='"
                + status_url(game_code, team_index, embed=True) + "'>Refresh</a></p>")
    links = "<a class='btn secondary' href='" + status_url(game_code, team_index) + "'>Refresh</a> "
    if team is not None and team_setup_complete(team):
        links += ("<a class='btn' href='" + workspace_url(game_code, team_index)
                  + "'>Split view (Canvas + Workspace)</a> ")
    links += "<a href='/'>Home</a>"
    return help_html + "<p class='footer'>" + note + links + "</p>"


@app.route("/team/workspace")
def team_workspace():
    """Split-screen entry point for active gameplay: Canvas on the left, the team
    decision/status screen on the right, each an iframe onto its existing route.
    No game logic here -- purely a layout container."""
    game_code = request.args.get("game_code", "").strip().upper()
    team_index = request.args.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    ti = int(team_index)
    team = get_team(game_code, ti)
    # Setup (including its full-screen private reveals) must never be split; until
    # a team is in active play, fall back to the normal full-page flow.
    if not team_setup_complete(team):
        return resume_screen(game_code, ti, team)
    return workspace_container(game_code, ti, team)


def workspace_container(game_code, team_index, team):
    shell_src = e(shell_url(game_code, team_index, "home") + "&embed=1")
    work_src = e(status_url(game_code, team_index, embed=True))
    # The narrow-screen toggle is a pure-CSS radio group (the "checkbox hack"), so
    # it works with JavaScript DISABLED: the radios/labels are native form
    # controls, and :checked drives which pane is shown. On laptop/desktop widths
    # both panes always show and the toggle is hidden.
    css = (
        "<style>"
        ".ws-wrap{display:flex;flex-direction:column;height:100vh;height:100dvh;}"
        ".ws-vradio{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);}"
        ".ws-tabs{display:none;border-bottom:1px solid #c7cdd1;}"
        ".ws-tabs label{flex:1;padding:12px;text-align:center;background:#edf0f2;font-weight:700;"
        "cursor:pointer;color:#2d3b45;}"
        "#v-canvas:checked~.ws-tabs label[for=\"v-canvas\"],"
        "#v-work:checked~.ws-tabs label[for=\"v-work\"]{background:#fff;box-shadow:inset 0 -3px 0 var(--red);}"
        "#v-canvas:focus-visible~.ws-tabs label[for=\"v-canvas\"],"
        "#v-work:focus-visible~.ws-tabs label[for=\"v-work\"]{outline:2px solid var(--red);outline-offset:-2px;}"
        ".ws-panes{display:flex;flex:1;min-height:0;}"
        ".ws-pane{display:flex;flex-direction:column;min-width:0;min-height:0;}"
        ".ws-canvas{flex:1.15;border-right:1px solid #c7cdd1;}"
        ".ws-work{flex:1;}"
        ".ws-pane>h2{margin:0;padding:9px 14px;font-size:.95rem;letter-spacing:.02em;"
        "background:#394B58;color:#fff;}"
        ".ws-pane>iframe{flex:1;width:100%;border:0;background:#fff;}"
        "@media(max-width:899px){.ws-tabs{display:flex;}"
        ".ws-pane{flex:1 1 100%;border-right:0;display:none;}"
        "#v-canvas:checked~.ws-panes .ws-canvas{display:flex;}"
        "#v-work:checked~.ws-panes .ws-work{display:flex;}}"
        "</style>"
    )
    panes = (
        "<div class='ws-wrap'>"
        "<input type='radio' name='wsv' id='v-canvas' class='ws-vradio'>"
        "<input type='radio' name='wsv' id='v-work' class='ws-vradio' checked>"
        "<div class='ws-tabs' role='radiogroup' aria-label='Choose view: Canvas or Team Workspace'>"
        "<label for='v-canvas'>Canvas Classroom</label>"
        "<label for='v-work'>Team Workspace</label>"
        "</div>"
        "<div class='ws-panes'>"
        "<section id='ws-canvas' class='ws-pane ws-canvas' aria-label='Canvas Classroom'>"
        "<h2>Canvas Classroom</h2>"
        "<iframe title='Canvas Classroom' src='" + shell_src + "'></iframe></section>"
        "<section id='ws-work' class='ws-pane ws-work' aria-label='Team Workspace'>"
        "<h2>Team Workspace</h2>"
        "<iframe title='Team Workspace' src='" + work_src + "'></iframe></section>"
        "</div></div>"
        "<noscript><p style='padding:8px 14px;margin:0;background:#fff;color:#2d3b45'>"
        "On a small screen, use the <strong>Canvas Classroom</strong> / <strong>Team Workspace</strong> "
        "buttons above to switch views.</p></noscript>"
    )
    return page("Team Workspace - " + team["name"], css + panes, screen="shell", bare=True)


def status_screen(game_code, team_index, team):
    """Live waiting/checkpoint screen for a fully-set-up team (pre-final)."""
    game = games[game_code]
    index = game["checkpoint_index"]
    number = index + 1
    name = CHECKPOINTS[index]

    if game["checkpoint_open"]:
        return checkpoint_screen(game_code, team_index, team)
    if teaching_active(game):
        # The GM has started the teaching discussion for this checkpoint. Teams
        # see a neutral pointer to the projector -- never the question, the
        # strategies, or any facilitator content.
        body = ("<div class='card center teamwait'><h2>Discussion in progress</h2>"
                "<p class='big'>Please look at the projector.</p>"
                "<p class='muted'>Your decisions are locked while the group discusses this checkpoint.</p></div>")
    elif game["opened_once"]:
        # Between checkpoints: this team is finished; show room-wide progress.
        body = (team_waiting_note(game)
                + "<div class='card'><h2>Checkpoint " + str(number) + ": " + name + " &mdash; Locked</h2>"
                "<p>Your decisions are locked.</p></div>")
    else:
        body = ("<div class='card center'><h2>Waiting for the Game Master</h2>"
                "<p class='big'>Waiting for the Game Master to begin Checkpoint "
                + str(number) + ".</p>"
                "<p class='muted'>" + e(name) + "</p></div>")
    body = ("<div class='card'><h3>" + e(team["name"]) + " &mdash; " + e(team["course"]) + "</h3>"
            "<p>" + open_course_button(game_code, team_index) + "</p>"
            + resources_drawer(team) + "</div>"
            + resource_meters(team["resources"], team["statuses"])
            + team_roles_panel(team) + body)
    # This branch is only a WAITING state (the open-checkpoint case returned
    # above), so it is safe to auto-reload when the GM advances the room.
    return page("Team Screen - " + team["name"],
                body + team_footer(game_code, team_index)
                + live_reload_script(game_code, team_index))


# ---------------------------------------------------------------------------
# Rejected-student handoff (synchronized setup phase)
# ---------------------------------------------------------------------------
def handoff_done(team):
    h = team.get("handoff", {})
    return bool(h.get("pass_confirmed")) and bool(h.get("receive_confirmed"))


def handoff_screen(game_code, team_index, team):
    teams = games[game_code]["teams"]
    n = len(teams)
    heading = "<div class='card'><h2>Rejected Student Handoff</h2>"
    refresh = team_footer(game_code, team_index)

    if n <= 1:
        body = (heading + "<p><strong>Test mode:</strong> only one team is in this game, so there is no "
                "other team to pass to or receive from. No physical handoff is simulated.</p>"
                "<form method='post' action='/team/handoff/testmode'>"
                "<input type='hidden' name='game_code' value='" + game_code + "'>"
                "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
                "<button type='submit'>Continue (test mode)</button></form></div>")
        return page("Handoff - " + team["name"], body + refresh)

    dest_team = teams[(team_index + 1) % n]
    source_team = teams[(team_index - 1) % n]
    h = team.get("handoff", {})

    if not h.get("pass_confirmed"):
        body = (heading + "<p>Your team did not place these five students on your Starting Roster.</p>"
                + student_list_html(team.get("rejected_students", []))
                + "<p>Pass these five physical Student Cards to <strong>" + e(dest_team["name"]) + "</strong>.</p>"
                "<form method='post' action='/team/handoff/pass'>"
                "<input type='hidden' name='game_code' value='" + game_code + "'>"
                "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
                "<button type='submit'>Cards Passed</button></form></div>")
        return page("Handoff - " + team["name"], body + refresh)

    if not source_team.get("handoff", {}).get("pass_confirmed"):
        body = (heading + "<p>You have passed your Rejected Student Cards to " + e(dest_team["name"]) + ".</p>"
                "<p class='muted'>Waiting for <strong>" + e(source_team["name"])
                + "</strong> to pass your incoming Student Cards.</p></div>")
        return page("Handoff - " + team["name"], body + refresh)

    body = (heading + "<h3>Incoming Student Cards</h3>"
            "<p>You should receive these five physical Student Cards from <strong>"
            + e(source_team["name"]) + "</strong>.</p>"
            + student_list_html(source_team.get("rejected_students", []))
            + "<form method='post' action='/team/handoff/receive'>"
            "<input type='hidden' name='game_code' value='" + game_code + "'>"
            "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
            "<button type='submit'>Cards Received</button></form></div>")
    return page("Handoff - " + team["name"], body + refresh)


@app.route("/team/handoff/pass", methods=["POST"])
def team_handoff_pass():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    index = int(team_index)
    teams = games[game_code]["teams"]
    h = team.setdefault("handoff", {})
    if not h.get("pass_confirmed"):
        h["pass_confirmed"] = True
        h["pass_to"] = teams[(index + 1) % len(teams)]["name"]
    return resume_screen(game_code, index, team)


@app.route("/team/handoff/receive", methods=["POST"])
def team_handoff_receive():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    index = int(team_index)
    teams = games[game_code]["teams"]
    source_team = teams[(index - 1) % len(teams)]
    if not source_team.get("handoff", {}).get("pass_confirmed"):
        return handoff_screen(game_code, index, team)
    h = team.setdefault("handoff", {})
    if not h.get("receive_confirmed"):
        h["received"] = list(source_team.get("rejected_students", []))
        h["source"] = source_team["name"]
        h["receive_confirmed"] = True
    return resume_screen(game_code, index, team)


@app.route("/team/handoff/testmode", methods=["POST"])
def team_handoff_testmode():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    index = int(team_index)
    if not handoff_done(team):
        team["handoff"] = {"pass_confirmed": True, "receive_confirmed": True,
                           "received": [], "pass_to": None, "source": None, "test_mode": True}
    return resume_screen(game_code, index, team)


def received_students(team):
    """The five incoming cards (empty in single-team test mode)."""
    return team.get("handoff", {}).get("received", [])


# ===========================================================================
# Thunderflunked 2.0 digital student placement
#   Phase 1  Auto-deal 15 distinct students per team (independent deals).
#   Phase 2  Keep 10, refer 5 for reassignment (tap/click to select); lock.
#   Phase 3  Pool every team's 5 released, then redistribute 5 to each team so
#            that no team receives any character from its own original 15, using
#            a randomized max-flow matching that never strands the last team.
#   Phase 4  Final class = 10 retained + 5 reassigned = 15, scored as a whole.
#
# FEASIBILITY (why there is a backfill step):
#   With 30 characters, deals of 15, and teams free to release any 5, the pooled
#   redistribution alone is NOT always possible. Let deg(c) = number of deals
#   containing character c. Adversarial teams can release every copy of c, and
#   those copies can only land on the T - deg(c) teams that exclude c, so we need
#   deg(c) <= T - deg(c), i.e. deg(c) <= floor(T/2), for every c. But the degrees
#   sum to 15*T, so some c has deg(c) >= ceil(15*T/30) = ceil(T/2). For ODD team
#   counts ceil(T/2) > floor(T/2): a dead-end is unavoidable no matter how deals
#   are generated. For EVEN counts the bound is only met by a perfectly balanced
#   design, which (a) is still not sufficient in general (a uniform T=4 design
#   has adversarial dead-ends) and (b) cannot be built here anyway, since each
#   team is dealt independently when it registers its course, before the final
#   team count is known. So instead of banning the dead-end we remove its
#   consequence: place as many pooled students as possible, then top up any
#   shortfall from a team's un-dealt canonical pool (always >= 15 available).
#   This keeps the exclusion rule, no in-team duplicates, no new characters, and
#   no re-picking. A released student with nowhere valid to go simply transfers
#   out (is not placed) rather than blocking the game.
# ===========================================================================
REASSIGN_COUNT = DEALT_COUNT - ROSTER_COUNT   # 5 released / 5 incoming


def ensure_dealt(team):
    """Phase 1: give the team 15 distinct students from the canonical 30, once.
    Independent per team (characters may recur across teams). Idempotent: a
    refresh never redeals because the stored deal is returned unchanged."""
    if not team.get("dealt"):
        team["dealt"] = random.sample(STUDENTS, DEALT_COUNT)
        team["deal_version"] = 2
    return team["dealt"]


def _maxflow_reassignment(released_by_team, dealt_by_team):
    """Phase 3 matching. Returns a list (one per team) of UP TO REASSIGN_COUNT
    labels drawn from the shared released pool, placing as many as the exclusion
    and no-duplicate rules allow (a MAXIMUM matching). The caller tops up any
    team left short from that team's un-dealt canonical pool, so a live game can
    never dead-end (see attempt_reassignment and the feasibility analysis in the
    module header).

    Every label a team gets here honors both hard rules: it is not in that team's
    own deal, and no character is repeated within a team. Modeled as integer
    max-flow: Source -> character (cap = #released instances) -> team (cap 1, only
    if not in that team's deal) -> Sink (cap REASSIGN_COUNT). Neighbour order is
    randomized so the matching (and thus any shortfall) is random, not biased to
    the last team."""
    T = len(released_by_team)
    need = REASSIGN_COUNT
    supply = {}
    for released in released_by_team:
        for lab in released:
            supply[lab] = supply.get(lab, 0) + 1
    dealt_sets = [set(d) for d in dealt_by_team]

    S, K = "__S__", "__K__"
    cap = {}

    def add(u, v, c):
        du = cap.setdefault(u, {})
        du[v] = du.get(v, 0) + c
        cap.setdefault(v, {}).setdefault(u, 0)

    for lab, c in supply.items():
        add(S, ("c", lab), c)
    for i in range(T):
        for lab in supply:
            if lab not in dealt_sets[i]:
                add(("c", lab), ("t", i), 1)
        add(("t", i), K, need)

    def augment():
        parent = {S: None}
        queue = [S]
        while queue:
            u = queue.pop(0)
            nbrs = [v for v, c in cap.get(u, {}).items() if c > 0 and v not in parent]
            random.shuffle(nbrs)
            for v in nbrs:
                parent[v] = u
                if v == K:
                    # Walk back, push 1 unit (every edge on any S->K path is >=1).
                    node = K
                    while parent[node] is not None:
                        p = parent[node]
                        cap[p][node] -= 1
                        cap[node][p] = cap[node].get(p, 0) + 1
                        node = p
                    return True
                queue.append(v)
        return False

    while augment():
        pass

    # Best-effort: whatever the maximum matching placed (each team gets 0..need).
    assign = []
    for i in range(T):
        labs = [lab for lab in supply
                if lab not in dealt_sets[i] and cap[("c", lab)].get(("t", i), 0) == 0]
        assign.append(labs)
    return assign


def finalize_team_placement(team, pooled, backfill):
    """Phase 4: build the 15-student final class and score it as a whole.

    `pooled` are students reassigned from the shared rejection pool (other teams'
    referrals); `backfill` are administrative placements from the team's un-dealt
    canonical pool. Their order (pool first) is preserved so the final class and
    the Canvas roster can label each student's origin. starting_roster stays the
    10 retained (narrative cast is drawn from it); active_roster is the full 15
    and stays visible all semester; late_adds holds the 5 reassigned so the shell
    and CP2 intake label them as transfers (display only, never a score)."""
    pooled = list(pooled)
    backfill = list(backfill)
    reassigned = pooled + backfill
    retained = list(team["starting_roster"])
    final_roster = retained + reassigned
    team["reassigned"] = reassigned
    team["reassigned_pool"] = pooled
    team["reassigned_backfill"] = backfill
    team["final_roster"] = final_roster
    team["active_roster"] = list(final_roster)
    team["late_adds"] = list(reassigned)

    scores, statuses, overall = compute_scores(final_roster)
    team["resources"] = dict(scores)
    team["start_resources"] = dict(scores)
    team["statuses"] = statuses
    team["overall"] = overall
    team["start_overall"] = overall


def reassignment_ready(game):
    """All teams in the game have locked their retain/release decision."""
    teams = game.get("teams", [])
    return bool(teams) and all(t.get("roster_confirmed") for t in teams)


def attempt_reassignment(game_code):
    """Phase 3 trigger. Runs once, atomically, when every team is finalized.
    Validates feasibility before mutating any team; on failure it records a
    safe error and leaves all state untouched (never invents students, never
    bypasses the exclusion rule)."""
    game = games[game_code]
    if game.get("reassignment_done"):
        return
    teams = game["teams"]
    if not reassignment_ready(game):
        return

    if len(teams) == 1:
        # Single-team test mode: there is no other table to pool from, so the
        # only released students are this team's own -- all excluded by the
        # rule. We draw the 5 replacements from the canonical characters this
        # team was NOT originally dealt (15 are available). This invents no new
        # characters and still honors the exclusion rule.
        t = teams[0]
        pool = [s for s in STUDENTS if s not in set(t["dealt"])]
        # No other table to pool from: all five are administrative placements.
        finalize_team_placement(t, [], random.sample(pool, REASSIGN_COUNT))
        t["reassignment_mode"] = "single_team_pool"
        game["reassignment_done"] = True
        game["reassignment_error"] = None
        return

    assign = _maxflow_reassignment([t["released"] for t in teams],
                                   [t["dealt"] for t in teams])
    # Place as many pooled students as the rules allow, then top up any team the
    # shared pool could not fully cover from that team's un-dealt canonical pool.
    # Because a team excludes only its own 15, at least 30-15=15 characters are
    # always available to it, so 5 can always be reached: a live game can never
    # dead-end. Backfill honors both hard rules (not in the team's deal; no
    # duplicate character) and invents no new characters. See the module header
    # analysis for why no deal-generation scheme can avoid this for odd team
    # counts (and why even counts cannot be guaranteed in the live, independent
    # deal flow either).
    for t, pooled in zip(teams, assign):
        pooled = list(pooled)
        backfill = []
        if len(pooled) < REASSIGN_COUNT:
            excluded = set(t["dealt"]) | set(pooled)
            backfill_pool = [s for s in STUDENTS if s not in excluded]
            backfill = random.sample(backfill_pool, REASSIGN_COUNT - len(pooled))
            t["reassignment_mode"] = "shared_pool_plus_backfill"
        else:
            t["reassignment_mode"] = "shared_pool"
        finalize_team_placement(t, pooled, backfill)
    game["reassignment_done"] = True
    game["reassignment_error"] = None


# ---------------------------------------------------------------------------
# Phase 1 + 2 screen: review the 15 dealt students, retain 10, refer 5.
# ---------------------------------------------------------------------------
def placement_count_bar(target):
    """A persistent, high-visibility 'X of N students selected' counter. Live JS
    is progressive enhancement only -- with JavaScript off the server still
    validates the exact count on submit."""
    return ("<div class='selcount-bar' id='selcount-bar'>"
            "<span><span class='n' id='selcount-n'>0</span> of " + str(target)
            + " students selected</span>"
            "<span class='muted' style='font-size:.85rem;font-weight:400'>"
            "Tap or click a card to keep that student; the other "
            + str(DEALT_COUNT - target) + " are referred automatically. "
            "Keyboard: Tab to a card, then Space to toggle.</span></div>")


def placement_select_script(target):
    """Live counter + double-submit guard. The submit button is enabled only once
    exactly `target` cards are selected and is disabled on submit so a double tap
    cannot send the decision twice. All optional -- selection itself is native
    checkbox behavior that needs no JavaScript."""
    return (
        "<script>(function(){var f=document.getElementById('selform');if(!f)return;"
        "var t=" + str(target) + ";var boxes=f.querySelectorAll('input[name=\"students\"]');"
        "var n=document.getElementById('selcount-n');var bar=document.getElementById('selcount-bar');"
        "var btn=document.getElementById('placement-submit');"
        "function up(){var c=0;boxes.forEach(function(b){if(b.checked)c++;});"
        "if(n)n.textContent=c;if(bar)bar.className='selcount-bar'+(c===t?' ok':'');"
        "if(btn)btn.disabled=(c!==t);}"
        "boxes.forEach(function(b){b.addEventListener('change',up);});up();"
        "f.addEventListener('submit',function(){if(btn){btn.disabled=true;btn.textContent='Saving\\u2026';}});"
        "})();</script>")


def roster_decision_screen(game_code, team_index, team, error_message="",
                           checked=None):
    ensure_dealt(team)
    dealt = team["dealt"]
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    grid = ("<div class='cardgrid'>"
            + "".join(student_mini_card(s, selectable=True, checked=bool(checked and s in checked))
                      for s in dealt)
            + "</div>")
    body = (
        "<div class='card'><h2>Placement Review &mdash; " + e(team["name"]) + "</h2>"
        "<p>Course: <strong>" + e(team["course"]) + "</strong></p>"
        "<p>You were dealt <strong>" + str(DEALT_COUNT) + "</strong> students. Keep exactly "
        "<strong>" + str(ROSTER_COUNT) + "</strong> &mdash; the other <strong>"
        + str(REASSIGN_COUNT) + "</strong> are automatically referred for reassignment. Referred "
        "students join a shared pool and are redistributed to other teams; you will receive "
        + str(REASSIGN_COUNT) + " students in return.</p>" + error_html +
        "<form method='post' action='/team/retain' id='selform'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        + placement_count_bar(ROSTER_COUNT)
        + grid
        + "<p><button type='submit' id='placement-submit'>Review selection</button></p></form>"
        + placement_select_script(ROSTER_COUNT) + "</div>")
    return page("Placement Review - " + team["name"], body + help_widget(game_code, team_index, team), width="wide")


def retain_confirm_screen(game_code, team_index, team, retain, release):
    retained = "".join(student_mini_card(s, badge="Keep") for s in retain)
    released = "".join(student_mini_card(s, badge="Refer") for s in release)
    hidden = ("<input type='hidden' name='game_code' value='" + game_code + "'>"
              "<input type='hidden' name='team_index' value='" + str(team_index) + "'>")
    body = (
        "<div class='card'><h2>Confirm Placement &mdash; " + e(team["name"]) + "</h2>"
        "<p>Review carefully. <strong>After you confirm, this decision locks.</strong></p>"
        "<h3>Keep (" + str(len(retain)) + ")</h3><div class='cardgrid'>" + retained + "</div>"
        "<h3>Refer for reassignment (" + str(len(release)) + ")</h3>"
        "<div class='cardgrid'>" + released + "</div>"
        "<form method='post' action='/team/retain/confirm' style='display:inline' id='lockform'>" + hidden
        + "<button type='submit' id='lockbtn'>Confirm &amp; Lock</button></form> "
        "<form method='post' action='/team/retain/edit' style='display:inline'>" + hidden
        + "<button class='secondary' type='submit'>Go Back</button></form></div>"
        "<script>(function(){var f=document.getElementById('lockform');if(!f)return;"
        "f.addEventListener('submit',function(){var b=document.getElementById('lockbtn');"
        "if(b){b.disabled=true;b.textContent='Locking\\u2026';}});})();</script>")
    return page("Confirm Placement - " + team["name"], body + help_widget(game_code, team_index, team), width="wide")


@app.route("/team/retain", methods=["POST"])
def team_retain():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if team.get("roster_confirmed"):
        return resume_screen(game_code, int(team_index), team)
    dealt = team.get("dealt", [])
    selected = selected_from_form(dealt)
    if len(selected) != ROSTER_COUNT:
        return roster_decision_screen(game_code, int(team_index), team,
                                      "Please keep exactly " + str(ROSTER_COUNT) + ". You have "
                                      + str(len(selected)) + ".", set(selected))
    retain = [s for s in dealt if s in selected]
    release = [s for s in dealt if s not in selected]
    # Hold the proposed decision until explicit confirmation (not yet locked).
    team["retain_pending"] = retain
    team["release_pending"] = release
    return retain_confirm_screen(game_code, int(team_index), team, retain, release)


@app.route("/team/retain/edit", methods=["POST"])
def team_retain_edit():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if team.get("roster_confirmed"):
        return resume_screen(game_code, int(team_index), team)
    return roster_decision_screen(game_code, int(team_index), team,
                                  checked=set(team.get("retain_pending", [])))


@app.route("/team/retain/confirm", methods=["POST"])
def team_retain_confirm():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if team.get("roster_confirmed"):
        return resume_screen(game_code, int(team_index), team)
    retain = team.get("retain_pending")
    release = team.get("release_pending")
    if not retain or not release or len(retain) != ROSTER_COUNT or len(release) != REASSIGN_COUNT:
        # Nothing valid to confirm; send them back to choose.
        return roster_decision_screen(game_code, int(team_index), team)
    team["starting_roster"] = list(retain)
    team["released"] = list(release)
    team["rejected_students"] = list(release)   # legacy field kept in sync
    team["roster_confirmed"] = True
    # Distribution is a deliberate GM action (the GM confirms every team has both
    # joined AND finalized before pooling), so we do not auto-run it here. See
    # /gm/reassign and the GM "Student Reassignment" readiness panel.
    return resume_screen(game_code, int(team_index), team)


# ---------------------------------------------------------------------------
# Phase 3/4 screen: waiting for all teams, then the final 15-student class.
# ---------------------------------------------------------------------------
def reassignment_screen(game_code, team_index, team):
    game = games[game_code]
    teams = game["teams"]
    refresh = team_footer(game_code, team_index)

    if not team.get("reassigned"):
        ready = sum(1 for t in teams if t.get("roster_confirmed"))
        total = len(teams)
        if game.get("reassignment_error"):
            body = ("<div class='card'><h2>Reassignment Paused</h2>"
                    "<p class='err'>" + e(game["reassignment_error"]) + "</p>"
                    "<p class='muted'>No students were moved. Please wait for the Game Master.</p></div>")
        else:
            body = ("<div class='card'><h2>Placement Locked &mdash; Awaiting Reassignment</h2>"
                    "<p>Your retain/refer decision is locked. Replacements are distributed once "
                    "<strong>all</strong> teams have finalized.</p>"
                    "<p class='muted'>" + str(ready) + " of " + str(total)
                    + " teams finalized.</p></div>")
        return page("Reassignment - " + team["name"],
                    body + refresh + live_reload_script(game_code, team_index), width="wide")

    # Reassigned: show the final 15-student class with clear labels.
    retained = team["starting_roster"]
    reassigned = team["reassigned"]
    note = ""
    if team.get("reassignment_mode") == "single_team_pool":
        note = ("<p class='muted'>Single-team practice game: with no other table to pool from, your "
                + str(REASSIGN_COUNT) + " replacements were drawn from canonical students you were not "
                "originally dealt. No student your team had was returned to you.</p>")
    elif team.get("reassignment_mode") == "shared_pool_plus_backfill":
        note = ("<p class='muted'>Some replacements came from the pool of students other teams referred; "
                "the rest were assigned from students you had not worked with, because no remaining "
                "referred student could join you without repeating a character already in your class. "
                "No student from your original 15 was returned to you.</p>")
    pool_in = team.get("reassigned_pool", [])
    backfill_in = team.get("reassigned_backfill", [])
    cards_ret = "".join(student_mini_card(s, badge="Retained") for s in retained)
    pool_section = ""
    if pool_in:
        pool_section = ("<h3>Reassigned from the shared pool (" + str(len(pool_in)) + ")</h3>"
                        "<div style='text-align:center'>"
                        + "".join(student_mini_card(s, badge="Shared pool") for s in pool_in)
                        + "</div>")
    backfill_section = ""
    if backfill_in:
        backfill_section = ("<h3>Administrative placement (" + str(len(backfill_in)) + ")</h3>"
                            "<p class='muted'>Students you had not worked with, placed because the "
                            "shared pool could not supply an eligible match.</p>"
                            "<div style='text-align:center'>"
                            + "".join(student_mini_card(s, badge="Administrative") for s in backfill_in)
                            + "</div>")
    body = (
        "<div class='card'><h2>Your Final Class (" + str(len(retained) + len(reassigned)) + ") &mdash; "
        + e(team["name"]) + "</h2>" + note
        + "<h3>Retained (" + str(len(retained)) + ")</h3>"
        "<div style='text-align:center'>" + cards_ret + "</div>"
        + pool_section + backfill_section + "</div>"
        + resource_meters(team["resources"], team["statuses"])
        + "<div class='card'><p><strong>Overall Course Score: " + str(team["overall"]) + "</strong></p>"
        "<form method='post' action='/team/placement/ack'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        "<button type='submit'>Continue</button></form></div>")
    return page("Final Class - " + team["name"], body, width="wide")


@app.route("/team/placement/ack", methods=["POST"])
def team_placement_ack():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    if team.get("reassigned"):
        team["placement_acked"] = True
    # Setup is finished: enter active gameplay through the split workspace.
    if team_setup_complete(team):
        return redirect(workspace_url(game_code, int(team_index)))
    return resume_screen(game_code, int(team_index), team)


# ===========================================================================
# CHECKPOINT ENGINE
# ===========================================================================
def team_cast(team):
    """Pick stable narrative representatives from the starting roster so authored
    evidence is coherent regardless of which students were drafted. Computed once."""
    if "cast" in team:
        return team["cast"]
    roster = team["starting_roster"]
    used = set()

    def pick(scorefn):
        best, bestv = None, None
        for s in roster:
            if s in used:
                continue
            v = scorefn(STUDENT_STATS[s])
            if bestv is None or v > bestv:
                bestv, best = v, s
        if best is not None:
            used.add(best)
        return best

    cast = {
        "calendar": pick(lambda st: st["motivation"] + st["readiness"] - 2 * st["self_management"]),
        "dodge": pick(lambda st: -(st["motivation"] + st["self_management"])),
        "question_mark": pick(lambda st: st["motivation"] - st["readiness"] - st["adaptability"]),
        "silent_sos": pick(lambda st: st["communication"] - 2 * st["help_seeking"]),
        "engaged": pick(lambda st: st["motivation"] + st["readiness"]),
        "strong": pick(lambda st: st["readiness"] + st["communication"] + st["self_management"]),
    }
    team["cast"] = cast
    return cast


def course_reality_note():
    return ("<div class='notice'><p><strong>Before deciding, review your Course Card.</strong> "
            "Your course has structural realities that may change how an option actually plays out.</p></div>")


# --- Checkpoint action tables. deltas apply to (SS, CQ, FC, TR). -----------
def D(ss=0, cq=0, fc=0, tr=0):
    return {"Student Success": ss, "Course Quality": cq,
            "Faculty Capacity": fc, "Time/Resources": tr}


CP_ACTIONS = {
    0: [  # First Week - LOOK BEFORE REACTING
        {"key": "zeros", "label": "Enter zeros for missing work so the gradebook is accurate",
         "meta": "Effort: Low | Reach: Whole class | Consideration: Students see the real picture; early grades look worse",
         "deltas": D(ss=-3, cq=10, fc=-2)},
        {"key": "message", "label": "Message the students who haven't submitted (Canvas 'Message Students Who')",
         "meta": "Effort: Low | Reach: Targeted | Consideration: Fast, but relies on student follow-through",
         "deltas": D(ss=9, fc=-3)},
        {"key": "contact", "label": "Call or email each at-risk student individually",
         "meta": "Effort: High | Reach: Individual | Consideration: Personal and effective, but heavy on your time",
         "deltas": D(ss=12, fc=-10, tr=-5)},
        {"key": "clarify", "label": "Post a clarification, an example, and the rubric for the whole class",
         "meta": "Effort: Moderate | Reach: Whole class | Consideration: Heads off repeat confusion for everyone",
         "deltas": D(ss=9, cq=12, fc=-4)},
        {"key": "fix_schedule", "label": "Reconcile the syllabus and Canvas due dates",
         "meta": "Effort: Low | Reach: Whole class | Consideration: Fixes a course inconsistency, not a student mistake",
         "deltas": D(ss=5, cq=13, fc=-2)},
        {"key": "extend", "label": "Extend the first assignment deadline for everyone",
         "meta": "Effort: Low | Reach: Whole class | Consideration: Helps now, but compresses your grading window",
         "deltas": D(ss=6, cq=-5, tr=-7)},
        {"key": "tutoring", "label": "Connect students with academic support (Learning Center or Brainfuse)",
         "meta": "Faculty effort: Low | Reach: Whole class | Consideration: Scalable, but students must choose to use it",
         "deltas": D(ss=7, cq=3)},
        {"key": "monitor", "label": "Monitor / take no action yet",
         "meta": "Effort: None | Reach: None | Consideration: Conditions may resolve, hold, or worsen",
         "deltas": D(ss=-9, cq=-5)},
    ],
    1: [  # Early Semester - BUILD SYSTEMS THAT ABSORB CHANGE
        {"key": "start_here", "label": "Send the late-add students a targeted Start Here message",
         "meta": "Effort: Low | Scope: Targeted | Consideration: Fast and specific to the new students",
         "deltas": D(ss=9, fc=-3)},
        {"key": "pathway", "label": "Build a reusable late-start module in Canvas",
         "meta": "Effort: High now | Scope: Scalable | Consideration: A system that serves every future late add",
         "deltas": D(cq=14, tr=8, fc=-8)},
        {"key": "catchup", "label": "Open a short catch-up window for foundational work",
         "meta": "Effort: Low | Scope: Flexible | Consideration: Flexibility now, grading/workload later",
         "deltas": D(ss=9, cq=-3, tr=-7)},
        {"key": "orientation", "label": "Hold an orientation / office-hours check-in for the new students",
         "meta": "Effort: High | Scope: Whole group | Consideration: Strong clarification; costs your bandwidth",
         "deltas": D(ss=12, cq=4, fc=-9)},
        {"key": "advisor", "label": "Loop in advising and academic support (Learning Center / Brainfuse) for the new students",
         "meta": "Effort: Low | Scope: Scalable | Consideration: Routes needs to the right support, off your plate",
         "deltas": D(ss=8, tr=4, fc=-2)},
        {"key": "monitor", "label": "Monitor (no onboarding action yet)",
         "meta": "Effort: None | Scope: None | Consideration: New students may drift without an entry point",
         "deltas": D(ss=-11, cq=-3)},
    ],
    2: [  # First Major Assessment - INVESTIGATE BEFORE DECIDING
        {"key": "reteach", "label": "Reteach the weak skill to the whole class",
         "meta": "Faculty effort: Moderate | Reach: Whole class | Consideration: Addresses the shared gap directly",
         "deltas": D(ss=11, cq=9, fc=-5)},
        {"key": "aligned_practice", "label": "Add aligned practice that matches the rubric's demand",
         "meta": "Faculty effort: High | Reach: Whole class | Consideration: Fixes the practice-to-assessment mismatch",
         "deltas": D(ss=9, cq=14, fc=-5, tr=-4)},
        {"key": "revision", "label": "Allow a structured revision/resubmission after feedback",
         "meta": "Faculty effort: High | Reach: Whole class | Consideration: Strong learning, heavy grading load",
         "deltas": D(ss=13, cq=5, fc=-9, tr=-6)},
        {"key": "outreach_low", "label": "Outreach to low-engagement students + refer to tutoring",
         "meta": "Faculty effort: Moderate | Reach: Targeted | Consideration: Addresses engagement, not the class-wide gap",
         "deltas": D(ss=9, fc=-4)},
        {"key": "change_future", "label": "Revise how you teach this skill next term",
         "meta": "Faculty effort: Low | Reach: Future | Consideration: Helps later cohorts, not this one now",
         "deltas": D(cq=8)},
        {"key": "redesign_assessment", "label": "Realign the rubric/assessment for future terms",
         "meta": "Faculty effort: Low | Reach: Future | Consideration: Improves alignment going forward",
         "deltas": D(cq=10, fc=-3)},
        {"key": "continue", "label": "Continue without change",
         "meta": "Faculty effort: None | Reach: None | Consideration: Leaves the mismatch in place",
         "deltas": D(ss=-12, cq=-10)},
        {"key": "monitor", "label": "Monitor and reassess",
         "meta": "Faculty effort: None | Reach: None | Consideration: The gap persists for now",
         "deltas": D(ss=-7, cq=-7)},
    ],
}

CP_LIMITS = {0: 3, 1: 2, 2: 3}  # max selectable actions
CP_MONITOR = {0: "monitor", 1: "monitor", 2: "monitor"}

# What KIND of need each action addresses. Teaches faculty to classify the problem
# (support / course-design / communication / advising / accessibility) before acting.
# Keys with no entry (monitor, continue) deliberately address no need.
ACTION_NEEDS = {
    (0, "zeros"): "course_design", (0, "message"): "communication",
    (0, "contact"): "communication", (0, "clarify"): "course_design",
    (0, "fix_schedule"): "course_design", (0, "extend"): "course_design",
    (0, "tutoring"): "support",
    (1, "start_here"): "communication", (1, "pathway"): "course_design",
    (1, "catchup"): "course_design", (1, "orientation"): "communication",
    (1, "advisor"): "advising",
    (2, "reteach"): "course_design", (2, "aligned_practice"): "course_design",
    (2, "revision"): "course_design", (2, "outreach_low"): "support",
    (2, "change_future"): "course_design", (2, "redesign_assessment"): "course_design",
}


def support_referral_outcome(team):
    """One concise, modality-aware line describing how an academic-support referral
    actually played out for this course. Not a lookup of the 'right' answer - it
    reflects context so faculty see that modality changes how a referral lands."""
    mc = modality_class(COURSE_BY_NAME.get(team.get("course"), {}).get("modality"))
    if mc == "online":
        return ("Because this is an online course, the students who followed through mostly used "
                "Brainfuse (the Canvas sidebar link, within their 15-hour yearly allowance); the "
                "Learning Center (Student Success Center) stayed open to them as well.")
    if mc == "cte":
        return ("Because this is a hands-on course, tutoring helped a few students but did not replace "
                "guided practice on the physical competencies - watch whether the real need was "
                "practice, clearer directions, or an accommodation.")
    return ("Being on campus, several students used the Learning Center (Student Success Center) in "
            "Building A; others used Brainfuse online tutoring (15 hours per year).")

CP1_REASSESS_OPTIONS = [
    "After the next assignment",
    "If another assignment is missed",
    "At the next semester checkpoint",
]

# CP4 (Midterm) - PRIORITIZE AND SCALE. One priority + one supporting.
CP4_PRIORITY = [
    {"key": "urgent", "label": "Dropout Detective alert + advisor note for the highest-risk student",
     "deltas": D(ss=11, fc=-5)},
    {"key": "scalable", "label": "Scalable whole-class fix for the shared misconception",
     "deltas": D(ss=10, cq=13, fc=-5)},
    {"key": "feedback", "label": "Show students where feedback lives (SpeedGrader comments)",
     "deltas": D(ss=6, cq=9, fc=-2)},
    {"key": "tool", "label": "Use a Canvas tool/workflow to flag and route at-risk students",
     "deltas": D(ss=8, fc=-2, tr=4)},
    {"key": "individual_all", "label": "Individually email every student in the class",
     "deltas": D(ss=7, fc=-16, tr=-10)},
    {"key": "defer", "label": "Intentionally defer a lower-priority issue this round",
     "deltas": D(ss=-3, tr=6)},
]
# Supporting response: same menu, roughly half the effect.
CP4_SUPPORT = CP4_PRIORITY

# CP5 (Final Stretch) - FINISH SUSTAINABLY AND ACCURATELY.
# "sustainable" is a legitimate recovery choice: protecting an efficient routine you
# already built (reuse, don't rebuild) is a realistic sustainable practice that
# restores Faculty Capacity for the finish. It is not a generic "restore" button -
# it competes with the other protect choices and reflects a real teaching behavior.
CP5_PROTECT = [
    {"key": "sustainable", "label": "Protect a sustainable routine you already built (reuse feedback and systems instead of rebuilding)",
     "deltas": D(cq=5, fc=10, tr=4)},
    {"key": "grading", "label": "Timely grading/feedback on remaining required work",
     "deltas": D(ss=8, cq=9, fc=-5)},
    {"key": "persistence", "label": "The student sitting just below the passing threshold",
     "deltas": D(ss=12, fc=-4)},
    {"key": "communication", "label": "Clear final communication: what's left, and when",
     "deltas": D(ss=7, cq=7, fc=-3)},
]
CP5_REPAIR = [
    {"key": "gradebook", "label": "Gradebook accuracy: zeros entered and last date of attendance recorded",
     "deltas": D(ss=-3, cq=12)},
    {"key": "visibility", "label": "Feedback visibility so students can actually find your comments",
     "deltas": D(ss=7, cq=8)},
    {"key": "alignment", "label": "Final alignment of remaining assessments",
     "deltas": D(cq=11)},
]
# Declining unnecessary end-of-term work protects the instructor: it recovers
# Faculty Capacity for the finish (a realistic sustainable practice).
CP5_NOT_ADD = [
    {"key": "extra_credit", "label": "A brand-new extra-credit assignment for the whole class",
     "deltas": D(tr=8, fc=9)},
    {"key": "new_module", "label": "A new end-of-term module or content unit",
     "deltas": D(tr=10, fc=10)},
    {"key": "new_policy", "label": "A new late/grading policy this late in the term",
     "deltas": D(cq=3, tr=6, fc=8)},
]


# ---------------------------------------------------------------------------
# GM-guided teaching moments: one scenario-specific discussion question and a
# small set of concrete Canvas/NCC strategies per checkpoint, plus GM-only
# facilitator notes. Aligned to the ACTUAL checkpoint scenarios above; it names
# only tools/outcomes that already exist in the game and the real NCC toolset.
# The `question` and `strategies` are projector-safe (public). `notes` are
# GM-only and must never reach a team screen, the projector, or any live payload.
# ---------------------------------------------------------------------------
TEACHING = {
    0: {  # First Week
        "title": "First Week: a course problem, or a student problem?",
        "question": ("Before you “fix the students”: your syllabus and Canvas disagree on a due "
                     "date, missing work shows as blank cells, and one student has the same confusion as "
                     "a classmate who spoke up — but stays silent. Which of these is a COURSE problem "
                     "you own, and which is a student problem?"),
        "strategies": [
            {"do": "Make the gradebook tell the truth — enter 0s for missing work.",
             "where": "Canvas Grades → column menu → “Set Default Grade” (or “Message Students Who → Haven’t submitted”).",
             "solves": "Blank cells hide who is behind and make the class look better than it is."},
            {"do": "Give due dates one source of truth.",
             "where": "Set each Assignment’s due date to match the syllabus; point to a Start Here / Simple Syllabus page.",
             "solves": "A syllabus-vs-Canvas mismatch is a course error, not a student mistake."},
            {"do": "Nudge the students who haven’t started — in two minutes.",
             "where": "Grades → “Message Students Who… Haven’t submitted yet,” with the instructions + rubric.",
             "solves": "Fast, scalable early outreach before small gaps become disengagement."},
            {"do": "Watch for the quiet ones who never ask.",
             "where": "New Analytics → low page views + no messages.",
             "solves": "Engagement is not help-seeking; the silent student needs you to reach first."},
        ],
        "notes": {
            "opener": ("Quick show of hands: who has ever treated a “student problem” in week one that "
                       "turned out to be a setup problem of their own? Keep that in mind as we look at this."),
            "followups": [
                "When does one class-wide clarification beat twenty individual replies?",
                "What’s your two-minute move for students who have gone quiet but aren’t failing yet?",
                "Where in YOUR course could a syllabus/Canvas mismatch be quietly costing students points?"],
            "objective": ("Separate course-design problems from student problems in week one, and act early "
                          "with scalable tools instead of heroics."),
            "tools": ("Canvas Grades (Set Default Grade, Message Students Who), Assignment due dates, "
                      "Simple Syllabus / Start Here, New Analytics; refer to the Learning Center or Brainfuse."),
            "why": ("Week-one accuracy and clarity prevent avoidable failures; blank-cell gradebooks and "
                    "conflicting dates erode trust and regular contact from day one."),
            "time": "~5–6 min",
            "transition": "Early systems pay off next — let’s see what happens when new students arrive mid-stream.",
        },
    },
    1: {  # Early Semester / late adds
        "title": "Early Semester: onboard by hand, or build it once?",
        "question": ("New students just added two weeks in. Do you onboard them by hand again — or build "
                     "ONE reusable “Start Here” that onboards every late add from now on? What would it "
                     "actually contain?"),
        "strategies": [
            {"do": "Build a reusable “Start Here” / late-start module once.",
             "where": "Canvas Modules → a Start Here module: welcome, where things live, first steps, how to catch up.",
             "solves": "Every future late add self-onboards without a new email from you."},
            {"do": "Message just the new students with their catch-up steps.",
             "where": "People/Grades → “Message Students Who,” or a Canvas Group for late adds.",
             "solves": "Specific guidance without spamming the whole class."},
            {"do": "Route needs to the right support.",
             "where": "Link Academic Advising + the Learning Center / Brainfuse right in the Start Here page.",
             "solves": "Onboarding isn’t all on you — scalable and sustainable."},
            {"do": "For labs/CTE: gate lab access behind a safety/orientation step.",
             "where": "Module prerequisites / requirements (a safety quiz before lab content unlocks).",
             "solves": "Late adds can’t skip required safety or sequence."},
        ],
        "notes": {
            "opener": "Who has onboarded a late add three separate times the hard way? Let’s build it once.",
            "followups": [
                "What belongs on the very first screen a late add sees?",
                "How do you make the same Start Here work for face-to-face AND asynchronous students?",
                "What can advising or the Learning Center own so it’s off your plate?"],
            "objective": "Replace repeated manual onboarding with a reusable Canvas system that scales.",
            "tools": ("Canvas Modules (Start Here, prerequisites/requirements), Groups, Message Students Who, "
                      "Advising + Learning Center/Brainfuse links."),
            "why": ("Scalable onboarding protects faculty capacity and gives every student a consistent entry "
                    "— essential for hybrid/asynchronous courses and rolling CTE cohorts."),
            "time": "~4–5 min",
            "transition": "Systems in place — now the first big assessment tests whether practice prepared students.",
        },
    },
    2: {  # First Major Assessment / alignment
        "title": "First Major Assessment: studying, teaching, or alignment?",
        "question": ("The class did fine overall, but most students lost points on the ONE rubric skill your "
                     "practice never actually asked them to do. Is this a studying problem, a teaching "
                     "problem, or an ALIGNMENT problem — and how can you tell the difference?"),
        "strategies": [
            {"do": "Align practice to the rubric’s verb.",
             "where": "If the rubric says “justify,” rebuild one low-stakes practice to ask students to justify — not just “identify.”",
             "solves": "The practice-to-assessment mismatch that caused the gap."},
            {"do": "Let the rubric do the teaching.",
             "where": "Attach the Canvas Rubric to the assignment and grade in SpeedGrader so students see exactly where points were lost.",
             "solves": "Vague “try harder” feedback; students learn the standard."},
            {"do": "Offer a structured revision tied to the weak criterion.",
             "where": "A resubmission that must address the specific rubric row (use sparingly for capacity).",
             "solves": "Turns a low score into actual learning."},
            {"do": "Separate the two causes before you act.",
             "where": "New Analytics: low-practice students → outreach/tutoring; engaged students who still struggled → reteach.",
             "solves": "One fix can’t address two different causes at once."},
        ],
        "notes": {
            "opener": "When a WHOLE class misses the same thing, the odds it’s purely “them” drop fast.",
            "followups": [
                "How do you check that your practice matches the cognitive demand of the assessment?",
                "When is a reteach worth it versus a revision window — given your capacity?",
                "How do rubric-anchored comments in SpeedGrader change what students do next?"],
            "objective": "Diagnose assessment results by cause and fix alignment, not just symptoms.",
            "tools": ("Canvas Rubrics + SpeedGrader, aligned practice assignments, New Analytics, "
                      "Learning Center/Brainfuse referrals."),
            "why": ("Alignment and rubric-anchored feedback are the core of regular substantive interaction "
                    "and meaningful feedback — the biggest lever on learning."),
            "time": "~6–7 min",
            "transition": "Midterm raises the stakes: more needs than time. Next we practice triage.",
        },
    },
    3: {  # Midterm / triage
        "title": "Midterm: triage, not a to-do list.",
        "question": ("At midterm you have a declining student, a shared misconception, a grading backlog, AND "
                     "students who can’t even FIND your feedback — with no time for all of it. What do you "
                     "protect first, and what do you deliberately let go this round?"),
        "strategies": [
            {"do": "Use early alert for your highest-risk student.",
             "where": "Dropout Detective (in Canvas) → flag + notify the advisor for the declining student.",
             "solves": "One coordinated response instead of chasing it alone."},
            {"do": "Fix the shared misconception once, for everyone.",
             "where": "A short Announcement or a 3-minute mini-lesson addressing the common error.",
             "solves": "Scalable beats individual when the problem is class-wide."},
            {"do": "Make feedback findable.",
             "where": "Show where SpeedGrader comments live (a 30-second Canvas Studio clip or an Announcement with a screenshot).",
             "solves": "Feedback students can’t find isn’t feedback."},
            {"do": "Protect your capacity on purpose.",
             "where": "Deliberately defer one lower-priority task — and say so.",
             "solves": "Doing everything “a little” at midterm burns you out and helps no one."},
        ],
        "notes": {
            "opener": "Midterm is triage, not a to-do list. If you can only protect two things, what are they?",
            "followups": [
                "What makes a student “highest-risk” versus just behind?",
                "When is a scalable whole-class move better than twenty personal emails?",
                "What will you deliberately NOT do this round — and how will you communicate that?"],
            "objective": "Prioritize high-impact, scalable moves and protect faculty capacity under real constraints.",
            "tools": ("Dropout Detective (early alert), Announcements, SpeedGrader + Canvas Studio for feedback "
                      "visibility, Message Students Who."),
            "why": ("Prioritization and early alert catch persistence risks in time; feedback students can’t "
                    "find is a quiet failure of regular substantive interaction."),
            "time": "~5–6 min",
            "transition": "Last stretch: finishing accurately and sustainably — without adding scope.",
        },
    },
    4: {  # Final Stretch
        "title": "Final Stretch: protect, repair, add nothing new.",
        "question": ("Final stretch: a student is one point from passing, grading is piling up, some zeros are "
                     "still blank, and students want brand-new extra credit. What do you PROTECT, what do you "
                     "REPAIR, and what do you refuse to ADD — and why is “add nothing new” sometimes the "
                     "most professional call?"),
        "strategies": [
            {"do": "Close the gradebook accurately.",
             "where": "Canvas Grades → enter real 0s and record the last date of attendance / last activity for non-submitters.",
             "solves": "Accurate final grades and financial-aid / completion reporting depend on it."},
            {"do": "Protect a sustainable routine — reuse, don’t rebuild.",
             "where": "Reuse existing rubrics and feedback banks for the remaining grading.",
             "solves": "Recovers the capacity you need to finish accurately."},
            {"do": "Decline new scope, on purpose.",
             "where": "Say no to a brand-new extra-credit assignment or new module this late.",
             "solves": "New work this late rarely helps learning and crushes capacity."},
            {"do": "Publish grades and close the loop.",
             "where": "Publish final grades; complete the Course Improvement Survey; send one clear “what’s left and when” message.",
             "solves": "Students know where they stand, and you capture improvements for next term."},
        ],
        "notes": {
            "opener": ("The finish line is where accuracy and sustainability matter most — and where “do "
                       "more” is usually the wrong instinct."),
            "followups": [
                "What’s the difference between a student failing and the COURSE failing?",
                "Which end-of-term tasks are compliance-critical (grades, last date of attendance) versus optional?",
                "What will you reuse instead of rebuild to protect your capacity?"],
            "objective": "Finish sustainably and accurately: protect capacity, repair what matters, add nothing new.",
            "tools": ("Canvas Grades (zeros, last date of attendance), Grade Publish, Course Improvement Survey, "
                      "reused rubrics; one final Announcement."),
            "why": ("Accurate final grades and last-activity dates are compliance-critical; declining new scope "
                    "models sustainable, professional practice."),
            "time": "~5–6 min",
            "transition": "That’s the full arc — week one to finals. Next: the final accusation and the real debrief.",
        },
    },
}


def cp_dict(team):
    return team.setdefault("cp", {})


def apply_checkpoint(team, index, chosen_deltas_list, summary):
    """Apply a checkpoint's combined deltas exactly once and store a summary."""
    cp = cp_dict(team)
    rec = cp.setdefault(index, {})
    if rec.get("applied"):
        return
    total = D()
    for d in chosen_deltas_list:
        for k, v in d.items():
            total[k] += v
    apply_deltas(team, total)
    rec["applied"] = True
    rec["summary"] = summary


# ---------------------------------------------------------------------------
# Checkpoint dispatch
# ---------------------------------------------------------------------------
def checkpoint_screen(game_code, team_index, team):
    index = games[game_code]["checkpoint_index"]
    if index == 0:
        return cp1_screen(game_code, team_index, team)
    if index == 1:
        return cp2_screen(game_code, team_index, team)
    if index == 2:
        return cp3_screen(game_code, team_index, team)
    if index == 3:
        return cp4_screen(game_code, team_index, team)
    if index == 4:
        return cp5_screen(game_code, team_index, team)
    return status_screen(game_code, team_index, team)


def open_course_button(game_code, team_index):
    if _embedded():
        return ""   # Canvas is already shown in the split's left pane.
    return ("<a class='btn secondary' href='" + shell_url(game_code, team_index, "home")
            + "'>View Course (gradebook, messages, announcements)</a>")


def cp_header(game_code, team_index, team, index):
    return ("<div class='card'><h3>" + e(team["name"]) + " &mdash; " + e(team["course"]) + "</h3>"
            "<p>" + open_course_button(game_code, team_index) + "</p>"
            + resources_drawer(team) + "</div>"
            + resource_meters(team["resources"], team["statuses"])
            + team_roles_panel(team))


def multiselect_form(game_code, team_index, index, error_message=""):
    actions = CP_ACTIONS[index]
    limit = CP_LIMITS[index]
    monitor_key = CP_MONITOR.get(index)
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    rows = ""
    for a in actions:
        cb_id = " id='monitor-cb'" if a["key"] == monitor_key else ""
        rows += ("<div class='choice'><label><input type='checkbox' name='actions' value='" + a["key"]
                 + "'" + cb_id + "> <strong>" + a["label"] + "</strong></label>"
                 "<small>" + a["meta"] + "</small></div>")
    extra = script = ""
    if index == 0:
        reassess = ""
        for opt in CP1_REASSESS_OPTIONS:
            reassess += "<label><input type='radio' name='reassess' value='" + e(opt) + "'> " + opt + "</label>"
        # Shown only when Monitor is selected (progressive enhancement: if JS is
        # unavailable the block stays visible and the server still validates it).
        extra = ("<div id='reassess-block'><p><strong>Because you are considering Monitor, when will "
                 "you reassess?</strong></p>" + reassess + "</div>")
        script = ("<script>(function(){var c=document.getElementById('monitor-cb'),"
                  "b=document.getElementById('reassess-block');if(!c||!b)return;"
                  "function u(){b.style.display=c.checked?'':'none';}u();"
                  "c.addEventListener('change',u);})();</script>")
    return (
        "<div class='card'><h3>What, if anything, will your team do?</h3>"
        "<p>You may select up to " + str(limit) + " action(s). Choosing more is not automatically better: "
        "every action also spends <strong>Faculty Capacity</strong>, and stacking several labor-intensive "
        "actions can cost more than your team can realistically sustain.</p>"
        + error_html +
        "<form method='post' action='/team/cp/submit'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        "<input type='hidden' name='cp' value='" + str(index) + "'>" + _embed_hidden()
        + rows + extra + "<p><button type='submit'>Submit Decision</button></p></form>"
        + script + "</div>"
    )


# ---- Checkpoint 1: First Week ---------------------------------------------
def cp1_evidence(team):
    c = team_cast(team)
    a, b, d, s = c["calendar"], c["dodge"], c["question_mark"], c["silent_sos"]
    return (
        "<div class='card evidence'><h3>First Week Course Check</h3>"
        "<p>Review four areas before reacting.</p>"
        "<h4>1. Roster</h4><p>Your class is enrolled and active. Four students stand out this week:</p>"
        "<ul><li><strong>" + e(a) + "</strong></li><li><strong>" + e(b) + "</strong></li>"
        "<li><strong>" + e(d) + "</strong></li><li><strong>" + e(s) + "</strong></li></ul>"
        "<h4>2. Grades</h4><ul>"
        "<li><strong>" + e(a) + "</strong> &mdash; Assignment 1: missing (blank grade cell)</li>"
        "<li><strong>" + e(b) + "</strong> &mdash; Assignment 1: missing (blank grade cell)</li>"
        "<li><strong>" + e(d) + "</strong> &mdash; Assignment 1: submitted</li>"
        "<li><strong>" + e(s) + "</strong> &mdash; Assignment 1: submitted</li></ul>"
        "<p><strong>Class-level issue:</strong> several missing assignments show as blank cells "
        "instead of zeros, so the gradebook may look better than reality.</p>"
        "<h4>3. Course Activity</h4><ul>"
        "<li><strong>" + e(a) + "</strong> &mdash; logging in and reading regularly, but did not submit "
        "Assignment 1. Notes that the syllabus says Sunday while the Canvas due date shows Thursday.</li>"
        "<li><strong>" + e(b) + "</strong> &mdash; low, sporadic activity; rarely logs in, missing assignment.</li>"
        "<li><strong>" + e(d) + "</strong> &mdash; active and submitted, but the work suggests confusion "
        "about what the assignment was asking for.</li>"
        "<li><strong>" + e(s) + "</strong> &mdash; active and submitted, with the same misunderstanding as "
        + e(d) + ", but has not sent any message.</li></ul>"
        "<h4>4. Messages</h4><ul>"
        "<li><strong>" + e(d) + "</strong> messaged you: \"I turned it in but I'm not sure I did the right "
        "thing &mdash; the instructions were confusing.\"</li>"
        "<li><strong>" + e(a) + "</strong> has read announcements but not written in.</li>"
        "<li><strong>" + e(b) + "</strong> and <strong>" + e(s) + "</strong> have not written in.</li></ul>"
        "<p class='muted'>Note the due-date mismatch: the syllabus and Canvas disagree. That may be a course "
        "inconsistency, not only a student mistake.</p></div>"
    )


def cp1_narrative(team):
    c = team_cast(team)
    a, b, d, s = c["calendar"], c["dodge"], c["question_mark"], c["silent_sos"]
    actions = team["cp"][0]["actions"]
    lines = []
    if "zeros" in actions:
        lines.append("You entered zeros for the missing work. The gradebook now reflects reality, and "
                     + e(a) + " and " + e(b) + " can see the real impact of the missing assignment.")
    if "message" in actions:
        lines.append("Your targeted message reaches " + e(b) + ", who responds, submits the missing work, and catches up.")
    if "contact" in actions:
        lines.append("You contacted students individually. " + e(a) + " re-engages after the personal outreach, though it took real instructor time.")
    if "clarify" in actions:
        lines.append("You posted a clarification, an example, and the rubric. " + e(d) + " and " + e(s)
                     + " both see the corrected expectation; the shared misunderstanding stops spreading.")
    if "fix_schedule" in actions:
        lines.append("You reconciled the syllabus and Canvas due dates. " + e(a)
                     + " was not careless after all - the course was telling students two different things. That's fixed now.")
    if "extend" in actions:
        lines.append("You extended the deadline for everyone. More students submit, but your grading window is now compressed.")
    if "tutoring" in actions:
        lines.append("You connected the class with academic support. " + support_referral_outcome(team)
                     + " " + e(d) + " schedules a session. A few days later, " + e(s)
                     + " submits work showing the same misunderstanding of the directions.")
    if "monitor" in actions:
        line = ("You chose to monitor for now. " + e(s) + " remains active and keeps submitting; the same "
                "misunderstanding appears again, and no message has been received from them.")
        if team["cp"][0].get("reassess"):
            line += " You plan to reassess: " + e(team["cp"][0]["reassess"]) + "."
        lines.append(line)
    if "clarify" not in actions:
        lines.append("Because the class-wide clarification did not go out, " + e(s)
                     + " keeps working from the same misunderstanding without asking for help.")
    return "".join("<p>" + x + "</p>" for x in lines)


def cp1_screen(game_code, team_index, team):
    team["cp1_reviewed"] = True
    rec = team.get("cp", {}).get(0)
    intro = ("<div class='card'><h2>Checkpoint 1: First Week</h2>"
             "<p>Your course has run for one week. Look before reacting.</p></div>")
    base = cp_header(game_code, team_index, team,0) + intro
    if rec and rec.get("submitted"):
        chosen = "".join("<li>" + action_label(0, k) + "</li>" for k in rec["actions"])
        body = (base + "<div class='card'><h3>Your First Week decision</h3><ul>" + chosen + "</ul>"
                "<h3>What happened</h3>" + cp1_narrative(team) + "</div>"
                + private_block(game_code, team_index, team, 0)
                + checkpoint_done_tail(game_code, team_index, team))
        return page("Checkpoint 1 - " + team["name"], body + team_footer(game_code, team_index))
    body = base + cp1_evidence(team) + resources_consult_block(team, 0) + multiselect_form(game_code, team_index, 0)
    return page("Checkpoint 1 - " + team["name"], body + team_footer(game_code, team_index))


# ---- Checkpoint 2: Early Semester / Late Adds -----------------------------
def ensure_late_adds(team):
    """Controlled-random late adds from the received cards, chosen once."""
    if "late_adds" in team:
        return team["late_adds"]
    received = received_students(team)
    if not received:
        # Single-team test mode: simulate a couple of new enrollments from
        # students not already in play, so the checkpoint still functions.
        in_play = set(team.get("starting_roster", [])) | set(team.get("rejected_students", []))
        pool = [s for s in STUDENTS if s not in in_play]
        random.shuffle(pool)
        count = random.choice([2, 3])
        adds = pool[:count]
    else:
        count = random.choice([2, 3, min(4, len(received))])
        count = min(count, len(received))
        adds = random.sample(received, count)
    team["late_adds"] = adds
    team["active_roster"] = list(team.get("starting_roster", [])) + adds
    return adds


def cp2_evidence(team):
    adds = ensure_late_adds(team)
    cards = "".join(student_card_html(s) for s in adds)
    return (
        "<div class='card evidence'><h3>New Student Intake</h3>"
        "<p><strong>" + str(len(adds)) + "</strong> student(s) have joined your course after the start of the "
        "term. Students joining the course are not damage &mdash; the question is whether your course can absorb "
        "normal enrollment change.</p>"
        "<h4>Where your course is right now</h4><ul>"
        "<li>You are a few modules in; foundational content has already been covered.</li>"
        "<li>New students can access the current module but may have missed start-here material.</li>"
        "<li>An assessment is coming up within the next couple of weeks.</li>"
        "<li>Onboarding / start-here structure and course navigation determine how quickly they orient.</li>"
        "<li>Support visibility varies by how students entered the course.</li></ul></div>"
        "<div class='card'><h3>The students joining your roster</h3>"
        "<p>Review their full cards before deciding.</p>" + cards + "</div>"
        + course_reality_note()
    )


def cp2_narrative(team):
    actions = team["cp"][1]["actions"]
    adds = team.get("late_adds", [])
    names = ", ".join(e(s) for s in adds) or "the new students"
    lines = []
    if "start_here" in actions:
        lines.append("Your targeted start-here message reaches " + names + "; they know where to begin.")
    if "pathway" in actions:
        lines.append("You built a reusable late-start pathway. It cost real time up front, but it now exists for every future late add &mdash; a system, not a one-off.")
    if "catchup" in actions:
        lines.append("You opened a catch-up window. Students use it, though it stretches your grading timeline.")
    if "orientation" in actions:
        lines.append("You held a group orientation. Clarity improves sharply, but it consumed a meaningful chunk of your bandwidth.")
    if "advisor" in actions:
        lines.append("You looped in advising and academic support for " + names
                     + ". Their advisors follow up. " + support_referral_outcome(team))
    if "monitor" in actions:
        lines.append("You chose not to onboard the new students yet. A couple of them drift early without a clear entry point.")
    if not actions:
        lines.append("No onboarding action was taken; the new students find their own way in, unevenly.")
    return "".join("<p>" + x + "</p>" for x in lines)


def cp2_screen(game_code, team_index, team):
    rec = team.get("cp", {}).get(1)
    intro = ("<div class='card'><h2>Checkpoint 2: Early Semester</h2>"
             "<p>Build systems that can absorb normal change.</p></div>")
    base = cp_header(game_code, team_index, team,1) + intro
    if rec and rec.get("submitted"):
        chosen = "".join("<li>" + action_label(1, k) + "</li>" for k in rec["actions"]) or "<li>(no action)</li>"
        body = (base + "<div class='card'><h3>Your onboarding decision</h3><ul>" + chosen + "</ul>"
                "<h3>What happened</h3>" + cp2_narrative(team) + "</div>"
                + "<div class='card'><p class='muted'>No private player check this checkpoint. "
                  "Please wait for the Game Master to advance.</p></div>"
                + checkpoint_done_tail(game_code, team_index, team))
        return page("Checkpoint 2 - " + team["name"], body + team_footer(game_code, team_index))
    body = base + cp2_evidence(team) + resources_consult_block(team, 1) + multiselect_form(game_code, team_index, 1)
    return page("Checkpoint 2 - " + team["name"], body + team_footer(game_code, team_index))


# ---- Checkpoint 3: First Major Assessment ---------------------------------
def cp3_evidence(team):
    c = team_cast(team)
    strong, eng, dodge = c["strong"], c["engaged"], c["dodge"]
    return (
        "<div class='card evidence'><h3>First Major Assessment Results</h3>"
        "<p>The first major assessment has closed. Performance is not catastrophic.</p>"
        "<ul><li><strong>Class average: about 71%.</strong></li>"
        "<li>Credibility Analysis: strong across the class.</li>"
        "<li>Evidence Selection: strong to moderate.</li>"
        "<li>Source Comparison: moderate.</li>"
        "<li><strong>Justification of Evaluation: significantly weak &mdash; about 9 of 13 students "
        "lost substantial points here.</strong></li></ul>"
        "<h4>Investigate before deciding what it means</h4><ul>"
        "<li>Three of the lowest-scoring students also show limited practice activity &mdash; e.g. <strong>"
        + e(dodge) + "</strong>.</li>"
        "<li>But at least two highly engaged students struggled with the same category &mdash; e.g. <strong>"
        + e(eng) + "</strong>, who practiced fully.</li>"
        "<li>A strong student, <strong>" + e(strong) + "</strong>, also missed part of it.</li>"
        "<li>A student message: \"I could tell which source was stronger, I just didn't know how to prove "
        "<em>why</em> the evidence supported that.\"</li></ul>"
        "<h4>Practice vs. assessment</h4>"
        "<p>Your practice activities asked students to <strong>identify</strong> which source is stronger. "
        "The assessment rubric expected students to <strong>justify</strong> the evaluation using multiple "
        "criteria and evidence. That is a real mismatch in cognitive demand &mdash; and some students also "
        "simply did not practice. The evidence shows more than one cause at once.</p></div>"
        + course_reality_note()
    )


def cp3_narrative(team):
    actions = team["cp"][2]["actions"]
    lines = []
    if "reteach" in actions:
        lines.append("Your targeted reteach lands on the specific weak skill (justification), and scores on the next related task improve.")
    if "aligned_practice" in actions:
        lines.append("You added practice that actually asks students to justify, not just identify. The practice-to-assessment mismatch closes. It cost grading and prep time.")
    if "revision" in actions:
        lines.append("You opened structured revision after feedback. Learning deepens, but your grading load climbs noticeably.")
    if "outreach_low" in actions:
        lines.append("You reached the lowest-engagement students individually and pointed them to academic support. "
                     + support_referral_outcome(team)
                     + " A few re-engage with practice. On the next task, several students who were already keeping up "
                     "still miss points on the justification criterion.")
    if "change_future" in actions:
        lines.append("You revised how you will teach this skill in future terms. It helps later cohorts more than this one.")
    if "redesign_assessment" in actions:
        lines.append("You revised the future assessment design so practice and assessment align.")
    if "continue" in actions:
        lines.append("You continued without change. The justification gap persists into later work.")
    if "monitor" in actions:
        lines.append("You chose to monitor. The mismatch remains unaddressed for now.")
    return "".join("<p>" + x + "</p>" for x in lines)


def cp3_screen(game_code, team_index, team):
    rec = team.get("cp", {}).get(2)
    intro = ("<div class='card'><h2>Checkpoint 3: First Major Assessment</h2>"
             "<p>Investigate performance before deciding what it means.</p></div>")
    base = cp_header(game_code, team_index, team,2) + intro
    if rec and rec.get("submitted"):
        chosen = "".join("<li>" + action_label(2, k) + "</li>" for k in rec["actions"]) or "<li>(no action)</li>"
        body = (base + "<div class='card'><h3>Your decision</h3><ul>" + chosen + "</ul>"
                "<h3>What happened</h3>" + cp3_narrative(team) + "</div>"
                + private_block(game_code, team_index, team, 2)
                + checkpoint_done_tail(game_code, team_index, team))
        return page("Checkpoint 3 - " + team["name"], body + team_footer(game_code, team_index))
    body = base + cp3_evidence(team) + resources_consult_block(team, 2) + multiselect_form(game_code, team_index, 2)
    return page("Checkpoint 3 - " + team["name"], body + team_footer(game_code, team_index))


# ---- Generic multiselect submit (CP1, CP2, CP3) ---------------------------
def action_label(index, key):
    for a in CP_ACTIONS[index]:
        if a["key"] == key:
            return a["label"]
    return key


@app.route("/team/cp/submit", methods=["POST"])
def team_cp_submit():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    cp = request.form.get("cp", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    if not cp.isdigit() or int(cp) not in CP_ACTIONS:
        return error_page("Something went wrong with that checkpoint.")

    team = get_team(game_code, int(team_index))
    index = int(cp)
    ti = int(team_index)
    if "resources" not in team:  # defensive: cannot act before setup is complete
        return resume_screen(game_code, ti, team)
    # Must be the live checkpoint.
    if games[game_code]["checkpoint_index"] != index or not games[game_code]["checkpoint_open"]:
        return checkpoint_screen(game_code, ti, team)
    rec = cp_dict(team).get(index)
    if rec and rec.get("submitted"):
        return checkpoint_screen(game_code, ti, team)

    actions = CP_ACTIONS[index]
    keys = [a["key"] for a in actions]
    selected = [k for k in request.form.getlist("actions") if k in keys]
    reassess = request.form.get("reassess", "")
    limit = CP_LIMITS[index]

    if not selected:
        return _multiselect_error(game_code, ti, team, index, "Please select at least one action (Monitor is a valid choice).")
    if len(selected) > limit:
        return _multiselect_error(game_code, ti, team, index,
                                  "You may select at most " + str(limit) + " actions.")
    if index == 0 and CP_MONITOR[index] in selected and reassess not in CP1_REASSESS_OPTIONS:
        return _multiselect_error(game_code, ti, team, index,
                                  "You chose Monitor, so please choose when you will reassess.")

    ordered = [k for k in keys if k in selected]
    rec = cp_dict(team).setdefault(index, {})
    rec["submitted"] = True
    rec["actions"] = ordered
    if index == 0:
        rec["reassess"] = reassess if CP_MONITOR[index] in ordered else None
    deltas = [a["deltas"] for a in actions if a["key"] in ordered]
    summary = [action_label(index, k) for k in ordered]
    apply_checkpoint(team, index, deltas, summary)
    return checkpoint_screen(game_code, ti, team)


def _multiselect_error(game_code, team_index, team, index, msg):
    if index == 0:
        base = cp_header(game_code, team_index, team,0) + ("<div class='card'><h2>Checkpoint 1: First Week</h2></div>") + cp1_evidence(team)
    elif index == 1:
        base = cp_header(game_code, team_index, team,1) + ("<div class='card'><h2>Checkpoint 2: Early Semester</h2></div>") + cp2_evidence(team)
    else:
        base = cp_header(game_code, team_index, team,2) + ("<div class='card'><h2>Checkpoint 3: First Major Assessment</h2></div>") + cp3_evidence(team)
    return page("Checkpoint - " + team["name"],
                base + multiselect_form(game_code, team_index, index, msg) + team_footer(game_code, team_index))


# ---- Checkpoint 4: Midterm (priority + supporting) ------------------------
def cp4_prior_help(team):
    """Callback: did an earlier whole-class intervention already help?"""
    cp = team.get("cp", {})
    whole_class = set()
    for i, keys in [(0, {"clarify"}), (1, {"orientation", "pathway"}), (2, {"reteach", "aligned_practice"})]:
        acts = set(cp.get(i, {}).get("actions", []))
        if acts & keys:
            whole_class |= acts & keys
    return bool(whole_class)


def cp4_constrained(team):
    """If a resource is critical, remove the most expensive option."""
    return any(team["resources"][n] < 30 for n in RESOURCE_NAMES)


def cp4_options(team):
    opts = list(CP4_PRIORITY)
    if cp4_constrained(team):
        opts = [o for o in opts if o["key"] != "individual_all"]
    return opts


def cp4_evidence(team):
    c = team_cast(team)
    dodge, sos = c["dodge"], c["silent_sos"]
    callback = ""
    if cp4_prior_help(team):
        callback = ("<li><strong>Callback:</strong> an earlier whole-class step you took appears to have helped "
                    + e(sos) + " re-engage without any individual outreach.</li>")
    else:
        callback = ("<li><strong>Note:</strong> " + e(sos) + " is still quietly behind; no whole-class step has "
                    "reached them yet.</li>")
    constraint = ""
    if cp4_constrained(team):
        constraint = ("<div class='notice'><p>One of your resources is Critical. Individualized outreach to "
                      "everyone is not available this round &mdash; you do not have the capacity.</p></div>")
    return (
        "<div class='card evidence'><h3>Midterm: more needs than you can perfectly address</h3>"
        "<p>Prioritize and scale. You cannot do everything well this round.</p><ul>"
        "<li><strong>Persistence risk:</strong> " + e(dodge) + " missed another assignment and activity is declining.</li>"
        "<li><strong>Shared misconception:</strong> several students show the same moderate error on recent work.</li>"
        "<li><strong>Faculty backlog:</strong> grading and feedback are piling up.</li>"
        "<li><strong>Process clarity:</strong> multiple students cannot find your feedback comments.</li>"
        + callback + "</ul></div>" + constraint + course_reality_note()
    )


def cp4_select(name, options, label):
    tags = "<option value=''>-- choose --</option>"
    for o in options:
        tags += "<option value='" + o["key"] + "'>" + o["label"] + "</option>"
    return ("<p><label><strong>" + label + "</strong><br><select name='" + name + "'>" + tags + "</select></label></p>")


def cp4_form(game_code, team_index, team, error_message=""):
    opts = cp4_options(team)
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    return (
        "<div class='card'><h3>Choose your midterm response</h3>" + error_html +
        "<form method='post' action='/team/cp4/submit'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>" + _embed_hidden()
        + cp4_select("priority", opts, "One priority response")
        + cp4_select("supporting", opts, "One supporting response")
        + "<p><button type='submit'>Submit Midterm Decision</button></p></form></div>"
    )


def cp4_label(key):
    for o in CP4_PRIORITY:
        if o["key"] == key:
            return o["label"]
    return key


def cp4_narrative(team):
    c = team_cast(team)
    rec = team["cp"][3]
    p, s = rec["priority"], rec["supporting"]
    lines = ["Priority: <strong>" + cp4_label(p) + "</strong>.",
             "Supporting: <strong>" + cp4_label(s) + "</strong>."]
    if "urgent" in (p, s):
        lines.append("You open Dropout Detective, add a note to " + e(c["dodge"]) + "'s advisor, and activate the alert. "
                     "A day later the advisor replies that they have reached out to " + e(c["dodge"]) + " to talk about staying on track.")
    if "individual_all" in (p, s):
        lines.append("Reaching every student by hand takes most of your remaining time this round; many of them got the same message you could have sent once.")
    if "scalable" in (p, s):
        lines.append("The scalable whole-class move addressed the shared misconception efficiently.")
    if "tool" in (p, s):
        lines.append("Using a Course Tool let you flag the at-risk student without heavy manual effort.")
    if "defer" in (p, s):
        lines.append("You intentionally deferred a lower-risk issue, protecting capacity for what mattered most.")
    return "".join("<p>" + x + "</p>" for x in lines)


def cp4_screen(game_code, team_index, team):
    rec = team.get("cp", {}).get(3)
    intro = ("<div class='card'><h2>Checkpoint 4: Midterm</h2><p>Prioritize and scale.</p></div>")
    base = cp_header(game_code, team_index, team,3) + intro
    if rec and rec.get("submitted"):
        body = (base + "<div class='card'><h3>Your midterm decision</h3>" + cp4_narrative(team) + "</div>"
                + private_block(game_code, team_index, team, 3)
                + checkpoint_done_tail(game_code, team_index, team))
        return page("Checkpoint 4 - " + team["name"], body + team_footer(game_code, team_index))
    body = base + cp4_evidence(team) + resources_consult_block(team, 3) + cp4_form(game_code, team_index, team)
    return page("Checkpoint 4 - " + team["name"], body + team_footer(game_code, team_index))


@app.route("/team/cp4/submit", methods=["POST"])
def team_cp4_submit():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    ti = int(team_index)
    if "resources" not in team:
        return resume_screen(game_code, ti, team)
    if games[game_code]["checkpoint_index"] != 3 or not games[game_code]["checkpoint_open"]:
        return checkpoint_screen(game_code, ti, team)
    if team.get("cp", {}).get(3, {}).get("submitted"):
        return checkpoint_screen(game_code, ti, team)

    opts = cp4_options(team)
    valid = {o["key"] for o in opts}
    priority = request.form.get("priority", "")
    supporting = request.form.get("supporting", "")
    if priority not in valid or supporting not in valid:
        return page("Checkpoint 4 - " + team["name"],
                    cp_header(game_code, team_index, team,3) + cp4_evidence(team)
                    + cp4_form(game_code, ti, team, "Please choose a valid priority and supporting response.")
                    + team_footer(game_code, ti))
    if priority == supporting:
        return page("Checkpoint 4 - " + team["name"],
                    cp_header(game_code, team_index, team,3) + cp4_evidence(team)
                    + cp4_form(game_code, ti, team, "Choose two different responses.")
                    + team_footer(game_code, ti))

    rec = cp_dict(team).setdefault(3, {})
    rec["submitted"] = True
    rec["priority"] = priority
    rec["supporting"] = supporting
    # Priority full effect; supporting half effect.
    pd = next(o["deltas"] for o in CP4_PRIORITY if o["key"] == priority)
    sd = next(o["deltas"] for o in CP4_PRIORITY if o["key"] == supporting)
    half = {k: round(v / 2) for k, v in sd.items()}
    summary = ["Priority: " + cp4_label(priority), "Supporting: " + cp4_label(supporting)]
    apply_checkpoint(team, 3, [pd, half], summary)
    return checkpoint_screen(game_code, ti, team)


# ---- Checkpoint 5: Final Stretch (protect / repair / do-not-add) ----------
def cp5_evidence(team):
    c = team_cast(team)
    near = c["dodge"]
    return (
        "<div class='card evidence'><h3>Final Stretch</h3>"
        "<p>Finish the semester sustainably and accurately. A student failing does not automatically "
        "mean the course failed.</p><ul>"
        "<li><strong>" + e(near) + "</strong> is near the passing threshold heading into finals.</li>"
        "<li>Remaining required assessments still need grading and feedback.</li>"
        "<li>Some missing work still shows as blank cells rather than zeros.</li>"
        "<li>Final grades, accurate last-activity dates, and the Course Improvement Survey are due soon.</li>"
        "<li>A few students have asked for new extra-credit opportunities.</li></ul></div>"
        "<div class='card'><p>Choose one thing to <strong>protect</strong>, one thing to <strong>repair</strong>, "
        "and one thing you will intentionally <strong>not add</strong>.</p></div>"
    )


def cp5_select(name, options, label):
    tags = "<option value=''>-- choose --</option>"
    for o in options:
        tags += "<option value='" + o["key"] + "'>" + o["label"] + "</option>"
    return ("<p><label><strong>" + label + "</strong><br><select name='" + name + "'>" + tags + "</select></label></p>")


def cp5_form(game_code, team_index, team, error_message=""):
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    return (
        "<div class='card'><h3>Your final decision</h3>" + error_html +
        "<form method='post' action='/team/cp5/submit'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>" + _embed_hidden()
        + cp5_select("protect", CP5_PROTECT, "Protect")
        + cp5_select("repair", CP5_REPAIR, "Repair")
        + cp5_select("not_add", CP5_NOT_ADD, "Intentionally do NOT add")
        + "<p><button type='submit'>Submit Final Decision</button></p></form></div>"
    )


def _cp5_label(options, key):
    for o in options:
        if o["key"] == key:
            return o["label"]
    return key


def cp5_narrative(team):
    rec = team["cp"][4]
    return ("<p>Protect: <strong>" + _cp5_label(CP5_PROTECT, rec["protect"]) + "</strong></p>"
            "<p>Repair: <strong>" + _cp5_label(CP5_REPAIR, rec["repair"]) + "</strong></p>"
            "<p>Do not add: <strong>" + _cp5_label(CP5_NOT_ADD, rec["not_add"]) + "</strong></p>"
            "<p>You spend the final weeks on the work you chose to protect and repair, and you hold the line "
            "on what you decided not to add.</p>")


def cp5_screen(game_code, team_index, team):
    rec = team.get("cp", {}).get(4)
    intro = ("<div class='card'><h2>Checkpoint 5: Final Stretch</h2><p>Finish sustainably and accurately.</p></div>")
    base = cp_header(game_code, team_index, team,4) + intro
    if rec and rec.get("submitted"):
        body = (base + "<div class='card'><h3>Your final decision</h3>" + cp5_narrative(team) + "</div>"
                + private_block(game_code, team_index, team, 4)
                + checkpoint_done_tail(game_code, team_index, team))
        return page("Checkpoint 5 - " + team["name"], body + team_footer(game_code, team_index))
    body = base + cp5_evidence(team) + resources_consult_block(team, 4) + cp5_form(game_code, team_index, team)
    return page("Checkpoint 5 - " + team["name"], body + team_footer(game_code, team_index))


@app.route("/team/cp5/submit", methods=["POST"])
def team_cp5_submit():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    ti = int(team_index)
    if "resources" not in team:
        return resume_screen(game_code, ti, team)
    if games[game_code]["checkpoint_index"] != 4 or not games[game_code]["checkpoint_open"]:
        return checkpoint_screen(game_code, ti, team)
    if team.get("cp", {}).get(4, {}).get("submitted"):
        return checkpoint_screen(game_code, ti, team)

    protect = request.form.get("protect", "")
    repair = request.form.get("repair", "")
    not_add = request.form.get("not_add", "")
    if (protect not in {o["key"] for o in CP5_PROTECT}
            or repair not in {o["key"] for o in CP5_REPAIR}
            or not_add not in {o["key"] for o in CP5_NOT_ADD}):
        return page("Checkpoint 5 - " + team["name"],
                    cp_header(game_code, team_index, team,4) + cp5_evidence(team)
                    + cp5_form(game_code, ti, team, "Please choose one option in each category.")
                    + team_footer(game_code, ti))

    rec = cp_dict(team).setdefault(4, {})
    rec["submitted"] = True
    rec["protect"], rec["repair"], rec["not_add"] = protect, repair, not_add
    deltas = [
        next(o["deltas"] for o in CP5_PROTECT if o["key"] == protect),
        next(o["deltas"] for o in CP5_REPAIR if o["key"] == repair),
        next(o["deltas"] for o in CP5_NOT_ADD if o["key"] == not_add),
    ]
    summary = ["Protect: " + _cp5_label(CP5_PROTECT, protect),
               "Repair: " + _cp5_label(CP5_REPAIR, repair),
               "Do not add: " + _cp5_label(CP5_NOT_ADD, not_add)]
    apply_checkpoint(team, 4, deltas, summary)
    return checkpoint_screen(game_code, ti, team)


# ===========================================================================
# PRIVATE PLAYER CHECK (CP1, CP3, CP5): allegiance reveal, mission, (sabotage
# at CP3 for Thunderflunked), suspicion vote. Pass-the-computer, one at a time.
# ===========================================================================
def private_round(team, cp):
    return team.setdefault("private", {}).setdefault(cp, {"decisions": {}, "votes": {}, "started": False})


def is_full_private(cp):
    """CP1/CP3/CP5 run the full private round (mission + suspicion vote)."""
    return cp in HAS_PRIVATE


def private_current(team, cp):
    """First player index who has not finished their turn. In a full round a turn
    ends with a suspicion vote; in the brief CP4 round it ends with a 'pass'."""
    rnd = private_round(team, cp)
    done = rnd["votes"] if is_full_private(cp) else {str(i): True for i in rnd.setdefault("passed", [])}
    for i in range(len(team["players"])):
        if str(i) not in done:
            return i
    return None


def private_complete(team, cp):
    if is_full_private(cp):
        return private_current(team, cp) is None and bool(private_round(team, cp)["votes"])
    # Brief round: complete once every player has passed through.
    rnd = private_round(team, cp)
    return private_current(team, cp) is None and bool(rnd.get("started"))


def suspicion_report(team, cp):
    rnd = private_round(team, cp)
    tally = {}
    for v in rnd["votes"].values():
        tally[v] = tally.get(v, 0) + 1
    if not tally:
        return "<p>No suspicion has been recorded.</p>"
    top = max(tally.values())
    leaders = [c for c, n in tally.items() if n == top]
    if top == 1:
        text = "Suspicion is scattered across the team."
    elif len(leaders) == 1:
        text = "Suspicion is concentrated around one member of the team."
    else:
        text = "Suspicion is divided across more than one member of the team."
    return "<p class='big'>" + text + "</p>"


def sabotage_available(team, game):
    return (not team["sabotage"]["used"]) and (game["checkpoint_index"] in SABOTAGE_WINDOW)


def private_block(game_code, team_index, team, cp):
    """Embedded in a CP1/CP3/CP5 screen after the decision: neutral pass gate,
    or the vague suspicion report once everyone has gone. For the brief CP4 round
    (cp not in HAS_PRIVATE) the heading and the completed-state message are
    kept neutral so the Thunderflunked is not singled out."""
    full = is_full_private(cp)
    heading = "Private Player Check" if full else "Private Check"
    cur = private_current(team, cp)
    if cur is None:
        done = (suspicion_report(team, cp) if full
                else "<p>Everyone has passed the device.</p>")
        return ("<div class='card'><h2>" + heading + "</h2>" + done
                + "<p class='muted'>Please wait for the Game Master to advance.</p></div>")
    lead = ("Everyone takes a short private turn. " if not full else "")
    return (
        "<div class='card'><h2>" + heading + "</h2>"
        "<p>" + lead + "Pass the computer to <strong>" + e(team["players"][cur])
        + "</strong>. Everyone else should look away.</p>"
        # target=_top: if we are inside the split view, the private turn takes over
        # the WHOLE window so no allegiance/mission/vote ever renders beside Canvas.
        # (Harmless when already full-page.)
        "<form method='post' action='/team/private/ready' target='_top'>"
        "<input type='hidden' name='game_code' value='" + game_code + "'>"
        "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
        "<input type='hidden' name='cp' value='" + str(cp) + "'>"
        "<button type='submit'>I'm Ready</button></form></div>"
    )


def brief_pass_page(game_code, team_index, team, cp, player):
    """The only screen most players see in the brief CP4 round: a neutral 'nothing
    for you this round' pass screen (identical for everyone, so it never reveals
    who the Thunderflunked is)."""
    body = ("<div class='card'><h2>Private Check</h2>"
            "<p><strong>" + e(team["players"][player]) + "</strong> &mdash; there is nothing for "
            "you to do privately this round. Pass the computer to the next player.</p>"
            "<form method='post' action='/team/private/pass'>"
            + private_hidden(game_code, team_index, cp, player)
            + "<button type='submit'>Pass the Device</button></form></div>")
    return page("Private", body, "private")


def private_hidden(game_code, team_index, cp, player):
    return ("<input type='hidden' name='game_code' value='" + game_code + "'>"
            "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
            "<input type='hidden' name='cp' value='" + str(cp) + "'>"
            "<input type='hidden' name='player' value='" + str(player) + "'>")


def private_route(game_code, team_index, team, cp):
    """Return the next private sub-screen for the current player, or None when
    the current player's turn is done (caller then re-shows the checkpoint)."""
    cur = private_current(team, cp)
    if cur is None:
        return None
    i = cur
    k = str(i)
    rnd = private_round(team, cp)
    game = games[game_code]

    # Brief round (CP4): sabotage opportunity for the Thunderflunked only; everyone
    # else just passes. No allegiance / mission / vote.
    if not is_full_private(cp):
        if (i == team["thunderflunked"] and cp in SABOTAGE_WINDOW and sabotage_available(team, game)
                and not team["sabotage"]["prompted"].get(cp)):
            return sabotage_page(game_code, team_index, team, cp, i)
        return brief_pass_page(game_code, team_index, team, cp, i)

    # Step 0: allegiance reveal (once per player, ever).
    if i not in team["allegiance_seen"]:
        return allegiance_page(game_code, team_index, team, cp, i)

    # Step 1: secret mission (skip if already completed in an earlier round).
    mission_decided = (k in rnd["decisions"]) or (k in team["missions_complete"])
    if not mission_decided:
        return private_mission_form(game_code, team_index, team, cp, i)

    # Step 2: sabotage (only the Thunderflunked, only during the CP2-CP4 window,
    # offered once per eligible checkpoint, if still available).
    if (i == team["thunderflunked"] and cp in SABOTAGE_WINDOW and sabotage_available(team, game)
            and not team["sabotage"]["prompted"].get(cp)):
        return sabotage_page(game_code, team_index, team, cp, i)

    # Step 3: suspicion vote.
    if k not in rnd["votes"]:
        return private_vote_form(game_code, team_index, team, cp, i)
    return None


def allegiance_page(game_code, team_index, team, cp, player):
    is_tf = (player == team["thunderflunked"])
    if is_tf:
        role_html = ("<h3>You are THUNDERFLUNKED</h3>"
                     "<p>You're here to make sure they don't get the students and course through the semester.</p>"
                     "<p>You are the only Thunderflunked person at this table. Sabotage mainly through persuasion: "
                     "push reasonable ideas too far, favor individual fixes over scalable ones, spend capacity on "
                     "low-priority issues. Occasionally support good ideas to stay unsuspected.</p>")
    else:
        role_html = ("<h3>You are THUNDERSTRUCK</h3>"
                     "<p>You're here to get your students and course through the semester.</p>"
                     "<p>Someone at your table is Thunderflunked. You do not know who.</p>")
    body = ("<div class='card'><h2>Private Player Check</h2>"
            "<p><strong>" + e(team["players"][player]) + "</strong> &mdash; your allegiance (keep it secret):</p>"
            "<div class='card'>" + role_html + "</div>"
            "<form method='post' action='/team/private/allegiance'>"
            + private_hidden(game_code, team_index, cp, player)
            + "<button type='submit'>Continue</button></form></div>")
    return page("Private", body, "private")


# ===========================================================================
# DIGITAL SECRET MISSIONS (Batch 5). Replaces the physical Secret Mission cards.
# Each player is dealt one mission during setup; it is revealed privately in the
# pass-the-laptop sequence and can be re-viewed (behind a privacy gate) and
# claimed during the CP1/CP3/CP5 private rounds, only inside the card's claim
# window. Content is the canonical 18 printed missions, verbatim.
#
# DESIGN NOTE: every mission is a social/persuasion goal (get the team to discuss,
# shift a mind, raise sustainability). Software cannot observe whether the table's
# conversation actually met the condition, so completion stays a deliberate,
# self-attested Claim -- exactly as the printed cards worked. What the software
# NOW enforces reliably is the claim TIMING (which checkpoints a mission may be
# claimed at) and single-award (a mission can be completed once). Rewards/scoring
# are unchanged: a mission is a secondary end-of-game recognition only.
# ===========================================================================
_CLAIM_ANY = frozenset({0, 2, 4})   # "any Mission Check: CP1, CP3, or CP5"
_CLAIM_MID = frozenset({2, 4})      # "CP3 or CP5"
_CLAIM_END = frozenset({4})         # "CP5 / endgame only"

MISSIONS = [
    {"n": 1,  "claim": _CLAIM_ANY, "role": None,
     "text": "Get another player to support proactive outreach to a student who has not asked for help."},
    {"n": 2,  "claim": _CLAIM_ANY, "role": None,
     "text": "Get the team to explicitly discuss the difference between a student problem and a course problem before deciding."},
    {"n": 3,  "claim": _CLAIM_END, "role": "Efficiency Gremlin",
     "text": "Get the Efficiency Gremlin to back a reusable, scalable solution at least once."},
    {"n": 4,  "claim": _CLAIM_ANY, "role": None,
     "text": "Get the team to look at course-activity or practice data (not just grades) before choosing an action."},
    {"n": 5,  "claim": _CLAIM_END, "role": None,
     "text": "Convince the team to decline adding at least one new thing in the Final Stretch."},
    {"n": 6,  "claim": _CLAIM_MID, "role": "Receipts Department",
     "text": "Get the Receipts Department to state out loud what the evidence does and does not show."},
    {"n": 7,  "claim": _CLAIM_END, "role": None,
     "text": "Shift another player off a position they argued for earlier. Change at least one mind during the game."},
    {"n": 8,  "claim": _CLAIM_ANY, "role": None,
     "text": "Get the team to choose a whole-class response over an individual one at least once."},
    {"n": 9,  "claim": _CLAIM_END, "role": None,
     "text": "Persuade the team to choose an option because it protects Faculty Capacity, and keep Faculty Capacity out of Critical for the entire game."},
    {"n": 10, "claim": _CLAIM_MID, "role": "Burnout Bouncer",
     "text": "Get the Burnout Bouncer to raise sustainability before the team finalizes a decision."},
    {"n": 11, "claim": _CLAIM_END, "role": None,
     "text": "Get the team to fix gradebook accuracy (zeros / blank cells) at some point in the semester."},
    {"n": 12, "claim": _CLAIM_ANY, "role": None,
     "text": "Name a specific Course Reality from your Course Card and get the team to factor it into a decision."},
    {"n": 13, "claim": _CLAIM_END, "role": None,
     "text": "Get the team to intentionally defer a lower-priority issue rather than address everything."},
    {"n": 14, "claim": _CLAIM_ANY, "role": "Student Whisperer",
     "text": "Get the Student Whisperer to speak up for a student who has gone quiet."},
    {"n": 15, "claim": _CLAIM_END, "role": None,
     "text": "Persuade the team to choose a fix that improves Course Quality over a quicker option, and end the game with Course Quality in the Normal band (60+)."},
    {"n": 16, "claim": _CLAIM_MID, "role": None,
     "text": "Get the team to connect a performance gap to a practice-to-assessment alignment issue, not just effort."},
    {"n": 17, "claim": _CLAIM_END, "role": "Professional Buzzkill",
     "text": "Get the Professional Buzzkill to talk the team out of a decision by naming its later cost."},
    {"n": 18, "claim": _CLAIM_END, "role": None,
     "text": "Be the player who proposes the option the team ultimately adopts at three or more checkpoints."},
]
MISSION_BY_N = {m["n"]: m for m in MISSIONS}


def claim_window_label(mission):
    c = mission["claim"]
    if c == _CLAIM_END:
        return "Claimable at Checkpoint 5 (Final Stretch)"
    if c == _CLAIM_MID:
        return "Claimable at Checkpoint 3 or 5"
    return "Claimable at Checkpoint 1, 3, or 5"


def mission_art_src(n):
    return "/static/art/missions/mission_%02d.png" % int(n)


def ensure_missions(team):
    """Deal one distinct Secret Mission to each player (idempotent -- never
    re-rolled once stored). Role-specific missions are dealt only if that public
    role is in play, and no player is dealt the mission that names their own role
    (the printed deck's 'trade it' rule). Stored as
    team['missions'] = {str(player_index): mission_number}."""
    existing = team.get("missions")
    if existing:
        return existing
    assignments = team.get("assignments")
    if not assignments:
        return {}
    drawn_roles = {a["role"] for a in assignments}
    eligible = [m for m in MISSIONS if (m["role"] is None or m["role"] in drawn_roles)]
    n = len(assignments)
    result = {}
    for _attempt in range(60):
        pool = list(eligible)
        random.shuffle(pool)
        trial, used, ok = {}, set(), True
        for i in range(n):
            my_role = assignments[i]["role"]
            pick = next((m for m in pool if m["n"] not in used and m["role"] != my_role), None)
            if pick is None:
                ok = False
                break
            trial[str(i)] = pick["n"]
            used.add(pick["n"])
        if ok:
            result = trial
            break
    if not result:
        # Extremely unlikely fallback (always enough distinct missions for 4-6
        # players): relax only the self-role rule, never the distinctness.
        pool = random.sample(eligible, min(n, len(eligible)))
        result = {str(i): pool[i]["n"] for i in range(len(pool))}
    team["missions"] = result
    return result


def player_mission(team, player):
    return MISSION_BY_N.get(team.get("missions", {}).get(str(player)))


def mission_reveal_card(mission):
    """The private 'Your Secret Mission' card shown during the setup reveal. Only
    ever rendered for the single acting player on their own device."""
    if not mission:
        return ""
    return ("<div class='card mission-card'>"
            "<img src='" + mission_art_src(mission["n"]) + "' alt='Secret Mission' "
            "style='width:100%;max-width:300px;height:auto;border-radius:10px;display:block;margin:.4em auto'>"
            "<h3>Your Secret Mission</h3>"
            "<p>" + e(mission["text"]) + "</p>"
            "<p class='muted'>" + e(claim_window_label(mission)) + ".</p>"
            "<p class='muted'>Keep this to yourself. Completing it is a small end-of-game recognition only "
            "&mdash; it does not affect scoring or who wins, and it is separate from your allegiance.</p></div>")


def private_mission_form(game_code, team_index, team, cp, player, revealed=False):
    pname = e(team["players"][player])
    m = player_mission(team, player)
    if m is None:
        # Legacy team with no dealt mission on record: nothing to claim; continue.
        body = ("<div class='card'><h2>Private Player Check</h2>"
                "<p><strong>" + pname + "</strong> &mdash; you have no Secret Mission on record.</p>"
                "<form method='post' action='/team/private/mission/notyet'>"
                + private_hidden(game_code, team_index, cp, player)
                + "<button type='submit'>Continue</button></form></div>")
        return page("Private", body, "private")
    if not revealed:
        # Privacy gate: no mission text is shown until the player deliberately
        # reveals it (safe for re-viewing round after round).
        body = ("<div class='card'><h2>Private Player Check</h2>"
                "<p><strong>" + pname + "</strong> &mdash; Part A: Secret Mission</p>"
                "<p>Your mission is private. When no one else can see the screen, reveal it.</p>"
                "<form method='post' action='/team/private/mission/reveal'>"
                + private_hidden(game_code, team_index, cp, player)
                + "<button type='submit'>Reveal My Mission</button></form></div>")
        return page("Private", body, "private")
    claimable = cp in m["claim"]
    art = ("<img src='" + mission_art_src(m["n"]) + "' alt='Secret Mission' "
           "style='width:100%;max-width:280px;height:auto;border-radius:10px;display:block;margin:.3em auto'>")
    if claimable:
        timing = ("<p class='muted'>" + e(claim_window_label(m))
                  + " &mdash; you may claim it now if you have completed it.</p>")
        claim_btn = ("<form method='post' action='/team/private/mission/claim' style='display:inline'>"
                     + private_hidden(game_code, team_index, cp, player)
                     + "<button type='submit'>Claim Mission</button></form> ")
    else:
        timing = ("<p class='muted'>" + e(claim_window_label(m)) + ". You cannot claim it at this "
                  "checkpoint &mdash; you will be asked again when its window opens.</p>")
        claim_btn = ""
    body = ("<div class='card'><h2>Private Player Check</h2>"
            "<p><strong>" + pname + "</strong> &mdash; Part A: Secret Mission</p>"
            "<div class='card'>" + art + "<p>" + e(m["text"]) + "</p>" + timing + "</div>"
            "<p class='muted'>Completing a mission is a small bonus recognition at the end. It does not decide "
            "who wins, and it is separate from your Thunderstruck/Thunderflunked allegiance.</p>"
            + claim_btn +
            "<form method='post' action='/team/private/mission/notyet' style='display:inline'>"
            + private_hidden(game_code, team_index, cp, player)
            + "<button class='secondary' type='submit'>Not Yet</button></form></div>")
    return page("Private", body, "private")


def private_mission_confirm_page(game_code, team_index, team, cp, player):
    m = player_mission(team, player)
    mtext = ("<p class='muted'>&ldquo;" + e(m["text"]) + "&rdquo;</p>") if m else ""
    body = ("<div class='card'><h2>Private Player Check</h2>"
            "<p><strong>" + e(team["players"][player]) + "</strong>: confirm you have completed this mission. "
            "Only claim it if its condition was genuinely met.</p>" + mtext +
            "<form method='post' action='/team/private/mission/confirm' style='display:inline'>"
            + private_hidden(game_code, team_index, cp, player) + "<button type='submit'>Confirm Claim</button></form> "
            "<form method='post' action='/team/private/mission/back' style='display:inline'>"
            + private_hidden(game_code, team_index, cp, player)
            + "<button class='secondary' type='submit'>Go Back</button></form></div>")
    return page("Private", body, "private")


def private_vote_form(game_code, team_index, team, cp, voter, error_message=""):
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    candidates = [i for i in range(len(team["players"])) if i != voter]
    random.shuffle(candidates)
    options = ""
    for pos, cand in enumerate(candidates, start=1):
        options += ("<label><input type='radio' name='candidate' value='" + str(cand) + "'> "
                    + str(pos) + ". " + e(team["players"][cand]) + "</label>")
    body = ("<div class='card'><h2>Private Player Check</h2>"
            "<p><strong>" + e(team["players"][voter]) + "</strong> &mdash; Part B: Suspicion Vote</p>"
            "<p>Select the person you currently suspect most. This is not your final accusation.</p>"
            + error_html +
            "<form method='post' action='/team/private/vote'>"
            + private_hidden(game_code, team_index, cp, voter)
            + options + "<p><button type='submit'>Confirm Vote</button></p></form></div>")
    return page("Private", body, "private")


def _private_team(game_code):
    team_index = request.form.get("team_index", "")
    cp = request.form.get("cp", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return None
    if not cp.isdigit():
        return None
    return get_team(game_code, int(team_index)), int(team_index), int(cp)


def _private_resume(game_code, team_index, team, cp):
    nxt = private_route(game_code, team_index, team, cp)
    if nxt is None:
        return checkpoint_screen(game_code, team_index, team)
    return nxt


@app.route("/team/private/ready", methods=["POST"])
def team_private_ready():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    private_round(team, cp)["started"] = True
    return _private_resume(game_code, ti, team, cp)


@app.route("/team/private/allegiance", methods=["POST"])
def team_private_allegiance():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    player = request.form.get("player", "")
    if player.isdigit() and int(player) not in team["allegiance_seen"]:
        team["allegiance_seen"].append(int(player))
    return _private_resume(game_code, ti, team, cp)


@app.route("/team/private/mission/reveal", methods=["POST"])
def team_private_mission_reveal():
    """Deliberate privacy-gate step: re-render the current player's mission WITH
    its text shown (transient -- a refresh returns to the gate)."""
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    cur = private_current(team, cp)
    if cur is None:
        return checkpoint_screen(game_code, ti, team)
    return private_mission_form(game_code, ti, team, cp, cur, revealed=True)


@app.route("/team/private/mission/claim", methods=["POST"])
def team_private_mission_claim():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    cur = private_current(team, cp)
    if cur is None:
        return checkpoint_screen(game_code, ti, team)
    m = player_mission(team, cur)
    # Enforce the claim window server-side: a mission can only be claimed at the
    # checkpoints its card allows. Out of window -> back to the revealed mission.
    if m is not None and cp not in m["claim"]:
        return private_mission_form(game_code, ti, team, cp, cur, revealed=True)
    return private_mission_confirm_page(game_code, ti, team, cp, cur)


@app.route("/team/private/mission/back", methods=["POST"])
def team_private_mission_back():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    return _private_resume(game_code, ti, team, cp)


@app.route("/team/private/mission/confirm", methods=["POST"])
def team_private_mission_confirm():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    cur = private_current(team, cp)
    if cur is None:
        return checkpoint_screen(game_code, ti, team)
    m = player_mission(team, cur)
    # Enforce the claim window here too, so a mission can never be completed
    # outside the checkpoints its card allows (even via a direct/replayed POST).
    if m is not None and cp not in m["claim"]:
        return private_mission_form(game_code, ti, team, cp, cur, revealed=True)
    k = str(cur)
    private_round(team, cp)["decisions"][k] = "complete"
    if k not in team["missions_complete"]:
        team["missions_complete"].append(k)
    return _private_resume(game_code, ti, team, cp)


@app.route("/team/private/mission/notyet", methods=["POST"])
def team_private_mission_notyet():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    cur = private_current(team, cp)
    if cur is None:
        return checkpoint_screen(game_code, ti, team)
    private_round(team, cp)["decisions"][str(cur)] = "not_yet"
    return _private_resume(game_code, ti, team, cp)


@app.route("/team/private/pass", methods=["POST"])
def team_private_pass():
    """Ends a player's turn in the brief CP4 round (no vote)."""
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    cur = private_current(team, cp)
    if cur is None:
        return checkpoint_screen(game_code, ti, team)
    passed = private_round(team, cp).setdefault("passed", [])
    if cur not in passed:
        passed.append(cur)
    # Ends this player's brief turn: return to the pass gate (checkpoint screen),
    # not the next player's screen, so the device is handed over deliberately.
    return checkpoint_screen(game_code, ti, team)


@app.route("/team/private/vote", methods=["POST"])
def team_private_vote():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    cur = private_current(team, cp)
    # The voter index is submitted in the hidden "player" field (see private_hidden).
    voter = request.form.get("player", "")
    if cur is None or voter != str(cur):
        return checkpoint_screen(game_code, ti, team)
    candidate = request.form.get("candidate", "")
    valid = [str(i) for i in range(len(team["players"])) if i != cur]
    if candidate not in valid:
        return private_vote_form(game_code, ti, team, cp, cur,
                                 "Please select one candidate (you cannot vote for yourself).")
    private_round(team, cp)["votes"][str(cur)] = candidate
    # The vote ends this player's turn: return to the pass gate (checkpoint screen),
    # which shows "pass to the next player" or the vague report when everyone is done.
    # Do NOT auto-advance into the next player's private screens on this device.
    return checkpoint_screen(game_code, ti, team)


# ===========================================================================
# EMERGENCY SABOTAGE - offered to the Thunderflunked inside their private turn
# at CP3 (index 2, full private round) and CP4 (index 3, brief round). One time
# total; expires after CP4. (Keys are checkpoint indices.)
# ===========================================================================
SABOTAGE_OPTIONS = {
    2: [  # CP3 (full private round)
        {"key": "revision_drag", "label": "Revision grading drags on longer than expected", "deltas": D(fc=-3)},
        {"key": "reteach_delay", "label": "The reteach is delayed about a week", "deltas": D(ss=-3)},
        {"key": "practice_clunky", "label": "The new practice rolls out clunky and eats time", "deltas": D(tr=-3)},
    ],
    3: [  # CP4 (brief round)
        {"key": "outreach_delay", "label": "The priority outreach is delayed", "deltas": D(ss=-3)},
        {"key": "feedback_misroute", "label": "Feedback routing is misconfigured", "deltas": D(cq=-3)},
        {"key": "manual_tracking", "label": "Extra manual tracking is introduced", "deltas": D(fc=-3)},
    ],
}


# ===========================================================================
# HALLWAY RUMORS (Batch 5). When a Thunderflunked player ACTIVATES their one
# Emergency Sabotage, the room never sees a mechanical "sabotage!" alert. Instead
# a single faculty-lounge rumor is queued privately for the GM, who decides when
# (if ever) to read it to the room as a full-screen projector takeover titled
# "WHAT YOU'RE HEARING IN THE HALLWAYS...". The public wording describes only the
# observable fallout -- never the mechanic, never a Traitor / sabotage / secret
# action, never the responsible player, their allegiance, or the affected team.
# ===========================================================================
RUMOR_TITLE = "WHAT YOU'RE HEARING IN THE HALLWAYS..."
RUMOR_COPY = {
    "revision_drag": "Word around the department is that revision grades in one course are taking forever to post. Students keep refreshing the gradebook and finding nothing there yet, and a few are starting to wonder if their work got lost.",
    "reteach_delay": "People are saying a planned re-teach got pushed back about a week. The students who were counting on that second pass are grumbling that the help they were promised keeps sliding further out.",
    "practice_clunky": "Rumor has it a brand-new practice activity rolled out a little rough. A class spent a good chunk of its time just getting it to open, and the minutes that should have gone to learning went to troubleshooting instead.",
    "outreach_delay": "There is chatter that the big push to reach struggling students stalled out. The outreach that was supposed to go this week still has not landed, and a couple of the quieter students seem to be drifting.",
    "feedback_misroute": "Word in the hallway is that feedback is ending up in the wrong place. Students swear they never saw comments an instructor is just as sure were sent, and nobody can quite find where they went.",
    "manual_tracking": "Heard through the grapevine that someone is tracking everything by hand now, off in a separate spreadsheet. It is twice the work, and people are nervous something is going to slip through the cracks.",
}
_RUMOR_FALLBACK = "Something seems to be slowing one of the courses down this week, and students are starting to notice."


def _rumors_read(game):
    """Non-mutating view of rumor state (safe on GET requests / signatures)."""
    r = game.get("rumors")
    if isinstance(r, dict):
        return {"queue": r.get("queue", []), "announced": r.get("announced", []),
                "active": r.get("active")}
    return {"queue": [], "announced": [], "active": None}


def rumors_state(game):
    """Mutating getter (POST handlers only): ensure the rumor store exists."""
    r = game.get("rumors")
    if not isinstance(r, dict):
        r = {"queue": [], "announced": [], "active": None}
        game["rumors"] = r
    r.setdefault("queue", [])
    r.setdefault("announced", [])
    if "active" not in r:
        r["active"] = None
    return r


def enqueue_rumor(game, team_index, option_key, cause):
    """Queue exactly one hallway rumor for a team's sabotage activation. Idempotent
    per team (sabotage is one-time, so a team contributes at most one rumor)."""
    r = rumors_state(game)
    rid = "t" + str(team_index)
    known = {x["id"] for x in r["queue"]} | {x["id"] for x in r["announced"]}
    if r.get("active"):
        known.add(r["active"]["id"])
    if rid in known:
        return
    r["queue"].append({"id": rid, "team_index": team_index, "key": option_key, "cause": cause})


def rumor_public_text(rumor):
    return RUMOR_COPY.get((rumor or {}).get("key"), _RUMOR_FALLBACK)


def rumors_sig(game):
    """Signature of rumor state for reload triggers (projector + GM dashboard).
    Never added to game_rev/team_rev, so team devices are never reloaded by a
    rumor (their private forms are never disturbed)."""
    r = _rumors_read(game)
    active = r["active"]
    return "R%d|%d|%s" % (len(r["queue"]), len(r["announced"]), active["id"] if active else "-")


def sabotage_page(game_code, team_index, team, cp, player):
    """Offered to the Thunderflunked during their private turn at CP3 (full round)
    or CP4 (brief round).
    Activate applies a small hidden effect; Save For Later continues the turn."""
    opts = SABOTAGE_OPTIONS.get(cp, [])
    options_html = ""
    for o in opts:
        options_html += ("<label><input type='radio' name='option' value='" + o["key"] + "'> " + o["label"] + "</label>")
    body = ("<div class='card'><h2>Emergency Sabotage Available</h2>"
            "<p>You are Thunderflunked. You may activate your one Emergency Sabotage now, or save it for a "
            "later checkpoint. It is small by design, and it expires after Checkpoint 4. No one else will be "
            "told you did this.</p>"
            "<form method='post' action='/team/sabotage/activate'>"
            + private_hidden(game_code, team_index, cp, player)
            + options_html + "<p><button type='submit'>Activate</button></p></form>"
            "<form method='post' action='/team/sabotage/save' style='display:inline'>"
            + private_hidden(game_code, team_index, cp, player)
            + "<button class='secondary' type='submit'>Save For Later</button></form></div>")
    return page("Private", body, "private")


@app.route("/team/sabotage/save", methods=["POST"])
def sabotage_save():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    team["sabotage"]["prompted"][cp] = True  # don't re-offer this checkpoint
    return _private_resume(game_code, ti, team, cp)


@app.route("/team/sabotage/activate", methods=["POST"])
def sabotage_activate():
    game_code = request.form.get("game_code", "").strip().upper()
    r = _private_team(game_code)
    if r is None:
        return error_page("Something went wrong. Please refresh and try again.")
    team, ti, cp = r
    game = games[game_code]
    option = request.form.get("option", "")

    # Expired or invalid checkpoint: silently continue the private turn.
    if not sabotage_available(team, game) or cp not in SABOTAGE_OPTIONS:
        team["sabotage"]["prompted"][cp] = True
        return _private_resume(game_code, ti, team, cp)

    chosen = next((o for o in SABOTAGE_OPTIONS[cp] if o["key"] == option), None)
    if chosen is None:
        player = int(request.form.get("player", "0"))
        return sabotage_page(game_code, ti, team, cp, player)

    apply_deltas(team, chosen["deltas"])
    team["sabotage"]["used"] = True
    team["sabotage"]["cp"] = cp + 1  # store as 1-based checkpoint number
    team["sabotage"]["label"] = chosen["label"]
    team["sabotage"]["prompted"][cp] = True
    # A successful activation (never a preview or Save For Later) queues exactly
    # one private hallway rumor for the GM to announce when they choose. The effect
    # was applied once, just above; announcing the rumor later never re-applies it.
    enqueue_rumor(game, ti, chosen["key"], chosen["label"])
    return _private_resume(game_code, ti, team, cp)


# ===========================================================================
# COURSE SHELL (Canvas-style read-only view of the team's course, updating at
# each checkpoint and reflecting the team's own decisions). This is a
# presentation surface over existing state - no new simulation. It never shows
# allegiance, missions, suspicion, or votes.
# ===========================================================================
SHELL_TABS = [
    ("home", "Home"),
    ("syllabus", "Syllabus"),
    ("modules", "Modules"),
    ("gradebook", "Gradebook"),
    ("announcements", "Announcements"),
    ("inbox", "Inbox"),
    ("roster", "Roster"),
    ("resources", "Student Support & Tools"),
]

# Graded items and when (by checkpoint index) they first appear in the gradebook.
GB_ITEMS = [
    {"key": "a1", "name": "Assignment 1: Getting Started", "reveal": 0, "max": 20, "type": "simple"},
    {"key": "a2", "name": "Assignment 2", "reveal": 1, "max": 20, "type": "simple"},
    {"key": "ma1", "name": "Major Assessment 1", "reveal": 2, "max": 100, "type": "major"},
    {"key": "a3", "name": "Assignment 3", "reveal": 3, "max": 20, "type": "simple"},
    {"key": "final", "name": "Final Project", "reveal": 4, "max": 100, "type": "simple"},
]

MA1_CATEGORIES = ["Credibility Analysis", "Evidence Selection",
                  "Source Comparison", "Justification of Evaluation"]


def shell_url(game_code, team_index, tab="home"):
    return ("/team/shell?game_code=" + game_code + "&team_index=" + str(team_index)
            + "&tab=" + tab)


def shell_cp_index(game_code):
    return games[game_code]["checkpoint_index"]


def shell_active_roster(team):
    return team.get("active_roster") or team.get("starting_roster", [])


def _enrolled_from(team, label):
    """Checkpoint index at which a student is on the roster (0 start; 1 if late add)."""
    return 1 if label in team.get("late_adds", []) else 0


def _zeros_entered(team):
    return "zeros" in team.get("cp", {}).get(0, {}).get("actions", [])


def _addressed_alignment(team):
    acts = set(team.get("cp", {}).get(2, {}).get("actions", []))
    return bool(acts & {"reteach", "aligned_practice", "revision"})


def _gb_missing(team, label, item):
    """Deterministic (no RNG) submission state, with cast-driven narrative overrides."""
    c = team_cast(team)
    st = STUDENT_STATS[label]
    if item["key"] == "a1" and label in (c.get("calendar"), c.get("dodge")):
        return True
    if item["key"] == "a3" and label == c.get("dodge"):
        return True  # Midterm persistence risk: misses another assignment
    return (st["self_management"] + st["motivation"]) <= 4


def _gb_simple_score(label, maxpts):
    st = STUDENT_STATS[label]
    ab = st["readiness"] + st["motivation"]  # 2..10
    frac = 0.55 + (ab - 2) / 8.0 * 0.4       # 0.55..0.95
    return int(round(maxpts * frac))


def _gb_major(team, label):
    st = STUDENT_STATS[label]
    ab = st["readiness"] + st["motivation"]            # 2..10
    practice = st["self_management"] + st["help_seeking"]  # engagement proxy
    cred = min(25, 15 + ab)
    evid = min(25, 13 + ab)
    source = min(25, 10 + int(round(ab * 0.9)))
    justif = max(3, min(25, 5 + int(round((ab - 4) * 0.9))))  # the authored weak skill
    total = cred + evid + source + justif
    if practice <= 4:
        total = max(35, total - 12)
    return {"total": total,
            "cats": dict(zip(MA1_CATEGORIES, [cred, evid, source, justif]))}


def _gb_cell(team, label, item, cp_index):
    """Return (display, css_class) for one gradebook cell."""
    if _enrolled_from(team, label) > item["reveal"]:
        return ("enrolled later", "muted")
    if item["type"] == "major":
        m = _gb_major(team, label)
        return (str(m["total"]) + " / 100", "")
    if _gb_missing(team, label, item):
        return ("0" if _zeros_entered(team) else "missing", "status-Critical" if _zeros_entered(team) else "muted")
    score = _gb_simple_score(label, item["max"])
    if item["key"] == "final" and _addressed_alignment(team):
        score = min(item["max"], score + 2)  # prior alignment work pays off
    return (str(score) + " / " + str(item["max"]), "")


def shell_gradebook(team, cp_index):
    roster = shell_active_roster(team)
    items = [it for it in GB_ITEMS if it["reveal"] <= cp_index]
    if not items:
        return "<div class='card'><p>No graded items have been posted yet.</p></div>"
    head = "<tr><th>Student</th>" + "".join("<th>" + it["name"] + "</th>" for it in items) + "</tr>"
    rows = ""
    for s in roster:
        cells = ""
        for it in items:
            disp, cls = _gb_cell(team, s, it, cp_index)
            cells += "<td class='" + cls + "'>" + disp + "</td>"
        late = " <span class='pill'>late add</span>" if s in team.get("late_adds", []) else ""
        rows += "<tr><td>" + e(s) + late + "</td>" + cells + "</tr>"
    out = ("<div class='card'><h2>Gradebook</h2>"
           "<p class='muted'>Missing work shows as "
           + ("<strong>0</strong> because your team entered zeros."
              if _zeros_entered(team) else "<strong>missing</strong> (blank cell) - the gradebook may look better than reality.")
           + "</p>"
           "<div style='overflow-x:auto'><table>" + head + rows + "</table></div></div>")
    # Major-assessment detail once it exists.
    if any(it["key"] == "ma1" for it in items):
        totals = [_gb_major(team, s)["total"] for s in roster if _enrolled_from(team, s) <= 2]
        avg = round(sum(totals) / len(totals)) if totals else 0
        cat_rows = ""
        for s in roster:
            if _enrolled_from(team, s) > 2:
                continue
            m = _gb_major(team, s)
            cat_rows += ("<tr><td>" + e(s) + "</td>"
                         + "".join("<td>" + str(m["cats"][c]) + " / 25</td>" for c in MA1_CATEGORIES)
                         + "</tr>")
        out += ("<div class='card'><h3>Major Assessment 1 - category breakdown</h3>"
                "<p><strong>Class average: " + str(avg) + "%.</strong> Most of the lost points are in "
                "<strong>Justification of Evaluation</strong> - including some highly engaged students. "
                "Practice asked students to <em>identify</em> the stronger source; the rubric expected them to "
                "<em>justify</em> the evaluation with evidence.</p>"
                "<div style='overflow-x:auto'><table><tr><th>Student</th>"
                + "".join("<th>" + c + "</th>" for c in MA1_CATEGORIES) + "</tr>"
                + cat_rows + "</table></div></div>")
    return out


def shell_syllabus(team, cp_index):
    course = COURSE_BY_NAME[team["course"]]
    mismatch = ""
    if cp_index == 0:
        mismatch = ("<div class='notice'><p><strong>Heads up:</strong> the syllabus of record lists Assignment 1 "
                    "as due <strong>Sunday 11:59 PM</strong>, but the Canvas due date currently shows "
                    "<strong>Thursday</strong>. These disagree.</p></div>")
    return (
        "<div class='card'><h2>Syllabus</h2>"
        "<p><strong>" + e(course["name"]) + "</strong> (" + course["code"] + ") &mdash; "
        + str(course["credits"]) + " credits &mdash; " + course["modality"] + "</p>"
        "<p>" + course["desc"] + "</p>"
        "<h3>Meeting / learning pattern</h3><p>" + course["pattern"] + "</p>"
        "<h3>Assessment pattern</h3><p>" + course["assessment"] + "</p>"
        "<h3>Prerequisite</h3><p>" + course["prereq"] + "</p>"
        "<h3>Course notes (structural)</h3><ul><li>" + course["realities"][0] + "</li>"
        "<li>" + course["realities"][1] + "</li></ul></div>"
        + mismatch +
        "<div class='card'><h3>Schedule (excerpt)</h3><table>"
        "<tr><th>When</th><th>Item</th><th>Due</th></tr>"
        "<tr><td>Week 1</td><td>Assignment 1: Getting Started</td><td>Sunday 11:59 PM (syllabus of record)</td></tr>"
        "<tr><td>Early term</td><td>Assignment 2</td><td>Posted in Canvas</td></tr>"
        "<tr><td>Mid term</td><td>Major Assessment 1</td><td>Posted in Canvas</td></tr>"
        "<tr><td>Later</td><td>Assignment 3 / Final Project</td><td>Posted in Canvas</td></tr>"
        "</table></div>"
    )


def shell_modules(team, cp_index):
    course = COURSE_BY_NAME[team["course"]]
    modality = course["modality"]
    if "Asynchronous" in modality:
        gate = ("This course is self-paced online with no fixed meeting time, so the Start Here module and clear "
                "navigation carry the course. Students who drift are hard to see.")
    elif "CTE" in modality:
        gate = ("Competencies are sequential: later modules and the capstone require earlier ones to be complete. "
                "Lab/shop stations and build teams are limited.")
    else:
        gate = "Modules are aligned to the in-class meetings; content builds week to week."
    names = ["Start Here & Syllabus", "Module 1: Foundations", "Module 2",
             "Module 3", "Module 4", "Final / Capstone"]
    current = min(cp_index + 1, len(names) - 1)
    items = ""
    for i, nm in enumerate(names):
        state = "complete" if i < current else ("current" if i == current else "upcoming")
        tag = {"complete": "<span class='pill'>done</span>",
               "current": "<span class='pill'>current</span>",
               "upcoming": "<span class='muted'>upcoming</span>"}[state]
        items += "<li>" + nm + " &mdash; " + tag + "</li>"
    late_note = ""
    if cp_index >= 1 and team.get("late_adds"):
        late_note = ("<div class='notice'><p>New students enter at the current module. Use the Start Here module "
                     "and onboarding to help them catch up on foundational content.</p></div>")
    return ("<div class='card'><h2>Modules</h2><p class='muted'>" + gate + "</p>"
            "<ul class='clean'>" + items + "</ul></div>" + late_note)


def _resource_card_html(r):
    needs = " ".join("<span class='pill'>" + e(NEED_LABELS[n]) + "</span>" for n in r["needs"])
    cat_cls = "tool" if r["category"] == TOOL else "support"
    return (
        "<div class='card resource " + cat_cls + "'>"
        "<p class='muted' style='margin:0 0 4px'><strong>" + e(r["category"]) + "</strong></p>"
        "<h3 style='margin:0 0 6px'>" + e(r["name"]) + "</h3>"
        "<p style='margin:0 0 8px'>" + needs + "</p>"
        "<p><strong>What it is:</strong> " + e(r["what"]) + "</p>"
        "<p><strong>When it may help:</strong> " + e(r["when"]) + "</p>"
        "<p><strong>How faculty use/connect:</strong> " + e(r["how"]) + "</p>"
        "<p class='muted'><strong>Limitations:</strong> " + e(r["limits"]) + "</p>"
        "</div>"
    )


def resources_panel_full(team):
    """Full two-category reference for the course shell Resources tab."""
    tools = "".join(_resource_card_html(r) for r in COURSE_TOOLS)
    supports = "".join(_resource_card_html(r) for r in STUDENT_SUPPORTS)
    return (
        "<div class='card'><h2>Resources &amp; Student Support</h2>"
        "<p class='muted'>Two kinds of help. <strong>Course tools</strong> are what you do inside "
        "this course. <strong>Student supports</strong> are real NCC services you connect a student "
        "with. Match the response to what the evidence actually shows: a support need, a course-design "
        "need, a communication need, an advising/persistence concern, or an accessibility need.</p></div>"
        "<div class='card notice'><p><strong>Choosing a student support for this course:</strong> "
        + support_context_note(team) + "</p></div>"
        "<div class='card'><h3>Student Supports (real NCC services)</h3></div>" + supports
        + "<div class='card'><h3>Course Tools (inside Canvas)</h3></div>" + tools
    )


def shell_resources(team, cp_index):
    return resources_panel_full(team)


def resources_drawer(team):
    """Always-available Resources overlay for the PUBLIC Team Workspace. Reuses
    the existing 12 instructor tools + 6 student supports and their descriptions
    (no new resources, no rewritten text). Category and teaching-context filters
    are a pure-CSS radio group that works with JavaScript disabled; JS adds
    Escape-to-close, the close button, backdrop-click, and focus handling. It
    opens in place without navigating, so decision-form selections are preserved.
    Rendered only on public gameplay screens -- never on a private screen."""
    mc = modality_class(COURSE_BY_NAME.get(team.get("course"), {}).get("modality"))
    items = ""
    for r in RESOURCES:
        cat = "tool" if r["category"] == TOOL else "support"
        src = RESOURCE_IMAGE.get(r["name"])
        img = ("<img src='" + src + "' alt='' loading='lazy' class='res-img'>") if src else ""
        mnote = ("<p class='muted'><strong>Teaching-context note:</strong> " + e(r["modality_note"])
                 + "</p>") if r.get("modality_note") else ""
        items += ("<div class='res-item' data-cat='" + cat + "'>" + img
                  + _resource_card_html(r) + mnote + "</div>")
    notes = ("<p class='res-note res-note-online'>" + support_context_note_for("online") + "</p>"
             "<p class='res-note res-note-campus'>" + support_context_note_for("campus") + "</p>"
             "<p class='res-note res-note-cte'>" + support_context_note_for("cte") + "</p>")
    ck = {"online": "", "campus": "", "cte": ""}
    ck[mc] = " checked"
    css = ("<style>"
           ".res-drawer{display:block;margin:6px 0 0;}"
           ".res-open{display:inline-block;padding:8px 16px;background:var(--red);color:#fff;"
           "border-radius:8px;font-weight:700;cursor:pointer;list-style:none;}"
           ".res-open::-webkit-details-marker{display:none;}.res-open::marker{content:'';}"
           # Closed by default so the fixed backdrop/panel can never intercept
           # clicks or focus on the decision controls; shown only when [open].
           # (Author rules can override the UA's details-closed hiding, so we gate
           # display explicitly rather than relying on it.)
           ".res-overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:60;"
           "justify-content:flex-end;display:none;}"
           ".res-drawer[open] .res-overlay{display:flex;}"
           ".res-panel{background:#fff;color:#2d3b45;width:min(600px,100%);height:100%;overflow:auto;"
           "padding:16px 18px;box-shadow:-4px 0 20px rgba(0,0,0,.25);}"
           ".res-r{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);}"
           ".res-head{display:flex;justify-content:space-between;align-items:center;gap:12px;}"
           ".res-close{background:#edf0f2;border:0;border-radius:8px;font-size:1.4rem;line-height:1;"
           "padding:2px 12px;cursor:pointer;color:#2d3b45;}"
           ".res-bar{margin:8px 0;font-size:.9rem;}"
           ".res-bar label{display:inline-block;margin:2px 4px 2px 0;padding:6px 12px;border:1px solid #c7cdd1;"
           "border-radius:16px;cursor:pointer;background:#f5f5f5;color:#2d3b45;}"
           "#rc-all:checked~.res-bar label[for=\"rc-all\"],#rc-tools:checked~.res-bar label[for=\"rc-tools\"],"
           "#rc-supports:checked~.res-bar label[for=\"rc-supports\"],#rx-online:checked~.res-bar label[for=\"rx-online\"],"
           "#rx-campus:checked~.res-bar label[for=\"rx-campus\"],#rx-cte:checked~.res-bar label[for=\"rx-cte\"]"
           "{background:var(--red);color:#fff;border-color:var(--red);}"
           ".res-r:focus-visible~.res-bar label{outline:2px solid var(--red);outline-offset:2px;}"
           "#rc-tools:checked~.res-list .res-item[data-cat=\"support\"]{display:none;}"
           "#rc-supports:checked~.res-list .res-item[data-cat=\"tool\"]{display:none;}"
           ".res-note{display:none;}"
           "#rx-online:checked~.res-notes .res-note-online,#rx-campus:checked~.res-notes .res-note-campus,"
           "#rx-cte:checked~.res-notes .res-note-cte{display:block;}"
           ".res-notes{background:#faecec;border-radius:8px;padding:2px 12px;margin:6px 0;}"
           ".res-img{width:100%;max-width:120px;height:auto;border-radius:6px;float:right;margin:0 0 6px 10px;}"
           "@media(max-width:600px){.res-panel{width:100%;}.res-img{float:none;max-width:100%;}}"
           "</style>")
    body = ("<details class='res-drawer'>"
            "<summary class='res-open'>Resources &amp; Student Support</summary>"
            "<div class='res-overlay'>"
            "<div class='res-panel' role='dialog' aria-label='Teaching tools and student supports' tabindex='-1'>"
            "<input type='radio' class='res-r' name='res-cat' id='rc-all' checked>"
            "<input type='radio' class='res-r' name='res-cat' id='rc-tools'>"
            "<input type='radio' class='res-r' name='res-cat' id='rc-supports'>"
            "<input type='radio' class='res-r' name='res-ctx' id='rx-online'" + ck["online"] + ">"
            "<input type='radio' class='res-r' name='res-ctx' id='rx-campus'" + ck["campus"] + ">"
            "<input type='radio' class='res-r' name='res-ctx' id='rx-cte'" + ck["cte"] + ">"
            "<div class='res-head'><h2 style='margin:0'>Resources &amp; Student Support</h2>"
            "<button type='button' class='res-close' aria-label='Close resources'>&times;</button></div>"
            "<p class='muted' style='margin:.3em 0'>Browse teaching tools and student supports at any time. "
            "Reference only &mdash; it does not choose for you.</p>"
            "<div class='res-bar' role='radiogroup' aria-label='Filter by category'><strong>Show:</strong> "
            "<label for='rc-all'>All</label><label for='rc-tools'>Instructor Tools</label>"
            "<label for='rc-supports'>Student Supports</label></div>"
            "<div class='res-bar' role='radiogroup' aria-label='Teaching context'><strong>Teaching context:</strong> "
            "<label for='rx-online'>Online</label><label for='rx-campus'>Face-to-Face</label>"
            "<label for='rx-cte'>CTE / Lab</label></div>"
            "<div class='res-notes'>" + notes + "</div>"
            "<div class='res-list'>" + items + "</div>"
            "</div></div></details>")
    script = ("<script>(function(){var d=document.querySelector('.res-drawer');if(!d)return;"
              "var s=d.querySelector('summary'),p=d.querySelector('.res-panel'),"
              "x=d.querySelector('.res-close'),o=d.querySelector('.res-overlay');"
              "function close(){d.open=false;if(s)s.focus();}"
              "if(x)x.addEventListener('click',close);"
              "if(o)o.addEventListener('click',function(e){if(e.target===o)close();});"
              "d.addEventListener('toggle',function(){if(d.open&&p)p.focus();});"
              "document.addEventListener('keydown',function(e){"
              "if((e.key==='Escape'||e.key==='Esc')&&d.open)close();});"
              "})();</script>")
    return css + body + script


# A SMALL set of resources plausibly worth considering at each checkpoint - a mix
# of course tools and student supports, deliberately including options that may NOT
# fit, so the game never signals the "correct" choice. The full catalog lives on the
# shell's Student Support & Tools tab and on the physical cards.
CP_RELEVANT_RESOURCES = {
    0: ["Simple Syllabus", "Canvas Grades / Entering Zeros", "Message Students Who…",
        "Rubrics", "Announcements / Scheduled Announcements", "Brainfuse Online Tutoring"],
    1: ["Canvas Modules / Start Here", "Announcements / Scheduled Announcements",
        "Academic Advising", "Learning Center (Student Success Center)",
        "Brainfuse Online Tutoring", "Dropout Detective"],
    2: ["Rubrics", "SpeedGrader / Reusable Feedback", "Canvas Modules / Start Here",
        "Course Analytics / Access Report", "Brainfuse Online Tutoring",
        "Accessibility / Accommodation Services"],
    3: ["Dropout Detective", "Course Analytics / Access Report", "SpeedGrader / Reusable Feedback",
        "Message Students Who…", "Academic Advising", "TRiO Student Support Services"],
    4: ["Canvas Grades / Entering Zeros", "Grade Publish", "Course Improvement Survey",
        "SpeedGrader / Reusable Feedback", "Academic Advising",
        "Learning Center (Student Success Center)"],
}


def resources_consult_block(team, cp_index):
    """Compact, collapsible reference for the current checkpoint. Shows only a small,
    situation-relevant subset (mixing tools and supports, with some that may not fit)
    so faculty still have to interpret the evidence. It does not name a correct choice."""
    names = CP_RELEVANT_RESOURCES.get(cp_index, [])
    items = ""
    for nm in names:
        r = RESOURCES_BY_NAME.get(nm)
        if not r:
            continue
        tag = "Student support" if r["category"] == SUPPORT else "Course tool"
        items += ("<li><strong>" + e(r["name"]) + "</strong> <span class='muted'>(" + tag
                  + ")</span> &mdash; " + e(r["what"]) + "</li>")
    return (
        "<details class='card'><summary><strong>Consult your Course Tool and Student Support cards</strong> "
        "if one may help</summary>"
        "<p class='muted'>Decide what kind of need the evidence shows before you choose. Some of these may "
        "help here and some may not &mdash; you decide. Your physical cards and the "
        "<em>Student Support &amp; Tools</em> tab carry the full When / How / Limitations for every resource.</p>"
        "<ul>" + items + "</ul></details>"
    )


def shell_announcements(team, cp_index):
    cp0 = team.get("cp", {}).get(0, {}).get("actions", [])
    cp1 = team.get("cp", {}).get(1, {}).get("actions", [])
    cp2 = team.get("cp", {}).get(2, {}).get("actions", [])
    anns = [("Week 0", "Welcome to " + e(team["course"]),
             "Welcome! Please review the syllabus and complete Assignment 1.")]
    if "clarify" in cp0:
        anns.append(("Week 1", "Clarification on Assignment 1",
                     "Several of you asked about expectations. Here is a clearer explanation, an example, "
                     "and the rubric."))
    if "fix_schedule" in cp0:
        anns.append(("Week 1", "Corrected due date for Assignment 1",
                     "The syllabus and Canvas listed different due dates. Canvas now matches the syllabus of "
                     "record. Thank you to those who flagged it."))
    if "extend" in cp0:
        anns.append(("Week 1", "Assignment 1 deadline extended",
                     "The Assignment 1 due date has been extended for everyone."))
    if "tutoring" in cp0:
        anns.append(("Week 1", "Free tutoring: Learning Center (Student Success Center) and Brainfuse",
                     "Reminder: the Learning Center (Student Success Center) in Building A offers free tutoring, and "
                     "Brainfuse online tutoring (the sidebar link in this course, 15 hours per year) is free as well."))
    if cp_index >= 1 and "start_here" in cp1:
        anns.append(("Early semester", "Start Here for new students",
                     "If you recently joined, begin with this message and the Start Here module."))
    if cp_index >= 1 and "orientation" in cp1:
        anns.append(("Early semester", "Orientation / check-in",
                     "Optional live check-in to get oriented. Details inside."))
    if cp_index >= 1 and "advisor" in cp1:
        anns.append(("Early semester", "Support for new students",
                     "New students: your advisor, the Learning Center (Student Success Center), and Brainfuse online tutoring are ready to help you catch up."))
    if cp_index >= 2 and ("reteach" in cp2 or "aligned_practice" in cp2):
        anns.append(("After Assessment 1", "Revisiting justification",
                     "We will revisit how to justify an evaluation with evidence. New aligned practice is posted."))
    if cp_index >= 4:
        anns.append(("Final stretch", "End-of-term checklist",
                     "Remaining required work, the final grades timeline, and the Course Improvement Survey."))
    cards = ""
    for when, title, body in reversed(anns):
        cards += ("<div class='card'><p class='muted'>" + when + "</p><h3>" + title + "</h3><p>" + body + "</p></div>")
    return "<div class='card'><h2>Announcements</h2></div>" + cards


def shell_inbox(team, cp_index):
    c = team_cast(team)
    msgs = [("Week 1", e(c["question_mark"]),
             "I turned it in but I'm not sure I did the right thing - the instructions were confusing."),
            ("Week 1", "Course note",
             "No message has been received from " + e(c["silent_sos"]) + ", who is active and submitting but "
             "appears to share the same misunderstanding. They have not asked for help.")]
    if cp_index >= 1 and team.get("late_adds"):
        msgs.append(("Early semester", e(team["late_adds"][0]),
                     "Hi, I was just added to this course - where should I start?"))
    if cp_index >= 3:
        msgs.append(("Midterm", e(c["strong"]),
                     "I can't find your feedback comments on my last assignment - where do I look?"))
    if cp_index >= 4:
        msgs.append(("Final stretch", e(c["engaged"]),
                     "Is there any extra credit available to bring my grade up?"))
    cards = ""
    for when, who, body in msgs:
        cards += ("<div class='card'><p class='muted'>" + when + " &mdash; from " + who + "</p><p>" + body + "</p></div>")
    return "<div class='card'><h2>Inbox</h2></div>" + cards


def shell_roster(team, cp_index):
    roster = shell_active_roster(team)
    rows = ""
    labels = ["R", "M", "C", "SM", "A", "HS"]
    for s in roster:
        st = STUDENT_STATS[s]
        late = " <span class='pill'>late add</span>" if s in team.get("late_adds", []) else ""
        stat_cells = "".join("<td>" + str(st[k]) + "</td>" for k in STAT_KEYS)
        rows += ("<tr><td>" + e(s) + late + "</td>" + stat_cells
                 + "<td class='muted'>" + e(STUDENT_INFO[s]["context"]) + "</td></tr>")
    header = ("<tr><th>Student</th>"
              + "".join("<th title='" + STAT_LABELS[k] + "'>" + labels[i] + "</th>"
                        for i, k in enumerate(STAT_KEYS))
              + "<th>Context</th></tr>")
    return ("<div class='card'><h2>Roster</h2>"
            "<p class='muted'>Stats (1-5): R Readiness, M Motivation, C Communication, "
            "SM Self-Management, A Adaptability, HS Help-Seeking. Communication is \"can we have a productive "
            "conversation?\"; Help-Seeking is \"will they start it when stuck?\"</p>"
            "<div style='overflow-x:auto'><table>" + header + rows + "</table></div></div>")


def shell_home(game_code, team_index, team, cp_index):
    roster = shell_active_roster(team)
    latest_simple = None
    for it in GB_ITEMS:
        if it["type"] == "simple" and it["reveal"] <= cp_index:
            latest_simple = it
    missing = 0
    if latest_simple:
        missing = sum(1 for s in roster
                      if _enrolled_from(team, s) <= latest_simple["reveal"]
                      and _gb_missing(team, s, latest_simple))
    num = cp_index + 1
    tiles = (
        "<table><tr><th>Checkpoint</th><td>" + str(num) + " of " + str(len(CHECKPOINTS))
        + " &mdash; " + CHECKPOINTS[cp_index] + "</td></tr>"
        "<tr><th>Active students</th><td>" + str(len(roster)) + "</td></tr>"
        "<tr><th>Missing on latest assignment</th><td>" + str(missing) + "</td></tr>"
        "</table>")
    return ("<div class='card'><h2>Course Home</h2>" + tiles + "</div>"
            + resource_meters(team["resources"], team["statuses"]))


# Canvas-style course navigation order/labels (keys map to the renderers).
COURSE_NAV = [
    ("home", "Home"),
    ("announcements", "Announcements"),
    ("modules", "Modules"),
    ("gradebook", "Grades"),
    ("roster", "People"),
    ("syllabus", "Syllabus"),
    ("inbox", "Inbox"),
    ("resources", "Student Support"),
]
NAV_LABEL = dict(COURSE_NAV)

# ---------------------------------------------------------------------------
# Contextual Canvas highlights (Step 13). For each checkpoint index we flag 1-3
# course-navigation sections that hold genuinely relevant, current evidence for
# that scenario -- a neutral "Worth exploring" nudge toward investigation, never
# a hint at the correct decision. Keys are COURSE_NAV keys; every section stays
# fully accessible whether or not it is flagged. Each entry is justified by the
# actual section content at that checkpoint:
#   CP1 (idx0): Grades (blank/missing cells), Syllabus (due-date mismatch notice),
#               Inbox (the week-1 student messages).
#   CP2 (idx1): People (the newly joined students, pilled), Modules (Start-Here /
#               onboarding note for new students).
#   CP3 (idx2): Grades (Major Assessment 1 + the category breakdown).
#   CP4 (idx3): Grades (midterm backlog / scores), Inbox ("can't find your feedback").
#   CP5 (idx4): Grades (final items, missing-as-blank), Announcements (end-of-term
#               checklist: final grades timeline + Course Improvement Survey).
CP_CANVAS_HIGHLIGHTS = {
    0: ["gradebook", "syllabus", "inbox"],
    1: ["roster", "modules"],
    2: ["gradebook"],
    3: ["gradebook", "inbox"],
    4: ["gradebook", "announcements"],
}

# Minimal inline SVG icons for the decorative global nav rail (no emoji, no assets).
_GI = {
    "account": "<circle cx='12' cy='8' r='4'/><path d='M4 21c0-4 4-6 8-6s8 2 8 6'/>",
    "dashboard": "<rect x='3' y='3' width='7' height='7'/><rect x='14' y='3' width='7' height='7'/>"
                 "<rect x='3' y='14' width='7' height='7'/><rect x='14' y='14' width='7' height='7'/>",
    "courses": "<path d='M5 4h12a2 2 0 0 1 2 2v14H7a2 2 0 0 0-2 2z'/><path d='M9 4v16'/>",
    "calendar": "<rect x='3' y='4' width='18' height='17' rx='2'/><path d='M3 9h18M8 2v4M16 2v4'/>",
    "inbox": "<path d='M3 12l4-7h10l4 7v7a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z'/><path d='M3 12h5l1 2h4l1-2h5'/>",
    "help": "<circle cx='12' cy='12' r='9'/><path d='M9.5 9a2.5 2.5 0 0 1 4.5 1.5c0 1.5-2 2-2 3.5'/>"
            "<circle cx='12' cy='17.5' r='.7' fill='currentColor' stroke='none'/>",
}


def _gi_item(key, label):
    return ("<div class='gi'><svg viewBox='0 0 24 24' aria-hidden='true'>" + _GI[key]
            + "</svg>" + label + "</div>")


def global_rail():
    # Decorative Canvas-style global rail (aria-hidden; real nav is the course list).
    items = [("account", "Account"), ("dashboard", "Dashboard"), ("courses", "Courses"),
             ("calendar", "Calendar"), ("inbox", "Inbox"), ("help", "Help")]
    return ("<nav class='lms-global' aria-hidden='true'>"
            + "".join(_gi_item(k, lbl) for k, lbl in items) + "</nav>")


def course_nav(game_code, team_index, active, course_code):
    # Keep the embed flag on in-pane Canvas navigation so tab clicks stay inside
    # the split's left pane (and keep their cross-pane chrome suppressed).
    suffix = "&embed=1" if _embedded() else ""
    # Contextual highlights for the CURRENT checkpoint. Read-only: derived from the
    # game's checkpoint_index, so a refresh shows the right highlights and never
    # changes state, and advancing the checkpoint updates them automatically.
    highlights = set(CP_CANVAS_HIGHLIGHTS.get(shell_cp_index(game_code), []))
    links = ""
    for key, label in COURSE_NAV:
        classes = (["active"] if key == active else []) + (["wx"] if key in highlights else [])
        cls = (" class='" + " ".join(classes) + "'") if classes else ""
        cur = " aria-current='page'" if key == active else ""
        if key in highlights:
            # Neutral nudge: a dot (CSS) + a small visible/sr label. Never says
            # "correct" or anything strategy-revealing.
            inner = (label + "<span class='nav-wx'>Worth exploring</span>")
            extra = " title='Worth exploring'"
        else:
            inner, extra = label, ""
        links += ("<a" + cls + cur + extra + " href='"
                  + shell_url(game_code, team_index, key) + suffix + "'>" + inner + "</a>")
    return ("<nav class='lms-course' aria-label='Course navigation'>"
            "<div class='ch'>" + course_code + "</div>" + links + "</nav>")


@app.route("/team/shell")
def team_shell():
    game_code = request.args.get("game_code", "").strip().upper()
    team_index = request.args.get("team_index", "")
    tab = request.args.get("tab", "home")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    ti = int(team_index)
    if "resources" not in team:  # course not set up yet
        return resume_screen(game_code, ti, team)
    if tab not in dict(SHELL_TABS):
        tab = "home"
    cp_index = shell_cp_index(game_code)
    # Make sure late adds exist once the game has reached CP2, so the gradebook/
    # roster reflect them even if the team opens the shell before the CP2 decision.
    if cp_index >= 1:
        ensure_late_adds(team)

    renderers = {
        "home": lambda: shell_home(game_code, ti, team, cp_index),
        "syllabus": lambda: shell_syllabus(team, cp_index),
        "modules": lambda: shell_modules(team, cp_index),
        "gradebook": lambda: shell_gradebook(team, cp_index),
        "announcements": lambda: shell_announcements(team, cp_index),
        "inbox": lambda: shell_inbox(team, cp_index),
        "roster": lambda: shell_roster(team, cp_index),
        "resources": lambda: shell_resources(team, cp_index),
    }
    course = COURSE_BY_NAME[team["course"]]
    embedded = _embedded()
    # In a split pane the team name is shown by the container and the decision
    # screen is already on the right, so suppress cross-pane nav (a link here
    # would otherwise load the workspace into the Canvas pane).
    crumb_team = (e(team["name"]) if embedded
                  else "<a href='" + status_url(game_code, ti) + "'>" + e(team["name"]) + "</a>")
    crumb = ("<div class='lms-crumb'>" + crumb_team + " &rsaquo; " + course["code"]
             + " &rsaquo; " + NAV_LABEL[tab] + "</div>")
    h1_text = e(team["course"]) if tab == "home" else NAV_LABEL[tab]
    title = ("<h1 class='lms-h1'>" + h1_text + "</h1>"
             "<p class='muted' style='margin-top:-10px'>" + course["code"] + " &middot; " + course["modality"]
             + " &middot; Checkpoint " + str(cp_index + 1) + ": " + CHECKPOINTS[cp_index] + "</p>")
    back = "" if embedded else ("<p class='footer'><a class='btn' href='" + status_url(game_code, ti)
            + "'>Back to team decision screen</a> <a href='/'>Home</a></p>")
    content = ("<main class='lms-content'>" + crumb + title + renderers[tab]() + back + "</main>")
    body = ("<div class='lms'>" + global_rail() + course_nav(game_code, ti, tab, course["code"])
            + content + "</div>")
    return page(e(team["course"]) + " - " + NAV_LABEL[tab], body, screen="shell", bare=True)


# ===========================================================================
# FINAL: accusation, reveal, outcome, debrief
# ===========================================================================
# Survival thresholds (documented in GAME_RULES.md). Tuned against simulated
# playthroughs so that: thoughtful play survives from ANY legal roster (roster
# does not determine success), sustained poor/passive play tends to fail, and a
# single catastrophic resource cannot be hidden by three healthy ones.
SURVIVE_OVERALL = 58   # Overall Course Score must be at least this.
SURVIVE_FLOOR = 30     # No single resource may be below this (weakest-link).


def course_survives(team):
    res = team["resources"]
    if team["overall"] < SURVIVE_OVERALL:
        return False
    if min(res[n] for n in RESOURCE_NAMES) < SURVIVE_FLOOR:
        return False
    return True


def final_screen(game_code, team_index, team):
    # Require the CP5 private check to be complete before accusing.
    if not private_complete(team, 4):
        body = ("<div class='card'><h2>Final Stretch complete</h2>"
                "<p>Finish the final Private Player Check before the accusation.</p></div>"
                + private_block(game_code, team_index, team, 4))
        return page("Final - " + team["name"], cp_header(game_code, team_index, team,4) + body + team_footer(game_code, team_index))

    if team.get("accusation") is None:
        return final_accusation_screen(game_code, team_index, team)
    return final_outcome_screen(game_code, team_index, team)


def final_accusation_screen(game_code, team_index, team, error_message=""):
    error_html = "<p class='err'>" + e(error_message) + "</p>" if error_message else ""
    options = ""
    for i, p in enumerate(team["players"]):
        options += "<label><input type='radio' name='accused' value='" + str(i) + "'> " + e(p) + "</label>"
    body = ("<div class='card'><h2>Final Suspicion Report</h2>" + suspicion_report(team, 4) + "</div>"
            "<div class='card'><h2>Final Accusation</h2>"
            "<p>Discuss as a team, then choose the one player you believe is Thunderflunked. "
            "Prefer consensus.</p>" + error_html +
            "<form method='post' action='/team/accuse'>"
            "<input type='hidden' name='game_code' value='" + game_code + "'>"
            "<input type='hidden' name='team_index' value='" + str(team_index) + "'>"
            + options + "<p><button type='submit'>Lock In Accusation</button></p></form></div>")
    return page("Final Accusation - " + team["name"], body + team_footer(game_code, team_index))


@app.route("/team/accuse", methods=["POST"])
def team_accuse():
    game_code = request.form.get("game_code", "").strip().upper()
    team_index = request.form.get("team_index", "")
    if not team_index.isdigit() or get_team(game_code, int(team_index)) is None:
        return error_page("Something went wrong finding your team. Please start again.")
    team = get_team(game_code, int(team_index))
    ti = int(team_index)
    if not games[game_code].get("final_open"):
        return resume_screen(game_code, ti, team)
    if team.get("accusation") is not None:
        return final_outcome_screen(game_code, ti, team)
    accused = request.form.get("accused", "")
    if not accused.isdigit() or int(accused) >= len(team["players"]):
        return final_accusation_screen(game_code, ti, team, "Please choose one player.")
    team["accusation"] = int(accused)
    return final_outcome_screen(game_code, ti, team)


def semester_recap(team):
    cp = team.get("cp", {})
    bits = []
    for i in range(len(CHECKPOINTS)):
        d = cp.get(i)
        if d and d.get("summary"):
            bits.append("<li><strong>CP" + str(i + 1) + " (" + CHECKPOINTS[i] + "):</strong> "
                        + e("; ".join(d["summary"])) + "</li>")
    return "<ul>" + "".join(bits) + "</ul>" if bits else "<p>(no decisions recorded)</p>"


def final_outcome_screen(game_code, team_index, team):
    survived = course_survives(team)
    team["outcome"] = "Survived" if survived else "Did Not Survive"
    tf_name = team["players"][team["thunderflunked"]]
    accused_name = team["players"][team["accusation"]]
    correct = (team["accusation"] == team["thunderflunked"])
    missions = len(team.get("missions_complete", []))

    outcome_html = ("<div class='card'><h2>Course Outcome</h2>"
                    "<p class='big status-" + ("Normal" if survived else "Critical") + "'>"
                    + ("SURVIVED THE SEMESTER" if survived else "DID NOT SURVIVE THE SEMESTER") + "</p>"
                    "<p><strong>" + e(team["course"]) + "</strong></p>"
                    "<p><strong>Final Overall Course Score: " + str(team["overall"]) + "</strong></p></div>"
                    + resource_meters(team["resources"], team["statuses"]))

    # Endgame Secret Mission reveal: now that the game is decided (and allegiance
    # is being revealed on this same screen), it is safe to show who held which
    # mission and whether it was claimed. Nothing here appears before the final.
    completed = set(team.get("missions_complete", []))
    mrows = ""
    for i, pname in enumerate(team["players"]):
        m = player_mission(team, i)
        if m is None:
            continue
        done = ("<span class='status-Normal'>&#10003; completed</span>" if str(i) in completed
                else "<span class='muted'>not claimed</span>")
        mrows += ("<tr><td>" + e(pname) + "</td><td>" + e(m["text"]) + "</td><td>" + done + "</td></tr>")
    mission_table = (("<h3>Secret Missions</h3><table><tr><th>Player</th><th>Mission</th><th>Result</th></tr>"
                      + mrows + "</table>") if mrows else "")

    reveal_html = ("<div class='card'><h2>Thunderflunked Reveal</h2>"
                   "<p>The Thunderflunked player was <strong>" + e(tf_name) + "</strong>.</p>"
                   "<p>Your team accused <strong>" + e(accused_name) + "</strong> &mdash; "
                   + ("<strong>correct.</strong>" if correct else "incorrect.") + "</p>"
                   "<p class='muted'>Identifying Thunderflunked is a secondary achievement. It does not decide "
                   "whether the course survived.</p>"
                   "<p><strong>Secret Missions completed at this table:</strong> " + str(missions)
                   + " (secondary recognition).</p>" + mission_table + "</div>")

    recap_html = ("<div class='card'><h2>Semester Recap</h2>" + semester_recap(team) + "</div>")

    debrief_html = ("<div class='card'><h2>Debrief</h2>"
                    "<p>Thunderflunked did not have the power to destroy your course alone.</p>"
                    "<p>They could suggest ideas, push priorities, and create pressure.</p>"
                    "<p>Your team's process determined which ideas became decisions.</p>"
                    "<p><strong>Good course design is not about knowing who has the bad idea. "
                    "It is about having a process that catches the bad idea.</strong></p></div>")

    body = outcome_html + reveal_html + recap_html + debrief_html
    return page("Course Outcome - " + team["name"], body
                + "<p class='footer'><a href='/'>Home</a></p>")


# ===========================================================================
# Projector / Room Screen (public). Never leaks secret information.
# ===========================================================================
@app.route("/projector")
def projector():
    if active_code is None:
        return page("Projector", "<div class='card'><p>No active game yet.</p></div>"
                    + "<p class='footer'><a href='/projector'>Refresh</a></p>", "projector")
    game = games[active_code]
    prog = room_progress(game)
    teams = game["teams"]

    # Auto-advance the room display on any phase/pause/progress/outcome change.
    # The projector has no inputs, so a reload-on-change is safe; it shows only
    # public information -- never allegiance, votes, missions, sabotage, or (until
    # the final reveal) scoring consequences.
    cur = projector_rev(game)
    live = ("<script>(function(){var cur=" + json.dumps(cur) + ";"
            "var u=" + json.dumps("/live/game?game_code=" + active_code) + ";"
            "function tick(){fetch(u,{headers:{'X-Requested-With':'fetch'}}).then(function(r){return r.json();})"
            ".then(function(j){if(j&&typeof j.rev==='string'&&j.rev&&j.rev!==cur){location.reload();}})"
            ".catch(function(){});}setInterval(tick,5000);})();</script>")
    footer = "<p class='footer'><a href='/projector'>Refresh</a></p>" + live

    # Hallway rumor: when the GM has announced one, it owns the projector as a full
    # takeover until the GM dismisses it. On dismiss the previous view is restored
    # automatically (every other branch below derives from game state). It shows
    # only the public gossip wording -- no mechanic, player, allegiance, or team.
    rum = _rumors_read(game)["active"]
    if rum:
        body = ("<div class='card center proj-rumor'>"
                "<div class='proj-rumor-kicker'>" + e(RUMOR_TITLE) + "</div>"
                "<div class='proj-rumor-text'>" + e(rumor_public_text(rum)) + "</div></div>")
        return page("Projector", body + footer, "projector")

    # Paused: a single, unmistakable room-wide message.
    if game.get("paused"):
        body = ("<div class='card center'><h1>Paused</h1>"
                "<p class='big'>Discussion in progress. Please give the Game Master your attention.</p></div>")
        return page("Projector", body + footer, "projector")

    # Teaching moment: the ONE discussion question, then (only after the GM
    # reveals) the Canvas strategies. Nothing here appears until the GM acts, and
    # facilitator notes / correct answers are never shown.
    if teaching_active(game):
        idx = game["checkpoint_index"]
        t = TEACHING.get(idx)
        if t:
            stage = teaching_stage(game, idx)
            head = ("<div class='proj-teach-head'>Checkpoint " + str(idx + 1) + " of "
                    + str(len(CHECKPOINTS)) + " &mdash; " + e(CHECKPOINTS[idx]) + "</div>")
            if stage == "discussion":
                body = ("<div class='card proj-question'>" + head
                        + "<div class='proj-q-label'>Discussion</div>"
                        "<div class='proj-q-text'>" + e(t["question"]) + "</div></div>")
            else:  # revealed
                cards = "".join(
                    "<div class='proj-strat'><div class='proj-strat-do'>" + e(s["do"]) + "</div>"
                    "<div class='proj-strat-where'>" + e(s["where"]) + "</div>"
                    "<div class='proj-strat-solves'>&#8594; " + e(s["solves"]) + "</div></div>"
                    for s in t["strategies"])
                body = ("<div class='card proj-strats'>" + head
                        + "<h1 class='proj-strats-title'>Practical Canvas strategies</h1>"
                        "<div class='proj-strat-grid'>" + cards + "</div></div>")
            return page("Projector", body + footer, "projector")

    # Final: the one place scores/outcomes are revealed.
    if game.get("final_open"):
        scored = [t for t in teams if t.get("overall") is not None and t.get("course")]
        rows = ""
        for t in scored:
            survived = course_survives(t) if t.get("accusation") is not None else None
            outcome = "(finishing)" if survived is None else ("Survived" if survived else "Did Not Survive")
            rows += ("<tr><td>" + e(t["name"]) + "</td><td>" + e(t["course"]) + "</td><td>"
                     + str(t["overall"]) + "</td><td>" + outcome + "</td></tr>")
        table = ("<table><tr><th>Team</th><th>Course</th><th>Final Score</th><th>Outcome</th></tr>"
                 + rows + "</table>") if rows else "<p>No results yet.</p>"
        body = ("<div class='card'><h1>Final: Accusations &amp; Outcomes</h1></div>"
                "<div class='card'>" + table + "</div>")
        return page("Projector", body + footer, "projector")

    # Active play: current public activity + large instruction + completion
    # progress. No per-team scores or checkpoint content (nothing premature).
    activity = e(prog["activity"])
    instruction = projector_instruction(game)
    if not teams:
        progress_html = "<p class='big'>Waiting for teams to join. Share the game code.</p>"
    elif prog["all_ready"]:
        progress_html = ("<p class='big cc-allready'>&#10003; All teams are ready.</p>"
                         "<p class='big'>Waiting for the Game Master.</p>")
    else:
        progress_html = ("<p class='big'><strong>" + str(prog["done"]) + " of " + str(prog["total"])
                         + "</strong> teams ready.</p>"
                         "<p>Finish on your team screen when you are ready.</p>")
    body = ("<div class='card'><h1>" + activity + "</h1>"
            + ("<p class='big'>" + e(instruction) + "</p>" if instruction else "") + "</div>"
            "<div class='card center'>" + progress_html + "</div>")
    return page("Projector", body + footer, "projector")


# ---------------------------------------------------------------------------
# Boot: enable persistence from the environment (DATABASE_URL) and restore any
# previously saved game. Runs at import so it applies under gunicorn on Render
# as well as `python app.py` locally. With no DATABASE_URL set (local dev and
# the test suite) persistence stays off and the game runs purely in memory.
# ---------------------------------------------------------------------------
storage.init_from_env()
restore_state()


if __name__ == "__main__":
    # Bind to all interfaces by default so each team's device and the projector
    # can reach the facilitator's machine over the room's local network. Override
    # with FACULTY_GAME_HOST / FACULTY_GAME_PORT if needed. Render sets PORT; we
    # honor it as a fallback. Debug/reloader is OFF by default so the server does
    # not restart (and wipe in-memory state) mid-session.
    host = os.environ.get("FACULTY_GAME_HOST", "0.0.0.0")
    port = int(os.environ.get("FACULTY_GAME_PORT", os.environ.get("PORT", "5000")))
    debug = os.environ.get("FACULTY_GAME_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
