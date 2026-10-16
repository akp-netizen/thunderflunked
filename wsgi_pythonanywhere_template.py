# -----------------------------------------------------------------------------
# PythonAnywhere WSGI configuration TEMPLATE for THUNDERFLUNKED 2.0.
#
# This is a TEMPLATE. Copy its contents into your real WSGI file on PythonAnywhere
# (Web tab -> "WSGI configuration file", which edits
#  /var/www/akpeters_pythonanywhere_com_wsgi.py), then fill in the real values.
#
# IMPORTANT:
#   * Put your REAL SECRET_KEY / GM_PASSWORD here, in the WSGI file on
#     PythonAnywhere ONLY. That file lives on PythonAnywhere's private disk and is
#     NOT in git. Never commit real secrets. The placeholders below are not secrets.
#   * Use a NEW SQLite file for 2.0 (state_v2.db) so the previous deployment's
#     database is never written to -- this keeps rollback trivial.
#   * Set "Number of workers" to 1 on the Web tab (required: the game is held in
#     memory and mirrored to the DB; multiple workers would split the game).
#   * PythonAnywhere serves HTTPS, so the app's Secure session cookies work.
# -----------------------------------------------------------------------------

import os
import sys

# 1) Make the project importable. Change this to your actual project directory
#    (see the Web tab -> "Source code" / "Working directory").
PROJECT_DIR = "/home/akpeters/faculty-game"
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# 2) Environment / secrets. Fill in REAL values here on PythonAnywhere only.
#    Generate a SECRET_KEY in a Bash console with:
#        python3 -c "import secrets; print(secrets.token_hex(32))"
os.environ["SECRET_KEY"] = "PASTE_A_LONG_RANDOM_STRING_HERE"
os.environ["GM_PASSWORD"] = "CHOOSE_A_STRONG_GM_PASSWORD_HERE"

# 3) Persistence: a NEW SQLite file on the persistent disk (four slashes = absolute
#    path). Using state_v2.db leaves any previous database untouched.
os.environ["DATABASE_URL"] = "sqlite:////home/akpeters/faculty-game/state_v2.db"

# (Setting DATABASE_URL turns on production mode, which requires SECRET_KEY and
#  enables Secure cookies -- correct for PythonAnywhere's HTTPS.)

# 4) Expose the Flask app as the WSGI callable named `application`.
from app import app as application
