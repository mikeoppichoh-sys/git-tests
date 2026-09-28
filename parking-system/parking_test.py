"""Smart Parking Management System (Flask + SQLite).

Run locally:  python parking_test.py
Production:   gunicorn parking_test:app
Environment variables: SECRET_KEY, ATTENDANT_PASSWORD, PARKING_DB
"""
import csv
import io
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta
from hmac import compare_digest
from zoneinfo import ZoneInfo

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)

TZ = ZoneInfo("Africa/Nairobi")
DB_NAME = os.environ.get("PARKING_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "parking.db"))
PASSWORD = os.environ.get("ATTENDANT_PASSWORD", "parking123")
DEFAULT_RATES = {"car": 50.0, "motorcycle": 30.0, "van": 80.0, "truck": 120.0}
ALIASES = {"bike": "motorcycle", "boda": "motorcycle", "motocycle": "motorcycle",
           "lorry": "truck", "bus": "truck", "mini-van": "van"}


def now():
    return datetime.now(TZ).replace(microsecond=0)


class ParkingLot:
    def __init__(self, db_name=DB_NAME, total_slots=12):
        self.conn = sqlite3.connect(db_name)
        self.conn.row_factory = sqlite3.Row
        self._setup(total_slots)

    @staticmethod
    def normalize_plate(plate):
        return re.sub(r"[^A-Za-z0-9]", "", plate or "").upper()

    @staticmethod
    def normalize_type(vtype):
        v = ALIASES.get((vtype or "car").strip().lower(), (vtype or "car").strip().lower())
        return v if v in DEFAULT_RATES else "car"

    def _setup(self, total_slots):
        c = self.conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS Slots (
                slot_id INTEGER PRIMARY KEY, status TEXT DEFAULT 'free', slot_type TEXT DEFAULT 'standard');
            CREATE TABLE IF NOT EXISTS Rates (
                rate_id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_type TEXT UNIQUE NOT NULL, price_per_hour REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS Transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, plate_number TEXT NOT NULL,
                driver_name TEXT, phone_number TEXT, vehicle_type TEXT, slot_id INTEGER,
                entry_time TEXT NOT NULL, exit_time TEXT, amount_paid REAL DEFAULT 0, notes TEXT,
                FOREIGN KEY (slot_id) REFERENCES Slots(slot_id));
            CREATE UNIQUE INDEX IF NOT EXISTS one_plate_inside ON Transactions(plate_number) WHERE exit_time IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS one_car_per_slot ON Transactions(slot_id) WHERE exit_time IS NULL;
        """)
        if c.execute("SELECT COUNT(*) FROM Slots").fetchone()[0] == 0:
            c.executemany("INSERT INTO Slots (slot_id) VALUES (?)", [(i,) for i in range(1, total_slots + 1)])
        c.executemany("INSERT OR IGNORE INTO Rates (vehicle_type, price_per_hour) VALUES (?, ?)", DEFAULT_RATES.items())
        c.commit()

    def rate(self, vtype):
        row = self.conn.execute("SELECT price_per_hour FROM Rates WHERE vehicle_type = ?",
                                (self.normalize_type(vtype),)).fetchone()
        return float(row[0]) if row else DEFAULT_RATES["car"]

    def slots(self):
        rows = self.conn.execute("""
            SELECT s.slot_id, t.plate_number, t.driver_name, t.vehicle_type, t.entry_time
            FROM Slots s LEFT JOIN Transactions t ON t.slot_id = s.slot_id AND t.exit_time IS NULL
            ORDER BY s.slot_id""").fetchall()
        return [dict(r, rate=self.rate(r["vehicle_type"]) if r["plate_number"] else 0) for r in rows]

    def check_in(self, plate, driver="", phone="", vtype="car", notes=""):
        plate = self.normalize_plate(plate)
        if not plate:
            return {"success": False, "message": "Enter a plate number."}
        slot = self.conn.execute("""SELECT slot_id FROM Slots WHERE slot_id NOT IN
            (SELECT slot_id FROM Transactions WHERE exit_time IS NULL) ORDER BY slot_id LIMIT 1""").fetchone()
        if slot is None:
            return {"success": False, "message": "The lot is full."}
        try:
            self.conn.execute("""INSERT INTO Transactions
                (plate_number, driver_name, phone_number, vehicle_type, slot_id, entry_time, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (plate, driver.strip() or "Guest", phone.strip(), self.normalize_type(vtype),
                 slot[0], now().isoformat(), notes.strip()))
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return {"success": False, "message": f"{plate} is already parked, or the slot was just taken. Try again."}
        return {"success": True, "message": f"{plate} parked in slot {slot[0]}."}

    def check_out(self, plate):
        plate = self.normalize_plate(plate)
        row = self.conn.execute("""SELECT transaction_id, slot_id, entry_time, vehicle_type
            FROM Transactions WHERE plate_number = ? AND exit_time IS NULL""", (plate,)).fetchone()
        if row is None:
            return {"success": False, "message": f"{plate or 'That plate'} is not in the lot."}
        entry = datetime.fromisoformat(row["entry_time"])
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=TZ)
        leave = now()
        hours = max((leave - entry).total_seconds() / 3600, 0)
        amount = max(1, math.ceil(hours)) * self.rate(row["vehicle_type"])
        self.conn.execute("UPDATE Transactions SET exit_time = ?, amount_paid = ? WHERE transaction_id = ?",
                          (leave.isoformat(), amount, row["transaction_id"]))
        self.conn.commit()
        return {"success": True,
                "message": f"{plate} left slot {row['slot_id']} after {hours:.1f} hours. Collect KES {amount:,.0f}."}

    def history(self, q=""):
        text = f"%{q.strip()}%"
        return self.conn.execute("""SELECT * FROM Transactions
            WHERE plate_number LIKE ? OR driver_name LIKE ? OR phone_number LIKE ?
            ORDER BY transaction_id DESC""", (f"%{self.normalize_plate(q)}%", text, text)).fetchall()

    def summary(self):
        slots = self.slots()
        parked = sum(1 for s in slots if s["plate_number"])
        rev = dict(self.conn.execute("""SELECT substr(exit_time, 1, 10), SUM(amount_paid)
            FROM Transactions WHERE exit_time IS NOT NULL GROUP BY 1""").fetchall())
        days = [now() - timedelta(days=i) for i in range(6, -1, -1)]
        week = [{"day": d.strftime("%a"), "total": rev.get(d.date().isoformat(), 0)} for d in days]
        top = max([w["total"] for w in week] + [1])
        for w in week:
            w["pct"] = round(w["total"] / top * 100)
        return {"slots": slots, "total": len(slots), "parked": parked, "free": len(slots) - parked,
                "today": week[-1]["total"], "all_time": sum(rev.values()), "week": week}


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")


