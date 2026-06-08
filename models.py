"""
Happy Billing — data layer
==========================
SQLite database with five tables. Plain sqlite3 (no ORM) so it's easy to read
and runs anywhere with zero extra installs.

Roles:
  - admin  : you / your staff. Sees and edits ALL clinics.
  - doctor : a clinic login. Sees ONLY its own patients + data.

Tables:
  users        - login accounts (admin or doctor)
  patients     - belong to a doctor (clinic)
  eligibility  - eligibility results you look up manually, per patient
  claims       - CMS-1500 claims, per patient
  payments     - deductible / payment tracking entries, per patient
"""

import os
import hashlib
import secrets
import datetime

try:
    import libsql                       # Turso / libSQL client (remote-capable)
except ImportError:                     # older package name on some installs
    import libsql_experimental as libsql

DB_PATH = os.path.join(os.path.dirname(__file__), "billing.db")

# Remote Turso database — set these as environment variables in Vercel.
# If they're unset (e.g. running on your laptop) we fall back to a local file,
# so `python app.py` keeps working exactly like before.
TURSO_URL   = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")


# ---------------------------------------------------------------------------
# sqlite3.Row compatibility shim
# ---------------------------------------------------------------------------
# libsql returns plain tuples, but the rest of this app expects sqlite3.Row-
# style access: row["column"] and dict(row). These thin wrappers restore that
# behaviour so NOTHING else in the codebase has to change.
class _Row:
    __slots__ = ("_vals", "_map")

    def __init__(self, cols, vals):
        self._vals = vals
        self._map = {c: v for c, v in zip(cols, vals)}

    def __getitem__(self, key):
        return self._map[key] if isinstance(key, str) else self._vals[key]

    def keys(self):                      # lets dict(row) work
        return list(self._map.keys())

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = getattr(cur, "lastrowid", None)

    def _cols(self):
        return [d[0] for d in (self._cur.description or [])]

    def fetchone(self):
        r = self._cur.fetchone()
        return None if r is None else _Row(self._cols(), r)

    def fetchall(self):
        cols = self._cols()
        return [_Row(cols, r) for r in self._cur.fetchall()]


