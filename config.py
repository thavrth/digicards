"""
Global settings shared by every store. Per-store details (name, logo, colours,
reward rules) live in stores.py instead.

Each value can be overridden with an environment variable, which is how you
configure the app when it's deployed. Locally, the defaults below are used.
"""

import os

# --- Your Google Wallet credentials -----------------------------------------
ISSUER_ID = os.getenv("ISSUER_ID", "3388000000023152461")   # your numeric Issuer ID
KEY_FILE = os.getenv("KEY_FILE", "service-account-key.json")  # local key file path

# When deployed, set the key's JSON *contents* in the GOOGLE_WALLET_KEY_JSON
# environment variable (a host secret) instead of shipping the file. wallet.py
# uses that if present, otherwise it falls back to KEY_FILE above.

# --- Where THIS app is reachable from a customer's phone --------------------
# Local test on the same Wi-Fi: your computer's LAN IP, e.g. http://192.168.1.20:8000
# Deployed: your public address, e.g. https://your-app.up.railway.app
BASE_URL = os.getenv("BASE_URL", "http://digicards-production-ac41.up.railway.app")