def get_lot():
    if "lot" not in g:
        g.lot = ParkingLot()
    return g.lot


@app.teardown_appcontext
def close_lot(_exc):
    lot = g.pop("lot", None)
    if lot:
        lot.conn.close()


@app.before_request
def require_login():
    if request.endpoint not in ("login", "health", "static") and not session.get("in"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
            session["in"] = True
            return redirect(url_for("index"))
        flash("Wrong password. Try again.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template("index.html", s=get_lot().summary(), rates=DEFAULT_RATES)


@app.route("/transactions")
def transactions():
    q = request.args.get("q", "")
    return render_template("transactions.html", history=get_lot().history(q), q=q)


@app.route("/checkin", methods=["POST"])
def checkin():
    f = request.form
    r = get_lot().check_in(f.get("plate_number", ""), f.get("driver_name", ""), f.get("phone_number", ""),
                           f.get("vehicle_type", "car"), f.get("notes", ""))
    flash(r["message"], "ok" if r["success"] else "error")
    return redirect(url_for("index"))


@app.route("/checkout", methods=["POST"])
def checkout():
    r = get_lot().check_out(request.form.get("plate_number", ""))
    flash(r["message"], "ok" if r["success"] else "error")
    return redirect(url_for("index"))


@app.route("/export.csv")
def export():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Plate", "Driver", "Phone", "Type", "Slot", "In", "Out", "Paid (KES)"])
    for t in get_lot().history(request.args.get("q", "")):
        w.writerow([t["plate_number"], t["driver_name"], t["phone_number"], t["vehicle_type"],
                    t["slot_id"], t["entry_time"], t["exit_time"] or "", t["amount_paid"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=parking-history.csv"})


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000)
