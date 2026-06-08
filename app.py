"""
Happy Billing — backend application
===================================
A real, runnable server that STORES everything you enter manually and shows it
to the right person:

  - YOU (admin) log in -> see every clinic, enter eligibility / claims / payments
  - A DOCTOR logs in   -> sees only their own patients and the info you entered

Run:
    pip install flask
    python models.py     # creates the database with demo data (first time only)
    python app.py        # starts the server
    open http://localhost:5000

Logins (created by models.py):
    admin     / admin123     (staff — sees & edits all clinics)
    dr.olivia / demo1234     (doctor — sees own clinic only)

NOTE ON PRIVACY (HIPAA): this stores patient info. Mock/demo data is safe
anywhere, but before using real patient data, host on a HIPAA-compliant server.
"""

import os
import functools
from flask import (Flask, request, jsonify, render_template,
                   session, redirect, url_for)
import models
import notify

app = Flask(__name__)
app.secret_key = os.environ.get("HB_SECRET", "dev-secret-change-in-production")

# Make sure the DB exists on startup — but ONLY for local development.
# In production the Turso database is already initialised and seeded, and
# re-running this on every serverless cold start would add several slow
# cross-network round-trips to the first request. So we skip it when a remote
# database is configured. (Wrapped so a misconfig can't take down public pages.)
if not models.TURSO_URL:
    try:
        models.init_db()
        models.seed()
    except Exception as e:
        app.logger.warning("Database init skipped: %s", e)


# ---------------------------------------------------------------------------
# PUBLIC marketing pages (open to everyone, no login)
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("public_home.html")


@app.route("/about")
def about():
    return render_template("public_about.html")


@app.route("/pricing")
def pricing():
    return render_template("public_pricing.html")


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    db = models.get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    db.close()
    return u


def login_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        # Require not just a session id, but a user that still exists.
        if current_user() is None:
            session.clear()
            return redirect(url_for("login_page"))
        return fn(*a, **k)
    return wrap


def api_login_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        if current_user() is None:
            session.clear()
            return jsonify({"error": "not logged in"}), 401
        return fn(*a, **k)
    return wrap


def doctor_can_access(patient_id):
    """A doctor may only touch their own patients; admin may touch anyone's."""
    u = current_user()
    if u["role"] == "admin":
        return True
    db = models.get_db()
    row = db.execute("SELECT doctor_id FROM patients WHERE id=?", (patient_id,)).fetchone()
    db.close()
    return row is not None and row["doctor_id"] == u["id"]


