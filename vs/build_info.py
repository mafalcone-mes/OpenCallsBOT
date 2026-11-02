"""What kind of copy this is. These are the values for running from the code
(`uv run vs serve`): no time limit, nothing locked.

scripts/build_beta.py rewrites this file in the copy it compiles (never in
git), turning on the beta limits for testers (vs/beta.py)."""

BETA = False
BUILD_DATE = ""  # YYYY-MM-DD, the day the beta was built
FEEDBACK_URL = ""  # the testers' feedback form, shown in the beta
