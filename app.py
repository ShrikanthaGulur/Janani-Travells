import calendar
import csv
import io
import os
import random
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from db import UPLOAD_DIR, audit, connect, init_db, refresh_notifications

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
DOC_KINDS = ["RC", "Insurance", "FC", "Permit", "Pollution"]
HANDOVER_ITEMS = [
    "Fasttag active - Sufficient balance",
    "Spare/extra tyre",
    "Jack",
    "Wheel spanner",
    "Jack rod/Handle",
    "Tow rope",
]
POSITIONS = ["Front Left", "Front Right", "Rear Left", "Rear Right", "Stepney"]


def clean_aadhaar(raw):
    text = (raw or "").strip().replace(" ", "")
    if not text:
        return ""
    if len(text) == 12 and text.isdigit():
        return text
    return None


def clean_phone(raw):
    text = (raw or "").strip().replace(" ", "")
    if len(text) == 10 and text.isdigit():
        return text
    return None


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("TMS_SECRET", "tms-dev-secret-change-me")
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    init_db()

    @app.before_request
    def open_db():
        g.db = connect()
        if request.endpoint not in {"static", None}:
            refresh_notifications(g.db)
            g.db.commit()

    @app.teardown_request
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def current_user():
        uid = session.get("uid")
        if not uid:
            return None
        return g.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            g.user = user
            return fn(*args, **kwargs)
        return wrapper

    def roles(*allowed):
        def deco(fn):
            @wraps(fn)
            @login_required
            def wrapper(*args, **kwargs):
                if g.user["role"] not in allowed:
                    abort(403)
                return fn(*args, **kwargs)
            return wrapper
        return deco

    @app.context_processor
    def inject():
        user = None
        unread = 0
        if session.get("uid"):
            try:
                user = g.db.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
                if user:
                    unread = g.db.execute(
                        "SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND is_read=0",
                        (user["id"],),
                    ).fetchone()["c"]
            except Exception:
                user = None
        return {"me": user, "unread": unread}

    def save_upload(file):
        if not file or not file.filename:
            return None
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED:
            flash("Upload a PDF or image file.")
            return None
        name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
        file.save(UPLOAD_DIR / name)
        return name

    def month_start():
        return date.today().replace(day=1).isoformat()

    def month_advance_sum(driver_id, month, exclude_id=None):
        if exclude_id is None:
            row = g.db.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM advances WHERE driver_id=? AND paid_on LIKE ?",
                (driver_id, month + "%"),
            ).fetchone()
        else:
            row = g.db.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM advances WHERE driver_id=? AND paid_on LIKE ? AND id!=?",
                (driver_id, month + "%", exclude_id),
            ).fetchone()
        return row["s"]

    def blocks_over_salary(amount, salary, month_total, note):
        if amount <= salary and month_total <= salary:
            return False
        flash(
            "This advance is more than the salary. For an emergency, write it in the note, then save the advance.",
            "toast",
        )
        return not (note or "").strip()

    def month_salary(driver, month):
        row = g.db.execute(
            "SELECT amount FROM monthly_salaries WHERE driver_id=? AND year_month=?",
            (driver["id"], month),
        ).fetchone()
        return row["amount"] if row else driver["monthly_salary"]

    def salary_summary(driver, month=None):
        month = month or date.today().strftime("%Y-%m")
        taken_all = g.db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM advances WHERE driver_id=?",
            (driver["id"],),
        ).fetchone()["s"]
        taken_month = g.db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM advances WHERE driver_id=? AND paid_on LIKE ?",
            (driver["id"], month + "%"),
        ).fetchone()["s"]
        monthly = month_salary(driver, month)
        return {
            "monthly": monthly,
            "advance_all": taken_all,
            "advance_month": taken_month,
            "remaining": monthly - taken_month,
        }

    def salary_ledger(driver, month):
        balance = month_salary(driver, month)
        lines = []
        rows = g.db.execute(
            "SELECT * FROM advances WHERE driver_id=? AND paid_on LIKE ? ORDER BY paid_on, id",
            (driver["id"], month + "%"),
        ).fetchall()
        for row in rows:
            balance -= row["amount"]
            lines.append({
                "id": row["id"],
                "paid_on": row["paid_on"],
                "amount": row["amount"],
                "note": row["note"],
                "balance": balance,
                "payment_mode": row["payment_mode"] if "payment_mode" in row.keys() else "Cash",
                "transaction_id": row["transaction_id"] if "transaction_id" in row.keys() else "",
            })
        return lines

    def assigned_vehicle(driver_id):
        return g.db.execute(
            """SELECT v.*, a.start_date FROM driver_assignments a
               JOIN vehicles v ON v.id=a.vehicle_id
               WHERE a.driver_id=? AND a.end_date IS NULL
               ORDER BY a.start_date DESC LIMIT 1""",
            (driver_id,),
        ).fetchone()

    def mileage_alert(vehicle, kmpl):
        if kmpl < vehicle["min_mileage"]:
            return "LOW"
        if kmpl > vehicle["max_mileage"]:
            return "HIGH"
        return None

    def record_mileage(vehicle, log_date, start_km, end_km, litres, note, user_id):
        if end_km <= start_km or litres <= 0:
            flash("End KM must be greater than start KM, and fuel must be above zero.")
            return
        kmpl = (end_km - start_km) / litres
        flag = mileage_alert(vehicle, kmpl)
        g.db.execute(
            """INSERT INTO mileage_logs
            (vehicle_id, log_date, start_km, end_km, fuel_litres, kmpl, alert, note, created_by)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (vehicle["id"], log_date, start_km, end_km, litres, round(kmpl, 2), flag, note, user_id),
        )
        if flag == "LOW":
            flash(f"Low mileage {kmpl:.2f} km/l on {vehicle['number']}. Check the vehicle or the entry.")
        elif flag == "HIGH":
            flash(f"High mileage {kmpl:.2f} km/l on {vehicle['number']}. Confirm the reading.")

    @app.route("/")
    def home():
        if not session.get("uid"):
            return redirect(url_for("login"))
        user = current_user()
        if user and user["role"] == "DRIVER":
            return redirect(url_for("driver_home"))
        return redirect(url_for("dashboard"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        dev_otp = session.get("dev_otp")
        phone = session.get("otp_phone", "")
        if request.method == "POST":
            action = request.form.get("action")
            phone = request.form.get("phone", "").strip()
            user = g.db.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
            if action == "request":
                if not user:
                    flash("That phone number is not registered.")
                else:
                    code = f"{random.randint(0, 999999):06d}"
                    expires = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
                    g.db.execute(
                        "INSERT INTO otp_codes (phone, code, expires_at) VALUES (?,?,?)",
                        (phone, code, expires),
                    )
                    g.db.commit()
                    session["otp_phone"] = phone
                    session["dev_otp"] = code
                    flash("OTP sent. SMS is not configured, so the development code is shown below.")
                return redirect(url_for("login"))
            code = request.form.get("code", "").strip()
            row = g.db.execute(
                """SELECT * FROM otp_codes WHERE phone=? AND code=? AND used=0
                   ORDER BY id DESC LIMIT 1""",
                (phone, code),
            ).fetchone()
            if not user or not row or row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
                flash("OTP is invalid or expired.")
                return redirect(url_for("login"))
            g.db.execute("UPDATE otp_codes SET used=1 WHERE id=?", (row["id"],))
            audit(g.db, user["id"], "LOGIN", "user", user["id"], user["phone"])
            g.db.commit()
            session["uid"] = user["id"]
            session.pop("dev_otp", None)
            return redirect(url_for("home"))
        return render_template("login.html", dev_otp=dev_otp, phone=phone)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def save_attendance(driver_id, work_date, status, check_in, check_out, note):
        if status not in {"PRESENT", "ABSENT", "LEAVE", "HALF"}:
            flash("Choose a valid attendance status.")
            return
        g.db.execute(
            """INSERT INTO attendance (driver_id, work_date, status, check_in, check_out, note)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(driver_id, work_date) DO UPDATE SET
               status=excluded.status, check_in=excluded.check_in, check_out=excluded.check_out, note=excluded.note""",
            (driver_id, work_date, status, check_in or None, check_out or None, note or ""),
        )
        audit(g.db, g.user["id"], "UPSERT", "attendance", driver_id, f"{work_date} {status}")

    def month_days(month):
        year, mon = [int(part) for part in month.split("-")]
        return [date(year, mon, day).isoformat() for day in range(1, calendar.monthrange(year, mon)[1] + 1)]

    def month_sheet(drivers, month):
        days = month_days(month)
        marks = {
            (row["driver_id"], row["work_date"]): row["status"]
            for row in g.db.execute("SELECT driver_id, work_date, status FROM attendance WHERE work_date LIKE ?", (month + "%",))
        }
        short = {"PRESENT": "P", "ABSENT": "A", "LEAVE": "L", "HALF": "H"}
        sheet = []
        for driver in drivers:
            cells = []
            counts = {"PRESENT": 0, "ABSENT": 0, "LEAVE": 0, "HALF": 0}
            for day in days:
                status = marks.get((driver["id"], day))
                if status:
                    counts[status] += 1
                cells.append(short.get(status, ""))
            sheet.append({"driver": driver, "cells": cells, "counts": counts})
        return days, sheet

    @app.route("/attendance", methods=["GET", "POST"])
    @login_required
    def attendance():
        month = request.values.get("month") or date.today().strftime("%Y-%m")
        work_date = request.values.get("work_date") or date.today().isoformat()
        if not work_date.startswith(month):
            work_date = month + "-01"
        if request.method == "POST":
            if g.user["role"] == "DRIVER":
                save_attendance(
                    g.user["driver_id"],
                    work_date,
                    request.form.get("status", "PRESENT"),
                    request.form.get("check_in"),
                    request.form.get("check_out"),
                    request.form.get("note"),
                )
            else:
                for driver in g.db.execute("SELECT id FROM drivers"):
                    status = request.form.get(f"status_{driver['id']}")
                    if not status:
                        continue
                    save_attendance(
                        driver["id"],
                        work_date,
                        status,
                        request.form.get(f"check_in_{driver['id']}"),
                        request.form.get(f"check_out_{driver['id']}"),
                        request.form.get(f"note_{driver['id']}"),
                    )
            g.db.commit()
            flash("Attendance saved.")
            return redirect(url_for("attendance", month=work_date[:7], work_date=work_date))
        if g.user["role"] == "DRIVER":
            drivers = g.db.execute("SELECT * FROM drivers WHERE id=?", (g.user["driver_id"],)).fetchall()
        else:
            drivers = g.db.execute("SELECT * FROM drivers ORDER BY name").fetchall()
        days, sheet = month_sheet(drivers, month)
        marked = {
            row["driver_id"]: row
            for row in g.db.execute("SELECT * FROM attendance WHERE work_date=?", (work_date,))
        }
        return render_template(
            "attendance.html",
            month=month,
            work_date=work_date,
            days=days,
            sheet=sheet,
            marked=marked,
            can_edit=g.user["role"] in {"ADMIN", "MANAGER"},
        )

    @app.route("/checklist", methods=["GET", "POST"])
    @login_required
    def checklist():
        def item_names():
            return [row["name"] for row in g.db.execute("SELECT name FROM checklist_items ORDER BY built_in DESC, id")]

        if request.method == "POST":
            action = request.form.get("action") or "handover"
            if action == "add_item":
                if g.user["role"] not in {"ADMIN", "MANAGER"}:
                    abort(403)
                name = request.form.get("item_name", "").strip()
                if not name:
                    flash("Enter a name for the extra checklist item.")
                elif g.db.execute("SELECT id FROM checklist_items WHERE name=?", (name,)).fetchone():
                    flash("That checklist item already exists.")
                else:
                    g.db.execute("INSERT INTO checklist_items (name, built_in) VALUES (?,0)", (name,))
                    audit(g.db, g.user["id"], "CREATE", "checklist_item", name, name)
                    g.db.commit()
                    flash(f"Added “{name}” to the checklist.")
                return redirect(url_for("checklist"))
            if action == "remove_item":
                if g.user["role"] not in {"ADMIN", "MANAGER"}:
                    abort(403)
                g.db.execute("DELETE FROM checklist_items WHERE id=? AND built_in=0", (int(request.form["item_id"]),))
                g.db.commit()
                flash("Extra checklist item removed.")
                return redirect(url_for("checklist"))
            if g.user["role"] == "DRIVER":
                driver_id = g.user["driver_id"]
                own = assigned_vehicle(driver_id)
                vehicle_id = own["id"] if own else None
                if not vehicle_id or vehicle_id != int(request.form["vehicle_id"]):
                    abort(403)
            else:
                driver_id = int(request.form["driver_id"])
                vehicle_id = int(request.form["vehicle_id"])
            taken_on = request.form["taken_on"]
            names = item_names()
            checked = set(request.form.getlist("item"))
            cur = g.db.execute(
                "INSERT INTO handovers (vehicle_id, driver_id, taken_on, note) VALUES (?,?,?,?)",
                (vehicle_id, driver_id, taken_on, request.form.get("note", "")),
            )
            for name in names:
                g.db.execute(
                    "INSERT INTO handover_items (handover_id, name, checked) VALUES (?,?,?)",
                    (cur.lastrowid, name, 1 if name in checked else 0),
                )
            audit(g.db, g.user["id"], "CREATE", "handover", vehicle_id, taken_on)
            g.db.commit()
            missing = [name for name in names if name not in checked]
            if missing:
                flash("Checklist saved. Still unchecked: " + ", ".join(missing) + ".")
            else:
                flash("Checklist saved. Every item is ticked.")
            return redirect(url_for("checklist"))
        if g.user["role"] == "DRIVER":
            driver = g.db.execute("SELECT * FROM drivers WHERE id=?", (g.user["driver_id"],)).fetchone()
            vehicle = assigned_vehicle(g.user["driver_id"])
            drivers = [driver]
            vehicles = [vehicle] if vehicle else []
        else:
            drivers = g.db.execute("SELECT * FROM drivers ORDER BY name").fetchall()
            vehicles = g.db.execute("SELECT * FROM vehicles ORDER BY number").fetchall()
        history = g.db.execute(
            """SELECT h.*, d.name AS driver_name, v.number
               FROM handovers h
               JOIN drivers d ON d.id=h.driver_id
               JOIN vehicles v ON v.id=h.vehicle_id
               ORDER BY h.taken_on DESC, h.id DESC LIMIT 30"""
        ).fetchall()
        if g.user["role"] == "DRIVER":
            history = [row for row in history if row["driver_id"] == g.user["driver_id"]]
        items_by_handover = {}
        for row in history:
            items_by_handover[row["id"]] = g.db.execute(
                "SELECT name, checked FROM handover_items WHERE handover_id=? ORDER BY id",
                (row["id"],),
            ).fetchall()
        catalog = g.db.execute("SELECT * FROM checklist_items ORDER BY built_in DESC, id").fetchall()
        return render_template(
            "checklist.html",
            items=[row["name"] for row in catalog],
            catalog=catalog,
            drivers=drivers,
            vehicles=vehicles,
            history=history,
            items_by_handover=items_by_handover,
            can_edit=g.user["role"] in {"ADMIN", "MANAGER"},
            is_driver=g.user["role"] == "DRIVER",
            today=date.today().isoformat(),
        )

    @app.route("/advances", methods=["GET", "POST"])
    @login_required
    def advances():
        if request.method == "POST":
            if g.user["role"] not in {"ADMIN", "MANAGER"}:
                abort(403)
            driver_id = int(request.form["driver_id"])
            driver = g.db.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()
            if not driver:
                abort(404)
            if request.form.get("action") == "salary":
                salary = float(request.form["monthly_salary"])
                pay_month = request.form.get("month") or date.today().strftime("%Y-%m")
                g.db.execute(
                    """INSERT INTO monthly_salaries (driver_id, year_month, amount) VALUES (?,?,?)
                       ON CONFLICT(driver_id, year_month) DO UPDATE SET amount=excluded.amount""",
                    (driver_id, pay_month, salary),
                )
                audit(g.db, g.user["id"], "UPDATE", "salary", driver_id, f"{pay_month} {salary}")
                g.db.commit()
                flash(f"{driver['name']}'s salary for {pay_month} is ₹{salary:,.0f}. Other months are unchanged.")
                return redirect(url_for("advances", driver=driver_id, month=pay_month))
            if request.form.get("action") == "edit_advance":
                if g.user["role"] != "ADMIN":
                    abort(403)
                advance_id = int(request.form["advance_id"])
                row = g.db.execute(
                    "SELECT * FROM advances WHERE id=? AND driver_id=?",
                    (advance_id, driver_id),
                ).fetchone()
                if not row:
                    abort(404)
                amount = float(request.form["amount"])
                note = request.form.get("note", "").strip()
                view_month = request.form.get("month") or row["paid_on"][:7]
                pay_month = row["paid_on"][:7]
                salary = month_salary(driver, pay_month)
                month_total = month_advance_sum(driver_id, pay_month, advance_id) + amount
                if blocks_over_salary(amount, salary, month_total, note):
                    return redirect(url_for("advances", driver=driver_id, month=view_month))
                g.db.execute(
                    "UPDATE advances SET amount=?, note=? WHERE id=? AND driver_id=?",
                    (amount, note, advance_id, driver_id),
                )
                audit(g.db, g.user["id"], "UPDATE", "advance", advance_id, str(amount))
                g.db.commit()
                flash("Advance updated.")
                return redirect(url_for("advances", driver=driver_id, month=view_month))
            amount = float(request.form["amount"])
            paid_on = request.form["paid_on"]
            view_month = request.form.get("month") or paid_on[:7]
            if not paid_on.startswith(view_month):
                flash("The advance date has to be inside the month you selected.")
                return redirect(url_for("advances", driver=driver_id, month=view_month))
            mode = request.form.get("payment_mode") or "Cash"
            if mode not in {"Cash", "UPI", "Bank transfer"}:
                mode = "Cash"
            txn = request.form.get("transaction_id", "").strip()
            note = request.form.get("note", "").strip()
            salary = month_salary(driver, paid_on[:7])
            month_total = month_advance_sum(driver_id, paid_on[:7]) + amount
            if blocks_over_salary(amount, salary, month_total, note):
                return redirect(url_for("advances", driver=driver_id, month=view_month))
            g.db.execute(
                "INSERT INTO advances (driver_id, amount, paid_on, note, payment_mode, transaction_id) VALUES (?,?,?,?,?,?)",
                (driver_id, amount, paid_on, note, mode, txn),
            )
            deducted = g.db.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM advances WHERE driver_id=? AND paid_on LIKE ?",
                (driver_id, paid_on[:7] + "%"),
            ).fetchone()["s"]
            balance = salary - deducted
            audit(g.db, g.user["id"], "CREATE", "advance", driver_id, str(amount))
            g.db.commit()
            flash(f"₹{amount:,.0f} deducted from {driver['name']}'s {paid_on[:7]} salary of ₹{salary:,.0f}. Balance payable is ₹{balance:,.0f}.")
            return redirect(url_for("advances", driver=driver_id, month=paid_on[:7]))
        month = request.args.get("month") or date.today().strftime("%Y-%m")
        if g.user["role"] == "DRIVER":
            people = g.db.execute("SELECT * FROM drivers WHERE id=?", (g.user["driver_id"],)).fetchall()
            can_edit = False
            selected_id = g.user["driver_id"]
        else:
            people = g.db.execute("SELECT * FROM drivers ORDER BY name").fetchall()
            can_edit = True
            selected_id = request.args.get("driver", type=int)
        ledgers = []
        selected = None
        for driver in people:
            summary = salary_summary(driver, month)
            item = {"driver": driver, "summary": summary, "lines": []}
            if selected_id and driver["id"] == selected_id:
                item["lines"] = salary_ledger(driver, month)
                selected = item
            ledgers.append(item)
        year, mon = [int(part) for part in month.split("-")]
        last_day = calendar.monthrange(year, mon)[1]
        days = [f"{month}-{day:02d}" for day in range(1, last_day + 1)]
        paid_default = date.today().isoformat() if date.today().strftime("%Y-%m") == month else f"{month}-01"
        return render_template(
            "advances.html",
            ledgers=ledgers,
            selected=selected,
            month=month,
            can_edit=can_edit,
            is_admin=g.user["role"] == "ADMIN",
            today=paid_default,
            month_start=f"{month}-01",
            month_end=f"{month}-{last_day:02d}",
            days=days,
        )

    @app.route("/dashboard")
    @roles("ADMIN", "MANAGER")
    def dashboard():
        stats = {
            "vehicles": g.db.execute("SELECT COUNT(*) c FROM vehicles").fetchone()["c"],
            "drivers": g.db.execute("SELECT COUNT(*) c FROM drivers").fetchone()["c"],
            "companies": g.db.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"],
            "fuel_month": g.db.execute(
                "SELECT COALESCE(SUM(cost),0) s FROM fuel_entries WHERE entry_date>=?",
                (month_start(),),
            ).fetchone()["s"],
        }
        expiring = g.db.execute(
            """SELECT d.kind, d.expiry_date, v.number FROM documents d
               JOIN vehicles v ON v.id=d.vehicle_id
               WHERE d.expiry_date IS NOT NULL AND d.expiry_date<=?
               ORDER BY d.expiry_date""",
            ((date.today() + timedelta(days=30)).isoformat(),),
        ).fetchall()
        mileage_flags = g.db.execute(
            """SELECT m.*, v.number FROM mileage_logs m JOIN vehicles v ON v.id=m.vehicle_id
               WHERE m.alert IS NOT NULL ORDER BY m.log_date DESC LIMIT 8"""
        ).fetchall()
        return render_template("dashboard.html", stats=stats, expiring=expiring, mileage_flags=mileage_flags)

    @app.route("/drivers", methods=["GET", "POST"])
    @roles("ADMIN", "MANAGER")
    def drivers():
        if request.method == "POST":
            phone = clean_phone(request.form["phone"])
            aadhaar = clean_aadhaar(request.form.get("aadhaar"))
            if phone is None:
                flash("Phone number must be 10 digits.")
            elif g.db.execute("SELECT id FROM drivers WHERE phone=?", (phone,)).fetchone():
                flash("A driver with that phone already exists.")
            elif aadhaar is None:
                flash("Aadhaar number must be 12 digits.")
            else:
                cur = g.db.execute(
                    """INSERT INTO drivers (name, phone, country_code, address, aadhaar, dl_number, dl_expiry, monthly_salary)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        request.form["name"].strip(),
                        phone,
                        "+91",
                        request.form.get("address", "").strip(),
                        aadhaar,
                        request.form.get("dl_number", "").strip(),
                        request.form.get("dl_expiry") or None,
                        float(request.form.get("monthly_salary") or 0),
                    ),
                )
                driver_id = cur.lastrowid
                g.db.execute(
                    "INSERT INTO users (phone, name, role, driver_id) VALUES (?,?, 'DRIVER', ?)",
                    (phone, request.form["name"].strip(), driver_id),
                )
                copy = save_upload(request.files.get("dl_copy"))
                aadhaar_copy = save_upload(request.files.get("aadhaar_copy"))
                if copy:
                    g.db.execute("UPDATE drivers SET dl_copy=? WHERE id=?", (copy, driver_id))
                if aadhaar_copy:
                    g.db.execute("UPDATE drivers SET aadhaar_copy=? WHERE id=?", (aadhaar_copy, driver_id))
                audit(g.db, g.user["id"], "CREATE", "driver", driver_id, request.form["name"])
                g.db.commit()
                flash("Driver added. They can sign in with this phone number.")
            return redirect(url_for("drivers"))
        q = request.args.get("q", "").strip()
        month = date.today().strftime("%Y-%m")
        sql = """SELECT d.*, COALESCE(
                   (SELECT amount FROM monthly_salaries m WHERE m.driver_id=d.id AND m.year_month=?),
                   d.monthly_salary) AS display_salary
                 FROM drivers d"""
        args = [month]
        if q:
            sql += " WHERE d.name LIKE ? OR d.phone LIKE ? OR d.dl_number LIKE ? OR d.aadhaar LIKE ?"
            args += [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"]
        sql += " ORDER BY d.name"
        rows = g.db.execute(sql, args).fetchall()
        return render_template("drivers.html", drivers=rows, q=q)

    @app.route("/drivers/<int:driver_id>", methods=["GET", "POST"])
    @login_required
    def driver_detail(driver_id):
        if g.user["role"] == "DRIVER" and g.user["driver_id"] != driver_id:
            abort(403)
        driver = g.db.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()
        if not driver:
            abort(404)
        if request.method == "POST" and g.user["role"] in {"ADMIN", "MANAGER"}:
            action = request.form.get("action")
            if action == "profile":
                aadhaar = clean_aadhaar(request.form.get("aadhaar"))
                phone = clean_phone(request.form.get("phone"))
                if phone is None:
                    flash("Phone number must be 10 digits.")
                    return redirect(url_for("driver_detail", driver_id=driver_id))
                if aadhaar is None:
                    flash("Aadhaar number must be 12 digits.")
                    return redirect(url_for("driver_detail", driver_id=driver_id))
                copy = save_upload(request.files.get("dl_copy"))
                aadhaar_copy = save_upload(request.files.get("aadhaar_copy"))
                salary = float(request.form.get("monthly_salary") or 0)
                g.db.execute(
                    """UPDATE drivers SET name=?, phone=?, country_code=?, address=?, aadhaar=?, dl_number=?, dl_expiry=?, monthly_salary=?
                       WHERE id=?""",
                    (
                        request.form["name"].strip(),
                        phone,
                        "+91",
                        request.form.get("address", "").strip(),
                        aadhaar,
                        request.form.get("dl_number", "").strip(),
                        request.form.get("dl_expiry") or None,
                        salary,
                        driver_id,
                    ),
                )
                pay_month = date.today().strftime("%Y-%m")
                g.db.execute(
                    """INSERT INTO monthly_salaries (driver_id, year_month, amount) VALUES (?,?,?)
                       ON CONFLICT(driver_id, year_month) DO UPDATE SET amount=excluded.amount""",
                    (driver_id, pay_month, salary),
                )
                if copy:
                    g.db.execute("UPDATE drivers SET dl_copy=? WHERE id=?", (copy, driver_id))
                if aadhaar_copy:
                    g.db.execute("UPDATE drivers SET aadhaar_copy=? WHERE id=?", (aadhaar_copy, driver_id))
                g.db.execute(
                    "UPDATE users SET name=?, phone=? WHERE driver_id=?",
                    (request.form["name"].strip(), phone, driver_id),
                )
                audit(g.db, g.user["id"], "UPDATE", "driver", driver_id, "profile")
                flash("Driver profile updated.")
            elif action == "advance":
                amount = float(request.form["amount"])
                paid_on = request.form["paid_on"]
                note = request.form.get("note", "").strip()
                salary = month_salary(driver, paid_on[:7])
                month_total = month_advance_sum(driver_id, paid_on[:7]) + amount
                if blocks_over_salary(amount, salary, month_total, note):
                    return redirect(url_for("driver_detail", driver_id=driver_id))
                g.db.execute(
                    "INSERT INTO advances (driver_id, amount, paid_on, note) VALUES (?,?,?,?)",
                    (driver_id, amount, paid_on, note),
                )
                audit(g.db, g.user["id"], "CREATE", "advance", driver_id, request.form["amount"])
                flash("Advance recorded.")
            elif action == "edit_advance":
                if g.user["role"] != "ADMIN":
                    abort(403)
                advance_id = int(request.form["advance_id"])
                row = g.db.execute(
                    "SELECT * FROM advances WHERE id=? AND driver_id=?",
                    (advance_id, driver_id),
                ).fetchone()
                if not row:
                    abort(404)
                amount = float(request.form["amount"])
                note = request.form.get("note", "").strip()
                pay_month = row["paid_on"][:7]
                salary = month_salary(driver, pay_month)
                month_total = month_advance_sum(driver_id, pay_month, advance_id) + amount
                if blocks_over_salary(amount, salary, month_total, note):
                    return redirect(url_for("driver_detail", driver_id=driver_id))
                g.db.execute(
                    "UPDATE advances SET amount=?, note=? WHERE id=? AND driver_id=?",
                    (amount, note, advance_id, driver_id),
                )
                audit(g.db, g.user["id"], "UPDATE", "advance", advance_id, str(amount))
                flash("Advance updated.")
            elif action == "assign":
                vehicle_id = int(request.form["vehicle_id"])
                start = request.form["start_date"]
                g.db.execute(
                    "UPDATE driver_assignments SET end_date=? WHERE driver_id=? AND end_date IS NULL AND start_date<?",
                    (start, driver_id, start),
                )
                g.db.execute(
                    "UPDATE driver_assignments SET end_date=? WHERE vehicle_id=? AND end_date IS NULL AND start_date<?",
                    (start, vehicle_id, start),
                )
                g.db.execute(
                    "INSERT INTO driver_assignments (driver_id, vehicle_id, start_date) VALUES (?,?,?)",
                    (driver_id, vehicle_id, start),
                )
                audit(g.db, g.user["id"], "ASSIGN", "driver", driver_id, str(vehicle_id))
                flash("Vehicle assigned to this driver.")
            elif action == "update_assignment":
                assignment_id = int(request.form["assignment_id"])
                row = g.db.execute(
                    "SELECT * FROM driver_assignments WHERE id=? AND driver_id=?",
                    (assignment_id, driver_id),
                ).fetchone()
                if not row:
                    abort(404)
                vehicle_id = int(request.form["vehicle_id"])
                start = request.form["start_date"]
                current = bool(request.form.get("current"))
                end = None if current else (request.form.get("end_date") or None)
                if not current and not end:
                    flash("Pick an end date, or leave Current ticked.")
                    return redirect(url_for("driver_detail", driver_id=driver_id))
                if end and end < start:
                    flash("The end date has to be on or after the start date.")
                    return redirect(url_for("driver_detail", driver_id=driver_id))
                g.db.execute(
                    "UPDATE driver_assignments SET vehicle_id=?, start_date=?, end_date=? WHERE id=?",
                    (vehicle_id, start, end, assignment_id),
                )
                if not end:
                    g.db.execute(
                        """UPDATE driver_assignments SET end_date=?
                           WHERE driver_id=? AND end_date IS NULL AND id!=? AND start_date<?""",
                        (start, driver_id, assignment_id, start),
                    )
                    g.db.execute(
                        """UPDATE driver_assignments SET end_date=?
                           WHERE vehicle_id=? AND end_date IS NULL AND id!=? AND start_date<?""",
                        (start, vehicle_id, assignment_id, start),
                    )
                audit(g.db, g.user["id"], "UPDATE", "assignment", assignment_id, str(vehicle_id))
                flash("Assignment updated.")
            g.db.commit()
            return redirect(url_for("driver_detail", driver_id=driver_id))
        driver = g.db.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()
        advances = g.db.execute(
            "SELECT * FROM advances WHERE driver_id=? ORDER BY paid_on DESC", (driver_id,)
        ).fetchall()
        history = g.db.execute(
            """SELECT a.*, v.number, v.model FROM driver_assignments a
               JOIN vehicles v ON v.id=a.vehicle_id
               WHERE a.driver_id=? ORDER BY a.start_date DESC""",
            (driver_id,),
        ).fetchall()
        vehicles = g.db.execute("SELECT id, number, model FROM vehicles ORDER BY number").fetchall()
        return render_template(
            "driver.html",
            driver=driver,
            advances=advances,
            history=history,
            vehicles=vehicles,
            summary=salary_summary(driver),
            can_edit=g.user["role"] in {"ADMIN", "MANAGER"},
            is_admin=g.user["role"] == "ADMIN",
            today=date.today().isoformat(),
        )

    @app.route("/vehicles", methods=["GET", "POST"])
    @roles("ADMIN", "MANAGER")
    def vehicles():
        if request.method == "POST":
            number = request.form["number"].strip().upper()
            if g.db.execute("SELECT id FROM vehicles WHERE number=?", (number,)).fetchone():
                flash("That vehicle number already exists.")
            else:
                cur = g.db.execute(
                    """INSERT INTO vehicles
                    (number, model, vtype, seats, purchase_date, purchase_price, min_mileage, max_mileage, service_interval_km, service_interval_days)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        number,
                        request.form["model"].strip(),
                        request.form.get("vtype") or "PASSENGER",
                        int(request.form.get("seats") or 0),
                        request.form.get("purchase_date") or None,
                        float(request.form.get("purchase_price") or 0),
                        float(request.form.get("min_mileage") or 4),
                        float(request.form.get("max_mileage") or 18),
                        float(request.form.get("service_interval_km") or 5000),
                        int(request.form.get("service_interval_days") or 90),
                    ),
                )
                vid = cur.lastrowid
                for name in ["Stepney", "Spanner", "Jack", "Jack Rod", "Music System"]:
                    g.db.execute(
                        "INSERT INTO accessories (vehicle_id, name, present, checked_on) VALUES (?,?,0,?)",
                        (vid, name, date.today().isoformat()),
                    )
                audit(g.db, g.user["id"], "CREATE", "vehicle", vid, number)
                g.db.commit()
                flash("Vehicle added.")
            return redirect(url_for("vehicles"))
        vtype = request.args.get("vtype", "")
        company_id = request.args.get("company_id", "")
        q = request.args.get("q", "").strip()
        sql = """SELECT v.*,
                 (SELECT c.name FROM company_assignments ca JOIN companies c ON c.id=ca.company_id
                  WHERE ca.vehicle_id=v.id AND ca.end_date IS NULL LIMIT 1) AS company_name
                 FROM vehicles v WHERE 1=1"""
        args = []
        if vtype:
            sql += " AND v.vtype=?"
            args.append(vtype)
        if q:
            sql += " AND (v.number LIKE ? OR v.model LIKE ?)"
            args.extend([f"%{q}%", f"%{q}%"])
        if company_id:
            sql += """ AND EXISTS (SELECT 1 FROM company_assignments ca
                      WHERE ca.vehicle_id=v.id AND ca.company_id=? AND ca.end_date IS NULL)"""
            args.append(company_id)
        sql += " ORDER BY v.number"
        rows = g.db.execute(sql, args).fetchall()
        companies = g.db.execute("SELECT * FROM companies ORDER BY name").fetchall()
        return render_template("vehicles.html", vehicles=rows, companies=companies, q=q, vtype=vtype, company_id=company_id)

    @app.route("/vehicles/<int:vehicle_id>", methods=["GET", "POST"])
    @login_required
    def vehicle_detail(vehicle_id):
        vehicle = g.db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if not vehicle:
            abort(404)
        if g.user["role"] == "DRIVER":
            own = assigned_vehicle(g.user["driver_id"])
            if not own or own["id"] != vehicle_id:
                abort(403)
        if request.method == "POST" and g.user["role"] in {"ADMIN", "MANAGER"}:
            action = request.form.get("action")
            if action == "profile":
                g.db.execute(
                    """UPDATE vehicles SET model=?, vtype=?, seats=?, purchase_date=?, purchase_price=?,
                       min_mileage=?, max_mileage=?, service_interval_km=?, service_interval_days=? WHERE id=?""",
                    (
                        request.form["model"].strip(),
                        request.form.get("vtype") or "PASSENGER",
                        int(request.form.get("seats") or 0),
                        request.form.get("purchase_date") or None,
                        float(request.form.get("purchase_price") or 0),
                        float(request.form.get("min_mileage") or 4),
                        float(request.form.get("max_mileage") or 18),
                        float(request.form.get("service_interval_km") or 5000),
                        int(request.form.get("service_interval_days") or 90),
                        vehicle_id,
                    ),
                )
                audit(g.db, g.user["id"], "UPDATE", "vehicle", vehicle_id, "profile")
            elif action == "document":
                kind = request.form.get("kind", "")
                if kind not in DOC_KINDS:
                    flash("Choose RC, Insurance, FC, Permit, or Pollution.")
                else:
                    path = save_upload(request.files.get("file"))
                    expiry = request.form.get("expiry_date") or None
                    existing = g.db.execute(
                        "SELECT id FROM documents WHERE vehicle_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                        (vehicle_id, kind),
                    ).fetchone()
                    if existing:
                        if path:
                            g.db.execute(
                                "UPDATE documents SET expiry_date=?, file_path=? WHERE id=?",
                                (expiry, path, existing["id"]),
                            )
                        else:
                            g.db.execute(
                                "UPDATE documents SET expiry_date=? WHERE id=?",
                                (expiry, existing["id"]),
                            )
                        audit(g.db, g.user["id"], "UPDATE", "document", vehicle_id, kind)
                        flash(f"{kind} updated.")
                    else:
                        g.db.execute(
                            "INSERT INTO documents (vehicle_id, kind, expiry_date, file_path) VALUES (?,?,?,?)",
                            (vehicle_id, kind, expiry, path),
                        )
                        audit(g.db, g.user["id"], "CREATE", "document", vehicle_id, kind)
                        flash(f"{kind} added.")
            elif action == "update_document":
                document_id = int(request.form["document_id"])
                row = g.db.execute(
                    "SELECT * FROM documents WHERE id=? AND vehicle_id=?",
                    (document_id, vehicle_id),
                ).fetchone()
                if not row:
                    abort(404)
                expiry = request.form.get("expiry_date") or None
                path = save_upload(request.files.get("file"))
                if path:
                    g.db.execute(
                        "UPDATE documents SET expiry_date=?, file_path=? WHERE id=?",
                        (expiry, path, document_id),
                    )
                else:
                    g.db.execute(
                        "UPDATE documents SET expiry_date=? WHERE id=?",
                        (expiry, document_id),
                    )
                audit(g.db, g.user["id"], "UPDATE", "document", vehicle_id, row["kind"])
                flash(f"{row['kind']} updated.")
            elif action == "photo":
                path = save_upload(request.files.get("file"))
                if path:
                    g.db.execute(
                        "INSERT INTO vehicle_photos (vehicle_id, file_path) VALUES (?,?)",
                        (vehicle_id, path),
                    )
            elif action == "mileage":
                record_mileage(
                    vehicle,
                    request.form["log_date"],
                    float(request.form["start_km"]),
                    float(request.form["end_km"]),
                    float(request.form["fuel_litres"]),
                    request.form.get("note", ""),
                    g.user["id"],
                )
                audit(g.db, g.user["id"], "CREATE", "mileage", vehicle_id, "")
            elif action == "company":
                start = request.form["start_date"]
                g.db.execute(
                    "UPDATE company_assignments SET end_date=? WHERE vehicle_id=? AND end_date IS NULL",
                    (start, vehicle_id),
                )
                end = request.form.get("end_date") or None
                g.db.execute(
                    "INSERT INTO company_assignments (vehicle_id, company_id, start_date, end_date) VALUES (?,?,?,?)",
                    (vehicle_id, int(request.form["company_id"]), start, end),
                )
                audit(g.db, g.user["id"], "ASSIGN", "vehicle", vehicle_id, request.form["company_id"])
            elif action == "tyre":
                if request.form.get("replace_id"):
                    g.db.execute(
                        "UPDATE tyres SET removed_on=? WHERE id=?",
                        (request.form["installed_on"], int(request.form["replace_id"])),
                    )
                g.db.execute(
                    """INSERT INTO tyres (vehicle_id, tyre_number, position, installed_on, notes)
                       VALUES (?,?,?,?,?)""",
                    (
                        vehicle_id,
                        request.form["tyre_number"].strip(),
                        request.form["position"],
                        request.form["installed_on"],
                        request.form.get("notes", ""),
                    ),
                )
            elif action == "accessories":
                checked = set(request.form.getlist("present"))
                for row in g.db.execute("SELECT * FROM accessories WHERE vehicle_id=?", (vehicle_id,)):
                    g.db.execute(
                        "UPDATE accessories SET present=?, checked_on=? WHERE id=?",
                        (1 if str(row["id"]) in checked else 0, date.today().isoformat(), row["id"]),
                    )
            elif action == "assign_driver":
                driver = g.db.execute(
                    "SELECT * FROM drivers WHERE id=?", (int(request.form["driver_id"]),)
                ).fetchone()
                start = request.form.get("start_date", "").strip()
                if not driver:
                    flash("Choose a driver.")
                elif not start:
                    flash("Pick a start date.")
                elif g.db.execute(
                    """SELECT id FROM driver_assignments
                       WHERE vehicle_id=? AND driver_id=? AND end_date IS NULL""",
                    (vehicle_id, driver["id"]),
                ).fetchone():
                    flash(f"{driver['name']} is already the driver of this vehicle.")
                else:
                    g.db.execute(
                        "UPDATE driver_assignments SET end_date=? WHERE driver_id=? AND end_date IS NULL AND start_date<?",
                        (start, driver["id"], start),
                    )
                    g.db.execute(
                        "UPDATE driver_assignments SET end_date=? WHERE vehicle_id=? AND end_date IS NULL AND start_date<?",
                        (start, vehicle_id, start),
                    )
                    g.db.execute(
                        "INSERT INTO driver_assignments (driver_id, vehicle_id, start_date) VALUES (?,?,?)",
                        (driver["id"], vehicle_id, start),
                    )
                    audit(g.db, g.user["id"], "ASSIGN", "vehicle", vehicle_id, str(driver["id"]))
                    flash(f"{driver['name']} assigned to this vehicle.")
            g.db.commit()
            return redirect(url_for("vehicle_detail", vehicle_id=vehicle_id))
        docs = g.db.execute("SELECT * FROM documents WHERE vehicle_id=? ORDER BY kind", (vehicle_id,)).fetchall()
        docs = sorted(docs, key=lambda row: DOC_KINDS.index(row["kind"]) if row["kind"] in DOC_KINDS else 99)
        saved_kinds = {row["kind"] for row in docs}
        missing_kinds = [kind for kind in DOC_KINDS if kind not in saved_kinds]
        photos = g.db.execute("SELECT * FROM vehicle_photos WHERE vehicle_id=?", (vehicle_id,)).fetchall()
        logs = g.db.execute(
            "SELECT * FROM mileage_logs WHERE vehicle_id=? ORDER BY log_date DESC", (vehicle_id,)
        ).fetchall()
        companies = g.db.execute("SELECT * FROM companies ORDER BY name").fetchall()
        company_history = g.db.execute(
            """SELECT ca.*, c.name FROM company_assignments ca JOIN companies c ON c.id=ca.company_id
               WHERE ca.vehicle_id=? ORDER BY ca.start_date DESC""",
            (vehicle_id,),
        ).fetchall()
        services = g.db.execute(
            "SELECT * FROM services WHERE vehicle_id=? ORDER BY service_date DESC", (vehicle_id,)
        ).fetchall()
        tyres = g.db.execute(
            "SELECT * FROM tyres WHERE vehicle_id=? ORDER BY installed_on DESC", (vehicle_id,)
        ).fetchall()
        accessories = g.db.execute(
            "SELECT * FROM accessories WHERE vehicle_id=? ORDER BY name", (vehicle_id,)
        ).fetchall()
        fuels = g.db.execute(
            "SELECT * FROM fuel_entries WHERE vehicle_id=? ORDER BY entry_date DESC", (vehicle_id,)
        ).fetchall()
        driver_history = g.db.execute(
            """SELECT a.*, d.name, d.phone, d.country_code
               FROM driver_assignments a JOIN drivers d ON d.id=a.driver_id
               WHERE a.vehicle_id=? ORDER BY a.start_date DESC, a.id DESC""",
            (vehicle_id,),
        ).fetchall()
        current_driver = next((row for row in driver_history if not row["end_date"]), None)
        drivers = g.db.execute(
            """SELECT d.id, d.name,
                      (SELECT v.number FROM driver_assignments a
                       JOIN vehicles v ON v.id=a.vehicle_id
                       WHERE a.driver_id=d.id AND a.end_date IS NULL
                       ORDER BY a.start_date DESC LIMIT 1) AS current_number
               FROM drivers d ORDER BY d.name"""
        ).fetchall()
        return render_template(
            "vehicle.html",
            vehicle=vehicle,
            current_driver=current_driver,
            driver_history=driver_history,
            drivers=drivers,
            docs=docs,
            photos=photos,
            logs=logs,
            companies=companies,
            company_history=company_history,
            services=services,
            tyres=tyres,
            accessories=accessories,
            fuels=fuels,
            kinds=DOC_KINDS,
            missing_kinds=missing_kinds,
            positions=POSITIONS,
            can_edit=g.user["role"] in {"ADMIN", "MANAGER"},
            today=date.today().isoformat(),
        )

    @app.route("/companies", methods=["GET", "POST"])
    @roles("ADMIN", "MANAGER")
    def companies():
        if request.method == "POST" and request.form.get("action") != "assign":
            cur = g.db.execute(
                "INSERT INTO companies (name, contact, address) VALUES (?,?,?)",
                (request.form["name"].strip(), request.form.get("contact", "").strip(), request.form.get("address", "").strip()),
            )
            audit(g.db, g.user["id"], "CREATE", "company", cur.lastrowid, request.form["name"])
            g.db.commit()
            flash("Company saved.")
            return redirect(url_for("companies"))
        if request.form.get("action") == "assign":
            vehicle_id = int(request.form["vehicle_id"])
            company_id = int(request.form["company_id"])
            start = request.form["start_date"]
            g.db.execute(
                "UPDATE company_assignments SET end_date=? WHERE vehicle_id=? AND end_date IS NULL",
                (start, vehicle_id),
            )
            g.db.execute(
                "INSERT INTO company_assignments (vehicle_id, company_id, start_date) VALUES (?,?,?)",
                (vehicle_id, company_id, start),
            )
            audit(g.db, g.user["id"], "ASSIGN", "company", company_id, str(vehicle_id))
            g.db.commit()
            flash("Vehicle added to this company.")
            return redirect(url_for("companies"))
        rows = g.db.execute("SELECT * FROM companies ORDER BY name").fetchall()
        fleet = []
        for company in rows:
            vehicles = g.db.execute(
                """SELECT v.number, v.model, v.vtype, v.seats, v.purchase_date, v.purchase_price, v.id,
                          ca.start_date, ca.end_date,
                          (SELECT d.name FROM driver_assignments da
                           JOIN drivers d ON d.id=da.driver_id
                           WHERE da.vehicle_id=v.id AND da.end_date IS NULL
                           ORDER BY da.start_date DESC LIMIT 1) AS driver_name
                   FROM company_assignments ca
                   JOIN vehicles v ON v.id=ca.vehicle_id
                   WHERE ca.company_id=?
                   ORDER BY ca.end_date IS NOT NULL, v.number""",
                (company["id"],),
            ).fetchall()
            trips = g.db.execute(
                "SELECT * FROM oncall_bookings WHERE company_id=? ORDER BY trip_date DESC, id DESC",
                (company["id"],),
            ).fetchall()
            fleet.append({
                "company": company,
                "vehicles": vehicles,
                "trips": trips,
                "bill_total": sum(row["total_amount"] for row in trips),
            })
        all_vehicles = g.db.execute("SELECT id, number, model FROM vehicles ORDER BY number").fetchall()
        return render_template("companies.html", fleet=fleet, all_vehicles=all_vehicles, today=date.today().isoformat())

    @app.route("/oncall", methods=["GET", "POST"])
    @roles("ADMIN", "MANAGER")
    def oncall():
        companies = g.db.execute("SELECT * FROM companies ORDER BY name").fetchall()
        trip = request.values.get("trip", "")
        company_id = request.values.get("company_id", type=int)
        company = None
        if trip == "company" and company_id:
            company = g.db.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if request.method == "POST":
            if trip == "company" and not company:
                flash("Select a company for this trip.")
                return redirect(url_for("oncall", trip="company"))
            if trip not in {"company", "direct"}:
                flash("Choose Company or Direct first.")
                return redirect(url_for("oncall"))
            vehicle = g.db.execute("SELECT * FROM vehicles WHERE id=?", (int(request.form["vehicle_id"]),)).fetchone()
            driver = g.db.execute("SELECT * FROM drivers WHERE id=?", (int(request.form["driver_id"]),)).fetchone()
            if not vehicle or not driver:
                abort(404)
            open_km = float(request.form["open_km"])
            close_km = float(request.form["close_km"])
            back = url_for("oncall", trip=trip, company_id=company["id"] if company else None)
            if close_km < open_km:
                flash("Closing KM cannot be less than Open KM.")
                return redirect(back)
            def amount(name):
                return round(float(request.form.get(name) or 0), 2)
            charges = {
                "extra_hour": amount("extra_hour"),
                "extra_km": amount("extra_km"),
                "check_post": amount("check_post"),
                "toll_fees": amount("toll_fees"),
                "parking": amount("parking"),
                "waiting": amount("waiting"),
                "bata": amount("bata"),
            }
            total_amount = round(sum(charges.values()), 2)
            g.db.execute(
                """INSERT INTO oncall_bookings (
                     trip_kind, trip_date, company_id, vehicle_id, vehicle_number, driver_id, driver_name, booked_name,
                     pickup_place, drop_place, open_time, open_km, close_time, close_km, total_km,
                     extra_hour, extra_km, check_post, toll_fees, parking, waiting, bata,
                     total_amount, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "COMPANY" if company else "DIRECT",
                    request.form["trip_date"],
                    company["id"] if company else None,
                    vehicle["id"], vehicle["number"], driver["id"], driver["name"],
                    request.form["booked_name"].strip(),
                    request.form["pickup_place"].strip(), request.form["drop_place"].strip(),
                    request.form["open_time"], open_km, request.form["close_time"], close_km,
                    round(close_km - open_km, 1),
                    charges["extra_hour"], charges["extra_km"], charges["check_post"], charges["toll_fees"],
                    charges["parking"], charges["waiting"], charges["bata"],
                    total_amount, datetime.now().isoformat(timespec="seconds"),
                ),
            )
            audit(g.db, g.user["id"], "CREATE", "oncall", company["id"] if company else None, request.form["booked_name"])
            g.db.commit()
            flash("On call booking saved.")
            return redirect(back)
        ready = trip == "direct" or company is not None
        vehicles = []
        bookings = []
        if ready and company:
            vehicles = g.db.execute(
                """SELECT v.id, v.number, v.model FROM vehicles v
                   JOIN company_assignments ca ON ca.vehicle_id=v.id
                   WHERE ca.company_id=? AND ca.end_date IS NULL
                   ORDER BY v.number""",
                (company["id"],),
            ).fetchall()
            if not vehicles:
                vehicles = g.db.execute("SELECT id, number, model FROM vehicles ORDER BY number").fetchall()
            bookings = g.db.execute(
                "SELECT * FROM oncall_bookings WHERE company_id=? ORDER BY trip_date DESC, id DESC",
                (company["id"],),
            ).fetchall()
        elif ready:
            vehicles = g.db.execute("SELECT id, number, model FROM vehicles ORDER BY number").fetchall()
            bookings = g.db.execute(
                "SELECT * FROM oncall_bookings WHERE trip_kind='DIRECT' ORDER BY trip_date DESC, id DESC"
            ).fetchall()
        drivers = g.db.execute("SELECT id, name FROM drivers ORDER BY name").fetchall()
        return render_template(
            "oncall.html",
            companies=companies,
            company=company,
            trip=trip,
            ready=ready,
            vehicles=vehicles,
            drivers=drivers,
            bookings=bookings,
            today=date.today().isoformat(),
        )

    @app.route("/fuel", methods=["GET", "POST"])
    @login_required
    def fuel():
        if request.method == "POST":
            vehicle = g.db.execute("SELECT * FROM vehicles WHERE id=?", (int(request.form["vehicle_id"]),)).fetchone()
            if not vehicle:
                abort(404)
            if g.user["role"] == "DRIVER":
                own = assigned_vehicle(g.user["driver_id"])
                if not own or own["id"] != vehicle["id"]:
                    abort(403)
            litres = float(request.form["litres"])
            cost = float(request.form["cost"])
            km = float(request.form["km_reading"])
            entry_date = request.form["entry_date"]
            prev = g.db.execute(
                "SELECT km_reading FROM fuel_entries WHERE vehicle_id=? AND km_reading<? ORDER BY km_reading DESC LIMIT 1",
                (vehicle["id"], km),
            ).fetchone()
            g.db.execute(
                "INSERT INTO fuel_entries (vehicle_id, entry_date, litres, cost, km_reading, created_by) VALUES (?,?,?,?,?,?)",
                (vehicle["id"], entry_date, litres, cost, km, g.user["id"]),
            )
            if prev:
                record_mileage(vehicle, entry_date, prev["km_reading"], km, litres, "From fuel entry", g.user["id"])
            audit(g.db, g.user["id"], "CREATE", "fuel", vehicle["id"], str(litres))
            g.db.commit()
            flash("Fuel entry saved.")
            return redirect(request.referrer or url_for("fuel"))
        vehicle_id = request.args.get("vehicle_id", "")
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        sql = """SELECT f.*, v.number FROM fuel_entries f JOIN vehicles v ON v.id=f.vehicle_id WHERE 1=1"""
        args = []
        if g.user["role"] == "DRIVER":
            own = assigned_vehicle(g.user["driver_id"])
            sql += " AND f.vehicle_id=?"
            args.append(own["id"] if own else -1)
        elif vehicle_id:
            sql += " AND f.vehicle_id=?"
            args.append(vehicle_id)
        if start:
            sql += " AND f.entry_date>=?"
            args.append(start)
        if end:
            sql += " AND f.entry_date<=?"
            args.append(end)
        sql += " ORDER BY f.entry_date DESC"
        rows = g.db.execute(sql, args).fetchall()
        vehicles = g.db.execute("SELECT id, number, model FROM vehicles ORDER BY number").fetchall()
        return render_template("fuel.html", rows=rows, vehicles=vehicles, vehicle_id=vehicle_id, start=start, end=end, today=date.today().isoformat())

    @app.route("/service", methods=["GET", "POST"])
    @roles("ADMIN", "MANAGER")
    def service_entry():
        if request.method == "POST":
            vehicle = g.db.execute(
                "SELECT id FROM vehicles WHERE id=?", (int(request.form["vehicle_id"]),)
            ).fetchone()
            if not vehicle:
                abort(404)
            service_type = request.form.get("service_type", "").strip()
            service_date = request.form.get("service_date", "").strip()
            if not service_type or not service_date:
                flash("Enter the service type and date.")
                return redirect(url_for("service_entry"))
            km = request.form.get("km_reading", "").strip()
            g.db.execute(
                """INSERT INTO services (vehicle_id, service_type, cost, service_date, km_reading, notes)
                   VALUES (?,?,?,?,?,?)""",
                (
                    vehicle["id"],
                    service_type,
                    float(request.form.get("cost") or 0),
                    service_date,
                    float(km) if km else None,
                    request.form.get("notes", "").strip(),
                ),
            )
            audit(g.db, g.user["id"], "CREATE", "service", vehicle["id"], service_type)
            g.db.commit()
            flash("Service saved.")
            return redirect(url_for("service_entry", vehicle_id=vehicle["id"]))
        vehicle_id = request.args.get("vehicle_id", "")
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        sql = """SELECT s.*, v.number FROM services s JOIN vehicles v ON v.id=s.vehicle_id WHERE 1=1"""
        args = []
        if vehicle_id:
            sql += " AND s.vehicle_id=?"
            args.append(vehicle_id)
        if start:
            sql += " AND s.service_date>=?"
            args.append(start)
        if end:
            sql += " AND s.service_date<=?"
            args.append(end)
        sql += " ORDER BY s.service_date DESC"
        rows = g.db.execute(sql, args).fetchall()
        vehicles = g.db.execute("SELECT id, number, model FROM vehicles ORDER BY number").fetchall()
        return render_template(
            "service.html",
            rows=rows,
            vehicles=vehicles,
            vehicle_id=vehicle_id,
            start=start,
            end=end,
            today=date.today().isoformat(),
        )

    @app.route("/maintenance")
    @login_required
    def maintenance():
        if g.user["role"] == "DRIVER":
            own = assigned_vehicle(g.user["driver_id"])
            if not own:
                return render_template("maintenance.html", cards=[])
            return redirect(url_for("vehicle_detail", vehicle_id=own["id"]))
        cards = []
        for vehicle in g.db.execute("SELECT * FROM vehicles ORDER BY number"):
            latest_km = g.db.execute(
                "SELECT MAX(km_reading) km FROM fuel_entries WHERE vehicle_id=?", (vehicle["id"],)
            ).fetchone()["km"] or 0
            last = g.db.execute(
                "SELECT * FROM services WHERE vehicle_id=? ORDER BY service_date DESC LIMIT 1",
                (vehicle["id"],),
            ).fetchone()
            status = "ok"
            detail = "No service yet"
            if last:
                km_since = latest_km - (last["km_reading"] or 0)
                days_since = (date.today() - date.fromisoformat(last["service_date"])).days
                detail = f"{last['service_type']} · {km_since:.0f} km · {days_since} days"
                if km_since >= vehicle["service_interval_km"] or days_since >= vehicle["service_interval_days"]:
                    status = "overdue"
                elif km_since >= vehicle["service_interval_km"] - 500 or days_since >= vehicle["service_interval_days"] - 7:
                    status = "soon"
            cards.append({"vehicle": vehicle, "status": status, "detail": detail, "latest_km": latest_km})
        return render_template("maintenance.html", cards=cards)

    @app.route("/mileage")
    @roles("ADMIN", "MANAGER")
    def mileage():
        start = request.args.get("start", month_start())
        end = request.args.get("end", date.today().isoformat())
        rates = {row["vtype"]: row["per_km"] for row in g.db.execute("SELECT * FROM billing_rates")}
        rows = []
        totals = {"km": 0, "revenue": 0, "fuel": 0, "maintenance": 0, "profit": 0}
        for vehicle in g.db.execute("SELECT * FROM vehicles ORDER BY number"):
            stats = g.db.execute(
                """SELECT COALESCE(SUM(end_km-start_km),0) km,
                          COALESCE(SUM(fuel_litres),0) litres,
                          COALESCE(SUM(CASE WHEN fuel_litres>0 THEN (end_km-start_km) END),0) alert_km
                   FROM mileage_logs WHERE vehicle_id=? AND log_date BETWEEN ? AND ?""",
                (vehicle["id"], start, end),
            ).fetchone()
            flags = g.db.execute(
                """SELECT
                     SUM(CASE WHEN alert='LOW' THEN 1 ELSE 0 END) low,
                     SUM(CASE WHEN alert='HIGH' THEN 1 ELSE 0 END) high
                   FROM mileage_logs WHERE vehicle_id=? AND log_date BETWEEN ? AND ?""",
                (vehicle["id"], start, end),
            ).fetchone()
            fuel_cost = g.db.execute(
                "SELECT COALESCE(SUM(cost),0) s FROM fuel_entries WHERE vehicle_id=? AND entry_date BETWEEN ? AND ?",
                (vehicle["id"], start, end),
            ).fetchone()["s"]
            maint_cost = g.db.execute(
                "SELECT COALESCE(SUM(cost),0) s FROM services WHERE vehicle_id=? AND service_date BETWEEN ? AND ?",
                (vehicle["id"], start, end),
            ).fetchone()["s"]
            km = stats["km"] or 0
            litres = stats["litres"] or 0
            kmpl = (km / litres) if litres else 0
            revenue = km * rates.get(vehicle["vtype"], 0)
            profit = revenue - fuel_cost - maint_cost
            company = g.db.execute(
                """SELECT c.name FROM company_assignments ca JOIN companies c ON c.id=ca.company_id
                   WHERE ca.vehicle_id=? AND ca.end_date IS NULL LIMIT 1""",
                (vehicle["id"],),
            ).fetchone()
            rows.append({
                "vehicle": vehicle,
                "company": company["name"] if company else "—",
                "km": km,
                "kmpl": kmpl,
                "revenue": revenue,
                "fuel": fuel_cost,
                "maintenance": maint_cost,
                "profit": profit,
                "low": flags["low"] or 0,
                "high": flags["high"] or 0,
            })
            totals["km"] += km
            totals["revenue"] += revenue
            totals["fuel"] += fuel_cost
            totals["maintenance"] += maint_cost
            totals["profit"] += profit
        return render_template("mileage.html", rows=rows, totals=totals, start=start, end=end, rates=rates)

    @app.route("/reports", methods=["GET", "POST"])
    @roles("ADMIN", "MANAGER")
    def reports():
        if request.method == "POST":
            vehicle = g.db.execute("SELECT * FROM vehicles WHERE id=?", (int(request.form["vehicle_id"]),)).fetchone()
            start = request.form["period_start"]
            end = request.form["period_end"]
            basis = request.form["basis"]
            rate = float(request.form["rate"])
            km = g.db.execute(
                """SELECT COALESCE(SUM(end_km-start_km),0) s FROM mileage_logs
                   WHERE vehicle_id=? AND log_date>=? AND log_date<=?""",
                (vehicle["id"], start, end),
            ).fetchone()["s"]
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            amount = rate * (km if basis == "KM" else days)
            g.db.execute(
                """INSERT INTO invoices
                (company_id, vehicle_id, period_start, period_end, basis, rate, km_driven, days, amount, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(request.form["company_id"]),
                    vehicle["id"],
                    start,
                    end,
                    basis,
                    rate,
                    km,
                    days,
                    round(amount, 2),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            audit(g.db, g.user["id"], "CREATE", "invoice", vehicle["id"], f"{amount:.2f}")
            g.db.commit()
            flash("Invoice created.")
            return redirect(url_for("reports"))
        start = request.args.get("start", month_start())
        end = request.args.get("end", date.today().isoformat())
        vehicle_expenses = g.db.execute(
            """SELECT v.number, v.model,
               COALESCE((SELECT SUM(cost) FROM fuel_entries f WHERE f.vehicle_id=v.id AND f.entry_date BETWEEN ? AND ?),0) fuel,
               COALESCE((SELECT SUM(cost) FROM services s WHERE s.vehicle_id=v.id AND s.service_date BETWEEN ? AND ?),0) maintenance
               FROM vehicles v ORDER BY v.number""",
            (start, end, start, end),
        ).fetchall()
        company_km = g.db.execute(
            """SELECT c.name,
               COALESCE(SUM(m.end_km-m.start_km),0) km
               FROM companies c
               LEFT JOIN company_assignments ca ON ca.company_id=c.id
               LEFT JOIN mileage_logs m ON m.vehicle_id=ca.vehicle_id
                 AND m.log_date BETWEEN ? AND ?
                 AND m.log_date>=ca.start_date
                 AND (ca.end_date IS NULL OR m.log_date<=ca.end_date)
               GROUP BY c.id ORDER BY c.name""",
            (start, end),
        ).fetchall()
        salaries = []
        for driver in g.db.execute("SELECT * FROM drivers ORDER BY name"):
            adv = g.db.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM advances WHERE driver_id=? AND paid_on BETWEEN ? AND ?",
                (driver["id"], start, end),
            ).fetchone()["s"]
            salaries.append({"name": driver["name"], "salary": driver["monthly_salary"], "advance": adv, "remaining": driver["monthly_salary"] - adv})
        fuel_cost = g.db.execute(
            "SELECT COALESCE(SUM(cost),0) s FROM fuel_entries WHERE entry_date BETWEEN ? AND ?", (start, end)
        ).fetchone()["s"]
        maint_cost = g.db.execute(
            "SELECT COALESCE(SUM(cost),0) s FROM services WHERE service_date BETWEEN ? AND ?", (start, end)
        ).fetchone()["s"]
        salary_cost = sum(row["salary"] for row in salaries)
        revenue = g.db.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM invoices WHERE period_start>=? AND period_end<=?",
            (start, end),
        ).fetchone()["s"]
        pnl = {
            "revenue": revenue,
            "fuel": fuel_cost,
            "maintenance": maint_cost,
            "salary": salary_cost,
            "profit": revenue - fuel_cost - maint_cost - salary_cost,
        }
        invoices = g.db.execute(
            """SELECT i.*, c.name AS company, v.number FROM invoices i
               JOIN companies c ON c.id=i.company_id JOIN vehicles v ON v.id=i.vehicle_id
               ORDER BY i.id DESC"""
        ).fetchall()
        companies = g.db.execute("SELECT * FROM companies ORDER BY name").fetchall()
        vehicles = g.db.execute("SELECT * FROM vehicles ORDER BY number").fetchall()
        rates = {r["vtype"]: r for r in g.db.execute("SELECT * FROM billing_rates")}
        return render_template(
            "reports.html",
            start=start,
            end=end,
            vehicle_expenses=vehicle_expenses,
            company_km=company_km,
            salaries=salaries,
            pnl=pnl,
            invoices=invoices,
            companies=companies,
            vehicles=vehicles,
            rates=rates,
        )

    @app.route("/invoices/<int:invoice_id>.csv")
    @roles("ADMIN", "MANAGER")
    def invoice_csv(invoice_id):
        inv = _invoice(invoice_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Invoice", inv["id"], "Company", inv["company"], "Vehicle", inv["number"]])
        writer.writerow(["Period", inv["period_start"], inv["period_end"], "Basis", inv["basis"]])
        writer.writerow(["Rate", inv["rate"], "KM", inv["km_driven"], "Days", inv["days"], "Amount", inv["amount"]])
        data = io.BytesIO(buf.getvalue().encode())
        return send_file(data, as_attachment=True, download_name=f"invoice-{inv['id']}.csv", mimetype="text/csv")

    @app.route("/invoices/<int:invoice_id>.xlsx")
    @roles("ADMIN", "MANAGER")
    def invoice_xlsx(invoice_id):
        inv = _invoice(invoice_id)
        rows = [
            ["Field", "Value"],
            ["Invoice", inv["id"]],
            ["Company", inv["company"]],
            ["Vehicle", inv["number"]],
            ["Period start", inv["period_start"]],
            ["Period end", inv["period_end"]],
            ["Basis", inv["basis"]],
            ["Rate", inv["rate"]],
            ["KM driven", inv["km_driven"]],
            ["Days", inv["days"]],
            ["Amount", inv["amount"]],
        ]
        data = io.BytesIO()
        _xlsx(data, rows)
        data.seek(0)
        return send_file(
            data,
            as_attachment=True,
            download_name=f"invoice-{inv['id']}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/invoices/<int:invoice_id>.pdf")
    @roles("ADMIN", "MANAGER")
    def invoice_pdf(invoice_id):
        inv = _invoice(invoice_id)
        lines = [
            "Janani",
            f"Invoice #{inv['id']}",
            f"Company: {inv['company']}",
            f"Vehicle: {inv['number']} ({inv['model']})",
            f"Period: {inv['period_start']} to {inv['period_end']}",
            f"Basis: {inv['basis']} at {inv['rate']}",
            f"KM driven: {inv['km_driven']}",
            f"Days: {inv['days']}",
            f"Amount: INR {inv['amount']}",
        ]
        data = io.BytesIO(_simple_pdf(lines))
        return send_file(data, as_attachment=True, download_name=f"invoice-{inv['id']}.pdf", mimetype="application/pdf")

    def _invoice(invoice_id):
        inv = g.db.execute(
            """SELECT i.*, c.name AS company, v.number, v.model FROM invoices i
               JOIN companies c ON c.id=i.company_id JOIN vehicles v ON v.id=i.vehicle_id
               WHERE i.id=?""",
            (invoice_id,),
        ).fetchone()
        if not inv:
            abort(404)
        return inv

    @app.route("/notifications")
    @login_required
    def notifications():
        rows = g.db.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC",
            (g.user["id"],),
        ).fetchall()
        g.db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],))
        g.db.commit()
        return render_template("notifications.html", rows=rows)

    @app.route("/audit")
    @roles("ADMIN")
    def audit_page():
        rows = g.db.execute(
            """SELECT a.*, u.name AS user_name FROM audit_logs a
               LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 200"""
        ).fetchall()
        return render_template("audit.html", rows=rows)

    @app.route("/search")
    @login_required
    def search():
        q = request.args.get("q", "").strip()
        if not q:
            return redirect(url_for("driver_home" if g.user["role"] == "DRIVER" else "dashboard"))
        drivers = []
        vehicles = []
        if q:
            if g.user["role"] == "DRIVER":
                drivers = g.db.execute(
                    "SELECT * FROM drivers WHERE id=? AND (name LIKE ? OR phone LIKE ?)",
                    (g.user["driver_id"], f"%{q}%", f"%{q}%"),
                ).fetchall()
                own = assigned_vehicle(g.user["driver_id"])
                if own and (q.lower() in own["number"].lower() or q.lower() in own["model"].lower()):
                    vehicles = [own]
            else:
                drivers = g.db.execute(
                    "SELECT * FROM drivers WHERE name LIKE ? OR phone LIKE ? OR dl_number LIKE ? OR aadhaar LIKE ?",
                    (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"),
                ).fetchall()
                vehicles = g.db.execute(
                    "SELECT * FROM vehicles WHERE number LIKE ? OR model LIKE ?",
                    (f"%{q}%", f"%{q}%"),
                ).fetchall()
        return render_template("search.html", q=q, drivers=drivers, vehicles=vehicles)

    @app.route("/m", methods=["GET"])
    @roles("DRIVER")
    def driver_home():
        driver = g.db.execute("SELECT * FROM drivers WHERE id=?", (g.user["driver_id"],)).fetchone()
        vehicle = assigned_vehicle(g.user["driver_id"])
        notes = g.db.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 8",
            (g.user["id"],),
        ).fetchall()
        fuels = []
        services = []
        if vehicle:
            fuels = g.db.execute(
                "SELECT * FROM fuel_entries WHERE vehicle_id=? ORDER BY entry_date DESC LIMIT 5",
                (vehicle["id"],),
            ).fetchall()
            services = g.db.execute(
                "SELECT * FROM services WHERE vehicle_id=? ORDER BY service_date DESC LIMIT 5",
                (vehicle["id"],),
            ).fetchall()
        return render_template(
            "driver_home.html",
            driver=driver,
            vehicle=vehicle,
            notes=notes,
            fuels=fuels,
            services=services,
            summary=salary_summary(driver),
            today=date.today().isoformat(),
        )

    @app.route("/files/<path:name>")
    @login_required
    def files(name):
        path = (UPLOAD_DIR / name).resolve()
        if UPLOAD_DIR.resolve() not in path.parents and path != UPLOAD_DIR.resolve():
            abort(404)
        if not path.exists():
            abort(404)
        return send_file(path)

    @app.route("/api/auth/otp", methods=["POST"])
    def api_otp():
        phone = (request.json or {}).get("phone", "").strip()
        user = g.db.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if not user:
            return jsonify({"error": "Unknown phone"}), 404
        code = f"{random.randint(0, 999999):06d}"
        expires = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
        g.db.execute("INSERT INTO otp_codes (phone, code, expires_at) VALUES (?,?,?)", (phone, code, expires))
        g.db.commit()
        return jsonify({"ok": True, "dev_otp": code, "expires_at": expires})

    @app.route("/api/auth/verify", methods=["POST"])
    def api_verify():
        body = request.json or {}
        phone = body.get("phone", "").strip()
        code = body.get("code", "").strip()
        user = g.db.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        row = g.db.execute(
            "SELECT * FROM otp_codes WHERE phone=? AND code=? AND used=0 ORDER BY id DESC LIMIT 1",
            (phone, code),
        ).fetchone()
        if not user or not row or row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
            return jsonify({"error": "Invalid OTP"}), 401
        g.db.execute("UPDATE otp_codes SET used=1 WHERE id=?", (row["id"],))
        g.db.commit()
        session["uid"] = user["id"]
        return jsonify({"ok": True, "role": user["role"], "name": user["name"]})

    @app.route("/api/me")
    @login_required
    def api_me():
        payload = {"id": g.user["id"], "name": g.user["name"], "phone": g.user["phone"], "role": g.user["role"]}
        if g.user["role"] == "DRIVER":
            vehicle = assigned_vehicle(g.user["driver_id"])
            payload["vehicle"] = dict(vehicle) if vehicle else None
        return jsonify(payload)

    @app.route("/api/fuel", methods=["POST"])
    @roles("DRIVER", "ADMIN", "MANAGER")
    def api_fuel():
        body = request.json or {}
        vehicle_id = int(body["vehicle_id"])
        if g.user["role"] == "DRIVER":
            own = assigned_vehicle(g.user["driver_id"])
            if not own or own["id"] != vehicle_id:
                abort(403)
        vehicle = g.db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        litres = float(body["litres"])
        km = float(body["km_reading"])
        prev = g.db.execute(
            "SELECT km_reading FROM fuel_entries WHERE vehicle_id=? AND km_reading<? ORDER BY km_reading DESC LIMIT 1",
            (vehicle_id, km),
        ).fetchone()
        g.db.execute(
            "INSERT INTO fuel_entries (vehicle_id, entry_date, litres, cost, km_reading, created_by) VALUES (?,?,?,?,?,?)",
            (vehicle_id, body.get("entry_date") or date.today().isoformat(), litres, float(body.get("cost") or 0), km, g.user["id"]),
        )
        if prev:
            record_mileage(vehicle, body.get("entry_date") or date.today().isoformat(), prev["km_reading"], km, litres, "API", g.user["id"])
        g.db.commit()
        return jsonify({"ok": True})

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", message="You do not have access to that page."), 403

    return app


def _xlsx(buf, rows):
    sheet = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    sheet.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
    for r_idx, row in enumerate(rows, start=1):
        sheet.append(f'<row r="{r_idx}">')
        for c_idx, value in enumerate(row, start=1):
            col = chr(64 + c_idx)
            text = str(value).replace("&", "&amp;").replace("<", "&lt;")
            sheet.append(f'<c r="{col}{r_idx}" t="inlineStr"><is><t>{text}</t></is></c>')
        sheet.append("</row>")
    sheet.append("</sheetData></worksheet>")
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Invoice" sheetId="1" r:id="rId1"/></sheets></workbook>"""
    wb_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", "".join(sheet))


def _simple_pdf(lines):
    text = ["BT /F1 12 Tf 50 800 Td 16 TL"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        text.append(f"({safe}) Tj T*")
    text.append("ET")
    stream = "\n".join(text).encode()
    objects = []
    objects.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
    objects.append(b"2 0 obj<< /Type /Pages /Count 1 /Kids [3 0 R] >>endobj\n")
    objects.append(b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n")
    objects.append(f"4 0 obj<< /Length {len(stream)} >>stream\n".encode() + stream + b"\nendstream endobj\n")
    objects.append(b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(out)


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