# ---------------------------------------------------------------------------
# pages (portal — behind login)
# ---------------------------------------------------------------------------
@app.route("/portal")
def portal_entry():
    if session.get("uid"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    # If the session points to a user that no longer exists (e.g. an old
    # cookie from a previous database), clear it and send them to log in
    # fresh instead of crashing.
    if u is None:
        session.clear()
        return redirect(url_for("login_page"))
    # Admins and doctors get purpose-built pages.
    if u["role"] == "admin":
        return render_template("dashboard_admin.html", user=u)
    return render_template("dashboard_doctor.html", user=u)


# ---------------------------------------------------------------------------
# auth API
# ---------------------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(force=True)
    db = models.get_db()
    u = db.execute("SELECT * FROM users WHERE username=?",
                   (d.get("username", "").strip().lower(),)).fetchone()
    db.close()
    if not u or not models.verify_password(d.get("password", ""), u["password"]):
        return jsonify({"error": "Invalid username or password"}), 401
    session["uid"] = u["id"]
    return jsonify({"role": u["role"], "name": u["name"]})


@app.route("/api/register", methods=["POST"])
def api_register():
    """Doctor self-registration (creates a doctor account)."""
    d = request.get_json(force=True)
    uname = d.get("username", "").strip().lower()
    if not uname or not d.get("password") or not d.get("name"):
        return jsonify({"error": "Name, username and password are required"}), 400
    if len(d.get("password")) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    db = models.get_db()
    if db.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone():
        db.close()
        return jsonify({"error": "That username is already taken"}), 400
    db.execute(
        "INSERT INTO users(role,username,password,name,clinic,phone,email,created_at) "
        "VALUES('doctor',?,?,?,?,?,?,?)",
        (uname, models.hash_password(d["password"]), d["name"],
         d.get("clinic"), d.get("phone"), d.get("email"), models.now()))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()["id"]
    db.close()
    session["uid"] = uid
    return jsonify({"role": "doctor", "name": d["name"]})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
@api_login_required
def api_me():
    u = current_user()
    return jsonify({"id": u["id"], "role": u["role"], "name": u["name"],
                    "clinic": u["clinic"]})


# ---------------------------------------------------------------------------
# doctors (admin only) — list clinics
# ---------------------------------------------------------------------------
@app.route("/api/doctors")
@api_login_required
def api_doctors():
    u = current_user()
    if u["role"] != "admin":
        return jsonify({"error": "admin only"}), 403
    db = models.get_db()
    rows = db.execute(
        "SELECT id,name,clinic,phone,email,"
        "(SELECT COUNT(*) FROM patients WHERE doctor_id=users.id) AS patient_count "
        "FROM users WHERE role='doctor' ORDER BY name").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# patients
# ---------------------------------------------------------------------------
@app.route("/api/patients")
@api_login_required
def api_patients():
    u = current_user()
    db = models.get_db()
    # admin can filter by ?doctor_id=, or see all; doctor sees only own
    if u["role"] == "admin":
        did = request.args.get("doctor_id")
        if did:
            rows = db.execute("SELECT p.*, d.name AS doctor_name, d.clinic AS doctor_clinic "
                              "FROM patients p JOIN users d ON d.id=p.doctor_id "
                              "WHERE p.doctor_id=? ORDER BY p.last_name", (did,)).fetchall()
        else:
            rows = db.execute("SELECT p.*, d.name AS doctor_name, d.clinic AS doctor_clinic "
                              "FROM patients p JOIN users d ON d.id=p.doctor_id "
                              "ORDER BY p.last_name").fetchall()
    else:
        rows = db.execute("SELECT * FROM patients WHERE doctor_id=? ORDER BY last_name",
                          (u["id"],)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/patients", methods=["POST"])
@api_login_required
def api_add_patient():
    u = current_user()
    d = request.get_json(force=True)
    # admin must say which doctor; doctor adds to self
    doctor_id = d.get("doctor_id") if u["role"] == "admin" else u["id"]
    if not doctor_id:
        return jsonify({"error": "doctor_id required"}), 400
    if not d.get("first_name") or not d.get("last_name"):
        return jsonify({"error": "First and last name required"}), 400
    db = models.get_db()
    cur = db.execute(
        "INSERT INTO patients(doctor_id,first_name,last_name,dob,member_id,payer,plan,notes,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (doctor_id, d["first_name"], d["last_name"], d.get("dob"), d.get("member_id"),
         d.get("payer"), d.get("plan"), d.get("notes"), models.now()))
    pid = cur.lastrowid or db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    # Automatically open an eligibility-check request to staff for the new
    # patient, so the doctor doesn't have to send one manually afterwards.
    db.execute(
        "INSERT INTO requests(patient_id,doctor_id,type,message,status,created_at) "
        "VALUES(?,?,?,?,'pending',?)",
        (pid, doctor_id, "eligibility",
         d.get("notes") or "Auto-created with new patient — please run an eligibility check.",
         models.now()))
    db.commit()
    db.close()

    # Notify staff (email if configured, otherwise console). Never blocks the save.
    notify.send_notification(
        subject=f"New patient + eligibility request — {d['first_name']} {d['last_name']}",
        body=(f"{u['name']} added a new patient and an eligibility check was requested.\n\n"
              f"Patient: {d['first_name']} {d['last_name']}\n"
              f"DOB: {d.get('dob') or '—'}   Member ID: {d.get('member_id') or '—'}\n"
              f"Payer: {d.get('payer') or '—'}   Plan: {d.get('plan') or '—'}\n"
              f"Note: {d.get('notes') or '(none)'}\n\n"
              f"Open the portal to respond."))
    return jsonify({"id": pid, "ok": True, "request_created": True})


@app.route("/api/patients/<int:pid>")
@api_login_required
def api_patient_detail(pid):
    if not doctor_can_access(pid):
        return jsonify({"error": "not allowed"}), 403
    db = models.get_db()
    p = db.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        db.close()
        return jsonify({"error": "not found"}), 404
    elig = db.execute("SELECT * FROM eligibility WHERE patient_id=? ORDER BY checked_at DESC",
                      (pid,)).fetchall()
    claims = db.execute("SELECT * FROM claims WHERE patient_id=? ORDER BY date_of_service DESC",
                        (pid,)).fetchall()
    pays = db.execute("SELECT * FROM payments WHERE patient_id=? ORDER BY entry_date DESC",
                      (pid,)).fetchall()
    db.close()
    return jsonify({
        "patient": dict(p),
        "eligibility": [dict(r) for r in elig],
        "claims": [dict(r) for r in claims],
        "payments": [dict(r) for r in pays],
    })


# ---------------------------------------------------------------------------
# eligibility / claims / payments — entered manually (admin or owning doctor)
# ---------------------------------------------------------------------------
@app.route("/api/patients/<int:pid>/eligibility", methods=["POST"])
@api_login_required
def api_add_eligibility(pid):
    if not doctor_can_access(pid):
        return jsonify({"error": "not allowed"}), 403
    d = request.get_json(force=True)
    u = current_user()
    db = models.get_db()
    db.execute(
        "INSERT INTO eligibility(patient_id,status,network,acu_covered,visits_per_year,"
        "visits_used,copay,deductible_total,deductible_met,est_reimbursement,cpt_codes,"
        "result_notes,checked_by,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, d.get("status"), d.get("network"), 1 if d.get("acu_covered") else 0,
         num(d.get("visits_per_year")), num(d.get("visits_used")), num(d.get("copay")),
         num(d.get("deductible_total")), num(d.get("deductible_met")),
         num(d.get("est_reimbursement")), d.get("cpt_codes"), d.get("result_notes"),
         u["name"], models.now()))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/patients/<int:pid>/claims", methods=["POST"])
