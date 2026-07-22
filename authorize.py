"""
Run this once to authorize the app against blocktrades32@gmail.com.
It opens a browser window for you to log in and approve access,
then saves token.json in this folder so future runs don't need you to log in again.

Usage: python authorize.py
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    with open("token.json", "w") as f:
        f.write(creds.to_json())
    print("Success! token.json has been created. You can now run the Streamlit app.")

if __name__ == "__main__":
    main()
