"""Admin password for data-changing endpoints (edge exclusions, hut corrections, added huts).

Viewing and tour planning stay open to everyone who can reach the app (on the server: invited users, via the Cloudflare Access login). Changes need
HTTP Basic auth with the admin password, whose salted hash lives in data/admin_password (not in git):

    uv run python -m app.admin set-password                                    # on the Mac
    docker compose exec huthop python -m app.admin set-password                # on the server, in /opt/huthop/server

Rules:
- A password is set → changes need it.
- No password and HUTHOP_REQUIRE_ADMIN=1 (set in server/compose.yaml) → changes are refused (safe default).
- No password and no HUTHOP_REQUIRE_ADMIN (local development) → changes are allowed.
"""

import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys

from fastapi import HTTPException, Request

from app.db import DB_PATH

PASSWORD_FILE = DB_PATH.parent / "admin_password"
ITERATIONS = 310_000
REALM = 'Basic realm="HutHop admin", charset="UTF-8"'


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.strip().split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def stored_hash() -> str | None:
    return PASSWORD_FILE.read_text().strip() if PASSWORD_FILE.exists() else None


def require_admin(request: Request) -> None:
    """FastAPI dependency for endpoints that change data."""
    stored = stored_hash()
    if stored is None:
        if os.environ.get("HUTHOP_REQUIRE_ADMIN") == "1":
            raise HTTPException(403, "Editing is disabled until an admin password is set (python -m app.admin set-password).")
        return
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            _, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            password = None
        if password is not None and check_password(password, stored):
            return
    raise HTTPException(401, "Admin password required to change data.", headers={"WWW-Authenticate": REALM})


def main() -> None:
    if sys.argv[1:] != ["set-password"]:
        print(__doc__)
        sys.exit(2)
    password = getpass.getpass("New admin password: ")
    if len(password) < 10:
        sys.exit("Please use at least 10 characters.")
    if getpass.getpass("Repeat: ") != password:
        sys.exit("The passwords don't match.")
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(hash_password(password) + "\n")
    PASSWORD_FILE.chmod(0o600)
    print(f"Saved the password hash to {PASSWORD_FILE}. Any username works in the browser prompt.")


if __name__ == "__main__":
    main()
