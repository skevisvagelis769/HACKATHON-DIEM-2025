# backend/scripts/seed_history.py
import os
import sys
import time
import random

# Allow "app.*" imports when running from backend/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.db import SessionLocal
from app.models import User, UserRole
from app import services

HOURS = 12
STEP_SECONDS = 5 * 60  # 5 minutes

def main():
    now = int(time.time())
    start = now - HOURS * 3600
    start = start - (start % STEP_SECONDS)  # align to 5-min boundary

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.role != UserRole.provider.value).all()
        if not users:
            print("⚠️ No non-provider users found. Create some users first.")
            return

        print(f"Seeding {HOURS}h of history every 5 minutes for {len(users)} users...")
        ts = start
        while ts <= now:
            hour = time.localtime(ts).tm_hour
            daylight = 8 <= hour <= 18  # day-time hours

            for u in users:
                base_prod = random.uniform(0.2, 1.2) if daylight else random.uniform(0.0, 0.2)
                base_cons = random.uniform(0.4, 0.9) if daylight else random.uniform(0.2, 0.6)

                prod = max(0.0, round(base_prod + random.uniform(-0.15, 0.15), 3))
                cons = max(0.0, round(base_cons + random.uniform(-0.15, 0.15), 3))

                services.record_meter_sample(db, user_id=u.id, prod_kwh=prod, cons_kwh=cons, ts=ts)

            ts += STEP_SECONDS

        print("✅ Done seeding history.")
    finally:
        db.close()

if __name__ == "__main__":
    main()