class _Conn:
    """Wraps a libsql connection so it behaves like the sqlite3 connection
    the rest of the code was written against."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        return _Cursor(self._raw.execute(sql, params))

    def executescript(self, script):
        self._raw.executescript(script)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def get_db():
    if TURSO_URL:
        raw = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    else:
        raw = libsql.connect(DB_PATH)    # local-file fallback for dev
    # NOTE: we deliberately skip "PRAGMA foreign_keys = ON" here — over a remote
    # database it costs an extra network round-trip on every single request, and
    # nothing in the app relies on cascade deletes.
    return _Conn(raw)


# ---------------------------------------------------------------------------
# password hashing (salted SHA-256 — fine for a prototype; use bcrypt in prod)
# ---------------------------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"


def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hash_password(password, salt) == stored


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        role          TEXT NOT NULL CHECK(role IN ('admin','doctor')),
        username      TEXT UNIQUE NOT NULL,
        password      TEXT NOT NULL,
        name          TEXT NOT NULL,
        clinic        TEXT,
        phone         TEXT,
        email         TEXT,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS patients (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        doctor_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        first_name    TEXT NOT NULL,
        last_name     TEXT NOT NULL,
        dob           TEXT,
        member_id     TEXT,
        payer         TEXT,
        plan          TEXT,
        notes         TEXT,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS eligibility (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id            INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        status                TEXT,         -- Active / Inactive / Pending
        network               TEXT,         -- In-Network / Out-of-Network
        acu_covered           INTEGER,      -- 1 / 0
        visits_per_year       INTEGER,
        visits_used           INTEGER,
        copay                 REAL,
        deductible_total      REAL,
        deductible_met        REAL,
        est_reimbursement     REAL,
        cpt_codes             TEXT,
        result_notes          TEXT,
        checked_by            TEXT,         -- who looked it up
        checked_at            TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS claims (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id    INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        date_of_service TEXT,
        icd10         TEXT,
        cpt           TEXT,
        units         INTEGER,
        charge        REAL,
        paid          REAL DEFAULT 0,
        status        TEXT DEFAULT 'Submitted',  -- Submitted/Accepted/Paid/Denied
        claim_number  TEXT,
        notes         TEXT,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS payments (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id    INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        entry_date    TEXT,
        kind          TEXT,        -- 'payment' or 'deductible'
        amount        REAL,
        description   TEXT,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS requests (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id    INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        doctor_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        type          TEXT NOT NULL DEFAULT 'eligibility', -- what's being requested
        message       TEXT,                                -- optional note from doctor
        status        TEXT NOT NULL DEFAULT 'pending',     -- pending / done
        admin_reply   TEXT,                                -- note back to doctor
        created_at    TEXT NOT NULL,
        resolved_at   TEXT
    );
    """)
    db.commit()
    db.close()


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# seed: one admin + one demo doctor with sample data, so the app isn't empty
# ---------------------------------------------------------------------------
def seed():
    db = get_db()
    if db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] > 0:
        db.close()
        return  # already seeded

    db.execute(
        "INSERT INTO users(role,username,password,name,clinic,phone,email,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("admin", "admin", hash_password("admin123"), "Happy Billing Staff",
         None, "408-930-1585", "acubilling@yahoo.com", now()),
    )
    db.execute(
        "INSERT INTO users(role,username,password,name,clinic,phone,email,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("doctor", "dr.olivia", hash_password("demo1234"), "Dr. Olivia",
         "Bright Path Acupuncture 明径中医诊所", "408-555-0101",
         "olivia@brightpath.com", now()),
    )
    doc_id = db.execute("SELECT id FROM users WHERE username='dr.olivia'").fetchone()["id"]

    samples = [
        ("Lisa", "Chen", "19710412", "ZGP882044170", "Blue Shield of California", "Silver PPO 2000"),
        ("David", "Wong", "19650822", "AET445218", "Aetna", "Aetna HMO"),
        ("Mei", "Lin", "19880305", "ANT99120", "Anthem Blue Cross", "Anthem PPO"),
    ]
    for fn, ln, dob, mid, payer, plan in samples:
        db.execute(
            "INSERT INTO patients(doctor_id,first_name,last_name,dob,member_id,payer,plan,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, fn, ln, dob, mid, payer, plan, now()),
        )

    # one sample eligibility result + claim for Lisa
    lisa = db.execute("SELECT id FROM patients WHERE first_name='Lisa'").fetchone()["id"]
    db.execute(
        "INSERT INTO eligibility(patient_id,status,network,acu_covered,visits_per_year,"
        "visits_used,copay,deductible_total,deductible_met,est_reimbursement,cpt_codes,"
        "result_notes,checked_by,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lisa, "Active", "In-Network", 1, 20, 6, 25, 2000, 1540, 78,
         "97810, 97811", "Confirmed by phone with Blue Shield 5/28.",
         "Happy Billing Staff", now()),
    )
    db.execute(
        "INSERT INTO claims(patient_id,date_of_service,icd10,cpt,units,charge,paid,status,"
        "claim_number,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (lisa, "2026-05-28", "M54.5", "97810", 2, 120, 0, "Submitted", "OA-558201", now()),
    )

    # a sample PENDING request from the doctor (so the admin queue shows something)
    mei = db.execute("SELECT id FROM patients WHERE first_name='Mei'").fetchone()["id"]
    db.execute(
        "INSERT INTO requests(patient_id,doctor_id,type,message,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (mei, doc_id, "eligibility",
         "New patient — please check acupuncture coverage. 新病人，请帮忙查针灸覆盖。",
         "pending", now()),
    )
    db.commit()
    db.close()


if __name__ == "__main__":
    init_db()
    seed()
    print("Database initialized at", DB_PATH)
    print("Logins:  admin / admin123   (staff, sees all)")
    print("         dr.olivia / demo1234   (doctor, sees own only)")
