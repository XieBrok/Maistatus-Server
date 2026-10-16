"""Minimal dependency-free contract checks; run with python tests/smoke_test.py."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = (root / "main.py").read_text(encoding="utf-8")
ast.parse(source)

for expected in (
    '"/healthz"',
    '"/api/v1/events"',
    '"/api/v1/status/latest"',
    '"Idempotency-Key must match event_id"',
    '"STATUS_ADMIN_TOKEN"',
    '"STATUS_API_TOKEN"',
    '"/admin\/devices"',
    '"/admin/api/devices"',
    '"/api/v1/devices/heartbeat"',
    '"/api/v1/evens"',
    '"event_type must be location, activity, or music"',
):
    assert expected in source, f"missing contract item: {expected}"

compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
assert '127.0.0.1:8090:8000' in compose
print("Smoke checks passed.")
