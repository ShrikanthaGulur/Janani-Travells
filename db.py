import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "tms.db"
UPLOAD_DIR = ROOT / "data" / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  phone TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('ADMIN','MANAGER','DRIVER')),
  driver_id INTEGER
);
CREATE TABLE IF NOT EXISTS otp_codes (
  id INTEGER PRIMARY KEY,
  phone TEXT NOT NULL,
  code TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drivers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT UNIQUE NOT NULL,
  address TEXT NOT NULL DEFAULT '',
  dl_number TEXT NOT NULL DEFAULT '',
  dl_expiry TEXT,
  dl_copy TEXT,
  monthly_salary REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS monthly_salaries (
  id INTEGER PRIMARY KEY,
  driver_id INTEGER NOT NULL REFERENCES drivers(id),
  year_month TEXT NOT NULL,
  amount REAL NOT NULL,
  UNIQUE(driver_id, year_month)
);
CREATE TABLE IF NOT EXISTS advances (
  id INTEGER PRIMARY KEY,
  driver_id INTEGER NOT NULL REFERENCES drivers(id),
  amount REAL NOT NULL,
  paid_on TEXT NOT NULL,
  note TEXT DEFAULT '',
  payment_mode TEXT NOT NULL DEFAULT 'Cash',
  transaction_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attendance (
  id INTEGER PRIMARY KEY,
  driver_id INTEGER NOT NULL REFERENCES drivers(id),
  work_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PRESENT','ABSENT','LEAVE','HALF')),
  check_in TEXT,
  check_out TEXT,
  note TEXT DEFAULT '',
  UNIQUE(driver_id, work_date)
);
CREATE TABLE IF NOT EXISTS vehicles (
  id INTEGER PRIMARY KEY,
  number TEXT UNIQUE NOT NULL,
  model TEXT NOT NULL,
  vtype TEXT NOT NULL CHECK(vtype IN ('COMMERCIAL','PASSENGER')),
  seats INTEGER NOT NULL DEFAULT 0,
  purchase_date TEXT,
  purchase_price REAL NOT NULL DEFAULT 0,
  min_mileage REAL NOT NULL DEFAULT 4,
  max_mileage REAL NOT NULL DEFAULT 18,
  service_interval_km REAL NOT NULL DEFAULT 5000,
  service_interval_days INTEGER NOT NULL DEFAULT 90
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  kind TEXT NOT NULL,
  expiry_date TEXT,
  file_path TEXT
);
CREATE TABLE IF NOT EXISTS vehicle_photos (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  file_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  contact TEXT DEFAULT '',
  address TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS company_assignments (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  company_id INTEGER NOT NULL REFERENCES companies(id),
  start_date TEXT NOT NULL,
  end_date TEXT
);
CREATE TABLE IF NOT EXISTS driver_assignments (
  id INTEGER PRIMARY KEY,
  driver_id INTEGER NOT NULL REFERENCES drivers(id),
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  start_date TEXT NOT NULL,
  end_date TEXT
);
CREATE TABLE IF NOT EXISTS mileage_logs (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  log_date TEXT NOT NULL,
  start_km REAL NOT NULL,
  end_km REAL NOT NULL,
  fuel_litres REAL NOT NULL,
  kmpl REAL,
  alert TEXT,
  note TEXT DEFAULT '',
  created_by INTEGER
);
CREATE TABLE IF NOT EXISTS fuel_entries (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  entry_date TEXT NOT NULL,
  litres REAL NOT NULL,
  cost REAL NOT NULL,
  km_reading REAL NOT NULL,
  created_by INTEGER
);
CREATE TABLE IF NOT EXISTS services (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  service_type TEXT NOT NULL,
  cost REAL NOT NULL DEFAULT 0,
  service_date TEXT NOT NULL,
  km_reading REAL,
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tyres (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  tyre_number TEXT NOT NULL,
  position TEXT NOT NULL,
  installed_on TEXT NOT NULL,
  removed_on TEXT,
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS accessories (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  name TEXT NOT NULL,
  present INTEGER NOT NULL DEFAULT 0,
  checked_on TEXT
);
CREATE TABLE IF NOT EXISTS handovers (
  id INTEGER PRIMARY KEY,
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  driver_id INTEGER NOT NULL REFERENCES drivers(id),
  taken_on TEXT NOT NULL,
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS handover_items (
  id INTEGER PRIMARY KEY,
  handover_id INTEGER NOT NULL REFERENCES handovers(id),
  name TEXT NOT NULL,
  checked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checklist_items (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  built_in INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS billing_rates (
  vtype TEXT PRIMARY KEY,
  per_km REAL NOT NULL,
  per_day REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  basis TEXT NOT NULL,
  rate REAL NOT NULL,
  km_driven REAL NOT NULL,
  days INTEGER NOT NULL,
  amount REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  kind TEXT NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY,
  user_id INTEGER,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT,
  detail TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
"""

ACCESSORIES = ["Stepney", "Spanner", "Jack", "Jack Rod", "Music System"]


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(vehicles)")]
    if "seats" not in columns:
        conn.execute("ALTER TABLE vehicles ADD COLUMN seats INTEGER NOT NULL DEFAULT 0")
    advance_columns = [row[1] for row in conn.execute("PRAGMA table_info(advances)")]
    if "payment_mode" not in advance_columns:
        conn.execute("ALTER TABLE advances ADD COLUMN payment_mode TEXT NOT NULL DEFAULT 'Cash'")
    if "transaction_id" not in advance_columns:
        conn.execute("ALTER TABLE advances ADD COLUMN transaction_id TEXT DEFAULT ''")
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
        seed(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM checklist_items").fetchone()["c"] == 0:
        for name in (
            "Fasttag active - Sufficient balance",
            "Spare/extra tyre",
            "Jack",
            "Wheel spanner",
            "Jack rod/Handle",
            "Tow rope",
        ):
            conn.execute("INSERT INTO checklist_items (name, built_in) VALUES (?,1)", (name,))
    if conn.execute("SELECT COUNT(*) AS c FROM attendance").fetchone()["c"] == 0:
        today = date.today()
        for driver in conn.execute("SELECT id FROM drivers"):
            for offset, status in ((2, "PRESENT"), (1, "PRESENT"), (0, "PRESENT")):
                conn.execute(
                    "INSERT INTO attendance (driver_id, work_date, status, check_in, check_out) VALUES (?,?,?,?,?)",
                    (driver["id"], (today - timedelta(days=offset)).isoformat(), status, "08:00", "18:00"),
                )
    conn.commit()
    conn.close()


def seed(conn):
    today = date.today()
    drivers = [
        ("Ravi Kumar", "9000000011", "12 Depot Road, Bengaluru", "KA0120230001111", (today + timedelta(days=40)).isoformat(), 28000),
        ("Anita Desai", "9000000012", "44 Yard Lane, Mysuru", "KA0220240002222", (today + timedelta(days=12)).isoformat(), 26000),
    ]
    for d in drivers:
        conn.execute(
            "INSERT INTO drivers (name, phone, address, dl_number, dl_expiry, monthly_salary) VALUES (?,?,?,?,?,?)",
            d,
        )
    ravi = conn.execute("SELECT id FROM drivers WHERE phone='9000000011'").fetchone()["id"]
    anita = conn.execute("SELECT id FROM drivers WHERE phone='9000000012'").fetchone()["id"]
    conn.execute(
        "INSERT INTO advances (driver_id, amount, paid_on, note) VALUES (?,?,?,?)",
        (ravi, 5000, today.replace(day=1).isoformat(), "Festival advance"),
    )
    conn.execute(
        "INSERT INTO advances (driver_id, amount, paid_on, note) VALUES (?,?,?,?)",
        (anita, 2000, (today - timedelta(days=2)).isoformat(), "Fuel personal"),
    )
    for offset, ravi_status, anita_status in (
        (2, "PRESENT", "PRESENT"),
        (1, "PRESENT", "LEAVE"),
        (0, "PRESENT", "PRESENT"),
    ):
        day = (today - timedelta(days=offset)).isoformat()
        conn.execute(
            "INSERT INTO attendance (driver_id, work_date, status, check_in, check_out) VALUES (?,?,?,?,?)",
            (ravi, day, ravi_status, "08:00" if ravi_status == "PRESENT" else None, "18:00" if ravi_status == "PRESENT" else None),
        )
        conn.execute(
            "INSERT INTO attendance (driver_id, work_date, status, check_in, check_out) VALUES (?,?,?,?,?)",
            (anita, day, anita_status, "08:10" if anita_status == "PRESENT" else None, "17:40" if anita_status == "PRESENT" else None),
        )
    users = [
        ("9000000001", "Asha Admin", "ADMIN", None),
        ("9000000002", "Mohan Manager", "MANAGER", None),
        ("9000000011", "Ravi Kumar", "DRIVER", ravi),
        ("9000000012", "Anita Desai", "DRIVER", anita),
    ]
    conn.executemany("INSERT INTO users (phone, name, role, driver_id) VALUES (?,?,?,?)", users)
    conn.executemany(
        "INSERT INTO companies (name, contact, address) VALUES (?,?,?)",
        [
            ("Northline Logistics", "9845011111", "Peenya Industrial Area, Bengaluru"),
            ("Harbor Transit", "9845022222", "Bandar Road, Mangaluru"),
        ],
    )
    vehicles = [
        ("KA01AB1001", "Tata Ace", "COMMERCIAL", "2022-04-12", 650000, 8, 16, 5000, 90),
        ("KA02CD2002", "Ashok Leyland", "COMMERCIAL", "2021-11-03", 1450000, 3.5, 7, 8000, 120),
        ("KA03EF3003", "Force Traveller", "PASSENGER", "2023-01-20", 980000, 7, 14, 6000, 90),
    ]
    conn.executemany(
        """INSERT INTO vehicles
        (number, model, vtype, purchase_date, purchase_price, min_mileage, max_mileage, service_interval_km, service_interval_days)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        vehicles,
    )
    v1 = conn.execute("SELECT id FROM vehicles WHERE number='KA01AB1001'").fetchone()["id"]
    v2 = conn.execute("SELECT id FROM vehicles WHERE number='KA02CD2002'").fetchone()["id"]
    v3 = conn.execute("SELECT id FROM vehicles WHERE number='KA03EF3003'").fetchone()["id"]
    docs = [
        (v1, "RC", (today + timedelta(days=400)).isoformat()),
        (v1, "Insurance", (today + timedelta(days=18)).isoformat()),
        (v1, "FC", (today + timedelta(days=200)).isoformat()),
        (v1, "Permit", (today + timedelta(days=90)).isoformat()),
        (v1, "Pollution", (today + timedelta(days=6)).isoformat()),
        (v2, "Insurance", (today - timedelta(days=4)).isoformat()),
        (v2, "RC", (today + timedelta(days=300)).isoformat()),
        (v3, "Insurance", (today + timedelta(days=120)).isoformat()),
        (v3, "Permit", (today + timedelta(days=25)).isoformat()),
    ]
    conn.executemany("INSERT INTO documents (vehicle_id, kind, expiry_date) VALUES (?,?,?)", docs)
    for vid in (v1, v2, v3):
        for name in ACCESSORIES:
            conn.execute(
                "INSERT INTO accessories (vehicle_id, name, present, checked_on) VALUES (?,?,1,?)",
                (vid, name, today.isoformat()),
            )
    conn.execute(
        "INSERT INTO driver_assignments (driver_id, vehicle_id, start_date, end_date) VALUES (?,?,?,NULL)",
        (ravi, v1, (today - timedelta(days=60)).isoformat()),
    )
    conn.execute(
        "INSERT INTO driver_assignments (driver_id, vehicle_id, start_date, end_date) VALUES (?,?,?,?)",
        (anita, v2, (today - timedelta(days=200)).isoformat(), (today - timedelta(days=20)).isoformat()),
    )
    conn.execute(
        "INSERT INTO driver_assignments (driver_id, vehicle_id, start_date, end_date) VALUES (?,?,?,NULL)",
        (anita, v3, (today - timedelta(days=19)).isoformat()),
    )
    c1 = 1
    c2 = 2
    conn.execute(
        "INSERT INTO company_assignments (vehicle_id, company_id, start_date, end_date) VALUES (?,?,?,NULL)",
        (v1, c1, (today - timedelta(days=50)).isoformat()),
    )
    conn.execute(
        "INSERT INTO company_assignments (vehicle_id, company_id, start_date, end_date) VALUES (?,?,?,NULL)",
        (v3, c2, (today - timedelta(days=15)).isoformat()),
    )
    fuels = [
        (v1, (today - timedelta(days=10)).isoformat(), 28, 2688, 45200),
        (v1, (today - timedelta(days=3)).isoformat(), 30, 2910, 45580),
        (v2, (today - timedelta(days=8)).isoformat(), 80, 7760, 120400),
        (v3, (today - timedelta(days=2)).isoformat(), 40, 3880, 22110),
    ]
    for vehicle_id, entry_date, litres, cost, km in fuels:
        conn.execute(
            "INSERT INTO fuel_entries (vehicle_id, entry_date, litres, cost, km_reading) VALUES (?,?,?,?,?)",
            (vehicle_id, entry_date, litres, cost, km),
        )
    logs = [
        (v1, (today - timedelta(days=10)).isoformat(), 44880, 45200, 28, 11.43, None, "Refuel"),
        (v1, (today - timedelta(days=3)).isoformat(), 45200, 45580, 30, 12.67, None, "Refuel"),
        (v2, (today - timedelta(days=8)).isoformat(), 120050, 120400, 80, 4.38, None, "Refuel"),
        (v3, (today - timedelta(days=2)).isoformat(), 21750, 22110, 40, 9.0, None, "Refuel"),
    ]
    conn.executemany(
        """INSERT INTO mileage_logs
        (vehicle_id, log_date, start_km, end_km, fuel_litres, kmpl, alert, note)
        VALUES (?,?,?,?,?,?,?,?)""",
        logs,
    )
    conn.execute(
        "INSERT INTO services (vehicle_id, service_type, cost, service_date, km_reading, notes) VALUES (?,?,?,?,?,?)",
        (v1, "Oil Change", 3200, (today - timedelta(days=100)).isoformat(), 40000, "Engine oil and filter"),
    )
    conn.execute(
        "INSERT INTO services (vehicle_id, service_type, cost, service_date, km_reading, notes) VALUES (?,?,?,?,?,?)",
        (v2, "General Service", 8400, (today - timedelta(days=20)).isoformat(), 119000, "Brake check"),
    )
    conn.execute(
        "INSERT INTO tyres (vehicle_id, tyre_number, position, installed_on, notes) VALUES (?,?,?,?,?)",
        (v1, "TY-1001", "Front Left", (today - timedelta(days=200)).isoformat(), "Current"),
    )
    conn.executemany(
        "INSERT INTO billing_rates (vtype, per_km, per_day) VALUES (?,?,?)",
        [("COMMERCIAL", 18, 2500), ("PASSENGER", 22, 3200)],
    )


def audit(conn, user_id, action, entity, entity_id, detail):
    conn.execute(
        "INSERT INTO audit_logs (user_id, action, entity, entity_id, detail, created_at) VALUES (?,?,?,?,?,?)",
        (user_id, action, entity, str(entity_id) if entity_id is not None else "", detail, datetime.now().isoformat(timespec="seconds")),
    )


def refresh_notifications(conn):
    today = date.today()
    staff = conn.execute("SELECT id FROM users WHERE role IN ('ADMIN','MANAGER')").fetchall()
    docs = conn.execute(
        """SELECT d.kind, d.expiry_date, v.number
           FROM documents d JOIN vehicles v ON v.id = d.vehicle_id
           WHERE d.expiry_date IS NOT NULL AND d.expiry_date <= ?""",
        ((today + timedelta(days=30)).isoformat(),),
    ).fetchall()
    for doc in docs:
        expiry = date.fromisoformat(doc["expiry_date"])
        days = (expiry - today).days
        title = f"{doc['kind']} expiry · {doc['number']}"
        if days < 0:
            body = f"{doc['kind']} for {doc['number']} expired {abs(days)} days ago."
        else:
            body = f"{doc['kind']} for {doc['number']} expires in {days} days ({doc['expiry_date']})."
        _notify_once(conn, staff, "DOCUMENT", title, body)
    vehicles = conn.execute("SELECT * FROM vehicles").fetchall()
    for vehicle in vehicles:
        latest_km = conn.execute(
            "SELECT MAX(km_reading) AS km FROM fuel_entries WHERE vehicle_id=?",
            (vehicle["id"],),
        ).fetchone()["km"] or 0
        last = conn.execute(
            "SELECT * FROM services WHERE vehicle_id=? ORDER BY service_date DESC LIMIT 1",
            (vehicle["id"],),
        ).fetchone()
        if not last:
            title = f"Service due · {vehicle['number']}"
            body = f"{vehicle['number']} has no service record."
            _notify_once(conn, staff, "MAINTENANCE", title, body)
            continue
        km_since = (latest_km or 0) - (last["km_reading"] or 0)
        days_since = (today - date.fromisoformat(last["service_date"])).days
        if km_since >= vehicle["service_interval_km"] or days_since >= vehicle["service_interval_days"]:
            title = f"Overdue service · {vehicle['number']}"
            body = f"{km_since:.0f} km and {days_since} days since {last['service_type']}."
            _notify_once(conn, staff, "MAINTENANCE", title, body)
        elif km_since >= vehicle["service_interval_km"] - 500 or days_since >= vehicle["service_interval_days"] - 7:
            title = f"Upcoming service · {vehicle['number']}"
            body = f"Service window is close: {km_since:.0f} km / {days_since} days since last service."
            _notify_once(conn, staff, "MAINTENANCE", title, body)
    if today.day >= 27:
        drivers = conn.execute("SELECT * FROM drivers").fetchall()
        month_start = today.replace(day=1).isoformat()
        for driver in drivers:
            taken = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM advances WHERE driver_id=? AND paid_on>=?",
                (driver["id"], month_start),
            ).fetchone()["s"]
            remaining = driver["monthly_salary"] - taken
            title = f"Salary reminder · {driver['name']}"
            body = f"Remaining payable this month is ₹{remaining:,.0f}."
            _notify_once(conn, staff, "SALARY", title, body)


def _notify_once(conn, users, kind, title, body):
    now = datetime.now().isoformat(timespec="seconds")
    for user in users:
        exists = conn.execute(
            "SELECT id FROM notifications WHERE user_id=? AND title=? AND created_at>=?",
            (user["id"], title, date.today().isoformat()),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO notifications (user_id, title, body, kind, created_at) VALUES (?,?,?,?,?)",
                (user["id"], title, body, kind, now),
            )
