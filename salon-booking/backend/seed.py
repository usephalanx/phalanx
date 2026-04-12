"""Seed script to populate sample staff and services."""

import asyncio
import json
import os
import sys

# Ensure app is importable
sys.path.insert(0, os.path.dirname(__file__))

from app.database import engine, init_db, async_session_factory
from app.models.staff import Staff
from app.models.service import Service
from app.models.staff_schedule import StaffSchedule


SAMPLE_STAFF = [
    {
        "name": "Maria Garcia",
        "email": "maria@salon.com",
        "phone": "555-0101",
        "role": "senior stylist",
        "specialties": json.dumps(["haircut", "coloring", "highlights"]),
    },
    {
        "name": "James Chen",
        "email": "james@salon.com",
        "phone": "555-0102",
        "role": "stylist",
        "specialties": json.dumps(["haircut", "blowout", "beard trim"]),
    },
    {
        "name": "Priya Patel",
        "email": "priya@salon.com",
        "phone": "555-0103",
        "role": "colorist",
        "specialties": json.dumps(["coloring", "balayage", "highlights"]),
    },
    {
        "name": "Sophie Laurent",
        "email": "sophie@salon.com",
        "phone": "555-0104",
        "role": "nail technician",
        "specialties": json.dumps(["manicure", "pedicure", "nail art"]),
    },
]

SAMPLE_SERVICES = [
    {
        "name": "Haircut",
        "description": "Professional haircut and styling",
        "duration_minutes": 45,
        "price": 55.00,
        "category": "hair",
    },
    {
        "name": "Hair Coloring",
        "description": "Full hair coloring service with premium products",
        "duration_minutes": 90,
        "price": 120.00,
        "category": "hair",
    },
    {
        "name": "Blowout",
        "description": "Wash and blowout styling",
        "duration_minutes": 30,
        "price": 35.00,
        "category": "hair",
    },
    {
        "name": "Highlights",
        "description": "Partial or full highlights",
        "duration_minutes": 120,
        "price": 150.00,
        "category": "hair",
    },
    {
        "name": "Manicure",
        "description": "Classic manicure with polish",
        "duration_minutes": 30,
        "price": 25.00,
        "category": "nails",
    },
    {
        "name": "Pedicure",
        "description": "Relaxing pedicure with polish",
        "duration_minutes": 45,
        "price": 35.00,
        "category": "nails",
    },
    {
        "name": "Gel Nails",
        "description": "Long-lasting gel nail application",
        "duration_minutes": 60,
        "price": 45.00,
        "category": "nails",
    },
    {
        "name": "Beard Trim",
        "description": "Professional beard shaping and trim",
        "duration_minutes": 20,
        "price": 20.00,
        "category": "grooming",
    },
]


async def seed() -> None:
    """Populate the database with sample data."""
    await init_db()

    async with async_session_factory() as session:
        # Check if data already exists
        from sqlalchemy import select, func

        count = await session.execute(select(func.count(Staff.id)))
        if (count.scalar() or 0) > 0:
            print("Database already seeded. Skipping.")
            return

        # Insert staff
        staff_records = []
        for data in SAMPLE_STAFF:
            s = Staff(**data)
            session.add(s)
            staff_records.append(s)
        await session.flush()

        # Insert default schedules (Mon–Fri 09:00–17:00, Sat 10:00–15:00)
        for s in staff_records:
            for day in range(5):  # Mon–Fri
                session.add(
                    StaffSchedule(
                        staff_id=s.id,
                        day_of_week=day,
                        start_time="09:00",
                        end_time="17:00",
                    )
                )
            # Saturday
            session.add(
                StaffSchedule(
                    staff_id=s.id,
                    day_of_week=5,
                    start_time="10:00",
                    end_time="15:00",
                )
            )

        # Insert services
        for data in SAMPLE_SERVICES:
            session.add(Service(**data))

        await session.commit()
        print(f"Seeded {len(SAMPLE_STAFF)} staff and {len(SAMPLE_SERVICES)} services.")


if __name__ == "__main__":
    asyncio.run(seed())
