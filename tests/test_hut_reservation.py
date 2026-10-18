"""Parsing of the hut-reservation.org season calendar (no network)."""

from app.clients.hut_reservation import parse_calendar
from app.planner.availability import classify


def test_parse_calendar():
    rows = parse_calendar([
        {"freeBedsPerCategory": {"106": 120}, "freeBeds": 120, "hutStatus": "SERVICED", "date": "2027-07-01T00:00:00Z",
         "dateFormatted": "01.07.2027", "totalSleepingPlaces": 120, "percentage": "AVAILABLE"},
        {"freeBedsPerCategory": {}, "freeBeds": None, "hutStatus": "CLOSED", "date": "2029-07-13T00:00:00Z",
         "dateFormatted": "13.07.2029", "totalSleepingPlaces": 0, "percentage": "CLOSED"},
    ])
    assert rows == [
        {"date": "2027-07-01", "hut_status": "SERVICED", "free_beds": 120, "total_places": 120},
        {"date": "2029-07-13", "hut_status": "CLOSED", "free_beds": None, "total_places": 0},
    ]


def test_classify():
    assert classify("SERVICED", 5, 40, None, 4).state == "available"
    assert classify("SERVICED", 3, 40, None, 4).state == "full"
    assert classify("SERVICED", None, 0, None, 1).state == "not_released"
    assert classify("CLOSED", 0, 20, None, 1).state == "closed"
