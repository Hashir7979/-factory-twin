from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.main import create_app
from api.settings import Settings
from api.ws import CLOSE_FORBIDDEN_ORIGIN, CLOSE_TRY_AGAIN_LATER, CLOSE_UNKNOWN_SCENARIO, origin_allowed


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    settings = Settings(
        cors_origins=["http://localhost:5173"],
        tick_seconds=0.01,
        kpi_interval_seconds=0.05,
        workers=2,
        max_sessions=3,
        max_replications=6,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def wait_job(client: TestClient, job_id: str, timeout: float = 120) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def receive_until(ws: Any, predicate: Any, limit: int = 2000) -> dict[str, Any]:
    for _ in range(limit):
        msg: dict[str, Any] = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected message not received")


# --------------------------------------------------------------------------- REST


def test_health_and_scenarios(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers["x-request-id"]
    ids = [s["id"] for s in client.get("/api/scenarios").json()]
    assert "battery_line" in ids
    detail = client.get("/api/scenarios/battery_line").json()
    assert {s["id"] for s in detail["stations"]} >= {"weld", "stack"}


@pytest.mark.parametrize("bad", ["nope", "..%2F..%2Fetc%2Fpasswd", "a" * 50])
def test_unknown_or_malicious_scenario_ids(client: TestClient, bad: str) -> None:
    assert client.get(f"/api/scenarios/{bad}").status_code in (404, 422)


def test_single_run_with_override(client: TestClient) -> None:
    body = {
        "scenario_id": "battery_line",
        "overrides": {"station.weld.servers": 2},
        "seed": 3,
        "horizon": 14400,
        "warmup": 1800,
    }
    r = client.post("/api/runs", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["bottleneck"]["station"] == "stack"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"scenario_id": "battery_line", "overrides": {"station.weld.process_time": 1}}, 422),
        ({"scenario_id": "battery_line", "horizon": 100, "warmup": 200}, 422),
        ({"scenario_id": "battery_line", "horizon": 10**9}, 422),
        ({"scenario_id": "missing"}, 404),
        ({"scenario_id": "battery_line", "extra": 1}, 422),
    ],
)
def test_run_validation(client: TestClient, body: dict[str, Any], status: int) -> None:
    r = client.post("/api/runs", json=body)
    assert r.status_code == status
    assert "Traceback" not in r.text


def test_experiment_job_lifecycle_and_csv(client: TestClient) -> None:
    r = client.post(
        "/api/experiments", json={"scenario_id": "battery_line", "replications": 3, "horizon": 14400, "warmup": 1800}
    )
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job
    result = job["result"]
    assert result["seeds"] == [1, 2, 3]
    tp = result["metrics"]["throughput_per_hour"]
    assert tp["n"] == 3 and tp["half_width"] > 0
    assert result["bottleneck_frequency"]
    csv = client.get(f"/api/jobs/{job['job_id']}/runs.csv")
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("seed,")
    assert len(csv.text.strip().splitlines()) == 4


def test_comparison_uses_paired_seeds(client: TestClient) -> None:
    body = {
        "scenario_id": "battery_line",
        "a_overrides": {},
        "b_overrides": {"station.weld.servers": 2},
        "replications": 3,
        "horizon": 14400,
        "warmup": 1800,
    }
    job = wait_job(client, client.post("/api/comparisons", json=body).json()["job_id"])
    assert job["status"] == "done", job
    diffs = {d["metric"]: d for d in job["result"]["differences"]}
    assert diffs["throughput_per_hour"]["diff"]["mean"] > 0
    assert job["result"]["a"]["seeds"] == job["result"]["b"]["seeds"]


def test_replication_limit_and_unknown_job(client: TestClient) -> None:
    r = client.post("/api/experiments", json={"scenario_id": "battery_line", "replications": 7})
    assert r.status_code == 422
    assert client.get("/api/jobs/doesnotexist").status_code == 404


# --------------------------------------------------------------------------- WebSocket


def test_ws_hello_play_and_events(client: TestClient) -> None:
    with client.websocket_connect("/ws/session?scenario=battery_line") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["status"] == "paused"
        assert set(hello["snapshot"]["machines"]) >= {"weld#1", "stack#1", "stack#2"}
        assert ws.receive_json()["type"] == "kpis"
        ws.send_json({"type": "speed", "value": 500})
        ws.send_json({"type": "play"})
        receive_until(ws, lambda m: m["type"] == "status" and m["status"] == "playing")
        ev = receive_until(ws, lambda m: m["type"] == "events" and m["events"])
        assert ev["t"] > 0
        kpis = receive_until(ws, lambda m: m["type"] == "kpis" and m["t"] > 0)
        assert "line" in kpis["data"]


