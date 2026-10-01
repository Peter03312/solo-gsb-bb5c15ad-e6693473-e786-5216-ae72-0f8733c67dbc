"""并发写入测试：同一货物的多笔并发交接必须串行化，保管链不得损坏。

本用例需要“文件型 SQLite（每线程独立连接）”的引擎，运行方式：

    DATABASE_URL="sqlite:////tmp/coldchain_conc.db" pytest tests/test_concurrency.py

（默认的内存 StaticPool 配置下本用例自动跳过。）
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

pytestmark = pytest.mark.skipif(
    engine.pool.__class__.__name__ == "StaticPool",
    reason="StaticPool（内存库）不支持多线程并发；"
    "请用文件型 sqlite 单独运行："
    "DATABASE_URL='sqlite:////tmp/x.db' pytest tests/test_concurrency.py",
)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def test_concurrent_handovers_preserve_chain(client):
    client.post(
        "/cargo",
        json={
            "cargo_id": "CC",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )

    def submit(i):
        # 所有线程都提交“同一时刻、A->Pi”的交接：货物行锁使其串行执行，
        # 只有第一笔成功；其余要么交出方失配（保管方已变更），
        # 要么触发同一时刻交接唯一约束
        return client.post(
            "/events",
            json={
                "event_id": f"hc{i}",
                "cargo_id": "CC",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "giver": "A",
                "receiver": f"P{i}",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c in (409, 422) for c in codes if c != 201)
    report = client.get("/cargo/CC/report").json()
    # 保管链只前进一次，当前保管方是唯一成功那笔的接收方
    assert report["current_holder"].startswith("P")


def test_concurrent_stance_submits_serialize_with_snapshot(client):
    """表态并发提交：同责任方相同内容只有第一笔 201，其余按幂等返回 200。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CS",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:10:00+00:00", "temperature": "8"})
    client.post("/events", json={
        "event_id": "m2", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:20:00+00:00", "temperature": "4"})
    snap = client.get("/cargo/CS/responsibilities").json()["intervals"][0]["snapshot_id"]

    def submit(i):
        return client.post(
            "/cargo/CS/responsibilities/m1/stances",
            json={
                "snapshot_id": snap,
                "responsible_party": "A",
                "stance": "confirm",
                # 所有线程同一编号同一内容：行锁串行后表现为 1 次写入 + 其余幂等
                "stance_id": "same-stance",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    hist = client.get("/cargo/CS/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1


def test_concurrent_onboard_reading_and_event_serialize(client):
    """车载读数写入与受影响货物的事件 / 表态写入串行一致。

    多线程并发写同一时刻、不同编号的车载读数与货物自有测温：
    数据库唯一约束（车次同刻）+ 货物行锁使其串行，结果必须自洽——
    不会出现同一货物同刻既有被采用的车载读数又有自有测温的矛盾快照。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CT",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/trips", json={"trip_id": "TT", "vehicle_no": "V"})
    client.post(
        "/trips/TT/loadings",
        json={"loadings": [{
            "cargo_id": "CT",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    )

    def submit_reading(i):
        return client.post(
            "/trips/TT/readings",
            json={
                "reading_id": f"tr{i}",
                "trip_id": "TT",
                "read_at": "2026-01-01T11:00:00+00:00",
                "temperature": "8",
            },
        ).status_code

    def submit_event(i):
        return client.post(
            "/events",
            json={
                "event_id": f"te{i}",
                "cargo_id": "CT",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "temperature": "9",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        reading_codes = list(pool.map(submit_reading, range(4)))
        event_codes = list(pool.map(submit_event, range(4)))

    # 车次同刻只有一笔读数成功（幂等编号都不同，故只有一笔 201，其余 409）
    assert reading_codes.count(201) == 1
    assert all(c == 409 for c in reading_codes if c != 201)
    # 货物同刻只有一笔自有测温成功
    assert event_codes.count(201) == 1
    assert all(c in (409, 422) for c in event_codes if c != 201)

    # 合并时间线自洽：该时刻自有测温优先，车载读数未被采用
    readings = client.get("/cargo/CT/readings").json()["readings"]
    at_11 = [r for r in readings if r["occurred_at"].startswith("2026-01-01T11:00:00")]
    adopted = [r for r in at_11 if r["adopted"]]
    assert len(adopted) == 1 and adopted[0]["source"] == "own"


def test_concurrent_vehicle_handovers_same_id_idempotent(client):
    """同车次同刻的同单号整车交接并发：恰好一次写入，其余按幂等返回 200。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CH",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/trips", json={"trip_id": "TH", "vehicle_no": "V"})
    client.post(
        "/trips/TH/loadings",
        json={"loadings": [{
            "cargo_id": "CH",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    )

    def submit(i):
        return client.post(
            "/trips/TH/handovers",
            json={
                "handover_id": "VH-SAME",
                "trip_id": "TH",
                "handed_over_at": "2026-01-01T11:00:00+00:00",
                "giver": "A",
                "receiver": "B",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    handovers = client.get("/trips/TH/handovers").json()
    assert len(handovers) == 1
    # 恰好一笔逐票交接事件
    report = client.get("/cargo/CH/report").json()
    assert report["current_holder"] == "B"


def test_concurrent_vehicle_handover_and_loading_freeze(client):
    """整车交接与补登记装载并发：清单冻结不变式恒成立。

    若装载先提交：交接时刻在车集合包含新货，交接清单随之包含新货（成功）；
    若交接先提交：新货不在冻结清单，装载整批拒绝（409 handover_manifest_conflict）。
    任何一种串行顺序下，已完成交接单的清单都必须等于交接时刻当前在车集合。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CF",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    for k in range(4):
        client.post(
            "/cargo",
            json={
                "cargo_id": f"CF{k}",
                "started_at": "2026-01-01T10:00:00+00:00",
                "temp_upper_limit": "5",
                "initial_holder": "A",
            },
        )
    client.post("/trips", json={"trip_id": "TF", "vehicle_no": "V"})
    client.post(
        "/trips/TF/loadings",
        json={"loadings": [{
            "cargo_id": "CF",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    )

    def submit_handover(i):
        return client.post(
            "/trips/TF/handovers",
            json={
                "handover_id": "VH-F",
                "trip_id": "TF",
                "handed_over_at": "2026-01-01T11:00:00+00:00",
                "giver": "A",
                "receiver": "B",
            },
        ).status_code

    def submit_loading(i):
        return client.post(
            "/trips/TF/loadings",
            json={"loadings": [{
                "cargo_id": f"CF{i}",
                "loaded_at": "2026-01-01T10:30:00+00:00",
                "unloaded_at": "2026-01-01T11:30:00+00:00",
            }]},
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        h_codes = list(pool.map(submit_handover, range(4)))
        l_codes = list(pool.map(submit_loading, range(4)))

    # 交接：同单号同内容并发 -> 1 次写入 + 幂等重传，无 5xx
    assert h_codes.count(201) == 1
    assert all(c in (200, 409) for c in h_codes if c != 201)
    assert all(c in (201, 409) for c in l_codes)

    # 冻结不变式：交接清单 == 当前装载下交接时刻（11:00）的在车货物集合
    detail = client.get("/trips/TF").json()
    on_board_at_11 = {
        l["cargo_id"]
        for l in detail["loadings"]
        if (l["loaded_at"] <= "2026-01-01T11:00:00"
            < l["unloaded_at"])
    }
    handover_doc = client.get("/handovers/VH-F").json()
    frozen = {m["cargo_id"] for m in handover_doc["manifest"]}
    assert frozen == on_board_at_11
    assert "CF" in frozen
