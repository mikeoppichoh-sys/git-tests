"""Smart Parking Management System.

This module contains a real-world parking lot manager with:
- slot tracking and active sessions
- checkout fee calculation with vehicle-specific rates
- persistent SQLite storage for the system state
- a Flask-based web interface for real-world usage
"""

import os
import re
import sqlite3
from datetime import datetime

try:
    from flask import Flask, flash, redirect, render_template, request, url_for # type: ignore
except ImportError:  # pragma: no cover - optional dependency for the web UI
    Flask = None
    flash = redirect = render_template = request = url_for = None

DB_NAME = os.environ.get("PARKING_DB", os.path.join(os.path.dirname(__file__), "parking.db"))


class ParkingLot:
    """Professional parking lot manager supporting multiple vehicle types."""

    DEFAULT_RATES = {
        "car": 50.0,
        "motorcycle": 30.0,
        "van": 80.0,
        "truck": 120.0,
    }

    def __init__(self, db_name=DB_NAME, total_slots=12, rate_per_hour=50):
        self.db_name = db_name
        self.conn = sqlite3.connect(db_name)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self.active_vehicles = {}
        self.default_rate = rate_per_hour
        self._setup_tables(total_slots)
        self._load_active_vehicles()

    @staticmethod
    def normalize_plate_number(plate_number):
        cleaned = re.sub(r"[^A-Za-z0-9]", "", (plate_number or "").strip())
        return cleaned.upper()

    @staticmethod
    def normalize_vehicle_type(vehicle_type):
        normalized = (vehicle_type or "car").strip().lower()
        alias_map = {
            "bike": "motorcycle",
            "boda": "motorcycle",
            "motocycle": "motorcycle",
            "truck": "truck",
            "lorry": "truck",
            "bus": "truck",
            "mini-van": "van",
        }
        normalized = alias_map.get(normalized, normalized)
        return normalized if normalized in ParkingLot.DEFAULT_RATES else "car"

    def _setup_tables(self, total_slots):
        self.cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS Slots (
                slot_id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'free',
                slot_type TEXT DEFAULT 'standard'
            );

            CREATE TABLE IF NOT EXISTS Rates (
                rate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_type TEXT UNIQUE NOT NULL,
                price_per_hour REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS Transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_number TEXT NOT NULL,
                driver_name TEXT,
                phone_number TEXT,
                vehicle_type TEXT,
                slot_id INTEGER,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                amount_paid REAL DEFAULT 0,
                notes TEXT,
                FOREIGN KEY (slot_id) REFERENCES Slots(slot_id)
            );
            """
        )

        self.cursor.execute("SELECT COUNT(*) FROM Slots")
        if self.cursor.fetchone()[0] == 0:
            self.cursor.executemany(
                "INSERT INTO Slots (slot_id, status) VALUES (?, 'free')",
                [(i,) for i in range(1, total_slots + 1)],
            )

        for vehicle_type, rate in self.DEFAULT_RATES.items():
            self.cursor.execute(
                "SELECT 1 FROM Rates WHERE vehicle_type = ?",
                (vehicle_type,),
            )
            if self.cursor.fetchone() is None:
                self.cursor.execute(
                    "INSERT INTO Rates (vehicle_type, price_per_hour) VALUES (?, ?)",
                    (vehicle_type, rate),
                )

        self.conn.commit()

    def _load_active_vehicles(self):
        self.cursor.execute(
            "SELECT plate_number, slot_id, entry_time, vehicle_type FROM Transactions WHERE exit_time IS NULL"
        )
        self.active_vehicles = {}
        for row in self.cursor.fetchall():
            self.active_vehicles[row["plate_number"]] = {
                "slot_id": row["slot_id"],
                "entry_time": datetime.fromisoformat(row["entry_time"]),
                "vehicle_type": row["vehicle_type"],
            }

    def _has_active_plate(self, plate_number):
        self.cursor.execute(
            "SELECT 1 FROM Transactions WHERE plate_number = ? AND exit_time IS NULL LIMIT 1",
            (plate_number,),
        )
        return self.cursor.fetchone() is not None

    def available_slots(self):
        self.cursor.execute("SELECT COUNT(*) FROM Slots WHERE status = 'free'")
        return self.cursor.fetchone()[0]

    def _get_rate(self, vehicle_type):
        normalized = self.normalize_vehicle_type(vehicle_type)
        self.cursor.execute(
            "SELECT price_per_hour FROM Rates WHERE vehicle_type = ?",
            (normalized,),
        )
        row = self.cursor.fetchone()
        if row is None:
            return self.DEFAULT_RATES["car"]
        return float(row["price_per_hour"])

    def _find_free_slot(self):
        self.cursor.execute("SELECT slot_id FROM Slots WHERE status = 'free' ORDER BY slot_id LIMIT 1")
        row = self.cursor.fetchone()
        return row["slot_id"] if row else None

    def _build_session_record(self, row):
        return {
            "transaction_id": row["transaction_id"],
            "plate_number": row["plate_number"],
            "driver_name": row["driver_name"],
            "phone_number": row["phone_number"],
            "vehicle_type": row["vehicle_type"],
            "slot_id": row["slot_id"],
            "entry_time": row["entry_time"],
            "exit_time": row["exit_time"],
            "amount_paid": row["amount_paid"],
            "notes": row["notes"],
        }

    def list_active_sessions(self):
        self.cursor.execute(
            """
            SELECT transaction_id, plate_number, driver_name, phone_number, vehicle_type,
                   slot_id, entry_time, exit_time, amount_paid, notes
            FROM Transactions
            WHERE exit_time IS NULL
            ORDER BY entry_time DESC
            """
        )
        return [self._build_session_record(row) for row in self.cursor.fetchall()]

    def get_transaction_history(self, limit=None):
        query = """
            SELECT transaction_id, plate_number, driver_name, phone_number, vehicle_type,
                   slot_id, entry_time, exit_time, amount_paid, notes
            FROM Transactions
            ORDER BY entry_time DESC
        """
        if limit is not None:
            query += " LIMIT ?"
            self.cursor.execute(query, (limit,))
        else:
            self.cursor.execute(query)
        return [self._build_session_record(row) for row in self.cursor.fetchall()]

    def check_in(self, plate_number, driver_name="", phone_number="", vehicle_type="car", notes=""):
        plate_number = self.normalize_plate_number(plate_number)
        if not plate_number:
            return {"success": False, "message": "Plate number is required."}

        if plate_number in self.active_vehicles or self._has_active_plate(plate_number):
            return {"success": False, "message": f"{plate_number} is already parked in the lot."}

        slot_id = self._find_free_slot()
        if slot_id is None:
            return {"success": False, "message": "No parking slots are available."}

        normalized_vehicle_type = self.normalize_vehicle_type(vehicle_type)
        entry_time = datetime.now()

        self.cursor.execute(
            """
            INSERT INTO Transactions (
                plate_number, driver_name, phone_number, vehicle_type,
                slot_id, entry_time, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plate_number,
                driver_name.strip() or "Guest",
                phone_number.strip(),
                normalized_vehicle_type,
                slot_id,
                entry_time.isoformat(),
                notes.strip(),
            ),
        )
        self.cursor.execute("UPDATE Slots SET status = 'occupied' WHERE slot_id = ?", (slot_id,))
        self.conn.commit()

        self.active_vehicles[plate_number] = {
            "slot_id": slot_id,
            "entry_time": entry_time,
            "vehicle_type": normalized_vehicle_type,
        }

        return {
            "success": True,
            "message": f"{plate_number} checked in successfully. Slot {slot_id} assigned.",
            "slot_id": slot_id,
            "plate_number": plate_number,
            "entry_time": entry_time,
        }

    def check_out(self, plate_number):
        plate_number = self.normalize_plate_number(plate_number)
        if not plate_number:
            return {"success": False, "message": "Plate number is required."}

        record = self.active_vehicles.get(plate_number)
        if not record:
            self.cursor.execute(
                "SELECT slot_id, entry_time, vehicle_type FROM Transactions WHERE plate_number = ? AND exit_time IS NULL ORDER BY transaction_id DESC LIMIT 1",
                (plate_number,),
            )
            row = self.cursor.fetchone()
            if row is None:
                return {"success": False, "message": f"No active parking record found for {plate_number}."}
            record = {
                "slot_id": row["slot_id"],
                "entry_time": datetime.fromisoformat(row["entry_time"]),
                "vehicle_type": row["vehicle_type"],
            }

        exit_time = datetime.now()
        entry_time = record["entry_time"]
        slot_id = record["slot_id"]
        duration_hours = max((exit_time - entry_time).total_seconds() / 3600, 0)
        rate = self._get_rate(record.get("vehicle_type", "car"))
        amount = round(duration_hours * rate, 2)
        minimum_charge = rate if duration_hours > 0 and duration_hours < 1 else 0
        amount = max(amount, minimum_charge)

        self.cursor.execute(
            """
            UPDATE Transactions
            SET exit_time = ?, amount_paid = ?
            WHERE plate_number = ? AND slot_id = ? AND exit_time IS NULL
            """,
            (exit_time.isoformat(), amount, plate_number, slot_id),
        )
        self.cursor.execute("UPDATE Slots SET status = 'free' WHERE slot_id = ?", (slot_id,))
        self.conn.commit()

        self.active_vehicles.pop(plate_number, None)

        return {
            "success": True,
            "message": f"{plate_number} checked out successfully from slot {slot_id}.",
            "plate_number": plate_number,
            "slot_id": slot_id,
            "duration_hours": round(duration_hours, 2),
            "amount_paid": round(amount, 2),
        }

    def get_dashboard_summary(self):
        total_slots = self.cursor.execute("SELECT COUNT(*) FROM Slots").fetchone()[0]
        occupied_slots = total_slots - self.available_slots()
        active_sessions = self.list_active_sessions()
        total_revenue = self.cursor.execute(
            "SELECT COALESCE(SUM(amount_paid), 0) FROM Transactions WHERE exit_time IS NOT NULL"
        ).fetchone()[0]
        return {
            "total_slots": total_slots,
            "occupied_slots": occupied_slots,
            "available_slots": self.available_slots(),
            "active_sessions_count": len(active_sessions),
            "total_revenue": float(total_revenue or 0),
        }

    def close(self):
        self.conn.close()


