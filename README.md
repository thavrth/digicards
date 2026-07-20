# Store loyalty-card enrollment (multi-store)

A store-counter web app. A clerk opens a store's page, types a customer's
**name + phone**, and the screen shows a **QR code** the customer scans with
their phone camera to add that store's loyalty card to **Google Wallet**.

Two stores are set up for the demo. Both enrollment pages fully work; the
deeper per-store features (stamp card, checkout) come in later steps.

## Pages

| URL | What it is |
|-----|-----------|
| `/` | Landing page listing the stores. |
| `/store/<id>` | That store's branded enrollment page (e.g. `/store/brewbar`). |
| `/store/<id>/lookup` | Staff page: find an existing customer by phone and re-show their card. |
| `/store/<id>/members` | Staff page: list every member, with a Remove button on each. |
| `/store/<id>/daily` | Staff page: members added, grouped by day (newest first). |
| `/store/<id>/checkout` | Staff page: scan/enter a member ID to add a stamp or redeem a reward. |
| `/c/<code>` | Short link the QR points to; redirects the phone to Google. |

## Files

| File | What it does |
|------|--------------|
| `config.py` | Global settings: Issuer ID, key file, the app's public URL. |
| `stores.py` | **Your stores.** Add/edit stores and their branding + reward rules here. |
| `wallet.py` | Talks to Google Wallet: per-store card template, each customer's card, save link. |
| `app.py` | Web server, landing + enrollment pages, per-store phone uniqueness, short links. |
| `enroll.db` | Created automatically on first run (SQLite). |

## Setup

1. `pip install -r requirements.txt`
2. Put your service-account JSON key in this folder (or point `KEY_FILE` at it).
3. Edit `config.py`: set `ISSUER_ID`, `KEY_FILE`, and `BASE_URL`.
4. Edit `stores.py`: set each store's name, a public HTTPS `logo_url`, and colour.

## Run

```
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` to see the store list.

## The one important setting: BASE_URL

The QR points at **this app**, so the customer's phone must be able to reach it.
`localhost` won't work from a phone.

- **Same Wi-Fi test:** set `BASE_URL` to your computer's LAN IP, e.g.
  `http://192.168.1.20:8000` (find it with `ipconfig` / `ifconfig`).
- **Real use:** deploy the app and set `BASE_URL` to its public HTTPS address.

## Stamp cards & the visual upgrade

The stamp progress currently shows as a number ("3 / 5") and a dot row on the
card, driven by the card's points + text fields. This works on localhost and
updates live. A fancier version swaps in a filled-stamp **image** on the card,
but hero images must be hosted at a public URL Google can fetch - so that
upgrade lands once the app is deployed (a later step). The scan-to-update
mechanism is identical, so nothing changes in the flow.

## Deploying (public URL)

Deploying gives the app a public HTTPS address so the QR works on any phone. The
app is deployment-ready: it reads settings from environment variables.

Set these environment variables on your host:

| Variable | Value |
|----------|-------|
| `ISSUER_ID` | your numeric Issuer ID |
| `BASE_URL` | your deployed URL, e.g. `https://your-app.up.railway.app` |
| `GOOGLE_WALLET_KEY_JSON` | the **entire contents** of your service-account JSON key |

Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT` (also in `Procfile`).

**Never commit your key or database.** `.gitignore` already excludes
`service-account-key.json`, `*.json`, and `enroll.db`. On the host, the key goes
in the `GOOGLE_WALLET_KEY_JSON` secret, not in the repo.

Note: on free hosting tiers the filesystem can reset on redeploy, which clears
the SQLite database. That's fine for a demo (just re-enroll), but production
should use a persistent volume or a hosted database.

## Notes

- **Phone uniqueness is per store.** The same phone can be a member at both
  stores; re-entering a phone at the *same* store re-shows that customer's card
  instead of creating a duplicate.
- While your issuer is in **demo mode**, only Google accounts added as **test
  users** in the Wallet console can save a card, and cards show a `[TEST ONLY]`
  banner until you request publishing access.
- Google Wallet (Android) only for now. Apple Wallet is a later step.

## Staff features

- **Look up a customer:** on any enrollment page tap *Look up a customer*, enter
  a phone number, and the customer's card (QR + link) is re-shown. Useful when a
  member lost their link or changed phones.
- **Live counts:** the enrollment page shows how many cards were added today and
  the store's total members, updating after each sign-up.
- **All members:** *Members* lists everyone signed up at that store. Each row has
  a **Remove** button that expires the customer's wallet card and frees their
  phone number for re-enrollment. (Google Wallet cards can't be hard-deleted,
  only expired, so a removed card shows as expired in the customer's wallet.)
- **Daily sign-ups:** *Daily* groups members by the day they were added.
- **Checkout / stamp card:** *Checkout* is the point-of-sale screen. Enter (or
  scan) a member ID, tap **Add a stamp**, and the customer's card updates live -
  showing "3 / 5" and a row of filled dots, like a digital punch card. At the
  goal (set per store by `reward_goal` in stores.py) the card shows **reward
  ready**; tap **Redeem** to give the reward and reset the count. Each update can
  trigger a push notification (Google caps these at 3 per card per 24h).

## Adding a store

Add a dict to `STORES` in `stores.py` with a new unique `id`, then restart. The
app creates its wallet card template automatically and its enrollment page
appears at `/store/<id>`.
