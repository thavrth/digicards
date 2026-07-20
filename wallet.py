"""
Google Wallet loyalty-card logic, now store-aware. Each store has its own
loyalty class (card template). Functions take a `store` dict from stores.py.

    ensure_class(store)                       -> create/update a store's template
    create_object(store, suffix, name, mid)   -> create one customer's card
    save_link(store, suffix)                  -> the "Add to Google Wallet" URL
"""

import json
import os
import time

import jwt  # PyJWT
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/wallet_object.issuer"]
API = "https://walletobjects.googleapis.com/walletobjects/v1"
SAVE = "https://pay.google.com/gp/v/save/"

# Load the service-account key. When deployed, put the key's JSON contents in the
# GOOGLE_WALLET_KEY_JSON environment variable (a host secret). Locally, it falls
# back to reading the file at config.KEY_FILE.
_raw_key = os.getenv("GOOGLE_WALLET_KEY_JSON")
if _raw_key:
    _key = json.loads(_raw_key)
    _credentials = Credentials.from_service_account_info(_key, scopes=SCOPES)
else:
    _credentials = Credentials.from_service_account_file(config.KEY_FILE, scopes=SCOPES)
    with open(config.KEY_FILE) as _f:
        _key = json.load(_f)

_session = AuthorizedSession(_credentials)
_SERVICE_ACCOUNT_EMAIL = _key["client_email"]
_PRIVATE_KEY = _key["private_key"]


def _class_id(store: dict) -> str:
    return f"{config.ISSUER_ID}.{store['id']}"


def _logo_url(store: dict) -> str:
    """Use a real external logo_url if one is set in stores.py, otherwise serve
    the app's logo endpoint (which returns logos/<id>.png or a placeholder)."""
    logo = store.get("logo_url")
    if not logo or "example.com" in logo:
        return f"{config.BASE_URL}/store/{store['id']}/logo"
    return logo


def _class_body(store: dict) -> dict:
    class_id = _class_id(store)
    return {
        "id": class_id,
        "issuerName": store["name"],
        "programName": store["program_name"],
        "reviewStatus": "UNDER_REVIEW",
        "programLogo": {
            "sourceUri": {"uri": _logo_url(store)},
            "contentDescription": {
                "defaultValue": {"language": "en-US", "value": f"{store['name']} logo"}
            },
        },
        "hexBackgroundColor": store["brand_color"],
    }


def ensure_class(store: dict) -> str:
    """Create the store's loyalty class, or patch it with current values so
    edits in stores.py take effect on the next restart."""
    class_id = _class_id(store)
    body = _class_body(store)

    resp = _session.get(f"{API}/loyaltyClass/{class_id}")

    if resp.status_code == 404:
        resp = _session.post(f"{API}/loyaltyClass", json=body)
        if not resp.ok:
            raise RuntimeError(f"Creating class failed: {resp.status_code} {resp.text}")
        print(f"[wallet] Created class: {class_id}")
    elif resp.status_code == 200:
        resp = _session.patch(f"{API}/loyaltyClass/{class_id}", json=body)
        if not resp.ok:
            raise RuntimeError(f"Updating class failed: {resp.status_code} {resp.text}")
        print(f"[wallet] Updated class: {class_id}")
    else:
        raise RuntimeError(f"Checking class failed: {resp.status_code} {resp.text}")

    return class_id


def _stamp_fields(store: dict, stamps: int) -> dict:
    """The loyaltyPoints, progress dots, and stamp-banner hero image for a card."""
    goal = store["reward_goal"]
    stamps = max(0, min(stamps, goal))
    hero = {
        "heroImage": {
            "sourceUri": {"uri": f"{config.BASE_URL}/store/{store['id']}/stamp/{stamps}"},
            "contentDescription": {
                "defaultValue": {"language": "en-US", "value": f"{stamps} of {goal} stamps"}
            },
        }
    }
    if stamps >= goal:
        return {
            "loyaltyPoints": {"label": "Reward", "balance": {"string": "READY"}},
            "textModulesData": [
                {"id": "progress", "header": "Reward ready", "body": "\u2605  Free reward earned  \u2605"}
            ],
            **hero,
        }
    dots = ("\u25cf " * stamps + "\u25cb " * max(0, goal - stamps)).strip()
    return {
        "loyaltyPoints": {"label": "Stamps", "balance": {"string": f"{stamps} / {goal}"}},
        "textModulesData": [{"id": "progress", "header": "Progress", "body": dots}],
        **hero,
    }


def create_object(store: dict, object_suffix: str, full_name: str, member_id: str) -> str:
    """Create one customer's loyalty card for a given store, starting at 0 stamps."""
    object_id = f"{config.ISSUER_ID}.{object_suffix}"

    loyalty_object = {
        "id": object_id,
        "classId": _class_id(store),
        "state": "ACTIVE",
        "accountId": member_id,
        "accountName": full_name,
        "barcode": {
            "type": "CODE_128",      # standard 1D barcode used on most loyalty cards
            "value": member_id,       # the member number, not the phone (privacy)
            "alternateText": member_id,
        },
        **_stamp_fields(store, 0),
    }

    resp = _session.get(f"{API}/loyaltyObject/{object_id}")
    if resp.status_code == 200:
        return object_id
    if resp.status_code != 404:
        raise RuntimeError(f"Checking object failed: {resp.status_code} {resp.text}")

    resp = _session.post(f"{API}/loyaltyObject", json=loyalty_object)
    if not resp.ok:
        raise RuntimeError(f"Creating object failed: {resp.status_code} {resp.text}")
    return object_id


def update_stamps(store: dict, object_suffix: str, stamps: int, notify: bool = True) -> None:
    """PATCH a customer's card to show a new stamp count. The card in their
    wallet updates automatically - number, dots, and the stamp banner image;
    notify=True also triggers a push notification (Google caps at 3/24h)."""
    object_id = f"{config.ISSUER_ID}.{object_suffix}"
    patch = _stamp_fields(store, stamps)
    if notify:
        patch["notifyPreference"] = "notifyOnUpdate"
    resp = _session.patch(f"{API}/loyaltyObject/{object_id}", json=patch)
    if not resp.ok:
        raise RuntimeError(f"Updating stamps failed: {resp.status_code} {resp.text}")


def expire_object(object_suffix: str) -> None:
    """Mark a customer's card EXPIRED so it drops out of active use in their
    wallet. Google Wallet objects can't be hard-deleted, only expired."""
    object_id = f"{config.ISSUER_ID}.{object_suffix}"
    resp = _session.patch(f"{API}/loyaltyObject/{object_id}", json={"state": "EXPIRED"})
    if resp.status_code not in (200, 404):
        raise RuntimeError(f"Expiring object failed: {resp.status_code} {resp.text}")


def save_link(store: dict, object_suffix: str) -> str:
    """Sign an 'Add to Google Wallet' link for a customer's card."""
    claims = {
        "iss": _SERVICE_ACCOUNT_EMAIL,
        "aud": "google",
        "typ": "savetowallet",
        "iat": int(time.time()),
        "origins": [],
        "payload": {
            "loyaltyObjects": [
                {"id": f"{config.ISSUER_ID}.{object_suffix}", "classId": _class_id(store)}
            ]
        },
    }
    token = jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256")
    return SAVE + token
