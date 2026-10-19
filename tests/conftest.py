"""Test setup: point the app at a throwaway database before anything imports app.db.

Without this, starting the app in a test (TestClient runs the startup hook) would open, migrate and switch
data/huthop.db, the developer's real database; the admin password file next to it would apply too."""

import os
import tempfile

os.environ["HUTHOP_DB"] = os.path.join(tempfile.mkdtemp(prefix="huthop-tests-"), "huthop.db")
os.environ.pop("HUTHOP_REQUIRE_ADMIN", None)