@api_login_required
def api_add_claim(pid):
    if not doctor_can_access(pid):
        return jsonify({"error": "not allowed"}), 403
    d = request.get_json(force=True)

    # Accept either one date (date_of_service) or many (dates: [...]).
    # This lets you log a single visit, a few specific dates, or a recurring
    # series (e.g. every Tuesday) — one claim row is created per date.
    dates = d.get("dates")
    if not dates:
        dates = [d.get("date_of_service")]
    dates = [dt for dt in dates if dt]  # drop blanks
    if not dates:
        return jsonify({"error": "At least one date is required"}), 400

    db = models.get_db()
    for dt in dates:
        db.execute(
            "INSERT INTO claims(patient_id,date_of_service,icd10,cpt,units,charge,paid,status,"
            "claim_number,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, dt, d.get("icd10"), d.get("cpt"),
             num(d.get("units")), num(d.get("charge")), num(d.get("paid")) or 0,
             d.get("status", "Submitted"), d.get("claim_number"), d.get("notes"),
             models.now()))
    db.commit()
    db.close()
    return jsonify({"ok": True, "created": len(dates)})


@app.route("/api/file-claim", methods=["POST"])
@api_login_required
def api_file_claim():
    """File a claim/appointment. Either pick an existing patient (patient_id)
    or provide a new patient's full info — in which case the patient is
    created and saved to the list first. Matches the real claim form."""
    u = current_user()
    d = request.get_json(force=True)

    dates = [dt for dt in (d.get("dates") or []) if dt]
    if not dates:
        return jsonify({"error": "At least one date is required"}), 400

    db = models.get_db()
    pid = d.get("patient_id")

    if pid:
        # existing patient — permission check
        if u["role"] != "admin":
            row = db.execute("SELECT doctor_id FROM patients WHERE id=?", (pid,)).fetchone()
            if not row or row["doctor_id"] != u["id"]:
                db.close()
                return jsonify({"error": "not allowed"}), 403
    else:
        # new patient — create and save to the list
        if not d.get("last_name") or not d.get("first_name"):
            db.close()
            return jsonify({"error": "New patient needs first and last name"}), 400
        # admin must say which clinic; doctor adds to self
        doctor_id = d.get("doctor_id") if u["role"] == "admin" else u["id"]
        if not doctor_id:
            db.close()
            return jsonify({"error": "Please choose a clinic for the new patient"}), 400
        db.execute(
            "INSERT INTO patients(doctor_id,first_name,last_name,dob,member_id,payer,plan,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (doctor_id, d.get("first_name"), d.get("last_name"), d.get("dob"),
             d.get("member_id"), d.get("payer"), d.get("plan"), models.now()))
        pid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    for dt in dates:
        db.execute(
            "INSERT INTO claims(patient_id,date_of_service,notes,status,created_at) "
            "VALUES(?,?,?,?,?)",
            (pid, dt, d.get("notes"), "Submitted", models.now()))
    db.commit()
    db.close()
    return jsonify({"ok": True, "patient_id": pid, "created": len(dates)})


@app.route("/api/claims/<int:cid>", methods=["PATCH"])
@api_login_required
def api_update_claim(cid):
    d = request.get_json(force=True)
    db = models.get_db()
    row = db.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    if not row or not doctor_can_access(row["patient_id"]):
        db.close()
        return jsonify({"error": "not allowed"}), 403
    # Update only the billing-outcome fields; keep anything not supplied.
    status = d.get("status") or row["status"]
    paid = num(d.get("paid")) if d.get("paid") not in (None, "") else row["paid"]
    claim_number = d.get("claim_number") if d.get("claim_number") is not None else row["claim_number"]
    db.execute("UPDATE claims SET status=?, paid=?, claim_number=? WHERE id=?",
               (status, paid or 0, claim_number, cid))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/patients/<int:pid>/payments", methods=["POST"])
@api_login_required
def api_add_payment(pid):
    if not doctor_can_access(pid):
        return jsonify({"error": "not allowed"}), 403
    d = request.get_json(force=True)
    db = models.get_db()
    db.execute(
        "INSERT INTO payments(patient_id,entry_date,kind,amount,description,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (pid, d.get("entry_date"), d.get("kind", "payment"), num(d.get("amount")),
         d.get("description"), models.now()))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/staff", methods=["GET"])