if Flask is not None:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "parking-system-secret-key"

    @app.route("/")
    def index():
        lot = get_lot()
        summary = lot.get_dashboard_summary()
        sessions = lot.list_active_sessions()
        lot.close()
        return render_template("index.html", summary=summary, sessions=sessions)

    @app.route("/transactions")
    def transactions():
        lot = get_lot()
        history = lot.get_transaction_history()
        lot.close()
        return render_template("transactions.html", history=history)

    @app.route("/checkin", methods=["POST"])
    def checkin():
        plate_number = request.form.get("plate_number", "")
        driver_name = request.form.get("driver_name", "")
        phone_number = request.form.get("phone_number", "")
        vehicle_type = request.form.get("vehicle_type", "car")
        notes = request.form.get("notes", "")

        lot = get_lot()
        result = lot.check_in(plate_number, driver_name, phone_number, vehicle_type, notes)
        lot.close()

        if result["success"]:
            flash(result["message"], "success")
        else:
            flash(result["message"], "error")
        return redirect(url_for("index"))

    @app.route("/checkout", methods=["POST"])
    def checkout():
        plate_number = request.form.get("plate_number", "")
        lot = get_lot()
        result = lot.check_out(plate_number)
        lot.close()

        if result["success"]:
            flash(f"{result['message']} Amount due: KES {result['amount_paid']:.2f}", "success")
        else:
            flash(result["message"], "error")
        return redirect(url_for("index"))

    @app.route("/health")
    def health():
        return {"status": "ok", "database": DB_NAME}
else:
    app = None


def get_lot():
    return ParkingLot(db_name=DB_NAME)


def main():
    if app is None:
        raise RuntimeError("Flask is not installed. Install dependencies with: python -m pip install -r requirements.txt")
    app.run(debug=True, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()