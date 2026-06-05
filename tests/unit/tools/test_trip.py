from src.bot.tools import trip
from src.bot.storage import dynamodb
from datetime import datetime


def test_start_trip_creates_active_trip(dynamodb_table):
    user_id = "123456"
    res = trip.start_trip.invoke({"telegram_user_id": user_id})
    
    record = dynamodb.get_item(f"USER#{user_id}", "TRIP#ACTIVE")
    assert record is not None
    
    date_now = datetime.today().strftime('%Y-%m-%d')
    assert res == f"New trip started on {date_now}."
    
    
def test_start_trip_returns_error_when_trip_already_active(dynamodb_table):
    user_id = "123456"
    
    dynamodb.put_item({
        "PK": f"USER#{user_id}",
        "SK": "TRIP#ACTIVE",
        "start_date": "2020-12-30"
    })

    res = trip.start_trip.invoke({"telegram_user_id": user_id})
    
    assert res == "There is already an active trip."