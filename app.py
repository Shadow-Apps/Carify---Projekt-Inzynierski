#!/usr/bin/env python3
"""
e-Dziennik Serwisowy — Flask + Supabase Postgres (Render ready)
Funkcje:
- Rejestracja/logowanie (sesja cookie)
- Pojazdy (z paliwem), wpisy serwisowe, przypomnienia
- Harmonogram serwisów okresowych (interwały mies./km + wyliczanie kolejnego terminu)
- Statystyki/TCO + średnie spalanie
- Tankowania (stacja, litry, cena za litr, przebieg, pełny bak)
- Trasy (przejechane / planowane, godziny, średnie spalanie)
- Frontend single-file (INDEX_HTML) — czarno-czerwony motyw, marka/model z listy, paliwo zamiast VIN
"""

import os
import csv
import io
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, request, jsonify, session, send_from_directory,
    make_response
)

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# --- DB (SQLAlchemy + psycopg) ---
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# --- Konfiguracja ---
APP_TITLE = "e-Dziennik Serwisowy"
UPLOAD_DIR = "/tmp/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "webp"}

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("EDZIENNIK_SECRET", "dev-secret-change-me"),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
)


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


DB_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
ENGINE = None
if not DB_URL:
    print("[BOOT] Brak DATABASE_URL – ustaw w Render → Environment → Variables")
else:
    try:
        ENGINE = create_engine(
            _normalize_db_url(DB_URL),
            pool_pre_ping=True,
            connect_args={"sslmode": "require"},
        )
        # test szybki
        with ENGINE.connect() as c:
            c.execute(text("SELECT 1"))
        print("[DB] Połączenie OK")
    except Exception as e:
        print("[DB] Błąd połączenia:", e)
        ENGINE = None


def require_db():
    if ENGINE is None:
        raise RuntimeError("Brak połączenia z bazą (sprawdź DATABASE_URL).")


def get_now_utc_iso():
    return datetime.utcnow().isoformat()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "auth_required"}), 401
        return f(*args, **kwargs)

    return wrapper


# --- Inicjalizacja schematu (CREATE TABLE IF NOT EXISTS) ---

