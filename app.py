"""
Multi-store loyalty-card enrollment app.

  /                            landing page listing the stores
  /store/<id>                  that store's branded enrollment page
  /store/<id>/enroll   (POST)  create a card for that store
  /store/<id>/lookup           staff page: find an existing customer by phone
  /store/<id>/lookup   (POST)  search by phone, returns the customer's card
  /store/<id>/stats            today's + total sign-up counts for that store
  /c/<code>                    short link the QR points to; redirects to Google
"""

import hashlib
import os
import random
import secrets
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pydantic import BaseModel

import config
import images
import stores as stores_module
import wallet

# Store the database on a persistent volume when one is attached. Railway sets
# RAILWAY_VOLUME_MOUNT_PATH automatically for an attached volume, so the DB
# survives redeploys. Locally, neither is set, so it stays as ./enroll.db.
_VOLUME = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.getenv("DB_PATH") or (os.path.join(_VOLUME, "enroll.db") if _VOLUME else "enroll.db")


# --- helpers ----------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    _dir = os.path.dirname(DB_PATH)
    if _dir:
        os.makedirs(_dir, exist_ok=True)
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id      TEXT NOT NULL,
                full_name     TEXT NOT NULL,
                phone         TEXT NOT NULL,
                member_id     TEXT,
                object_suffix TEXT,
                save_url      TEXT,
                short_code    TEXT UNIQUE,
                created_at    TEXT NOT NULL,
                UNIQUE(store_id, phone)
            )
            """
        )
        # Migration: add stamp-card columns to existing databases if missing.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(customers)")}
        if "stamps" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN stamps INTEGER NOT NULL DEFAULT 0")
        if "rewards" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN rewards INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def normalize_phone(raw: str) -> str:
    return "".join(ch for ch in raw.strip() if ch.isdigit() or ch == "+")


def _store_prefix(store_id: str) -> int:
    """A stable 2-digit prefix per store, so every member ID from a store shares
    the same leading pair — a recognisable pattern, then random digits."""
    return int(hashlib.md5(store_id.encode()).hexdigest(), 16) % 90 + 10  # 10-99


def generate_member_id(store_id: str) -> str:
    """A member ID like PP-RRRRRRRR: a 2-digit store prefix followed by 8 random
    digits (10 digits total), unique within the store."""
    prefix = _store_prefix(store_id)
    with closing(get_conn()) as conn:
        for _ in range(30):
            mid = f"{prefix}{random.randint(0, 99999999):08d}"
            hit = conn.execute(
                "SELECT 1 FROM customers WHERE store_id=? AND member_id=?", (store_id, mid)
            ).fetchone()
            if not hit:
                return mid
    raise RuntimeError("Could not generate a unique member ID.")


def darken(hex_color: str, factor: float = 0.72) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (max(0, int(c * factor)) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def find_by_member_id(store_id: str, raw: str):
    mid = raw.strip()
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE store_id=? AND member_id=?", (store_id, mid)
        ).fetchone()
        if not row and mid.isdigit():
            row = conn.execute(
                "SELECT * FROM customers WHERE store_id=? AND CAST(member_id AS INTEGER)=?",
                (store_id, int(mid)),
            ).fetchone()
    return row


def customer_state(row, goal: int) -> dict:
    stamps = row["stamps"] or 0
    return {
        "name": row["full_name"],
        "member_id": row["member_id"],
        "stamps": stamps,
        "goal": goal,
        "reward_ready": stamps >= goal,
        "rewards": row["rewards"] or 0,
    }


def render_store_page(template: str, store: dict) -> str:
    accent = store["brand_color"]
    return (template
            .replace("__STORE_NAME__", store["name"])
            .replace("__PROGRAM_NAME__", store["program_name"])
            .replace("__STORE_ID__", store["id"])
            .replace("__ACCENT_INK__", darken(accent))
            .replace("__ACCENT__", accent))


# --- app --------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    for store in stores_module.STORES:
        try:
            wallet.ensure_class(store)
        except Exception as e:
            print(f"\n[warning] Could not set up store '{store['id']}': {e}")
            print("          Check ISSUER_ID, the key file, and the logo URL.\n")
    yield


app = FastAPI(title="Wallet enrollment", lifespan=lifespan)


class EnrollRequest(BaseModel):
    name: str
    phone: str


class LookupRequest(BaseModel):
    phone: str


class RemoveRequest(BaseModel):
    id: int


class MemberIdRequest(BaseModel):
    member_id: str


# --- landing page -----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    cards = ""
    for s in stores_module.STORES:
        cards += (
            '<a class="store" href="/store/' + s["id"] + '">'
            '<span class="dot" style="background:' + s["brand_color"] + '"></span>'
            '<span class="store-text">'
            '<span class="store-name">' + s["name"] + "</span>"
            '<span class="store-sub">' + s["program_name"] + " · " + s["reward_text"] + "</span>"
            "</span><span class=\"arrow\">&rsaquo;</span></a>"
        )
    return HTMLResponse(LANDING.replace("__STORES__", cards))


# --- per-store enrollment page ---------------------------------------------
@app.get("/store/{store_id}", response_class=HTMLResponse)
def store_page(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    return HTMLResponse(render_store_page(ENROLL_PAGE, store))


@app.get("/store/{store_id}/lookup", response_class=HTMLResponse)
def lookup_page(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    return HTMLResponse(render_store_page(LOOKUP_PAGE, store))


@app.get("/store/{store_id}/stats")
def stats(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    today = datetime.now().strftime("%Y-%m-%d")
    with closing(get_conn()) as conn:
        today_count = conn.execute(
            "SELECT COUNT(*) AS c FROM customers WHERE store_id=? AND substr(created_at,1,10)=?",
            (store_id, today),
        ).fetchone()["c"]
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM customers WHERE store_id=?", (store_id,)
        ).fetchone()["c"]
    return {"today": today_count, "total": total}


@app.post("/store/{store_id}/lookup")
def lookup(store_id: str, req: LookupRequest):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    phone = normalize_phone(req.phone)
    if len(phone) < 6:
        raise HTTPException(status_code=400, detail="Enter a valid phone number.")

    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE store_id=? AND phone=?", (store_id, phone)
        ).fetchone()
    if not row or not row["short_code"]:
        raise HTTPException(status_code=404, detail="No customer found with that phone number.")

    return {
        "name": row["full_name"],
        "member_id": row["member_id"],
        "short_url": f"{config.BASE_URL}/c/{row['short_code']}",
    }


@app.post("/store/{store_id}/enroll")
def enroll(store_id: str, req: EnrollRequest):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")

    name = req.name.strip()
    phone = normalize_phone(req.phone)
    if not name:
        raise HTTPException(status_code=400, detail="Enter the customer's name.")
    if len(phone) < 6:
        raise HTTPException(status_code=400, detail="Enter a valid phone number.")

    with closing(get_conn()) as conn:
        existing = conn.execute(
            "SELECT * FROM customers WHERE store_id = ? AND phone = ?",
            (store_id, phone),
        ).fetchone()

    if existing and existing["short_code"]:
        return {
            "already_existed": True,
            "name": existing["full_name"],
            "member_id": existing["member_id"],
            "short_url": f"{config.BASE_URL}/c/{existing['short_code']}",
        }

    created_at = datetime.now().isoformat()
    try:
        with closing(get_conn()) as conn:
            cur = conn.execute(
                "INSERT INTO customers (store_id, full_name, phone, created_at) VALUES (?, ?, ?, ?)",
                (store_id, name, phone, created_at),
            )
            conn.commit()
            row_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That phone number is already registered here.")

    member_id = generate_member_id(store_id)
    object_suffix = f"{store_id}_{row_id}"
    short_code = secrets.token_urlsafe(6)

    try:
        wallet.create_object(store, object_suffix, name, member_id)
        save_url = wallet.save_link(store, object_suffix)
    except Exception as e:
        with closing(get_conn()) as conn:
            conn.execute("DELETE FROM customers WHERE id = ?", (row_id,))
            conn.commit()
        raise HTTPException(status_code=502, detail=f"Could not create the card: {e}")

    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE customers SET member_id=?, object_suffix=?, save_url=?, short_code=? WHERE id=?",
            (member_id, object_suffix, save_url, short_code, row_id),
        )
        conn.commit()

    return {
        "already_existed": False,
        "name": name,
        "member_id": member_id,
        "short_url": f"{config.BASE_URL}/c/{short_code}",
    }


@app.get("/store/{store_id}/members", response_class=HTMLResponse)
def members_page(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    return HTMLResponse(render_store_page(MEMBERS_PAGE, store))


@app.get("/store/{store_id}/members/data")
def members_data(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT id, full_name, phone, member_id, created_at FROM customers "
            "WHERE store_id=? ORDER BY id DESC", (store_id,)
        ).fetchall()
    members = [{
        "id": r["id"], "name": r["full_name"], "phone": r["phone"],
        "member_id": r["member_id"], "created_at": r["created_at"],
    } for r in rows]
    return {"members": members}


@app.post("/store/{store_id}/members/remove")
def remove_member(store_id: str, req: RemoveRequest):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE id=? AND store_id=?", (req.id, store_id)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Member not found.")
    if row["object_suffix"]:
        try:
            wallet.expire_object(row["object_suffix"])
        except Exception as e:
            print(f"[warning] Could not expire wallet object: {e}")
    with closing(get_conn()) as conn:
        conn.execute("DELETE FROM customers WHERE id=?", (req.id,))
        conn.commit()
    return {"ok": True}


@app.get("/store/{store_id}/daily", response_class=HTMLResponse)
def daily_page(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    return HTMLResponse(render_store_page(DAILY_PAGE, store))


@app.get("/store/{store_id}/daily/data")
def daily_data(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT full_name, member_id, created_at FROM customers "
            "WHERE store_id=? ORDER BY created_at DESC", (store_id,)
        ).fetchall()
    days, index = [], {}
    for r in rows:
        date = (r["created_at"] or "")[:10]
        if date not in index:
            index[date] = {"date": date, "count": 0, "members": []}
            days.append(index[date])
        index[date]["count"] += 1
        index[date]["members"].append({"name": r["full_name"], "member_id": r["member_id"]})
    return {"days": days}


@app.get("/store/{store_id}/checkout", response_class=HTMLResponse)
def checkout_page(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    return HTMLResponse(render_store_page(CHECKOUT_PAGE, store))


@app.post("/store/{store_id}/checkout/lookup")
def checkout_lookup(store_id: str, req: MemberIdRequest):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    row = find_by_member_id(store_id, req.member_id)
    if not row:
        raise HTTPException(status_code=404, detail="No member with that ID at this store.")
    return customer_state(row, store["reward_goal"])


@app.post("/store/{store_id}/checkout/add")
def checkout_add(store_id: str, req: MemberIdRequest):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    goal = store["reward_goal"]
    row = find_by_member_id(store_id, req.member_id)
    if not row:
        raise HTTPException(status_code=404, detail="No member with that ID at this store.")

    stamps = row["stamps"] or 0
    if stamps >= goal:
        raise HTTPException(status_code=400, detail="Reward is ready - redeem it first.")
    new_stamps = stamps + 1

    with closing(get_conn()) as conn:
        conn.execute("UPDATE customers SET stamps=? WHERE id=?", (new_stamps, row["id"]))
        conn.commit()
    try:
        wallet.update_stamps(store, row["object_suffix"], new_stamps)
    except Exception as e:
        with closing(get_conn()) as conn:
            conn.execute("UPDATE customers SET stamps=? WHERE id=?", (stamps, row["id"]))
            conn.commit()
        raise HTTPException(status_code=502, detail=f"Could not update the card: {e}")

    row = find_by_member_id(store_id, req.member_id)
    return customer_state(row, goal)


@app.post("/store/{store_id}/checkout/redeem")
def checkout_redeem(store_id: str, req: MemberIdRequest):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    goal = store["reward_goal"]
    row = find_by_member_id(store_id, req.member_id)
    if not row:
        raise HTTPException(status_code=404, detail="No member with that ID at this store.")

    stamps = row["stamps"] or 0
    if stamps < goal:
        raise HTTPException(status_code=400, detail="No reward to redeem yet.")
    new_rewards = (row["rewards"] or 0) + 1

    with closing(get_conn()) as conn:
        conn.execute("UPDATE customers SET stamps=0, rewards=? WHERE id=?", (new_rewards, row["id"]))
        conn.commit()
    try:
        wallet.update_stamps(store, row["object_suffix"], 0)
    except Exception as e:
        with closing(get_conn()) as conn:
            conn.execute("UPDATE customers SET stamps=?, rewards=? WHERE id=?",
                         (stamps, row["rewards"] or 0, row["id"]))
            conn.commit()
        raise HTTPException(status_code=502, detail=f"Could not update the card: {e}")

    row = find_by_member_id(store_id, req.member_id)
    return customer_state(row, goal)


@app.get("/store/{store_id}/stamp/{n}")
def stamp_image(store_id: str, n: int):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    png = images.stamp_banner(store, n)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/store/{store_id}/logo")
def logo_image(store_id: str):
    store = stores_module.get_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Unknown store.")
    path = os.path.join(os.path.dirname(__file__), "logos", f"{store_id}.png")
    if os.path.exists(path):
        with open(path, "rb") as f:
            png = f.read()
    else:
        png = images.logo_placeholder(store)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/c/{code}")
def short_redirect(code: str):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT save_url FROM customers WHERE short_code = ?", (code,)
        ).fetchone()
    if not row or not row["save_url"]:
        raise HTTPException(status_code=404, detail="Link not found.")
    return RedirectResponse(row["save_url"], status_code=302)


# --- shared CSS -------------------------------------------------------------
STYLE = r"""
  :root{
    --ink:#16211f;--muted:#5d6b67;--line:#e2e6e3;--bg:#eef1ee;--card:#fff;
    --accent:__ACCENT__;--accent-ink:__ACCENT_INK__;--danger:#b23b3b;--radius:16px;
  }
  *{box-sizing:border-box}html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    display:flex;align-items:center;justify-content:center;padding:24px;-webkit-font-smoothing:antialiased}
  .card{background:var(--card);width:100%;max-width:460px;border-radius:var(--radius);
    border:1px solid var(--line);padding:28px 30px 30px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
  a.nav{font-size:13px;color:var(--muted);text-decoration:none}
  a.nav:hover{color:var(--ink)}
  .brand{font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:15px;
    letter-spacing:.02em;color:var(--accent);margin:8px 0 4px}
  h1{font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:24px;margin:6px 0 6px;line-height:1.2}
  .stats{font-size:13px;color:var(--muted);margin:0 0 22px;min-height:16px}
  .stats b{color:var(--ink)}
  label{display:block;font-size:13px;font-weight:600;color:var(--muted);margin:0 0 6px}
  input{width:100%;font-size:18px;padding:14px 15px;border:1.5px solid var(--line);
    border-radius:12px;background:#fbfcfb;color:var(--ink);outline:none;transition:border-color .15s}
  input:focus{border-color:var(--accent)}
  .field{margin-bottom:18px}
  button{width:100%;font-size:17px;font-weight:600;color:#fff;background:var(--accent);
    border:none;border-radius:12px;padding:16px;cursor:pointer;transition:background .15s}
  button:hover{background:var(--accent-ink)}button:disabled{opacity:.6;cursor:default}
  .msg{margin-top:16px;font-size:14px;color:var(--danger);min-height:18px}
  .hidden{display:none}
  .result{text-align:center}
  .result .name{font-size:22px;font-weight:600;margin:2px 0 2px}
  .result .member{font-size:13px;color:var(--muted);margin:0 0 4px}
  .badge{display:inline-block;font-size:12px;font-weight:600;color:var(--accent-ink);
    background:rgba(0,0,0,.05);padding:4px 10px;border-radius:999px;margin-bottom:16px}
  #qr{display:flex;justify-content:center;padding:16px;background:#fff;border:1px solid var(--line);
    border-radius:12px;margin:6px auto 14px;width:max-content}
  #qr img,#qr canvas{display:block}
  .link{display:inline-block;font-size:15px;font-weight:600;color:var(--accent);
    text-decoration:none;margin:0 0 14px;word-break:break-all}
  .link:hover{text-decoration:underline}
  .scan{font-size:16px;font-weight:600;margin:0 0 4px}
  .scan-sub{font-size:14px;color:var(--muted);margin:0 0 22px}
  .secondary{background:#eef1ee;color:var(--ink)}.secondary:hover{background:#e2e6e3}
  .navset{display:flex;gap:14px;flex-wrap:wrap;justify-content:flex-end}
  .search{margin:2px 0 12px}
  .stamps-big{font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:36px;margin:10px 0 2px}
  .dots{display:flex;flex-wrap:wrap;gap:9px;justify-content:center;margin:14px 0 18px}
  .dot2{width:26px;height:26px;border-radius:50%;border:2px solid var(--accent);box-sizing:border-box}
  .dot2.filled{background:var(--accent)}
  .ready{color:var(--accent-ink);font-weight:600;font-size:20px;margin:10px 0 4px}
  .rewards-note{font-size:13px;color:var(--muted);margin:0 0 18px}
  .stack>button{margin-bottom:10px}
  .card.wide{max-width:560px}
  .list{list-style:none;padding:0;margin:6px 0 0}
  .rowitem{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);text-align:left}
  .rowitem:last-child{border-bottom:none}
  .rowitem .info{flex:1;min-width:0}
  .rowitem .rn{font-weight:600;font-size:16px}
  .rowitem .rm{font-size:13px;color:var(--muted);margin-top:2px}
  .remove{width:auto;padding:8px 13px;font-size:13px;background:#f6eaea;color:var(--danger)}
  .remove:hover{background:#efd9d9}
  .daygroup{margin-top:18px}
  .dayhead{display:flex;justify-content:space-between;align-items:baseline;
    font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:15px;padding-bottom:6px;border-bottom:2px solid var(--line)}
  .daycount{font-size:13px;color:var(--muted)}
  .empty{color:var(--muted);font-size:14px;margin-top:16px}
"""

_HEAD = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__STORE_NAME__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>""" + STYLE + "</style></head><body>"


# --- enrollment page --------------------------------------------------------
ENROLL_PAGE = _HEAD + r"""
  <div class="card">
    <section id="form-view">
      <div class="topbar">
        <a class="nav" href="/">&lsaquo; All stores</a>
        <span class="navset">
          <a class="nav" href="/store/__STORE_ID__/checkout">Checkout</a>
          <a class="nav" href="/store/__STORE_ID__/lookup">Look up</a>
          <a class="nav" href="/store/__STORE_ID__/members">Members</a>
          <a class="nav" href="/store/__STORE_ID__/daily">Daily</a>
        </span>
      </div>
      <p class="brand">__STORE_NAME__</p>
      <h1>Add a loyalty card</h1>
      <p class="stats" id="stats"></p>
      <div class="field">
        <label for="name">Customer name</label>
        <input id="name" type="text" autocomplete="off" autofocus placeholder="e.g. Sok Dara">
      </div>
      <div class="field">
        <label for="phone">Phone number</label>
        <input id="phone" type="tel" autocomplete="off" placeholder="e.g. 012 345 678">
      </div>
      <button id="submit" onclick="enroll()">Create card</button>
      <div class="msg" id="msg"></div>
    </section>

    <section id="result-view" class="result hidden">
      <div class="badge hidden" id="returning">Already a member</div>
      <p class="name" id="r-name"></p>
      <p class="member" id="r-member"></p>
      <div id="qr"></div>
      <a id="r-link" class="link" href="#" target="_blank" rel="noopener">Open link</a>
      <p class="scan">Scan with your phone camera</p>
      <p class="scan-sub">Point your camera here, tap the link, then tap Add to Google Wallet.</p>
      <button class="secondary" onclick="reset()">Add another customer</button>
    </section>
  </div>
<script>
  const $ = (id) => document.getElementById(id);
  const STORE_ID = "__STORE_ID__";

  async function loadStats(){
    try{
      const r = await fetch("/store/" + STORE_ID + "/stats");
      const d = await r.json();
      $("stats").innerHTML = "<b>" + d.today + "</b> added today · <b>" + d.total + "</b> total members";
    }catch(e){ /* ignore */ }
  }

  async function enroll(){
    const name = $("name").value.trim();
    const phone = $("phone").value.trim();
    $("msg").textContent = "";
    if(!name || !phone){ $("msg").textContent = "Enter a name and phone number."; return; }
    $("submit").disabled = true; $("submit").textContent = "Creating...";
    try{
      const res = await fetch("/store/" + STORE_ID + "/enroll", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({name, phone})
      });
      const data = await res.json();
      if(!res.ok){ throw new Error(data.detail || "Something went wrong."); }
      showResult(data);
    }catch(err){ $("msg").textContent = err.message; }
    finally{ $("submit").disabled = false; $("submit").textContent = "Create card"; }
  }

  function showResult(data){
    $("r-name").textContent = data.name;
    $("r-member").textContent = "Member " + data.member_id;
    $("returning").classList.toggle("hidden", !data.already_existed);
    $("qr").innerHTML = "";
    new QRCode($("qr"), {text: data.short_url, width: 240, height: 240, correctLevel: QRCode.CorrectLevel.M});
    $("r-link").href = data.short_url;
    $("form-view").classList.add("hidden");
    $("result-view").classList.remove("hidden");
  }

  function reset(){
    $("name").value = ""; $("phone").value = ""; $("msg").textContent = "";
    $("result-view").classList.add("hidden");
    $("form-view").classList.remove("hidden");
    $("name").focus();
    loadStats();
  }

  $("name").addEventListener("keydown", e => { if(e.key==="Enter") $("phone").focus(); });
  $("phone").addEventListener("keydown", e => { if(e.key==="Enter") enroll(); });
  loadStats();
</script>
</body></html>
"""


# --- lookup page ------------------------------------------------------------
LOOKUP_PAGE = _HEAD + r"""
  <div class="card">
    <section id="form-view">
      <div class="topbar">
        <a class="nav" href="/store/__STORE_ID__">&lsaquo; Add a card</a>
        <a class="nav" href="/">All stores &rsaquo;</a>
      </div>
      <p class="brand">__STORE_NAME__</p>
      <h1>Look up a customer</h1>
      <p class="stats">Find an existing member and re-show their card.</p>
      <div class="field">
        <label for="phone">Phone number</label>
        <input id="phone" type="tel" autocomplete="off" autofocus placeholder="e.g. 012 345 678">
      </div>
      <button id="submit" onclick="search()">Find customer</button>
      <div class="msg" id="msg"></div>
    </section>

    <section id="result-view" class="result hidden">
      <p class="name" id="r-name"></p>
      <p class="member" id="r-member"></p>
      <div id="qr"></div>
      <a id="r-link" class="link" href="#" target="_blank" rel="noopener">Open link</a>
      <p class="scan">Scan with your phone camera</p>
      <p class="scan-sub">Point your camera here, tap the link, then tap Add to Google Wallet.</p>
      <button class="secondary" onclick="reset()">Look up another</button>
    </section>
  </div>
<script>
  const $ = (id) => document.getElementById(id);
  const STORE_ID = "__STORE_ID__";

  async function search(){
    const phone = $("phone").value.trim();
    $("msg").textContent = "";
    if(!phone){ $("msg").textContent = "Enter a phone number."; return; }
    $("submit").disabled = true; $("submit").textContent = "Searching...";
    try{
      const res = await fetch("/store/" + STORE_ID + "/lookup", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({phone})
      });
      const data = await res.json();
      if(!res.ok){ throw new Error(data.detail || "Something went wrong."); }
      showResult(data);
    }catch(err){ $("msg").textContent = err.message; }
    finally{ $("submit").disabled = false; $("submit").textContent = "Find customer"; }
  }

  function showResult(data){
    $("r-name").textContent = data.name;
    $("r-member").textContent = "Member " + data.member_id;
    $("qr").innerHTML = "";
    new QRCode($("qr"), {text: data.short_url, width: 240, height: 240, correctLevel: QRCode.CorrectLevel.M});
    $("r-link").href = data.short_url;
    $("form-view").classList.add("hidden");
    $("result-view").classList.remove("hidden");
  }

  function reset(){
    $("phone").value = ""; $("msg").textContent = "";
    $("result-view").classList.add("hidden");
    $("form-view").classList.remove("hidden");
    $("phone").focus();
  }

  $("phone").addEventListener("keydown", e => { if(e.key==="Enter") search(); });
</script>
</body></html>
"""


# --- landing page -----------------------------------------------------------
# --- members page -----------------------------------------------------------
MEMBERS_PAGE = _HEAD + r"""
  <div class="card wide">
    <div class="topbar">
      <a class="nav" href="/store/__STORE_ID__">&lsaquo; Add a card</a>
      <span class="navset">
        <a class="nav" href="/store/__STORE_ID__/lookup">Look up</a>
        <a class="nav" href="/store/__STORE_ID__/daily">Daily</a>
      </span>
    </div>
    <p class="brand">__STORE_NAME__</p>
    <h1>All members</h1>
    <p class="stats" id="count"></p>
    <input id="search" class="search" type="tel" autocomplete="off" placeholder="Search by phone or name">
    <ul class="list" id="list"></ul>
  </div>
<script>
  const $=(id)=>document.getElementById(id);
  const STORE_ID="__STORE_ID__";
  function fmtDate(iso){return (iso||"").slice(0,10);}
  let allMembers=[];

  async function loadMembers(){
    const r=await fetch("/store/"+STORE_ID+"/members/data");
    const d=await r.json();
    allMembers=d.members;
    applyFilter();
  }

  function applyFilter(){
    const q=$("search").value.trim().toLowerCase();
    const qd=q.replace(/\D/g,"");
    let shown=allMembers;
    if(q){
      shown=allMembers.filter(m=>{
        const phoneDigits=(m.phone||"").replace(/\D/g,"");
        return (qd && phoneDigits.includes(qd)) ||
               (m.name||"").toLowerCase().includes(q) ||
               (m.member_id||"").includes(qd);
      });
    }
    renderList(shown, q, allMembers.length);
  }

  function renderList(members, q, total){
    const list=$("list"); list.innerHTML="";
    $("count").textContent = q
      ? (members.length + " of " + total + (total===1?" member":" members"))
      : (total + (total===1?" member":" members"));
    if(!allMembers.length){ list.innerHTML='<p class="empty">No members yet.</p>'; return; }
    if(!members.length){ list.innerHTML='<p class="empty">No matches.</p>'; return; }
    for(const m of members){
      const li=document.createElement("li"); li.className="rowitem";
      const info=document.createElement("div"); info.className="info";
      const rn=document.createElement("div"); rn.className="rn"; rn.textContent=m.name;
      const rm=document.createElement("div"); rm.className="rm";
      rm.textContent="Member "+m.member_id+"  \u00b7  "+m.phone+"  \u00b7  "+fmtDate(m.created_at);
      info.appendChild(rn); info.appendChild(rm);
      const btn=document.createElement("button"); btn.className="remove"; btn.textContent="Remove";
      btn.onclick=()=>removeMember(m);
      li.appendChild(info); li.appendChild(btn);
      list.appendChild(li);
    }
  }

  async function removeMember(m){
    if(!confirm("Remove "+m.name+" ("+m.phone+")?\nThis expires their wallet card and frees the phone number.")) return;
    const r=await fetch("/store/"+STORE_ID+"/members/remove",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({id:m.id})});
    if(r.ok){ loadMembers(); }
    else { const d=await r.json().catch(()=>({})); alert(d.detail||"Could not remove."); }
  }

  $("search").addEventListener("input", applyFilter);
  loadMembers();
</script>
</body></html>
"""


# --- daily sign-ups page ----------------------------------------------------
DAILY_PAGE = _HEAD + r"""
  <div class="card wide">
    <div class="topbar">
      <a class="nav" href="/store/__STORE_ID__">&lsaquo; Add a card</a>
      <span class="navset">
        <a class="nav" href="/store/__STORE_ID__/lookup">Look up</a>
        <a class="nav" href="/store/__STORE_ID__/members">Members</a>
      </span>
    </div>
    <p class="brand">__STORE_NAME__</p>
    <h1>Daily sign-ups</h1>
    <p class="stats">Members added each day, most recent first.</p>
    <div id="days"></div>
  </div>
<script>
  const $=(id)=>document.getElementById(id);
  const STORE_ID="__STORE_ID__";

  async function loadDaily(){
    const r=await fetch("/store/"+STORE_ID+"/daily/data");
    const d=await r.json();
    const wrap=$("days"); wrap.innerHTML="";
    if(!d.days.length){ wrap.innerHTML='<p class="empty">No sign-ups yet.</p>'; return; }
    for(const day of d.days){
      const g=document.createElement("div"); g.className="daygroup";
      const h=document.createElement("div"); h.className="dayhead";
      const dt=document.createElement("span"); dt.textContent=day.date;
      const c=document.createElement("span"); c.className="daycount";
      c.textContent=day.count+(day.count===1?" member":" members");
      h.appendChild(dt); h.appendChild(c); g.appendChild(h);
      const ul=document.createElement("ul"); ul.className="list";
      for(const m of day.members){
        const li=document.createElement("li"); li.className="rowitem";
        const info=document.createElement("div"); info.className="info";
        const rn=document.createElement("div"); rn.className="rn"; rn.textContent=m.name;
        const rm=document.createElement("div"); rm.className="rm"; rm.textContent="Member "+m.member_id;
        info.appendChild(rn); info.appendChild(rm); li.appendChild(info); ul.appendChild(li);
      }
      g.appendChild(ul); wrap.appendChild(g);
    }
  }

  loadDaily();
</script>
</body></html>
"""


# --- checkout / stamp page --------------------------------------------------
CHECKOUT_PAGE = _HEAD + r"""
  <div class="card">
    <section id="scan-view">
      <div class="topbar">
        <a class="nav" href="/store/__STORE_ID__">&lsaquo; Add a card</a>
        <span class="navset">
          <a class="nav" href="/store/__STORE_ID__/members">Members</a>
          <a class="nav" href="/store/__STORE_ID__/daily">Daily</a>
        </span>
      </div>
      <p class="brand">__STORE_NAME__</p>
      <h1>Checkout</h1>
      <p class="stats">Scan or type the customer's member ID to record a purchase.</p>
      <div class="field">
        <label for="mid">Member ID</label>
        <input id="mid" type="text" inputmode="numeric" autocomplete="off" autofocus placeholder="e.g. 8412345678">
      </div>
      <button id="scan-btn" onclick="checkoutScan()">Add a stamp</button>
      <div class="msg" id="msg"></div>
    </section>

    <section id="state-view" class="result hidden">
      <p class="name" id="s-name"></p>
      <div class="stamps-big" id="s-count"></div>
      <div class="dots" id="s-dots"></div>
      <div class="ready hidden" id="s-ready">&#9733; Free reward earned &#9733;</div>
      <p class="rewards-note" id="s-rewards"></p>
      <div class="stack">
        <button id="add-btn" onclick="addStamp()">Add a stamp</button>
        <button id="redeem-btn" class="hidden" onclick="redeem()">Redeem free reward</button>
        <button class="secondary" onclick="reset()">Next customer</button>
      </div>
      <div class="msg" id="msg2"></div>
    </section>
  </div>
<script>
  const $=(id)=>document.getElementById(id);
  const STORE_ID="__STORE_ID__";
  let currentId="";

  async function post(path){
    const r=await fetch("/store/"+STORE_ID+path,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({member_id:currentId})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||"Something went wrong.");
    return d;
  }

  async function checkoutScan(){
    currentId=$("mid").value.trim();
    $("msg").textContent="";
    if(!currentId){ $("msg").textContent="Enter or scan a member ID."; return; }
    $("scan-btn").disabled=true; $("scan-btn").textContent="Working...";
    try{
      // Look the customer up first. If their reward is already full, show the
      // redeem screen instead of adding another stamp; otherwise add one.
      let state = await post("/checkout/lookup");
      if(!state.reward_ready){ state = await post("/checkout/add"); }
      showState(state);
    }
    catch(e){ $("msg").textContent=e.message; }
    finally{ $("scan-btn").disabled=false; $("scan-btn").textContent="Add a stamp"; }
  }

  async function addStamp(){
    $("msg2").textContent="";
    $("add-btn").disabled=true; $("add-btn").textContent="Adding...";
    try{ showState(await post("/checkout/add")); }
    catch(e){ $("msg2").textContent=e.message; }
    finally{ $("add-btn").disabled=false; $("add-btn").textContent="Add a stamp"; }
  }

  async function redeem(){
    $("msg2").textContent="";
    $("redeem-btn").disabled=true; $("redeem-btn").textContent="Redeeming...";
    try{ showState(await post("/checkout/redeem")); }
    catch(e){ $("msg2").textContent=e.message; }
    finally{ $("redeem-btn").disabled=false; $("redeem-btn").textContent="Redeem free reward"; }
  }

  function showState(d){
    $("s-name").textContent=d.name;
    $("s-count").textContent = d.reward_ready ? "Reward ready!" : (d.stamps + " / " + d.goal);
    const dots=$("s-dots"); dots.innerHTML="";
    for(let i=0;i<d.goal;i++){
      const el=document.createElement("span");
      el.className="dot2"+(i<d.stamps?" filled":"");
      dots.appendChild(el);
    }
    $("s-ready").classList.toggle("hidden", !d.reward_ready);
    $("s-rewards").textContent = d.rewards>0 ? ("Rewards redeemed: "+d.rewards) : "";
    $("add-btn").classList.toggle("hidden", d.reward_ready);
    $("redeem-btn").classList.toggle("hidden", !d.reward_ready);
    $("scan-view").classList.add("hidden");
    $("state-view").classList.remove("hidden");
  }

  function reset(){
    currentId=""; $("mid").value=""; $("msg").textContent=""; $("msg2").textContent="";
    $("state-view").classList.add("hidden");
    $("scan-view").classList.remove("hidden");
    $("mid").focus();
  }

  $("mid").addEventListener("keydown", e => { if(e.key==="Enter") checkoutScan(); });
</script>
</body></html>
"""


LANDING = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Loyalty enrollment</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600&display=swap" rel="stylesheet">
<style>
  :root{--ink:#16211f;--muted:#5d6b67;--line:#e2e6e3;--bg:#eef1ee;--card:#fff;--radius:16px}
  *{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    display:flex;align-items:center;justify-content:center;padding:24px}
  .wrap{width:100%;max-width:460px}
  h1{font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:22px;margin:0 0 4px}
  .lede{color:var(--muted);font-size:14px;margin:0 0 22px}
  .store{display:flex;align-items:center;gap:14px;background:var(--card);border:1px solid var(--line);
    border-radius:var(--radius);padding:18px 18px;margin-bottom:12px;text-decoration:none;color:inherit;
    transition:border-color .15s,transform .05s}
  .store:hover{border-color:#c9d2ce}.store:active{transform:scale(.995)}
  .dot{width:34px;height:34px;border-radius:50%;flex:0 0 auto}
  .store-text{display:flex;flex-direction:column;flex:1;min-width:0}
  .store-name{font-weight:600;font-size:17px}
  .store-sub{color:var(--muted);font-size:13px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .arrow{color:#b7c0bc;font-size:26px;line-height:1}
</style></head><body>
  <div class="wrap">
    <h1>Choose a store</h1>
    <p class="lede">Open a store's enrollment page to add a customer.</p>
    __STORES__
  </div>
</body></html>
"""