def test_ws_inject_failure_and_errors(client: TestClient) -> None:
    with client.websocket_connect("/ws/session?scenario=battery_line") as ws:
        ws.receive_json()
        ws.send_json({"type": "inject_failure", "machine": "weld#1", "repair_time": 60})
        ev = receive_until(ws, lambda m: m["type"] == "events" and m["events"])
        assert {"type": "state", "t": 0, "machine": "weld#1", "state": "down"}.items() <= ev["events"][0].items()
        ws.send_json({"type": "inject_failure", "machine": "weld#1"})
        assert receive_until(ws, lambda m: m["type"] == "error")["code"] == "already_down"
        ws.send_json({"type": "inject_failure", "machine": "nope#1"})
        assert "unknown machine" in receive_until(ws, lambda m: m["type"] == "error")["message"]
        ws.send_text("not json")
        assert receive_until(ws, lambda m: m["type"] == "error")["message"].startswith("Invalid command")
        ws.send_json({"type": "speed", "value": 10**6})
        assert "speed must be" in receive_until(ws, lambda m: m["type"] == "error")["message"]
        ws.send_json({"type": "reset", "overrides": {"station.weld.name": 1}})
        assert "not allowed" in receive_until(ws, lambda m: m["type"] == "error")["message"]


def test_ws_reset_applies_overrides(client: TestClient) -> None:
    with client.websocket_connect("/ws/session?scenario=battery_line") as ws:
        first = ws.receive_json()
        ws.send_json({"type": "reset", "overrides": {"station.weld.servers": 2}, "seed": 9})
        hello = receive_until(ws, lambda m: m["type"] == "hello")
        assert hello["seq"] > first["seq"]
        assert hello["seed"] == 9 and hello["overrides"] == {"station.weld.servers": 2}
        assert "weld#2" in hello["snapshot"]["machines"]


def test_ws_resume_keeps_session(client: TestClient) -> None:
    with client.websocket_connect("/ws/session?scenario=battery_line") as ws:
        session_id = ws.receive_json()["session"]
    with client.websocket_connect(f"/ws/session?scenario=battery_line&resume={session_id}") as ws:
        assert ws.receive_json()["session"] == session_id
    with client.websocket_connect("/ws/session?scenario=battery_line&resume=forged-id") as ws:
        assert ws.receive_json()["session"] != session_id


def test_ws_unknown_scenario_and_bad_origin(client: TestClient) -> None:
    with client.websocket_connect("/ws/session?scenario=missing") as ws:
        assert ws.receive_json()["code"] == "unknown_scenario"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == CLOSE_UNKNOWN_SCENARIO
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/ws/session?scenario=battery_line", headers={"origin": "https://evil.example"}),
    ):
        pass
    assert exc.value.code == CLOSE_FORBIDDEN_ORIGIN


def test_ws_session_limit() -> None:
    settings = Settings(max_sessions=1, session_resume_ttl=60, workers=1)
    with TestClient(create_app(settings)) as c, c.websocket_connect("/ws/session?scenario=battery_line") as ws1:
        ws1.receive_json()
        with c.websocket_connect("/ws/session?scenario=battery_line") as ws2:
            assert ws2.receive_json()["code"] == "busy"
            with pytest.raises(WebSocketDisconnect) as exc:
                ws2.receive_json()
            assert exc.value.code == CLOSE_TRY_AGAIN_LATER


def test_origin_rules() -> None:
    allowed = ["http://localhost:5173"]
    assert origin_allowed(None, "api:8000", allowed)
    assert origin_allowed("http://localhost:5173", "api:8000", allowed)
    assert origin_allowed("https://twin.example", "twin.example", allowed)  # same origin
    assert not origin_allowed("https://evil.example", "twin.example", allowed)


def test_detached_session_is_evicted_for_new_user() -> None:
    settings = Settings(max_sessions=1, session_resume_ttl=60, workers=1)
    with TestClient(create_app(settings)) as c:
        with c.websocket_connect("/ws/session?scenario=battery_line") as ws:
            first = ws.receive_json()["session"]
        with c.websocket_connect("/ws/session?scenario=battery_line") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["session"] != first


def test_optimizer_defaults_and_job(client: TestClient) -> None:
    defaults = client.get("/api/scenarios/battery_line/optimizer-defaults").json()
    keys = [v["key"] for v in defaults["variables"]]
    assert "station.weld.servers" in keys and "buffer.B2.capacity" in keys
    body = {
        "scenario_id": "battery_line",
        "variables": [
            {"key": "station.weld.servers", "low": 1, "high": 2},
            {"key": "buffer.B2.capacity", "low": 3, "high": 12},
        ],
        "trials": 4,
        "replications": 2,
        "horizon": 14400,
        "warmup": 1800,
    }
    r = client.post("/api/optimizations", json=body)
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job_id"], timeout=180)
    assert job["status"] == "done", job
    res = job["result"]
    assert len(res["trials"]) == 4
    assert res["best"]["profit"]["mean"] >= res["baseline"]["profit"]["mean"]
    assert res["pareto"]


@pytest.mark.parametrize(
    "variables",
    [
        [{"key": "station.weld.process_time", "low": 1, "high": 2}],
        [{"key": "station.weld.servers", "low": 3, "high": 1}],
        [{"key": "station.weld.servers", "low": 1, "high": 99}],
        [{"key": "buffer.NOPE.capacity", "low": 1, "high": 2}],
        [{"key": "station.weld.servers", "low": 1, "high": 2}, {"key": "station.weld.servers", "low": 1, "high": 2}],
    ],
)
def test_optimizer_rejects_bad_variables(client: TestClient, variables: list[dict[str, Any]]) -> None:
    r = client.post("/api/optimizations", json={"scenario_id": "battery_line", "variables": variables, "trials": 3})
    assert r.status_code == 422
