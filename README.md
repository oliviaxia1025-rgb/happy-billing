# Happy Billing — Complete System

One program that runs **both** your public website **and** your private billing
portal, with a built-in request-and-notify workflow between doctors and you.

## What's in here

- **Public website** (open to everyone): Home `/`, About `/about`, Pricing `/pricing`
- **Doctor / staff portal** (behind login): `/portal`
- **The request workflow:** a doctor asks for a check -> you get notified -> you
  look it up and enter the result -> the doctor sees it.

---

## How the request workflow works

1. A doctor opens one of their patients and clicks **"Request a check."**
   They pick a type (eligibility / claim help / other) and can add a note.
2. That creates a **pending request**. You're notified two ways:
   - a **red "Requests" badge** in the portal top bar shows the pending count
     (auto-refreshes every 20 seconds), and
   - an **email** to you (once you turn email on -- see below).
3. You open **Requests**, read it, click **"Mark done & open patient."** That
   jumps you straight to that patient so you can type in the eligibility result,
   claim, or payment.
4. The doctor logs in and **sees the result** on their patient. If you left a
   reply note, they see that too.

Nothing is fake -- every request is a real database record.

---

## How to run it

    pip install flask
    python models.py     # first time only -- builds the database + demo data
    python app.py        # start the server

Open **http://localhost:5000** -- that's the public site. Click **Doctor Login**
(or go to /portal) to reach the portal.

### Demo logins

| Login | Password | Role |
|-------|----------|------|
| admin | admin123 | Staff -- sees all clinics + the full request queue |
| dr.olivia | demo1234 | Doctor -- own patients only; can submit requests |

---

## Turning on email notifications (optional)

Email is **off by default** so the app runs with zero setup. The in-app badge
works regardless. To also get emails, set these environment variables before
running python app.py (example for Yahoo):

    export HB_SMTP_HOST=smtp.mail.yahoo.com
    export HB_SMTP_PORT=587
    export HB_SMTP_USER=acubilling@yahoo.com
    export HB_SMTP_PASS=your_app_password
    export HB_NOTIFY_TO=acubilling@yahoo.com

**Important:** Yahoo and Gmail block your normal password for apps like this.
You must generate an **"app password"** in your email account's security
settings and use that as HB_SMTP_PASS. If email fails for any reason, the
request is still saved and the badge still updates -- it never breaks the flow.

---

## Files

| File | What it is |
|------|-----------|
| app.py | The whole server: public pages, portal, login, API, requests. |
| models.py | Database tables + demo data. Run once to create billing.db. |
| notify.py | Email helper (off until configured). |
| templates/public_home.html etc. | The public marketing pages. |
| templates/login.html | Login + register. |
| templates/dashboard.html | The portal workspace (adapts to admin vs doctor). |
| static/styles.css | Shared styling for the public pages. |
| billing.db | The database -- all data lives here. Delete + re-run models.py to reset. |

---

## Patient privacy (HIPAA)

This stores patient information and now runs your public site too. Demo data is
fake and safe anywhere. **Before real patient data goes in**, host on a
HIPAA-compliant server, use HTTPS, harden the login (bcrypt, secure sessions),
and back up billing.db. The current setup is a working prototype, not yet a
hardened production deployment.
