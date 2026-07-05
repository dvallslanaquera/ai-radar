"""One-time Google Drive authorization for the daily PDF digest.

Run this ONCE, on a machine with a browser:

    python authorize_drive.py

It opens your browser, asks you to sign in and grant the "drive.file" scope
(the app may only see, create, and edit files it creates itself - it can never
read the rest of your Drive), then writes a token file to the path in
GOOGLE_OAUTH_TOKEN_FILE. After that, `main.py` uploads the nightly PDF silently
and refreshes the token on its own; you never run this again unless you revoke
access or delete the token.

Prereqs (see README "Google Drive upload"):
  - a Google Cloud project with the Drive API enabled
  - an OAuth client of type "Desktop app"
  - .env holds GOOGLE_OAUTH_TOKEN_FILE (where the token is written),
    GOOGLE_DRIVE_FOLDER_ID (the target folder id), and the client either as
    a downloaded JSON file:
        GOOGLE_OAUTH_CLIENT_FILE=C:\\path\\to\\client_secret.json
    or pasted directly (no file needed):
        GOOGLE_OAUTH_CLIENT_ID=xxxx.apps.googleusercontent.com
        GOOGLE_OAUTH_CLIENT_SECRET=xxxx
"""

from __future__ import annotations

import os
from pathlib import Path

# drive.file: the app can only touch files it creates - never the rest of Drive.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (mirrors main.load_dotenv) so this script stays
    standalone and doesn't import the whole pipeline just to authorize."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    load_dotenv()

    token_file = os.environ.get("GOOGLE_OAUTH_TOKEN_FILE", "").strip()
    if not token_file:
        raise SystemExit(
            "Set GOOGLE_OAUTH_TOKEN_FILE in .env first (where the token is written)."
        )

    # Two ways to supply the OAuth client: the downloaded JSON, or just the
    # client id + secret pasted straight into .env (a Desktop-app secret isn't
    # truly confidential, and this keeps every secret in one place).
    client_file = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "").strip()
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency. Install the project deps first:\n"
            "    poetry install\n"
            "and run this via  poetry run python authorize_drive.py\n"
            f"({exc})"
        )

    if client_file:
        if not Path(client_file).exists():
            raise SystemExit(f"OAuth client file not found: {client_file}")
        flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    elif client_id and client_secret:
        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    else:
        raise SystemExit(
            "Set either GOOGLE_OAUTH_CLIENT_FILE (path to the downloaded JSON) or "
            "GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET in .env."
        )

    # Opens a browser, spins up a temporary localhost server for the redirect.
    creds = flow.run_local_server(port=0)

    token_path = Path(token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"\nAuthorized. Token saved to: {token_file}")
    print("main.py will now upload the nightly PDF to Drive silently.")


if __name__ == "__main__":
    main()
