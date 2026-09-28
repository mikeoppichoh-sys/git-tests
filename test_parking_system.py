import os
import tempfile

from parking_test import ParkingLot


def test_parking_lot_rehydrates_active_vehicles_from_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        lot = ParkingLot(db_name=db_path, total_slots=2, rate_per_hour=50)
        lot.check_in("ABC123", driver_name="Jane Doe", vehicle_type="car")
        lot.close()

        reopened = ParkingLot(db_name=db_path, total_slots=2, rate_per_hour=50)
        assert reopened.active_vehicles["ABC123"]["slot_id"] == 1
        assert reopened.available_slots() == 1

        reopened.check_out("ABC123")
        assert reopened.available_slots() == 2
        reopened.close()
    finally:
        os.remove(db_path)


def test_parking_lot_tracks_vehicle_details_and_checkout_history():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        lot = ParkingLot(db_name=db_path, total_slots=3, rate_per_hour=50)
        lot.check_in("XYZ999", driver_name="Sam", vehicle_type="van")
        lot.check_in("QWE111", driver_name="Nia", vehicle_type="motorcycle")

        active_sessions = lot.list_active_sessions()
        assert len(active_sessions) == 2
        assert {session["plate_number"] for session in active_sessions} == {"XYZ999", "QWE111"}
        assert lot.available_slots() == 1

        lot.check_out("XYZ999")
        history = lot.get_transaction_history()
        assert any(record["plate_number"] == "XYZ999" for record in history)
        lot.close()
    finally:
        os.remove(db_path)