@api_login_required
def api_list_staff():
    u = current_user()
    if u["role"] != "admin":
        return jsonify({"error": "admin only"}), 403
    db = models.get_db()
    rows = db.execute("SELECT id,name,username,phone,email,created_at FROM users "
                      "WHERE role='admin' ORDER BY created_at").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/staff", methods=["POST"])
@api_login_required
def api_create_staff():
    """Admin-only: create another staff (admin) login."""
    u = current_user()
    if u["role"] != "admin":
        return jsonify({"error": "admin only"}), 403
    d = request.get_json(force=True)
    uname = d.get("username", "").strip().lower()
    if not uname or not d.get("password") or not d.get("name"):
        return jsonify({"error": "Name, username and password are required"}), 400
    if len(d.get("password")) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    db = models.get_db()
    if db.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone():
        db.close()
        return jsonify({"error": "That username is already taken"}), 400
    db.execute(
        "INSERT INTO users(role,username,password,name,phone,email,created_at) "
        "VALUES('admin',?,?,?,?,?,?)",
        (uname, models.hash_password(d["password"]), d["name"],
         d.get("phone"), d.get("email"), models.now()))
    db.commit()
    db.close()
    return jsonify({"ok": True})


def num(v):
    """Safely turn a form value into a number or None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# REQUESTS — doctor asks for a check; admin sees queue + resolves
# ---------------------------------------------------------------------------
@app.route("/api/patients/<int:pid>/request", methods=["POST"])
@api_login_required
def api_create_request(pid):
    """A doctor (or admin) submits a request for an eligibility check."""
    if not doctor_can_access(pid):
        return jsonify({"error": "not allowed"}), 403
    u = current_user()
    d = request.get_json(force=True)
    db = models.get_db()
    p = db.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    db.execute(
        "INSERT INTO requests(patient_id,doctor_id,type,message,status,created_at) "
        "VALUES(?,?,?,?,'pending',?)",
        (pid, p["doctor_id"], d.get("type", "eligibility"),
         d.get("message"), models.now()))
    db.commit()
    db.close()

    # notify you (email if configured, else console) — never blocks the save
    notify.send_notification(
        subject=f"New {d.get('type','eligibility')} request — {p['first_name']} {p['last_name']}",
        body=(f"Dr. {u['name']} requested a {d.get('type','eligibility')} check.\n\n"
              f"Patient: {p['first_name']} {p['last_name']}\n"
              f"DOB: {p['dob']}   Member ID: {p['member_id']}\n"
              f"Payer: {p['payer']}   Plan: {p['plan']}\n"
              f"Note: {d.get('message') or '(none)'}\n\n"
              f"Open the portal to respond."))
    return jsonify({"ok": True})


@app.route("/api/requests")
@api_login_required
def api_list_requests():
    """Admin: all requests. Doctor: only their own."""
    u = current_user()
    db = models.get_db()
    base = ("SELECT r.*, p.first_name, p.last_name, p.payer, p.plan, "
            "d.name AS doctor_name, d.clinic AS doctor_clinic "
            "FROM requests r JOIN patients p ON p.id=r.patient_id "
            "JOIN users d ON d.id=r.doctor_id ")
    if u["role"] == "admin":
        rows = db.execute(base + "ORDER BY r.status='done', r.created_at DESC").fetchall()
    else:
        rows = db.execute(base + "WHERE r.doctor_id=? ORDER BY r.status='done', r.created_at DESC",
                          (u["id"],)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/requests/count")
@api_login_required
def api_request_count():
    """Pending count for the notification badge."""
    u = current_user()
    db = models.get_db()
    if u["role"] == "admin":
        c = db.execute("SELECT COUNT(*) c FROM requests WHERE status='pending'").fetchone()["c"]
    else:
        c = db.execute("SELECT COUNT(*) c FROM requests WHERE status='pending' AND doctor_id=?",
                       (u["id"],)).fetchone()["c"]
    db.close()
    return jsonify({"pending": c})


@app.route("/api/requests/<int:rid>/resolve", methods=["POST"])
@api_login_required
def api_resolve_request(rid):
    """Admin marks a request done (optionally with a reply note)."""
    u = current_user()
    if u["role"] != "admin":
        return jsonify({"error": "admin only"}), 403
    d = request.get_json(force=True)
    db = models.get_db()
    db.execute("UPDATE requests SET status='done', admin_reply=?, resolved_at=? WHERE id=?",
               (d.get("admin_reply"), models.now(), rid))
    db.commit()
    db.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("\n  Happy Billing backend running")
    print("  Open http://localhost:5000")
    print("  Logins:  admin / admin123   |   dr.olivia / demo1234\n")
    app.run(debug=True, port=5000)
