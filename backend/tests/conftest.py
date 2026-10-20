import os

os.environ.setdefault("MQTT_ENABLED", "false")

os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+pysqlite:////tmp/hydroponic_test_bootstrap.db",
)