DDL = """
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vehicles (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    make        TEXT NOT NULL,
    model       TEXT NOT NULL,
    year        INTEGER,
    fuel        TEXT,
    reg_plate   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS service_entries (
    id            BIGSERIAL PRIMARY KEY,
    vehicle_id    BIGINT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    date          DATE NOT NULL,
    mileage       INTEGER,
    service_type  TEXT NOT NULL,
    description   TEXT,
    cost          NUMERIC,
    attachment    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS reminders (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vehicle_id          BIGINT REFERENCES vehicles(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    due_date            DATE,
    due_mileage         INTEGER,
    notify_email        BOOLEAN NOT NULL DEFAULT FALSE,
    notify_before_days  INTEGER NOT NULL DEFAULT 7,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

-- Harmonogramy serwisów okresowych
CREATE TABLE IF NOT EXISTS service_schedules (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vehicle_id          BIGINT REFERENCES vehicles(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,          -- np. 'Wymiana oleju'
    interval_months     INTEGER,                -- np. 12
    interval_km         INTEGER,                -- np. 15000
    last_service_date   DATE,
    last_service_mileage INTEGER,
    next_due_date       DATE,
    next_due_mileage    INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tankowania / spalanie
CREATE TABLE IF NOT EXISTS fuel_logs (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vehicle_id      BIGINT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    station         TEXT,
    liters          NUMERIC NOT NULL,
    price_per_liter NUMERIC NOT NULL,
    total_cost      NUMERIC,
    odometer        INTEGER,
    full_tank       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Trasy (realne / planowane)
CREATE TABLE IF NOT EXISTS trips (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vehicle_id      BIGINT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    start_date      DATE NOT NULL,
    end_date        DATE,
    start_time      TIME,
    end_time        TIME,
    start_location  TEXT,
    end_location    TEXT,
    distance_km     NUMERIC,
    avg_consumption NUMERIC,
    planned         BOOLEAN NOT NULL DEFAULT FALSE,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def init_db():
    require_db()
    with ENGINE.begin() as conn:
        for stmt in DDL.strip().split(";\n\n"):
            s = stmt.strip()
            if s:
                conn.execute(text(s))


@app.get("/api/health")
def health():
    try:
        require_db()
        init_db()
        with ENGINE.connect() as c:
            c.execute(text("SELECT 1"))
        return jsonify({"ok": True})
    except Exception as e:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(e),
                    "detail": getattr(e, "args", [""])[0] if e.args else "",
                }
            ),
            500,
        )


@app.post("/api/register")
def register():
    try:
        require_db()
        data = request.get_json() or {}
        email = (data.get("email") or "").strip().lower()
        name = (data.get("name") or "").strip()
        password = data.get("password") or ""
        if not (email and name and password):
            return jsonify({"error": "missing_fields"}), 400
        with ENGINE.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users(email,name,password_hash,created_at) VALUES (:e,:n,:ph,NOW())"
                ),
                {"e": email, "n": name, "ph": generate_password_hash(password)},
            )
        return jsonify({"ok": True})
    except SQLAlchemyError as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "email_in_use"}), 400
        return jsonify({"error": "server_error", "detail": str(e)}), 500


@app.post("/api/login")
def login():
    try:
        require_db()
        data = request.get_json() or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        with ENGINE.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT id,name,email,password_hash FROM users WHERE email=:e"
                    ),
                    {"e": email},
                )
                .mappings()
                .first()
            )
        if not row or not check_password_hash(row["password_hash"], password):
            return jsonify({"error": "invalid_credentials"}), 401
        session["user_id"] = int(row["id"])
        session["user_name"] = row["name"]
        return jsonify(
            {
                "ok": True,
                "user": {
                    "id": row["id"],
                    "name": row["name"],
                    "email": row["email"],
                },
            }
        )
    except Exception as e:
        return jsonify({"error": "server_error", "detail": str(e)}), 500


@app.post("/api/logout")
@login_required
def logout():
    session.clear()
    return jsonify({"ok": True})


# --- Vehicles ---


@app.get("/api/vehicles")
@login_required
def list_vehicles():
    require_db()
    with ENGINE.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT * FROM vehicles WHERE owner_id=:uid ORDER BY created_at DESC"
                ),
                {"uid": session["user_id"]},
            )
            .mappings()
            .all()
        )
    return jsonify([dict(r) for r in rows])


@app.post("/api/vehicles")
@login_required
def add_vehicle():
    try:
        require_db()
        data = request.get_json() or {}
        make = (data.get("make") or "").strip()
        model = (data.get("model") or "").strip()
        year = data.get("year")
        fuel = (data.get("fuel") or "").strip()
        reg = (data.get("reg_plate") or "").strip()
        if not (make and model):
            return jsonify({"error": "missing_fields"}), 400
        with ENGINE.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "INSERT INTO vehicles (owner_id,make,model,year,fuel,reg_plate,created_at) "
                        "VALUES (:uid,:make,:model,:year,:fuel,:reg,NOW()) RETURNING id"
                    ),
                    {
                        "uid": session["user_id"],
                        "make": make,
                        "model": model,
                        "year": year,
                        "fuel": fuel,
                        "reg": reg,
                    },
                )
                .mappings()
                .first()
            )
        return jsonify({"ok": True, "id": row["id"]})
    except Exception as e:
        return jsonify({"error": "server_error", "detail": str(e)}), 500


@app.delete("/api/vehicles/<int:vehicle_id>")
@login_required
def delete_vehicle(vehicle_id):
    require_db()
    with ENGINE.begin() as conn:
        conn.execute(
            text("DELETE FROM vehicles WHERE id=:vid AND owner_id=:uid"),
            {"vid": vehicle_id, "uid": session["user_id"]},
        )
    return jsonify({"ok": True})


# --- Service entries ---


@app.get("/api/entries")
@login_required
def list_entries():
    require_db()
    vehicle_id = request.args.get("vehicle_id", type=int)
    q = request.args.get("q", type=str)
    params = {"uid": session["user_id"]}
    sql = (
        "SELECT e.* FROM service_entries e "
        "JOIN vehicles v ON v.id=e.vehicle_id "
        "WHERE v.owner_id=:uid"
    )
    if vehicle_id:
        sql += " AND e.vehicle_id=:vid"
        params["vid"] = vehicle_id
    if q:
        sql += " AND (e.service_type ILIKE :qq OR e.description ILIKE :qq)"
        params["qq"] = f"%{q}%"
    sql += " ORDER BY e.date DESC, e.id DESC"
    with ENGINE.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return jsonify([dict(r) for r in rows])


@app.post("/api/entries")
@login_required
def add_entry():
    require_db()
    data = request.form if request.form else (request.get_json() or {})
    try:
        vehicle_id = int(data.get("vehicle_id"))
    except Exception:
        return jsonify({"error": "vehicle_id_required"}), 400

    date_s = data.get("date") or date.today().isoformat()
    mileage = int(data.get("mileage") or 0)
    service_type = (data.get("service_type") or "").strip()
    description = (data.get("description") or "").strip()
    cost = float(data.get("cost") or 0)
    if not service_type:
        return jsonify({"error": "service_type_required"}), 400

    attachment_name = None
    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        fname = secure_filename(f.filename)
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({"error": "file_type_not_allowed"}), 400
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        attachment_name = f"{ts}_{fname}"
        f.save(os.path.join(UPLOAD_DIR, attachment_name))

    with ENGINE.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO service_entries (vehicle_id,date,mileage,service_type,description,cost,attachment,created_at) "
                "VALUES (:vid,:dt,:mil,:typ,:desc,:cost,:att,NOW())"
            ),
            {
                "vid": vehicle_id,
                "dt": date_s,
                "mil": mileage,
                "typ": service_type,
                "desc": description,
                "cost": cost,
                "att": attachment_name,
            },
        )
    return jsonify({"ok": True, "attachment": attachment_name})


@app.put("/api/entries/<int:entry_id>")
@login_required
def update_entry(entry_id):
    require_db()
    data = request.get_json() or {}
    fields = []
    params = {"id": entry_id, "uid": session["user_id"]}
    for key in ("date", "mileage", "service_type", "description", "cost"):
        if key in data:
            fields.append(f"{key}=:{key}")
            params[key] = data[key]
    if not fields:
        return jsonify({"error": "no_fields"}), 400
    sql = (
        "UPDATE service_entries SET "
        + ",".join(fields)
        + ", updated_at=NOW() "
        "WHERE id=:id AND vehicle_id IN (SELECT id FROM vehicles WHERE owner_id=:uid)"
    )
    with ENGINE.begin() as conn:
        conn.execute(text(sql), params)
    return jsonify({"ok": True})


@app.delete("/api/entries/<int:entry_id>")
@login_required
def delete_entry(entry_id):
    require_db()
    with ENGINE.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM service_entries WHERE id=:id AND vehicle_id IN (SELECT id FROM vehicles WHERE owner_id=:uid)"
            ),
            {"id": entry_id, "uid": session["user_id"]},
        )
    return jsonify({"ok": True})


@app.get("/uploads/<path:filename>")
@login_required
def get_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


# --- Reminders ---


@app.get("/api/reminders")
@login_required
def list_reminders():
    require_db()
    with ENGINE.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT * FROM reminders WHERE user_id=:uid ORDER BY COALESCE(due_date, '9999-12-31') ASC, id DESC"
                ),
                {"uid": session["user_id"]},
            )
            .mappings()
            .all()
        )
    # flag is_due (prosta flaga)
    res = []
    for r in rows:
        is_due = False
        if r["due_date"]:
            is_due = date.fromisoformat(str(r["due_date"])) <= date.today()
        res.append({**dict(r), "is_due": is_due})
    return jsonify(res)


@app.post("/api/reminders")
@login_required
def add_reminder():
    require_db()
    d = request.get_json() or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title_required"}), 400
    with ENGINE.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO reminders (user_id,vehicle_id,title,due_date,due_mileage,notify_email,notify_before_days,created_at) "
                "VALUES (:uid,:vid,:title,:dd,:dm,:mail,:days,NOW())"
            ),
            {
                "uid": session["user_id"],
                "vid": d.get("vehicle_id"),
                "title": title,
                "dd": d.get("due_date"),
                "dm": d.get("due_mileage"),
                "mail": bool(d.get("notify_email")),
                "days": int(d.get("notify_before_days") or 7),
            },
        )
    return jsonify({"ok": True})


@app.put("/api/reminders/<int:rid>")
@login_required
def update_reminder(rid):
    require_db()
    d = request.get_json() or {}
    fields, params = [], {"rid": rid, "uid": session["user_id"]}
    for key in (
        "title",
        "due_date",
        "due_mileage",
        "notify_email",
        "notify_before_days",
        "completed_at",
    ):
        if key in d:
            fields.append(f"{key}=:{key}")
            params[key] = d[key]
    if not fields:
        return jsonify({"error": "no_fields"}), 400
    sql = "UPDATE reminders SET " + ",".join(fields) + " WHERE id=:rid AND user_id=:uid"
    with ENGINE.begin() as conn:
        conn.execute(text(sql), params)
    return jsonify({"ok": True})


@app.delete("/api/reminders/<int:rid>")
@login_required
def delete_reminder(rid):
    require_db()
    with ENGINE.begin() as conn:
        conn.execute(
            text("DELETE FROM reminders WHERE id=:rid AND user_id=:uid"),
            {"rid": rid, "uid": session["user_id"]},
        )
    return jsonify({"ok": True})


# --- Harmonogram serwisów okresowych ---


@app.get("/api/schedules")
@login_required
def list_schedules():
    require_db()
    with ENGINE.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT * FROM service_schedules WHERE user_id=:uid ORDER BY created_at DESC"
                ),
                {"uid": session["user_id"]},
            )
            .mappings()
            .all()
        )
    return jsonify([dict(r) for r in rows])


@app.post("/api/schedules")
@login_required
def add_schedule():
    """
    Body: { vehicle_id?, kind, interval_months?, interval_km?, last_service_date?, last_service_mileage? }
    Wyliczamy next_due_date / next_due_mileage.
    """
    require_db()
    d = request.get_json() or {}
    kind = (d.get("kind") or "").strip()
    if not kind:
        return jsonify({"error": "kind_required"}), 400

    last_date = d.get("last_service_date")
    last_mil = d.get("last_service_mileage")
    interval_m = int(d.get("interval_months") or 0) or None
    interval_km = int(d.get("interval_km") or 0) or None

    next_date = None
    if last_date and interval_m:
        try:
            base = date.fromisoformat(last_date)
            month = base.month - 1 + interval_m
            year = base.year + month // 12
            month = month % 12 + 1
            day = min(
                base.day,
                [
                    31,
                    29
                    if year % 4 == 0
                    and (year % 100 != 0 or year % 400 == 0)
                    else 28,
                    31,
                    30,
                    31,
                    30,
                    31,
                    31,
                    30,
                    31,
                    30,
                    31,
                ][month - 1],
            )
            next_date = date(year, month, day).isoformat()
        except Exception:
            next_date = None

    next_km = None
    if last_mil and interval_km:
        try:
            next_km = int(last_mil) + int(interval_km)
        except Exception:
            next_km = None

    with ENGINE.begin() as conn:
        row = (
            conn.execute(
                text(
                    """INSERT INTO service_schedules
                    (user_id,vehicle_id,kind,interval_months,interval_km,last_service_date,last_service_mileage,next_due_date,next_due_mileage,created_at)
                    VALUES (:uid,:vid,:kind,:im,:ik,:ld,:lm,:nd,:nk,NOW()) RETURNING *"""
                ),
                {
                    "uid": session["user_id"],
                    "vid": d.get("vehicle_id"),
                    "kind": kind,
                    "im": interval_m,
                    "ik": interval_km,
                    "ld": last_date,
                    "lm": last_mil,
                    "nd": next_date,
                    "nk": next_km,
                },
            )
            .mappings()
            .first()
        )
    return jsonify(dict(row))


@app.put("/api/schedules/<int:sid>")
@login_required
def update_schedule(sid):
    require_db()
    d = request.get_json() or {}
    interval_m = d.get("interval_months")
    interval_km = d.get("interval_km")
    last_date = d.get("last_service_date")
    last_mil = d.get("last_service_mileage")

    next_date = d.get("next_due_date")
    next_km = d.get("next_due_mileage")

    if (last_date and interval_m) and not next_date:
        try:
            base = date.fromisoformat(last_date)
            interval_m = int(interval_m)
            month = base.month - 1 + interval_m
            year = base.year + month // 12
            month = month % 12 + 1
            day = min(
                base.day,
                [
                    31,
                    29
                    if year % 4 == 0
                    and (year % 100 != 0 or year % 400 == 0)
                    else 28,
                    31,
                    30,
                    31,
                    30,
                    31,
                    31,
                    30,
                    31,
                    30,
                    31,
                ][month - 1],
            )
            next_date = date(year, month, day).isoformat()
        except Exception:
            next_date = None

    if (last_mil and interval_km) and not next_km:
        try:
            next_km = int(last_mil) + int(interval_km)
        except Exception:
            next_km = None

    fields, params = [], {"sid": sid, "uid": session["user_id"]}
    for key in (
        "vehicle_id",
        "kind",
        "interval_months",
        "interval_km",
        "last_service_date",
        "last_service_mileage",
    ):
        if key in d:
            fields.append(f"{key}=:{key}")
            params[key] = d[key]
    if next_date is not None:
        fields.append("next_due_date=:nd")
        params["nd"] = next_date
    if next_km is not None:
        fields.append("next_due_mileage=:nk")
        params["nk"] = next_km

    if not fields:
        return jsonify({"error": "no_fields"}), 400

    sql = "UPDATE service_schedules SET " + ",".join(fields) + " WHERE id=:sid AND user_id=:uid"
    with ENGINE.begin() as conn:
        conn.execute(text(sql), params)
    return jsonify({"ok": True})


@app.delete("/api/schedules/<int:sid>")
@login_required
def delete_schedule(sid):
    require_db()
    with ENGINE.begin() as conn:
        conn.execute(
            text("DELETE FROM service_schedules WHERE id=:sid AND user_id=:uid"),
            {"sid": sid, "uid": session["user_id"]},
        )
    return jsonify({"ok": True})


# --- Tankowania ---


@app.get("/api/fuel_logs")
@login_required
def list_fuel_logs():
    require_db()
    vehicle_id = request.args.get("vehicle_id", type=int)
    params = {"uid": session["user_id"]}
    sql = (
        "SELECT f.* FROM fuel_logs f "
        "JOIN vehicles v ON v.id=f.vehicle_id "
        "WHERE v.owner_id=:uid"
    )
    if vehicle_id:
        sql += " AND f.vehicle_id=:vid"
        params["vid"] = vehicle_id
    sql += " ORDER BY f.date DESC, f.id DESC"
    with ENGINE.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return jsonify([dict(r) for r in rows])


@app.post("/api/fuel_logs")
@login_required
def add_fuel_log():
    require_db()
    d = request.get_json() or {}
    try:
        vehicle_id = int(d.get("vehicle_id") or 0)
    except Exception:
        return jsonify({"error": "vehicle_id_required"}), 400

    if not vehicle_id:
        return jsonify({"error": "vehicle_id_required"}), 400

    date_s = d.get("date") or date.today().isoformat()
    station = (d.get("station") or "").strip()
    try:
        liters = float(d.get("liters") or 0)
        price = float(d.get("price_per_liter") or 0)
    except Exception:
        return jsonify({"error": "bad_number"}), 400

    if liters <= 0 or price <= 0:
        return jsonify({"error": "missing_fields"}), 400

    total_cost = d.get("total_cost")
    try:
        total_cost = float(total_cost) if total_cost is not None else liters * price
    except Exception:
        total_cost = liters * price

    odometer = d.get("odometer")
    try:
        odometer = int(odometer) if odometer not in (None, "") else None
    except Exception:
        odometer = None

    full_tank = bool(d.get("full_tank"))

    with ENGINE.begin() as conn:
        # walidacja właściciela
        owner_id = conn.execute(
            text("SELECT owner_id FROM vehicles WHERE id=:vid"),
            {"vid": vehicle_id},
        ).scalar()
        if not owner_id or int(owner_id) != int(session["user_id"]):
            return jsonify({"error": "forbidden"}), 403

        conn.execute(
            text(
                """
            INSERT INTO fuel_logs
            (user_id,vehicle_id,date,station,liters,price_per_liter,total_cost,odometer,full_tank,created_at)
            VALUES (:uid,:vid,:dt,:st,:lit,:ppl,:tc,:odo,:full,NOW())
        """
            ),
            {
                "uid": session["user_id"],
                "vid": vehicle_id,
                "dt": date_s,
                "st": station,
                "lit": liters,
                "ppl": price,
                "tc": total_cost,
                "odo": odometer,
                "full": full_tank,
            },
        )
    return jsonify({"ok": True})


@app.delete("/api/fuel_logs/<int:fid>")
@login_required
def delete_fuel_log(fid):
    require_db()
    with ENGINE.begin() as conn:
        conn.execute(
            text("DELETE FROM fuel_logs WHERE id=:fid AND user_id=:uid"),
            {"fid": fid, "uid": session["user_id"]},
        )
    return jsonify({"ok": True})


# --- Trasy ---


@app.get("/api/trips")
@login_required
def list_trips():
    require_db()
    vehicle_id = request.args.get("vehicle_id", type=int)
    params = {"uid": session["user_id"]}
    sql = (
        "SELECT t.* FROM trips t "
        "JOIN vehicles v ON v.id=t.vehicle_id "
        "WHERE v.owner_id=:uid"
    )
    if vehicle_id:
        sql += " AND t.vehicle_id=:vid"
        params["vid"] = vehicle_id
    sql += " ORDER BY t.start_date DESC, t.id DESC"
    with ENGINE.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return jsonify([dict(r) for r in rows])


@app.post("/api/trips")
@login_required
def add_trip():
    require_db()
    d = request.get_json() or {}
    try:
        vehicle_id = int(d.get("vehicle_id") or 0)
    except Exception:
        return jsonify({"error": "vehicle_id_required"}), 400

    if not vehicle_id:
        return jsonify({"error": "vehicle_id_required"}), 400

    start_date = d.get("start_date") or date.today().isoformat()
    end_date = d.get("end_date") or None
    start_time = d.get("start_time") or None
    end_time = d.get("end_time") or None
    start_location = (d.get("start_location") or "").strip()
    end_location = (d.get("end_location") or "").strip()
    notes = (d.get("notes") or "").strip()

    try:
        distance_km = float(d.get("distance_km") or 0) or None
    except Exception:
        distance_km = None

    try:
        avg_consumption = float(d.get("avg_consumption") or 0) or None
    except Exception:
        avg_consumption = None

    planned = bool(d.get("planned"))

    with ENGINE.begin() as conn:
        owner_id = conn.execute(
            text("SELECT owner_id FROM vehicles WHERE id=:vid"),
            {"vid": vehicle_id},
        ).scalar()
        if not owner_id or int(owner_id) != int(session["user_id"]):
            return jsonify({"error": "forbidden"}), 403

        conn.execute(
            text(
                """
            INSERT INTO trips
            (user_id,vehicle_id,start_date,end_date,start_time,end_time,start_location,end_location,distance_km,avg_consumption,planned,notes,created_at)
            VALUES (:uid,:vid,:sd,:ed,:st,:et,:sl,:el,:dist,:avg,:plan,:notes,NOW())
        """
            ),
            {
                "uid": session["user_id"],
                "vid": vehicle_id,
                "sd": start_date,
                "ed": end_date,
                "st": start_time,
                "et": end_time,
                "sl": start_location,
                "el": end_location,
                "dist": distance_km,
                "avg": avg_consumption,
                "plan": planned,
                "notes": notes,
            },
        )
    return jsonify({"ok": True})


@app.delete("/api/trips/<int:tid>")
@login_required
def delete_trip(tid):
    require_db()
    with ENGINE.begin() as conn:
        conn.execute(
            text("DELETE FROM trips WHERE id=:tid AND user_id=:uid"),
            {"tid": tid, "uid": session["user_id"]},
        )
    return jsonify({"ok": True})


# --- Statystyki / TCO ---


@app.get("/api/stats")
@login_required
def stats():
    """
    Zwraca:
    - by_day: [{ymd, total_cost}]  (serwis + paliwo)
    - last_mileage: [{vehicle_id, label, mileage}]
    - tco: { total_cost, months, km, cost_per_km, cost_per_month }
    - fuel_stats: [{ vehicle_id, label, total_liters, distance_km, avg_l_100km }]
    - daily_vehicle_costs: [{ymd, vehicle_id, label, service_cost, fuel_cost, total_cost}]
    """
    require_db()
    uid = session["user_id"]
    with ENGINE.begin() as conn:
        # koszty dziennie serwis
        by_day_service = (
            conn.execute(
                text(
                    "SELECT TO_CHAR(date,'YYYY-MM-DD') AS ymd, COALESCE(SUM(cost),0) AS total_cost "
                    "FROM service_entries e JOIN vehicles v ON v.id=e.vehicle_id "
                    "WHERE v.owner_id=:uid GROUP BY 1 ORDER BY 1"
                ),
                {"uid": uid},
            )
            .mappings()
            .all()
        )

        # koszty dziennie paliwo
        by_day_fuel = (
            conn.execute(
                text(
                    "SELECT TO_CHAR(date,'YYYY-MM-DD') AS ymd, COALESCE(SUM(total_cost),0) AS total_cost "
                    "FROM fuel_logs f JOIN vehicles v ON v.id=f.vehicle_id "
                    "WHERE v.owner_id=:uid GROUP BY 1 ORDER BY 1"
                ),
                {"uid": uid},
            )
            .mappings()
            .all()
        )

        # ostatnie przebiegi per pojazd
        last_mileage = (
            conn.execute(
                text(
                    "SELECT v.id AS vehicle_id, (v.make || ' ' || v.model || ' ' || COALESCE(v.reg_plate,'')) AS label, "
                    "COALESCE(MAX(e.mileage),0) AS mileage "
                    "FROM vehicles v LEFT JOIN service_entries e ON e.vehicle_id=v.id "
                    "WHERE v.owner_id=:uid GROUP BY v.id, label ORDER BY label ASC"
                ),
                {"uid": uid},
            )
            .mappings()
            .all()
        )

        # TCO: suma kosztów serwis + paliwo
        total_service_cost = (
            conn.execute(
                text(
                    "SELECT COALESCE(SUM(cost),0) FROM service_entries e JOIN vehicles v ON v.id=e.vehicle_id WHERE v.owner_id=:uid"
                ),
                {"uid": uid},
            ).scalar()
            or 0
        )
        total_fuel_cost = (
            conn.execute(
                text(
                    "SELECT COALESCE(SUM(total_cost),0) FROM fuel_logs f JOIN vehicles v ON v.id=f.vehicle_id WHERE v.owner_id=:uid"
                ),
                {"uid": uid},
            ).scalar()
            or 0
        )
        total_cost = float(total_service_cost) + float(total_fuel_cost)

        mi = (
            conn.execute(
                text(
                    "SELECT MIN(date) FROM service_entries e JOIN vehicles v ON v.id=e.vehicle_id WHERE v.owner_id=:uid"
                ),
                {"uid": uid},
            ).scalar()
        )

        # km: różnica max-min mileage na poziomie całej floty
        min_mil = (
            conn.execute(
                text(
                    "SELECT COALESCE(MIN(mileage),0) FROM service_entries e JOIN vehicles v ON v.id=e.vehicle_id WHERE v.owner_id=:uid AND mileage IS NOT NULL"
                ),
                {"uid": uid},
            ).scalar()
            or 0
        )
        max_mil = (
            conn.execute(
                text(
                    "SELECT COALESCE(MAX(mileage),0) FROM service_entries e JOIN vehicles v ON v.id=e.vehicle_id WHERE v.owner_id=:uid AND mileage IS NOT NULL"
                ),
                {"uid": uid},
            ).scalar()
            or 0
        )
        km = max(0, (max_mil - min_mil))

        # Statystyki spalania z fuel_logs
        fuel_rows = (
            conn.execute(
                text(
                    """
                SELECT v.id AS vehicle_id,
                       (v.make || ' ' || v.model || ' ' || COALESCE(v.reg_plate,'')) AS label,
                       COALESCE(SUM(f.liters),0) AS total_liters,
                       MIN(f.odometer) AS min_odo,
                       MAX(f.odometer) AS max_odo
                FROM fuel_logs f
                JOIN vehicles v ON v.id=f.vehicle_id
                WHERE v.owner_id=:uid
                GROUP BY v.id, label
                ORDER BY label ASC
            """
                ),
                {"uid": uid},
            )
            .mappings()
            .all()
        )

        # dzienne koszty per auto (serwis + paliwo, rozbite)
        daily_rows = (
            conn.execute(
                text(
                    """
            SELECT TO_CHAR(d,'YYYY-MM-DD') AS ymd,
                   vehicle_id,
                   label,
                   SUM(service_cost) AS service_cost,
                   SUM(fuel_cost) AS fuel_cost,
                   SUM(service_cost + fuel_cost) AS total_cost
            FROM (
                SELECT e.date AS d,
                       v.id AS vehicle_id,
                       (v.make || ' ' || v.model || ' ' || COALESCE(v.reg_plate,'')) AS label,
                       COALESCE(e.cost,0) AS service_cost,
                       0::numeric AS fuel_cost
                FROM service_entries e
                JOIN vehicles v ON v.id=e.vehicle_id
                WHERE v.owner_id=:uid
                UNION ALL
                SELECT f.date AS d,
                       v.id AS vehicle_id,
                       (v.make || ' ' || v.model || ' ' || COALESCE(v.reg_plate,'')) AS label,
                       0::numeric AS service_cost,
                       COALESCE(f.total_cost,0) AS fuel_cost
                FROM fuel_logs f
                JOIN vehicles v ON v.id=f.vehicle_id
                WHERE v.owner_id=:uid
            ) s
            GROUP BY ymd, vehicle_id, label
            ORDER BY ymd, label
            """
                ),
                {"uid": uid},
            )
            .mappings()
            .all()
        )

    months = 0
    if mi:
        d0 = mi if isinstance(mi, date) else date.fromisoformat(str(mi))
        months = max(
            1, (date.today().year - d0.year) * 12 + (date.today().month - d0.month)
        )

    cost_per_km = float(total_cost) / km if km > 0 else None
    cost_per_month = float(total_cost) / months if months > 0 else None

    fuel_stats = []
    for r in fuel_rows:
        min_odo = r["min_odo"]
        max_odo = r["max_odo"]
        distance = None
        if min_odo is not None and max_odo is not None:
            try:
                distance = max(0, int(max_odo) - int(min_odo))
            except Exception:
                distance = None
        avg_l_100 = None
        total_liters = float(r["total_liters"] or 0)
        if distance and distance > 0 and total_liters > 0:
            avg_l_100 = total_liters * 100.0 / float(distance)
        fuel_stats.append(
            {
                "vehicle_id": r["vehicle_id"],
                "label": r["label"],
                "total_liters": total_liters,
                "distance_km": distance,
                "avg_l_100km": avg_l_100,
            }
        )

    # scalone koszty dziennie (serwis + paliwo) - tylko suma per data
    by_day_map = {}
    for r in by_day_service:
        y = r["ymd"]
        by_day_map[y] = float(r["total_cost"] or 0)
    for r in by_day_fuel:
        y = r["ymd"]
        by_day_map[y] = by_day_map.get(y, 0.0) + float(r["total_cost"] or 0)
    by_day_combined = [
        {"ymd": k, "total_cost": v} for k, v in sorted(by_day_map.items())
    ]

    return jsonify(
        {
            "by_day": by_day_combined,
            "last_mileage": [dict(r) for r in last_mileage],
            "tco": {
                "total_cost": float(total_cost),
                "months": months,
                "km": km,
                "cost_per_km": cost_per_km,
                "cost_per_month": cost_per_month,
            },
            "fuel_stats": fuel_stats,
            "daily_vehicle_costs": [dict(r) for r in daily_rows],
        }
    )


# --- FRONTEND (INDEX_HTML) ---#

INDEX_HTML = """
<!doctype html>
<html lang=pl>
<head>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
  <meta charset=utf-8>
  <meta name=viewport content="width=device-width,initial-scale=1">
  <title>{APP_TITLE}</title>

  <script>
    async function api(path, opts = {}) {
      const res = await fetch(path, Object.assign({ headers: {} }, opts));
      const ct = res.headers.get('content-type') || '';
      let data = null;
      if (ct.includes('application/json')) data = await res.json().catch(() => null);
      else data = await res.text().catch(() => null);
      if (!res.ok) {
        const msg = (data && (data.error || data.detail || data.message)) || String(data) || 'Błąd';
        throw new Error('[' + res.status + '] ' + msg);
      }
      return data;
    }
    window.addEventListener('error', ev => { console.error('[window.error]', ev.message); alert('Błąd JS: ' + ev.message); });
    window.addEventListener('unhandledrejection', ev => { console.error('[unhandledrejection]', ev.reason); alert('Błąd: ' + (ev.reason?.message || ev.reason || 'Nieznany')); });
  </script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

  <style>
    :root {
      --bg:#0a0a0a; --bg2:#1a0000; --card:#141414; --text:#f3f4f6; --muted:#9ca3af; --border:#262626;
      --accent:#ff3232; --accent-600:#cc2727; --amber:#f59e0b; --r:14px; --pad:14px; --gap:18px; --sh:0 10px 28px rgba(0,0,0,.7)
    }
    * { box-sizing:border-box }
    body {
      margin:0;
      background:linear-gradient(180deg,var(--bg),var(--bg2));
      color:var(--text);
      font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;
    }
    header {
      position:sticky; top:0; z-index:20; background:#0f0f0f; border-bottom:1px solid var(--border);
      display:flex; align-items:center; gap:var(--gap); padding:var(--pad) calc(var(--pad)*1.5)
    }
    .brand { display:flex; align-items:center; gap:10px; font-weight:800 }
    .brand svg { width:28px; height:28px }
    .hello { font-size:13px; color:var(--muted); }
    main {
      padding:calc(var(--pad)*1.5);
      display:grid;
      grid-template-columns:minmax(360px,420px) 1fr;
      gap:var(--gap);
      align-items:start
    }
    .card { background:var(--card); border:1px solid var(--border); border-radius:var(--r); padding:var(--pad); box-shadow:var(--sh) }
    h3 { margin:0 0 10px }
    h4 { margin:0 0 6px }
    label { display:block; font-size:12px; color:var(--muted); margin:8px 0 6px }
    input, select, textarea {
      width:100%; display:block; padding:12px; border-radius:10px;
      border:1px solid var(--border); background:#0f0f0f; color:#f3f4f6; outline:none;
      font-family:inherit;
    }
    input:focus, select:focus, textarea:focus { border-color:var(--accent); box-shadow:0 0 0 2px rgba(255,50,50,.45) }
    button {
      padding:10px 14px; border:1px solid var(--border); background:#0f0f0f;
      color:#f3f4f6; border-radius:10px; cursor:pointer; font-family:inherit;
    }
    button.primary { background:var(--accent); border-color:var(--accent); color:#fff }
    button.primary:hover { background:var(--accent-600) }
    button.cta { background:var(--amber); color:#111; border-color:#a16207 }
    a { color:#ff7b7b; text-decoration:none }
    a:hover { text-decoration:underline }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:var(--gap) }
    @media (max-width:1100px) { main { grid-template-columns:1fr } .row { grid-template-columns:1fr } }
    table {
      width:100%; border-collapse:collapse; background:#0f0f0f;
      border:1px solid var(--border); border-radius:var(--r); overflow:hidden;
      font-family:inherit;
    }
    thead th { background:#1f1f1f; color:#ff9c9c }
    th, td { padding:12px; border-bottom:1px solid var(--border); text-align:left; font-size:14px; font-family:inherit; }
    td.actions {
      display:flex; gap:8px; flex-wrap:wrap; border-bottom:none;
    }
    .muted { color:var(--muted) }
    .toast { position:fixed; right:16px; bottom:16px; background:var(--accent); color:#fff; padding:10px 14px; border-radius:10px; display:none; box-shadow:var(--sh) }
    canvas { background:radial-gradient(ellipse at top,#151515,#0d0d0d); border:1px solid var(--border); border-radius:12px; padding:8px; max-height:320px }
    .stats-wrap { display:grid; grid-template-columns: 2fr 1fr; gap:var(--gap); }
    @media (max-width:1100px) { .stats-wrap { grid-template-columns: 1fr } }
    .pill { padding:8px 10px; border:1px solid var(--border); border-radius:10px; background:#0f0f0f; width:auto; max-width:340px }
    .tooltray { display:flex; gap:8px; flex-wrap:wrap; align-items:center }

    .modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.65); display:none; align-items:center; justify-content:center; z-index:50; }
    .modal { width:min(720px, 94vw); background:#141414; border:1px solid var(--border); border-radius:14px; padding:18px; box-shadow:0 20px 60px rgba(0,0,0,.6) }
    .modal header { position:static; background:transparent; border:0; padding:0 0 8px; display:flex; justify-content:space-between; align-items:center }

    input[type="date"]::-webkit-calendar-picker-indicator,
    input[type="time"]::-webkit-calendar-picker-indicator,
    input[type="month"]::-webkit-calendar-picker-indicator {
      filter: invert(1);
    }

    .calendar-weekdays {
      display:grid;
      grid-template-columns:repeat(7,minmax(0,1fr));
      gap:8px;
      margin-top:4px;
    }
    .calendar-weekdays span {
      font-size:11px;
      text-align:center;
      color:var(--muted);
    }
    .calendar-grid {
      display:grid;
      grid-template-columns:repeat(7,minmax(0,1fr));
      gap:8px;
      margin-top:8px;
    }
    .calendar-cell {
      min-height:80px;
      padding:6px 8px;
      border-radius:10px;
      border:1px solid var(--border);
      background:#0f0f0f;
      font-size:11px;
    }
    .calendar-cell .day-num {
      font-weight:600;
      margin-bottom:4px;
    }
    .calendar-cell .cost-total {
      font-size:12px;
      margin-bottom:4px;
    }
    .calendar-cell .entry {
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#ff3232"/><stop offset="100%" stop-color="#cc2727"/></linearGradient></defs>
        <circle cx="20" cy="20" r="10" stroke="url(#g)" stroke-width="4"/>
        <path d="M28 28 L46 46 M46 46 h6 v6 h-6 v-6 m6 0 v-6 h-6" stroke="url(#g)" stroke-width="5" stroke-linecap="round"/>
        <circle cx="20" cy="20" r="3" fill="#ff3232"/>
      </svg>
      <span>{APP_TITLE}</span>
    </div>
    <div style="margin-left:auto; display:flex; gap:12px; align-items:center;">
      <span id="helloUser" class="hello"></span>
      <button type="button" id="authBtn" class="cta" onclick="openAuthModal()">Zaloguj / Zarejestruj</button>
      <button type="button" id="logoutBtn" style="display:none" onclick="logout()">Wyloguj</button>
    </div>
  </header>

  <main>
    <section class="card">
      <h3>Pojazdy</h3>
      <div>
        <label>Marka</label>
        <select id="makeSelect" onchange="onMakeChange()"></select>
        <div id="makeCustomWrap" style="display:none; margin-top:8px;">
          <label>Inna marka</label><input id="makeCustom" placeholder="np. Zastava">
        </div>

        <label>Model</label>
        <select id="modelSelect"></select>
        <div id="modelCustomWrap" style="display:none; margin-top:8px;">
          <label>Inny model</label><input id="modelCustom" placeholder="np. 750">
        </div>

        <div class="row" style="margin-top:8px;">
          <div>
            <label>Rok</label>
            <select id="year"></select>
          </div>
          <div>
            <label>Paliwo</label>
            <select id="fuel">
              <option value="">— wybierz —</option>
              <option value="Benzyna">Benzyna</option>
              <option value="Diesel">Diesel</option>
              <option value="LPG+benzyna">LPG+benzyna</option>
            </select>
          </div>
        </div>

        <label>Nr rej.</label>
        <input id="reg_plate" placeholder="WX1234Y" oninput="enforcePlate(this)" maxlength="10">
        <div style="margin-top:10px;"><button type="button" class="primary" onclick="addVehicle()">Dodaj pojazd</button></div>
      </div>

      <hr style="border-color:#262626; margin:16px 0;">
      <div class="tooltray">
        <button type="button" onclick="openReminders()">
          <i class="bi bi-bell-fill" style="margin-right:6px;"></i>Przypomnienia
        </button>
        <button type="button" onclick="openSchedules()">
          <i class="bi bi-calendar2-week-fill" style="margin-right:6px;"></i>Harmonogram
        </button>
      </div>
    </section>

    <section class="card">
      <div class="tooltray" style="justify-content:space-between;">
        <h3 style="margin:0;">Wpisy serwisowe</h3>
        <div style="display:flex; gap:8px; align-items:center;">
          <span class="muted" style="font-size:12px;">Aktualny pojazd:</span>
          <select id="vehicleSelect" class="pill" onchange="refreshEntries()"></select>
          <button type="button" onclick="deleteSelectedVehicle()">Usuń wybrany</button>
        </div>
      </div>

      <div class="row" style="margin-top:10px;">
        <div><label>Data</label><input id="date" type="date"></div>
        <div><label>Przebieg (km)</label><input id="mileage" type="number"></div>
      </div>
      <label>Typ usługi</label><input id="service_type" placeholder="Wymiana oleju">
      <label>Opis</label><textarea id="description" rows="3" placeholder="Szczegóły usługi..."></textarea>
      <div class="row">
        <div><label>Koszt (PLN)</label><input id="cost" type="number" step="0.01"></div>
        <div><label>Załącznik (jpg/png/pdf)</label><input id="file" type="file"></div>
      </div>
      <div style="margin-top:10px;"><button type="button" class="primary" onclick="addEntry()">Dodaj wpis</button></div>

      <div style="margin-top:16px; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <input id="search" placeholder="Szukaj w typie/opisie" oninput="refreshEntries()" style="max-width:360px;">
        <span class="muted" style="font-size:12px;">Kliknij link w kolumnie „Plik”, aby podejrzeć załącznik.</span>
      </div>

      <div style="overflow:auto; margin-top:10px;">
        <table>
          <thead><tr><th>Data</th><th>Przebieg</th><th>Typ</th><th>Opis</th><th>Koszt</th><th>Plik</th><th></th></tr></thead>
          <tbody id="entriesTbody"></tbody>
        </table>
      </div>
    </section>

    <!-- Karta: paliwo -->
    <section class="card" style="grid-column:1 / span 2;">
      <h3><i class="bi bi-fuel-pump-fill" style="margin-right:8px;"></i>Paliwo</h3>
      <div style="margin-top:10px;">
        <label>Pojazd</label>
        <select id="fuel_vehicle" onchange="loadFuelLogs()"></select>
        <div class="row">
          <div><label>Data</label><input id="fuel_date" type="date"></div>
          <div><label>Przebieg (km)</label><input id="fuel_odometer" type="number"></div>
        </div>
        <label>Stacja</label><input id="fuel_station" placeholder="np. Orlen, BP, Shell">
        <div class="row">
          <div><label>Litry</label><input id="fuel_liters" type="number" step="0.01"></div>
          <div><label>Cena za litr (PLN)</label><input id="fuel_price" type="number" step="0.01"></div>
        </div>
        <label><input type="checkbox" id="fuel_full" style="width:auto;display:inline-block;margin-right:6px;"> Pełny bak</label>
        <div style="margin-top:8px;"><button type="button" class="primary" onclick="addFuelLog()">Zapisz tankowanie</button></div>

        <h4 style="margin-top:16px;">Historia tankowań</h4>
        <div style="max-height:260px;overflow:auto;">
          <table>
            <thead><tr><th>Data</th><th>Stacja</th><th>Litry</th><th>PLN/l</th><th>Kwota</th><th>Przebieg</th><th>Pełny</th><th></th></tr></thead>
            <tbody id="fuelTbody"></tbody>
          </table>
        </div>

        <h4 style="margin-top:16px;">Średnie spalanie (z tankowań)</h4>
        <table>
          <thead><tr><th>Pojazd</th><th>Litry</th><th>Dystans (km)</th><th>Śr. l/100 km</th></tr></thead>
          <tbody id="fuelSummaryTbody"></tbody>
        </table>
      </div>
    </section>

    <!-- Karta: trasy -->
    <section class="card" style="grid-column:1 / span 2;">
      <h3><i class="bi bi-geo-alt-fill" style="margin-right:8px;"></i>Trasy</h3>
      <div style="margin-top:10px;">
        <label>Pojazd</label>
        <select id="trip_vehicle" onchange="loadTrips()"></select>
        <div class="row">
          <div><label>Data startu</label><input id="trip_start_date" type="date"></div>
          <div><label>Data końca (opcjonalnie)</label><input id="trip_end_date" type="date"></div>
        </div>
        <div class="row">
          <div><label>Godzina wyjazdu</label><input id="trip_start_time" type="time"></div>
          <div><label>Godzina przyjazdu</label><input id="trip_end_time" type="time"></div>
        </div>
        <div class="row">
          <div><label>Start (miejsce)</label><input id="trip_start_loc" placeholder="np. Warszawa"></div>
          <div><label>Cel (miejsce)</label><input id="trip_end_loc" placeholder="np. Kraków"></div>
        </div>
        <div class="row">
          <div><label>Dystans (km)</label><input id="trip_distance" type="number" step="0.1" placeholder="np. 300"></div>
          <div><label>Średnie spalanie (l/100 km)</label><input id="trip_avg_consumption" type="number" step="0.1" placeholder="np. 7.5"></div>
        </div>
        <label><input type="checkbox" id="trip_planned" style="width:auto;display:inline-block;margin-right:6px;"> Planowana trasa</label>
        <label>Notatki</label><textarea id="trip_notes" rows="3" placeholder="Np. autostrada A2, objazd, itp."></textarea>
        <div style="margin-top:8px;"><button type="button" class="primary" onclick="addTrip()">Zapisz trasę</button></div>

        <h4 style="margin-top:16px;">Lista tras</h4>
        <div style="max-height:260px;overflow:auto;">
          <table>
            <thead><tr><th>Data</th><th>Trasa</th><th>Dystans (km)</th><th>Czas</th><th>Śr. l/100 km</th><th>Status</th><th></th></tr></thead>
            <tbody id="tripTbody"></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>

  <section class="card" style="margin:0 calc(var(--pad)*1.5) calc(var(--pad)*1.5);">
    <div class="tooltray" style="justify-content:space-between;">
      <h3 class="section-title">
       <i class="bi bi-speedometer2" style="font-size: 1.4rem; margin-right: 8px;"></i>
       Statystyki
       </h3>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
        <label style="margin:0;align-self:center;">Zakres dni (dla wykresu):</label>
        <select id="dash_range" onchange="loadStats()" style="max-width:220px;">
          <option value="7">Ostatnie 7 dni</option>
          <option value="30" selected>Ostatnie 30 dni</option>
          <option value="90">Ostatnie 90 dni</option>
          <option value="0">Wszystko</option>
        </select>
      </div>
    </div>

    <div class="stats-wrap" style="margin-top:10px;">
      <div>
        <h4 style="margin:0 0 8px">Koszt wg pojazdu</h4>
        <canvas id="chartCost"></canvas>
      </div>
      <div>
        <h4 style="margin:0 0 8px">Suma kosztów auta</h4>
        <table>
          <thead><tr><th>Pojazd</th><th>Suma (PLN)</th></tr></thead>
          <tbody id="sumByVehicleTbody"></tbody>
          <tfoot><tr><th style="text-align:right;">Suma wszystkich</th><th id="sumAll">0,00</th></tr></tfoot>
        </table>
        <div style="margin-top:12px;">
          <h4 style="margin:0 0 8px">Ostatnie przebiegi</h4>
          <table>
            <thead><tr><th>Pojazd</th><th>Ostatni przebieg</th></tr></thead>
            <tbody id="mileageTbody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div style="margin-top:16px;">
      <h4 style="margin:0 0 8px">Kalendarium kosztów (serwis + paliwo)</h4>
      <div class="tooltray" style="margin-bottom:8px;">
        <div>
          <label style="margin:0 8px 0 0; font-size:12px;">Miesiąc:</label>
          <input type="month" id="cal_month" onchange="renderCostCalendar()" style="max-width:180px;">
        </div>
      </div>
      <div class="calendar-weekdays">
        <span>Pn</span><span>Wt</span><span>Śr</span><span>Cz</span><span>Pt</span><span>Sb</span><span>Nd</span>
      </div>
      <div id="calendarGrid" class="calendar-grid"></div>
    </div>
  </section>

  <div id="authModal" class="modal-backdrop" onclick="backdropClose(event)">
    <div class="modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <header>
        <h3>Zaloguj się / Zarejestruj</h3>
        <button onclick="closeAuthModal()">✕</button>
      </header>
      <div class="row">
        <div><label>Email</label><input id="regEmail" placeholder="uzytkownik@domena.pl"></div>
        <div><label>Imię</label><input id="regName" placeholder="Jan Kowalski"></div>
      </div>
      <label>Hasło</label><input id="regPass" type="password" placeholder="********">
      <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;">
        <button type="button" class="primary" onclick="register()">Rejestracja</button>
        <button type="button" onclick="login()">Logowanie</button>
      </div>
      <p class="muted" style="font-size:12px; margin-top:8px;">Zarejestruj konto albo zaloguj się aby zarządzać pojazdami.</p>
    </div>
  </div>

  <div id="remindersModal" class="modal-backdrop" onclick="backdropClose(event)">
    <div class="modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <header>
        <h3>🔔 Przypomnienia</h3>
        <button onclick="closeReminders()">✕</button>
      </header>
      <div class="row">
        <div>
          <label>Rodzaj</label>
          <select id="r_type" onchange="document.getElementById('r_type_custom_wrap').style.display=(this.value==='Inne'?'block':'none')">
            <option value="Przegląd techniczny">Przegląd techniczny</option>
            <option value="Naprawa u mechanika">Naprawa u mechanika</option>
            <option value="Ubezpieczenie OC/AC">Ubezpieczenie OC/AC</option>
            <option value="Wymiana oleju">Wymiana oleju</option>
            <option value="Inne">Inne</option>
          </select>
          <div id="r_type_custom_wrap" style="display:none; margin-top:8px;">
            <label>Własny powód</label><input id="r_type_custom" placeholder="np. wymiana opon">
          </div>
        </div>
        <div><label>Termin (data)</label><input id="r_date" type="date"></div>
      </div>
      <div class="row">
        <div><label>Termin (przebieg)</label><input id="r_mileage" type="number" placeholder="np. 120000"></div>
        <div><label>Pojazd (opcjonalnie)</label><select id="r_vehicle"></select></div>
      </div>
      <div class="row">
        <div><label><input type="checkbox" id="r_notify_mail" style="width:auto;display:inline-block;margin-right:8px;"> Wyślij e-mail</label></div>
        <div><label>Ile dni wcześniej</label><input id="r_notify_days" type="number" placeholder="np. 7"></div>
      </div>
      <div style="margin-top:8px;"><button type="button" class="primary" onclick="addReminder()">Dodaj przypomnienie</button></div>

      <div style="margin-top:12px; overflow:auto;">
        <table>
          <thead><tr><th></th><th>Rodzaj</th><th>Data</th><th>Przebieg</th><th>Mail</th><th>Dni wcześniej</th><th>Pojazd</th><th></th></tr></thead>
          <tbody id="r_tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <div id="schedulesModal" class="modal-backdrop" onclick="backdropClose(event)">
    <div class="modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <header>
        <h3>🛠️ Harmonogram serwisów okresowych</h3>
        <button onclick="closeSchedules()">✕</button>
      </header>
      <div class="row">
        <div><label>Pojazd (opcjonalnie)</label><select id="s_vehicle"></select></div>
        <div><label>Rodzaj (np. Wymiana oleju)</label><input id="s_kind" placeholder="Wymiana oleju"></div>
      </div>
      <div class="row">
        <div><label>Co ile miesięcy</label><input id="s_interval_m" type="number" placeholder="np. 12"></div>
        <div><label>Co ile km</label><input id="s_interval_km" type="number" placeholder="np. 15000"></div>
      </div>
      <div class="row">
        <div><label>Ostatni serwis — data</label><input id="s_last_date" type="date"></div>
        <div><label>Ostatni serwis — przebieg</label><input id="s_last_mil" type="number"></div>
      </div>
      <div style="margin-top:8px;"><button type="button" class="primary" onclick="addSchedule()">Dodaj harmonogram</button></div>
      <div style="margin-top:12px; overflow:auto;">
        <table>
          <thead><tr><th>Rodzaj</th><th>Interwał</th><th>Następny termin</th><th>Pojazd</th><th></th></tr></thead>
          <tbody id="s_tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="toast" id="toast">✓ Zapisano</div>

  <script>
    const $ = (id) => document.getElementById(id);
    window.loggedIn = false;
    window._entriesCache = [];
    window._dailyVehicleCosts = [];
    let currentUserName = '';

    // ====== Kolory pojazdów ======
    const VEHICLE_COLOR_PALETTE = [
      '#ef4444', '#f97316', '#eab308', '#22c55e', '#14b8a6',
      '#0ea5e9', '#6366f1', '#a855f7', '#ec4899', '#f97373',
      '#fb923c', '#84cc16', '#22c55e', '#2dd4bf', '#38bdf8',
      '#818cf8', '#a855f7', '#d946ef', '#f97373', '#f59e0b'
    ];
    const VEHICLE_COLORS = {};

    function getVehicleColor(vid) {
      if (vid === null || vid === undefined) return '#9ca3af';
      const key = String(vid);
      if (!VEHICLE_COLORS[key]) {
        const used = Object.keys(VEHICLE_COLORS).length;
        const idx = used % VEHICLE_COLOR_PALETTE.length;
        VEHICLE_COLORS[key] = VEHICLE_COLOR_PALETTE[idx];
      }
      return VEHICLE_COLORS[key];
    }

    // ====== Daty / godziny po polsku ======
    function onlyDate(value) {
      if (!value) return '';
      const s = value.toString();
      if (s.length >= 10) return s.slice(0, 10);
      return s;
    }
    function formatDatePl(value) {
      const dStr = onlyDate(value);
      if (!dStr) return '';
      const parts = dStr.split('-');
      if (parts.length === 3) {
        return parts[2] + '.' + parts[1] + '.' + parts[0];
      }
      return dStr;
    }
    function formatTimeHm(value) {
      if (!value) return '';
      const s = String(value);
      return s.slice(0,5);
    }
    function pad2(n){ return n < 10 ? '0' + n : String(n); }

    // ====== Modale ======
    function openAuthModal(){ $('authModal').style.display = 'flex'; }
    function closeAuthModal(){ $('authModal').style.display = 'none'; }
    function openReminders(){ $('remindersModal').style.display = 'flex'; loadReminders(); loadReminderVehicles(); }
    function closeReminders(){ $('remindersModal').style.display = 'none'; }
    function openSchedules(){ $('schedulesModal').style.display = 'flex'; loadSchedules(); }
    function closeSchedules(){ $('schedulesModal').style.display = 'none'; }
    function backdropClose(e){ if(e.target.classList.contains('modal-backdrop')) e.target.style.display='none'; }

    // ====== Marka / model / rok / nr rejestracyjny ======
    const CAR_DATA = {
      "Audi": ["A1","A3","S3","RS3","A4","S4","RS4","A5","S5","RS5","A6","S6","RS6","A7","S7","RS7","A8","Q2","Q3","RSQ3","Q5","SQ5","Q7","SQ7","Q8","SQ8","RSQ8","TT","TTS","TTRS","e-tron","e-tron GT","RS e-tron GT"],
      "BMW": ["1 Series","M135i","2 Series","M240i","3 Series","M3","4 Series","M4","5 Series","M5","7 Series","X1","X3","X3 M","X5","X5 M","X6","X6 M","X7","i3","i4","i5","i7","iX"],
      "Mercedes-Benz": ["A-Class","AMG A35","AMG A45","C-Class","AMG C43","AMG C63","E-Class","AMG E53","AMG E63","S-Class","GLA","GLB","GLC","AMG GLC 43","GLE","GLS","CLA","AMG CLA 45","CLS","EQA","EQB","EQE","EQS"],
      "Volkswagen": ["up!","Polo","Polo GTI","Golf","Golf GTI","Golf R","Passat","Arteon","Tiguan","T-Roc","T-Roc R","Touareg","ID.3","ID.4","ID.5"],
      "Škoda": ["Fabia","Scala","Octavia","Octavia RS","Superb","Kamiq","Karoq","Kodiaq","Enyaq"],
      "SEAT": ["Ibiza","Arona","Leon","Leon Cupra","Ateca","Tarraco"],
      "Cupra": ["Born","Formentor","Ateca","Leon"],
      "Renault": ["Clio","Clio RS","Captur","Megane","Megane RS","Austral","Arkana","Kadjar","Koleos","Twingo","Scenic"],
      "Dacia": ["Sandero","Logan","Duster","Jogger","Spring"],
      "Peugeot": ["208","e-208","208 GTi","308","308 GT","508","508 PSE","2008","e-2008","3008","5008"],
      "Citroën": ["C3","C3 Aircross","C4","C4 X","C5 X","C5 Aircross","Berlingo","ë-C4"],
      "DS": ["DS 3","DS 4","DS 7","DS 9"],
      "Opel": ["Corsa","Corsa-e","Astra","Insignia","Mokka","Crossland","Grandland"],
      "Vauxhall": ["Corsa","Astra","Insignia","Mokka","Crossland","Grandland"],
      "Ford": ["Fiesta","Fiesta ST","Puma","Puma ST","Focus","Focus ST","Focus RS","Mondeo","Kuga","S-Max","Galaxy","Mustang","Mustang Mach-E"],
      "Fiat": ["500","500 Abarth","500X","Panda","Tipo","Doblo"],
      "Abarth": ["595","695"],
      "Alfa Romeo": ["Giulia","Giulia Quadrifoglio","Stelvio","Stelvio Quadrifoglio","Tonale"],
      "Lancia": ["Ypsilon"],
      "Toyota": ["Aygo","Aygo X","Yaris","GR Yaris","Corolla","GR Corolla","Camry","C-HR","RAV4","GR86","Supra","Avensis","Highlander","Proace"],
      "Lexus": ["CT","IS","IS F","ES","GS","RC","RC F","NX","RX","UX","LC","LC 500"],
      "Nissan": ["Micra","Leaf","Juke","Juke Nismo","Qashqai","X-Trail","370Z","GT-R"],
      "Mazda": ["2","3","6","CX-3","CX-30","CX-5","MX-5"],
      "Honda": ["Jazz","Civic","Civic Type R","Accord","HR-V","CR-V","e"],
      "Subaru": ["Impreza","WRX STI","XV","Forester","Outback","BRZ"],
      "Suzuki": ["Swift","Swift Sport","Ignis","Baleno","Vitara","S-Cross","Jimny"],
      "Hyundai": ["i10","i20","i20 N","i30","i30 N","Elantra N","Tucson","Kona","Kona N","Santa Fe","Ioniq 5","Ioniq 6"],
      "Kia": ["Picanto","Picanto GT-Line","Rio","Ceed","Proceed","Stonic","Sportage","Sorento","Niro","EV6","EV6 GT"],
      "Volvo": ["S60","S60 Polestar","V60","V60 Polestar","S90","V90","XC40","XC60","XC90","EX30","EX90"],
      "Jaguar": ["XE","XF","XJ","E-Pace","F-Pace","I-Pace","F-Type"],
      "Land Rover": ["Defender","Discovery Sport","Discovery","Range Rover Evoque","Range Rover Velar","Range Rover Sport","Range Rover"],
      "MINI": ["3 Door","5 Door","Clubman","Countryman","Convertible","Cooper S","JCW","Electric"],
      "Porsche": ["718 Cayman","718 Boxster","718 GT4","911 Carrera","911 Turbo","911 GT3","Taycan","Panamera","Macan","Cayenne"],
      "Tesla": ["Model 3","Model Y","Model S","Model X"],
      "Smart": ["fortwo","forfour","#1"],
      "Mitsubishi": ["Space Star","ASX","Eclipse Cross","Outlander","L200"],
      "Jeep": ["Renegade","Compass","Cherokee","Grand Cherokee","Wrangler"]
    };
    const OTHER_MAKE = "Inna marka…";
    const OTHER_MODEL = "Inny model…";

    function populateMakes() {
      const makeSel = $('makeSelect');
      if (!makeSel) return;
      makeSel.innerHTML = '';
      const def = document.createElement('option'); def.value = ''; def.textContent = '— wybierz markę —'; makeSel.appendChild(def);
      Object.keys(CAR_DATA).sort().forEach(m => { const o = document.createElement('option'); o.value = m; o.textContent = m; makeSel.appendChild(o); });
      const other = document.createElement('option'); other.value = OTHER_MAKE; other.textContent = OTHER_MAKE; makeSel.appendChild(other);
      onMakeChange();
    }
    function onMakeChange() {
      const makeSel = $('makeSelect'), modelSel = $('modelSelect');
      const makeCustomWrap = $('makeCustomWrap'), modelCustomWrap = $('modelCustomWrap');
      const makeVal = makeSel.value;
      makeCustomWrap.style.display = (makeVal === OTHER_MAKE) ? 'block' : 'none';
      modelSel.innerHTML = '';
      const def = document.createElement('option'); def.value=''; def.textContent='— wybierz model —'; modelSel.appendChild(def);
      let models = [];
      if (makeVal && makeVal !== OTHER_MAKE) models = CAR_DATA[makeVal] || [];
      models.forEach(md => { const o = document.createElement('option'); o.value = md; o.textContent = md; modelSel.appendChild(o); });
      const other = document.createElement('option'); other.value = OTHER_MODEL; other.textContent = OTHER_MODEL; modelSel.appendChild(other);
      modelCustomWrap.style.display = 'none';
    }
    document.addEventListener('change', (ev) => {
      if (ev.target && ev.target.id === 'modelSelect') {
        $('modelCustomWrap').style.display = (ev.target.value === OTHER_MODEL) ? 'block' : 'none';
      }
    });
    function getSelectedMakeModel() {
      const makeSel = $('makeSelect'), modelSel = $('modelSelect');
      const makeCustom = $('makeCustom'), modelCustom = $('modelCustom');
      let make = '', model = '';
      make = (makeSel.value === OTHER_MAKE) ? (makeCustom.value||'').trim() : (makeSel.value||'');
      model = (modelSel.value === OTHER_MODEL) ? (modelCustom.value||'').trim() : (modelSel.value||'');
      return { make, model };
    }
    function populateYears() {
      const y = $('year'); if(!y) return;
      const now = new Date().getFullYear();
      y.innerHTML = '<option value="">— wybierz rok —</option>';
      for(let yy=now; yy>=1980; yy--) {
        const o = document.createElement('option'); o.value=yy; o.textContent=yy; y.appendChild(o);
      }
    }
    function enforcePlate(el){ el.value = (el.value || '').toUpperCase().replace(/[^A-Z0-9]/g,''); }

    // ====== Toast ======
    function toast(msg){ const t = $('toast'); t.textContent = msg || '✓ Zapisano'; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 1600); }

    // ====== Konto ======
    async function register(){
      const email = $('regEmail').value || '', name = $('regName').value || '', pass = $('regPass').value || '';
      if(!email || !name || !pass) return alert('Uzupełnij e-mail, imię i hasło.');
      await api('/api/register', { method:'POST', body: JSON.stringify({ email, name, password: pass }), headers:{'Content-Type':'application/json'} });
      toast('Konto utworzone. Zaloguj się.');
    }
    async function login(){
      try {
        const res = await api('/api/login', { method:'POST', body: JSON.stringify({ email: $('regEmail').value, password: $('regPass').value }), headers:{'Content-Type':'application/json'} });
        currentUserName = res.user.name || '';
        $('helloUser').textContent = currentUserName ? ('Cześć, ' + currentUserName) : 'Zalogowano';
        $('authBtn').style.display='none'; $('logoutBtn').style.display='inline-block'; closeAuthModal();
        window.loggedIn = true;
        await loadVehicles(); await loadReminderVehicles(); await refreshEntries(); await loadStats(); await loadReminders(); await loadSchedules();
        await loadFuelLogs(); await loadTrips();
        populateYears();
      } catch(e) { alert('Błędne dane logowania.'); }
    }
    async function logout(){ try{ await api('/api/logout',{method:'POST'}) }catch(e){} window.loggedIn=false; location.reload(); }

    // ====== Pojazdy ======
    async function loadVehicles(){
      const list = await api('/api/vehicles');
      const sel = $('vehicleSelect'),
            rsel = $('r_vehicle'),
            ssel = $('s_vehicle'),
            fsel = $('fuel_vehicle'),
            tsel = $('trip_vehicle');
      sel.innerHTML='';
      if(rsel) rsel.innerHTML='<option value="">—</option>';
      if(ssel) ssel.innerHTML='<option value="">—</option>';
      if(fsel) fsel.innerHTML='<option value="">— wybierz —</option>';
      if(tsel) tsel.innerHTML='<option value="">— wybierz —</option>';

      list.forEach(v => {
        const label = (v.make + ' ' + v.model + ' ' + (v.year||'') + (v.fuel?(' • '+v.fuel):'') + ' ' + (v.reg_plate||'')).trim();
        const o = document.createElement('option'); o.value = v.id; o.textContent = label; sel.appendChild(o);
        if(rsel){ const o2 = document.createElement('option'); o2.value = v.id; o2.textContent = label; rsel.appendChild(o2); }
        if(ssel){ const o3 = document.createElement('option'); o3.value = v.id; o3.textContent = label; ssel.appendChild(o3); }
        if(fsel){ const o4 = document.createElement('option'); o4.value = v.id; o4.textContent = label; fsel.appendChild(o4); }
        if(tsel){ const o5 = document.createElement('option'); o5.value = v.id; o5.textContent = label; tsel.appendChild(o5); }
      });
      if(list.length) {
        sel.value = String(list[0].id);
        if(fsel && !fsel.value) fsel.value = String(list[0].id);
        if(tsel && !tsel.value) tsel.value = String(list[0].id);
      }
    }
    async function addVehicle(){
      const { make, model } = getSelectedMakeModel();
      if (!make || !model) return alert('Wybierz markę i model (lub wpisz własne).');
      const body = {
        make, model,
        year: parseInt($('year').value||0)||null,
        fuel: $('fuel').value || null,
        reg_plate: $('reg_plate').value
      };
      await api('/api/vehicles', { method:'POST', body: JSON.stringify(body), headers:{'Content-Type':'application/json'} });
      toast('Dodano pojazd'); await loadVehicles(); await loadStats(); await loadReminders(); await loadSchedules(); await loadFuelLogs(); await loadTrips();
    }
    async function deleteSelectedVehicle(){
      const sel = $('vehicleSelect'); if(!sel.value) return alert('Wybierz pojazd');
      if(!confirm('Usunąć wybrany pojazd wraz z wpisami?')) return;
      await api('/api/vehicles/' + sel.value, {method:'DELETE'});
      toast('Usunięto pojazd'); await loadVehicles(); await loadStats(); await loadReminders(); await refreshEntries(); await loadSchedules(); await loadFuelLogs(); await loadTrips();
    }

    // ====== Wpisy ======
    let editEntryId = null;
    async function addEntry(){
      const sel = $('vehicleSelect'); if(!sel.value) return alert('Najpierw dodaj pojazd.');
      const fd = new FormData();
      fd.append('vehicle_id', sel.value);
      fd.append('date', $('date').value);
      fd.append('mileage', $('mileage').value);
      fd.append('service_type', $('service_type').value);
      fd.append('description', $('description').value);
      fd.append('cost', $('cost').value);
      const f = $('file').files[0]; if (f) fd.append('file', f);
      if(editEntryId){
        const body = { date:$('date').value, mileage:$('mileage').value, service_type:$('service_type').value, description:$('description').value, cost:$('cost').value };
        await api('/api/entries/' + editEntryId, { method:'PUT', body: JSON.stringify(body), headers:{'Content-Type':'application/json'} });
        editEntryId = null;
        document.querySelector('button.primary').textContent = 'Dodaj wpis';
      } else {
        await api('/api/entries', { method:'POST', body: fd });
        $('file').value = '';
      }
      toast('Zapisano'); await refreshEntries();
    }
    function editEntry(id){
      const e = (window._entriesCache||[]).find(x => String(x.id) === String(id)); if(!e) return;
      editEntryId = id;
      $('date').value = onlyDate(e.date) || '';
      $('mileage').value = e.mileage || '';
      $('service_type').value = e.service_type || ''; $('description').value = e.description || ''; $('cost').value = e.cost || '';
      document.querySelector('button.primary').textContent = 'Zapisz zmiany';
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
    async function delEntry(id){ if(!confirm('Usunąć wpis?')) return; await api('/api/entries/' + id, {method:'DELETE'}); toast('Usunięto'); refreshEntries(); }
    async function refreshEntries(){
      const sel = $('vehicleSelect'); const currentVehicleId = sel.value || null;
      const q = $('search').value || ''; const params = new URLSearchParams();
      if(currentVehicleId) params.set('vehicle_id', currentVehicleId); if(q) params.set('q', q);
      let list = []; try{ list = await api('/api/entries?' + params.toString()); }catch(e){ return; }
      window._entriesCache = list; const tb = $('entriesTbody'); tb.innerHTML = '';
      list.forEach(e => {
        const tr = document.createElement('tr');
        const dateStr = formatDatePl(e.date);
        const mileageStr = (e.mileage != null && e.mileage.toLocaleString)
          ? e.mileage.toLocaleString("pl-PL")
          : (e.mileage || '');
        
        tr.innerHTML =
          '<td>'+dateStr+'</td>' +
          '<td>' + mileageStr + '</td>' +
          '<td>' + e.service_type + '</td>' +
          '<td>' + (e.description || "") + '</td>' +
          '<td>' + Number(e.cost||0).toLocaleString("pl-PL",{minimumFractionDigits:2, maximumFractionDigits:2}) + '</td>' +
          '<td>' + (e.attachment ? ('<a target=_blank href="/uploads/' + e.attachment + '">plik</a>') : '') + '</td>' +
          '<td class="actions"><button type="button" onclick="editEntry('+e.id+')">Edytuj</button> <button type="button" onclick="delEntry('+e.id+')">Usuń</button></td>';

        tb.appendChild(tr);
      });
      await loadStats();
    }

    // ====== Tankowania (frontend) ======
    async function loadFuelLogs(){
      const sel = $('fuel_vehicle');
      if(!sel) return;
      const vid = sel.value || '';
      const params = new URLSearchParams();
      if(vid) params.set('vehicle_id', vid);
      let list = [];
      try { list = await api('/api/fuel_logs?' + params.toString()); } catch(e){ return; }
      const tb = $('fuelTbody'); if(!tb) return;
      tb.innerHTML = '';
      list.forEach(f => {
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td>'+ formatDatePl(f.date) +'</td>' +
          '<td>'+ (f.station || '') +'</td>' +
          '<td>'+ (f.liters != null ? Number(f.liters).toLocaleString('pl-PL',{minimumFractionDigits:2, maximumFractionDigits:2}) : '') +'</td>' +
          '<td>'+ (f.price_per_liter != null ? Number(f.price_per_liter).toLocaleString('pl-PL',{minimumFractionDigits:2, maximumFractionDigits:2}) : '') +'</td>' +
          '<td>'+ (f.total_cost != null ? Number(f.total_cost).toLocaleString('pl-PL',{minimumFractionDigits:2, maximumFractionDigits:2}) : '') +'</td>' +
          '<td>'+ (f.odometer != null ? Number(f.odometer).toLocaleString('pl-PL') : '') +'</td>' +
          '<td>'+ (f.full_tank ? 'tak' : 'nie') +'</td>' +
          '<td class="actions"><button type="button" onclick="deleteFuelLog('+f.id+')">Usuń</button></td>';
        tb.appendChild(tr);
      });
    }
    async function addFuelLog(){
      const vid = $('fuel_vehicle').value;
      if(!vid) return alert('Wybierz pojazd dla tankowania.');
      const body = {
        vehicle_id: vid,
        date: $('fuel_date').value || null,
        station: $('fuel_station').value || null,
        liters: $('fuel_liters').value || null,
        price_per_liter: $('fuel_price').value || null,
        odometer: $('fuel_odometer').value || null,
        full_tank: $('fuel_full').checked
      };
      await api('/api/fuel_logs', { method:'POST', body: JSON.stringify(body), headers:{'Content-Type':'application/json'} });
      toast('Zapisano tankowanie');
      $('fuel_station').value=''; $('fuel_liters').value=''; $('fuel_price').value=''; $('fuel_odometer').value=''; $('fuel_full').checked=true;
      await loadFuelLogs(); await loadStats();
    }
    async function deleteFuelLog(id){
      if(!confirm('Usunąć wpis tankowania?')) return;
      await api('/api/fuel_logs/' + id, { method:'DELETE' });
      toast('Usunięto tankowanie');
      await loadFuelLogs(); await loadStats();
    }

    // ====== Trasy (frontend) ======
    async function loadTrips(){
      const sel = $('trip_vehicle');
      if(!sel) return;
      const vid = sel.value || '';
      const params = new URLSearchParams();
      if(vid) params.set('vehicle_id', vid);
      let list = [];
      try { list = await api('/api/trips?' + params.toString()); } catch(e){ return; }
      const tb = $('tripTbody'); if(!tb) return;
      tb.innerHTML = '';
      list.forEach(t => {
        const dateStr = formatDatePl(t.start_date) + (t.end_date ? ' - ' + formatDatePl(t.end_date) : '');
        const route = (t.start_location || '') + (t.end_location ? ' → ' + t.end_location : '');
        const dist = (t.distance_km != null ? Number(t.distance_km).toLocaleString('pl-PL',{maximumFractionDigits:1}) : '');
        const timeRange = (formatTimeHm(t.start_time) || '') + (t.end_time ? ('–' + formatTimeHm(t.end_time)) : '');
        const avg = (t.avg_consumption != null ? Number(t.avg_consumption).toLocaleString('pl-PL',{minimumFractionDigits:1, maximumFractionDigits:1}) : '');
        const status = t.planned ? 'planowana' : 'zrealizowana';
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td>'+dateStr+'</td>' +
          '<td>'+route+'</td>' +
          '<td>'+dist+'</td>' +
          '<td>'+timeRange+'</td>' +
          '<td>'+avg+'</td>' +
          '<td>'+status+'</td>' +
          '<td class="actions"><button type="button" onclick="deleteTrip('+t.id+')">Usuń</button></td>';
        tb.appendChild(tr);
      });
    }
    async function addTrip(){
      const vid = $('trip_vehicle').value;
      if(!vid) return alert('Wybierz pojazd dla trasy.');
      const body = {
        vehicle_id: vid,
        start_date: $('trip_start_date').value || null,
        end_date: $('trip_end_date').value || null,
        start_time: $('trip_start_time').value || null,
        end_time: $('trip_end_time').value || null,
        start_location: $('trip_start_loc').value || null,
        end_location: $('trip_end_loc').value || null,
        distance_km: $('trip_distance').value || null,
        avg_consumption: $('trip_avg_consumption').value || null,
        planned: $('trip_planned').checked,
        notes: $('trip_notes').value || null
      };
      await api('/api/trips', { method:'POST', body: JSON.stringify(body), headers:{'Content-Type':'application/json'} });
      toast('Zapisano trasę');
      $('trip_start_date').value=''; $('trip_end_date').value='';
      $('trip_start_time').value=''; $('trip_end_time').value='';
      $('trip_start_loc').value=''; $('trip_end_loc').value='';
      $('trip_distance').value=''; $('trip_avg_consumption').value=''; $('trip_planned').checked=false; $('trip_notes').value='';
      await loadTrips();
    }
    async function deleteTrip(id){
      if(!confirm('Usunąć trasę?')) return;
      await api('/api/trips/' + id, { method:'DELETE' });
      toast('Usunięto trasę');
      await loadTrips();
    }

    // ====== Statystyki ======
    const BarValueLabels = {
      id: 'barValueLabels',
      afterDatasetsDraw(chart) {
        const {ctx} = chart;
        ctx.save();
        const ds = chart.getDatasetMeta(0);
        ds.data.forEach((bar, i) => {
          const val = chart.data.datasets[0].data[i];
          if (val == null) return;
          const label = Number(val).toLocaleString('pl-PL', {minimumFractionDigits:2, maximumFractionDigits:2});
          ctx.font = '12px system-ui, -apple-system, Segoe UI, Roboto, Arial';
          ctx.fillStyle = '#f3f4f6';
          ctx.textAlign = 'center';
          ctx.fillText(label, bar.x, bar.y - 6);
        });
        ctx.restore();
      }
    };

    async function loadStats(){
      try{
        const s = await api('/api/stats');
        window._dailyVehicleCosts = s.daily_vehicle_costs || [];

        const range = parseInt(($('dash_range')?.value || '0'), 10);
        let cutoff = null;
        if (range > 0) {
          cutoff = new Date();
          cutoff.setHours(0,0,0,0);
          cutoff.setDate(cutoff.getDate() - range + 1);
        }

        const sumsByVehicle = {};
        const labelsByVehicle = {};
        (window._dailyVehicleCosts || []).forEach(row => {
          const d = new Date(row.ymd + 'T00:00:00');
          if (cutoff) {
            if (isNaN(d)) return;
            d.setHours(0,0,0,0);
            if (d < cutoff) return;
          }
          const vid = row.vehicle_id;
          const label = row.label || ('Pojazd #' + vid);
          labelsByVehicle[vid] = label;
          sumsByVehicle[vid] = (sumsByVehicle[vid] || 0) + Number(row.total_cost || 0);
        });

        const vehicleIds = Object.keys(labelsByVehicle).sort((a,b) => {
          return (labelsByVehicle[a]||'').localeCompare(labelsByVehicle[b]||'', 'pl');
        });

        // ====== Wykres: koszt wg pojazdu (kolory per pojazd) ======
        const ctx = $('chartCost')?.getContext('2d');
        if (ctx) {
          const labels = vehicleIds.map(vid => labelsByVehicle[vid] || ('Pojazd #' + vid));
          const dataVals = vehicleIds.map(vid => sumsByVehicle[vid] || 0);
          const colors = vehicleIds.map(vid => getVehicleColor(vid));

          if(window._chartCost) window._chartCost.destroy();
          window._chartCost = new Chart(ctx, {
            type:'bar',
            data:{
              labels,
              datasets:[{
                label:'Koszt (PLN) / pojazd',
                data:dataVals,
                backgroundColor:colors,
                borderColor:colors,
                borderWidth:1
              }]
            },
            options:{
              responsive:true,
              interaction:{ mode:'index', intersect:false },
              scales:{
                x:{ grid:{color:'#222'}, ticks:{color:'#f3f4f6', maxRotation:0, autoSkip:true} },
                y:{ grid:{color:'#222'}, ticks:{color:'#f3f4f6'} }
              },
              plugins:{
                legend:{ labels:{ color:'#f3f4f6' } },
                tooltip:{ callbacks:{ label:(c)=> ' ' + Number(c.raw||0).toLocaleString('pl-PL',{minimumFractionDigits:2, maximumFractionDigits:2}) + ' PLN' } }
              }
            },
            plugins:[BarValueLabels]
          });
        }

        // ====== Tabela suma kosztów per pojazd (z kolorami) ======
        const tBody = $('sumByVehicleTbody'); tBody.innerHTML = '';
        let grand = 0;
        vehicleIds.forEach(vid => {
          const sum = sumsByVehicle[vid] || 0;
          grand += sum;
          const color = getVehicleColor(vid);
          const tr = document.createElement('tr');
          tr.style.borderLeft = '4px solid ' + color;
          tr.innerHTML = '<td>'+ (labelsByVehicle[vid]||('Pojazd #'+vid)) +'</td><td>'+
            sum.toLocaleString('pl-PL',{minimumFractionDigits:2, maximumFractionDigits:2}) +'</td>';
          tBody.appendChild(tr);
        });
        $('sumAll').textContent = grand.toLocaleString('pl-PL',{minimumFractionDigits:2, maximumFractionDigits:2});

        // ====== Tabela ostatnich przebiegów (również z kolorami) ======
        const tb = $('mileageTbody'); if(tb){ tb.innerHTML='';
          (s.last_mileage||[]).forEach(r => {
            const color = getVehicleColor(r.vehicle_id);
            const tr = document.createElement('tr');
            tr.style.borderLeft = '4px solid ' + color;
            tr.innerHTML = '<td>'+(r.label||'-')+'</td><td>'+Number(r.mileage||0).toLocaleString('pl-PL')+'</td>';
            tb.appendChild(tr);
          });
        }

        // ====== Średnie spalanie (fuel_stats) ======
        const fsBody = $('fuelSummaryTbody');
        if(fsBody){
          fsBody.innerHTML = '';
          (s.fuel_stats || []).forEach(row => {
            const color = getVehicleColor(row.vehicle_id);
            const tr = document.createElement('tr');
            tr.style.borderLeft = '4px solid ' + color;
            const avg = row.avg_l_100km != null
              ? Number(row.avg_l_100km).toLocaleString('pl-PL',{minimumFractionDigits:1, maximumFractionDigits:1})
              : '-';
            const dist = row.distance_km != null
              ? Number(row.distance_km).toLocaleString('pl-PL',{maximumFractionDigits:0})
              : '-';
            const liters = Number(row.total_liters || 0).toLocaleString('pl-PL',{minimumFractionDigits:1, maximumFractionDigits:1});
            tr.innerHTML =
              '<td>'+ (row.label || '-') +'</td>' +
              '<td>'+ liters +'</td>' +
              '<td>'+ dist +'</td>' +
              '<td>'+ avg +'</td>';
            fsBody.appendChild(tr);
          });
        }

        // ====== Kalendarz kosztów ======
        const monthInput = $('cal_month');
        if (monthInput && !monthInput.value) {
          const now = new Date();
          monthInput.value = now.toISOString().slice(0,7);
        }
        renderCostCalendar();

      }catch(e){ console.error(e); }
    }

    function renderCostCalendar(){
      const grid = $('calendarGrid');
      if(!grid) return;
      grid.innerHTML = '';
      const data = window._dailyVehicleCosts || [];
      if (!data.length) return;

      const monthInput = $('cal_month');
      let year, month;
      if (monthInput && monthInput.value) {
        const parts = monthInput.value.split('-');
        year = parseInt(parts[0],10);
        month = parseInt(parts[1],10);
      } else {
        const now = new Date();
        year = now.getFullYear();
        month = now.getMonth() + 1;
      }

      const daysInMonth = new Date(year, month, 0).getDate();
      const firstDay = new Date(year, month-1, 1);
      // JS: 0=niedziela, my chcemy 0=poniedziałek
      let offset = (firstDay.getDay() + 6) % 7;

      const byDate = {};
      data.forEach(row => {
        const ymd = row.ymd;
        if(!ymd) return;
        const d = new Date(ymd + 'T00:00:00');
        if (isNaN(d)) return;
        if (d.getFullYear() !== year || (d.getMonth()+1) !== month) return;
        if(!byDate[ymd]) byDate[ymd] = [];
        byDate[ymd].push(row);
      });

      for(let i=0;i<offset;i++){
        const empty = document.createElement('div');
        empty.className = 'calendar-cell';
        grid.appendChild(empty);
      }

      for(let day=1; day<=daysInMonth; day++){
        const ymd = year + '-' + pad2(month) + '-' + pad2(day);
        const rows = byDate[ymd] || [];
        const totalForDay = rows.reduce((s,r) => s + Number(r.total_cost || 0), 0);
        const cell = document.createElement('div');
        cell.className = 'calendar-cell';

        const dayNum = document.createElement('div');
        dayNum.className = 'day-num';
        dayNum.textContent = day;
        cell.appendChild(dayNum);

        if (totalForDay > 0) {
          const totalDiv = document.createElement('div');
          totalDiv.className = 'cost-total';
          totalDiv.textContent = 'Razem: ' + totalForDay.toLocaleString('pl-PL',{minimumFractionDigits:2, maximumFractionDigits:2}) + ' zł';
          cell.appendChild(totalDiv);

          rows.forEach(r => {
            const entry = document.createElement('div');
            entry.className = 'entry';
            const service = Number(r.service_cost || 0);
            const fuel = Number(r.fuel_cost || 0);
            let details = '';
            if (service > 0) details += 'serwis ' + service.toLocaleString('pl-PL',{maximumFractionDigits:2}) + ' zł';
            if (fuel > 0) {
              if (details) details += ', ';
              details += 'paliwo ' + fuel.toLocaleString('pl-PL',{maximumFractionDigits:2}) + ' zł';
            }
            entry.textContent = '• ' + (r.label || '-') + ' — ' + details;
            cell.appendChild(entry);
          });
        }

        grid.appendChild(cell);
      }
    }

    // ====== Przypomnienia ======
    async function loadReminders(){
      try{
        const list = await api('/api/reminders');
        const tb = $('r_tbody'); if(!tb) return; tb.innerHTML = '';
        list.forEach(r => {
          const tr = document.createElement('tr');
          const due = r.is_due ? '🔔' : '';
          tr.innerHTML =
            '<td>' + due + '</td>' +
            '<td>' + r.title + '</td>' +
            '<td>' + (r.due_date ? formatDatePl(r.due_date) : '') + '</td>' +
            '<td>' + (r.due_mileage||'') + '</td>' +
            '<td>' + (r.notify_email ? 'tak' : 'nie') + '</td>' +
            '<td>' + (r.notify_before_days ?? '') + '</td>' +
            '<td>' + (r.vehicle_id || '') + '</td>' +
            '<td class="actions"><button type="button" onclick="completeReminder(' + r.id + ')">Zakończ</button> <button type="button" onclick="deleteReminder(' + r.id + ')">Usuń</button></td>';
          tb.appendChild(tr);
        });
      }catch(e){}
    }
    async function loadReminderVehicles(){
      try{
        const list = await api('/api/vehicles'); const rsel = $('r_vehicle'); if(!rsel) return;
        rsel.innerHTML = '<option value="">—</option>';
        list.forEach(v => { const o = document.createElement('option'); o.value = v.id; o.textContent = (v.make+' '+v.model+' '+(v.year||'')+' '+(v.reg_plate||'')).trim(); rsel.appendChild(o); });
      }catch(e){}
    }
    async function addReminder(){
      const selType = $('r_type'), custom = $('r_type_custom');
      const typeVal = selType && selType.value === 'Inne' ? (custom.value||'').trim() : (selType ? selType.value : '');
      if(!typeVal) return alert('Wybierz rodzaj lub wpisz własny powód.');
      const body = {
        title: typeVal,
        due_date: $('r_date').value || null,
        due_mileage: $('r_mileage').value || null,
        vehicle_id: $('r_vehicle').value || null,
        notify_email: $('r_notify_mail').checked,
        notify_before_days: parseInt($('r_notify_days').value || '') || 7
      };
      await api('/api/reminders', { method:'POST', body: JSON.stringify(body), headers:{'Content-Type':'application/json'} });
      toast('Dodano przypomnienie'); selType.value='Przegląd techniczny'; if(custom) custom.value='';
      $('r_date').value=''; $('r_mileage').value=''; $('r_type_custom_wrap').style.display='none'; $('r_notify_mail').checked=false; $('r_notify_days').value='';
      await loadReminders();
    }
    async function completeReminder(id){ await api('/api/reminders/' + id, { method:'PUT', body: JSON.stringify({ completed_at: new Date().toISOString() }), headers:{'Content-Type':'application/json'} }); await loadReminders(); }
    async function deleteReminder(id){ await api('/api/reminders/' + id, { method:'DELETE' }); await loadReminders(); }

    // ====== Harmonogramy ======
    async function loadSchedules(){
      const tb = $('s_tbody'); if(!tb) return; tb.innerHTML='';
      const list = await api('/api/schedules');
      list.forEach(s => {
        const inter = [(s.interval_months? (s.interval_months+' mies.'):'') , (s.interval_km? (s.interval_km+' km'):'')].filter(Boolean).join(' / ') || '-';
        const next = (s.next_due_date ? formatDatePl(s.next_due_date) : (s.next_due_mileage || '-'));
        const tr = document.createElement('tr');
        tr.innerHTML = '<td>'+s.kind+'</td><td>'+inter+'</td><td>'+next+'</td><td>'+(s.vehicle_id||'')+'</td>' +
                       '<td class="actions"><button type="button" onclick="deleteSchedule('+s.id+')">Usuń</button></td>';
        tb.appendChild(tr);
      });
    }
    async function addSchedule(){
      const body = {
        vehicle_id: $('s_vehicle').value || null,
        kind: $('s_kind').value || 'Serwis okresowy',
        interval_months: parseInt($('s_interval_m').value || '') || null,
        interval_km: parseInt($('s_interval_km').value || '') || null,
        last_service_date: $('s_last_date').value || null,
        last_service_mileage: parseInt($('s_last_mil').value || '') || null
      };
      await api('/api/schedules', { method:'POST', body: JSON.stringify(body), headers:{'Content-Type':'application/json'} });
      toast('Dodano harmonogram'); $('s_kind').value=''; $('s_interval_m').value=''; $('s_interval_km').value=''; $('s_last_date').value=''; $('s_last_mil').value='';
      await loadSchedules();
    }
    async function deleteSchedule(id){ await api('/api/schedules/' + id, { method:'DELETE' }); await loadSchedules(); }

    // ====== Init ======
    document.addEventListener('DOMContentLoaded', () => { populateMakes(); populateYears(); });

    Object.assign(window, {
      openAuthModal, closeAuthModal, openReminders, closeReminders, openSchedules, closeSchedules, backdropClose,
      register, login, logout,
      loadVehicles, addVehicle, deleteSelectedVehicle,
      addEntry, refreshEntries, delEntry, editEntry,
      loadStats, loadReminders, loadReminderVehicles,
      addReminder, completeReminder, deleteReminder,
      loadSchedules, addSchedule, deleteSchedule,
      onMakeChange, enforcePlate,
      loadFuelLogs, addFuelLog, deleteFuelLog,
      loadTrips, addTrip, deleteTrip,
      renderCostCalendar
    });
  </script>
</body>
</html>
"""


@app.get("/")
def index_page():
    return INDEX_HTML.replace("{APP_TITLE}", APP_TITLE)


if __name__ == "__main__":
    print(f"\n{APP_TITLE} — start na http://127.0.0.1:5000\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
