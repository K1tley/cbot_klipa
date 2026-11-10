"""Optional helper: get a Google refresh token on your own computer.

(The easier route is the OAuth Playground — see SETUP.md, step 3.)

    pip install google-auth-oauthlib
    python get_token.py client_secret.json
"""
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/youtube.upload",
]

flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1] if len(sys.argv) > 1 else "client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("\nGOOGLE_REFRESH_TOKEN =", creds.refresh_token)
