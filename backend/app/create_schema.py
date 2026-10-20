from sqlalchemy import inspect, text

from app.db import engine
from app.models import Base


def upgrade_existing_schema() -> None:
    """Apply the small Phase 5 schema additions to an existing development database."""
    inspector = inspect(engine)
    tower_columns = {column["name"] for column in inspector.get_columns("towers")}
    telemetry_columns = {
        column["name"] for column in inspector.get_columns("telemetry_readings")
    }

    with engine.begin() as connection:
        if "tower_key" not in tower_columns:
            connection.execute(text("ALTER TABLE towers ADD COLUMN tower_key VARCHAR(80)"))
        if "circulation_on" not in telemetry_columns:
            connection.execute(
                text("ALTER TABLE telemetry_readings ADD COLUMN circulation_on BOOLEAN")
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_towers_tower_key "
                "ON towers (tower_key)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_action_logs_one_execution_request_per_recommendation "
                "ON action_logs (recommendation_id) "
                "WHERE event_type = 'execution_requested'"
            )
        )


def main() -> None:
    Base.metadata.create_all(engine)
    upgrade_existing_schema()
    tables = inspect(engine).get_table_names()
    print("Schema ready. Tables: " + ", ".join(sorted(tables)))


if __name__ == "__main__":
    main()
