"""整车交接 API 集成测试。

覆盖：
* 成功路径：按装载时段左闭右开确定在车清单、为每票货物产生普通交接事件、
  保管方变更、责任自交接时刻归接收方（含交接与测温同刻）；
* 整单拒绝且不产生任何货物事件：空车、双方相同、保管方非交出方、
  某票货物同刻已有交接、车次同刻已有整车交接、车次不存在、早于货物起始；
* 幂等：同交接单号同内容返回原结果（同一批事件编号），异内容冲突；
* 交接单保留当时清单：此后补登记装载若改变任一已完成交接时刻的在车集合
  则整批拒绝（handover_manifest_conflict）；无关时刻 / 首尾相接 / 其他车次允许；
* 逐票事件与既有事件查询、追责、责任表态接口完全兼容。
"""
from tests.conftest import event_ids_for


def make_cargo(client, cargo_id, limit="4", holder="A仓库",
               start="2026-10-01T00:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def make_trip(client, trip_id="T1", vehicle="京A-0001"):
    r = client.post("/trips", json={"trip_id": trip_id, "vehicle_no": vehicle})
    assert r.status_code == 201, r.text
    return r.json()


def loadings(client, trip_id, entries):
    return client.post(f"/trips/{trip_id}/loadings", json={"loadings": entries})


def loading(cid, lh, uh, day="2026-10-01"):
    return {
        "cargo_id": cid,
        "loaded_at": f"{day}T{lh:02d}:00:00+00:00",
        "unloaded_at": f"{day}T{uh:02d}:00:00+00:00",
    }


def handover(client, trip_id, hid, hh, giver, receiver, day="2026-10-01"):
    return client.post(
        f"/trips/{trip_id}/handovers",
        json={
            "handover_id": hid,
            "trip_id": trip_id,
            "handed_over_at": f"{day}T{hh:02d}:00:00+00:00",
            "giver": giver,
            "receiver": receiver,
        },
    )


def own_meas(client, eid, cid, hh, temp, day="2026-10-01"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cid,
            "occurred_at": f"{day}T{hh:02d}:00:00+00:00",
            "temperature": str(temp),
        },
    )


def own_hand(client, eid, cid, hh, giver, receiver, day="2026-10-01"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cid,
            "occurred_at": f"{day}T{hh:02d}:00:00+00:00",
            "giver": giver,
            "receiver": receiver,
        },
    )


# ----------------------------------------------------------- 成功路径


def test_vehicle_handover_success_creates_events_per_cargo(client):
    make_cargo(client, "C1", holder="A仓库")
    make_cargo(client, "C2", holder="A仓库")
    make_cargo(client, "C3", holder="X")  # 不在车上
    make_trip(client, "T1")
    r = loadings(client, "T1", [
        loading("C1", 8, 18), loading("C2", 9, 17),
    ])
    assert r.status_code == 201, r.text

    r = handover(client, "T1", "VH-1", 12, "A仓库", "B仓库")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["handover_id"] == "VH-1"
    assert body["giver"] == "A仓库" and body["receiver"] == "B仓库"
    manifest = {item["cargo_id"]: item["event_id"] for item in body["manifest"]}
    # 12:00 在车：C1、C2；C3 无装载，不在清单
    assert set(manifest) == {"C1", "C2"}
    # 逐票事件编号为普通交接事件，全局唯一
    assert len(set(manifest.values())) == 2

    # 每票货物各有一笔普通交接事件，双方为 A仓库 -> B仓库
    for cid in ("C1", "C2"):
        evs = client.get(f"/cargo/{cid}/events").json()
        hs = [e for e in evs if e["event_type"] == "handover"]
        assert len(hs) == 1
        assert hs[0]["event_id"] == manifest[cid]
        assert hs[0]["giver"] == "A仓库" and hs[0]["receiver"] == "B仓库"
        # 追责重建的当前保管方已变为接收方
        assert client.get(f"/cargo/{cid}/report").json()["current_holder"] == "B仓库"

    # 交接单可查：车次列表 + 直查
    lst = client.get("/trips/T1/handovers").json()
    assert [h["handover_id"] for h in lst] == ["VH-1"]
    got = client.get("/handovers/VH-1").json()
    assert {i["cargo_id"] for i in got["manifest"]} == {"C1", "C2"}


def test_handover_manifest_left_closed_right_open(client):
    make_cargo(client, "C1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 10, 12)])

    # 起点（左闭）：在车上，交接成功
    r = handover(client, "T1", "VH-LO", 10, "A", "B")
    assert r.status_code == 201, r.text

    # 终点（右开）：已卸车，空车拒绝
    r = handover(client, "T1", "VH-RO", 12, "B", "C")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "empty_vehicle"


def test_vehicle_handover_chained_holder(client):
    make_cargo(client, "C1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 20)])

    assert handover(client, "T1", "VH-1", 10, "A", "B").status_code == 201
    assert handover(client, "T1", "VH-2", 12, "B", "C").status_code == 201
    r = handover(client, "T1", "VH-3", 14, "B", "D")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "giver_mismatch"
    assert client.get("/cargo/C1/report").json()["current_holder"] == "C"


# ----------------------------------------------------------- 整单拒绝


def test_empty_vehicle_rejected(client):
    make_cargo(client, "C1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 10, 12)])
    # 13:00 已无在车货物
    r = handover(client, "T1", "VH-1", 13, "A", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "empty_vehicle"
    assert event_ids_for("C1") == set()


def test_same_party_rejected(client):
    make_cargo(client, "C1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 10, 12)])
    r = handover(client, "T1", "VH-1", 11, "A", "A")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_handover_shape"
    assert event_ids_for("C1") == set()


def test_giver_mismatch_rejects_whole_order(client):
    make_cargo(client, "C1", holder="A")
    make_cargo(client, "C2", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 18), loading("C2", 8, 18)])
    # C1 此前已单独交接 A -> X，12:00 保管方为 X；整单应拒绝
    own_hand(client, "h1", "C1", 11, "A", "X")

    before = event_ids_for("C1") | event_ids_for("C2")
    r = handover(client, "T1", "VH-1", 12, "A", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "giver_mismatch"
    # 两票货物都没有新增任何事件
    assert event_ids_for("C1") | event_ids_for("C2") == before
    # 交接单不存在
    assert client.get("/handovers/VH-1").status_code == 404


def test_before_start_rejected(client):
    make_cargo(client, "C1", holder="A", start="2026-10-01T09:00:00+00:00")
    make_trip(client, "T1")
    # 装载时段早于货物起始也允许登记；交接时刻 08:00 早于起始 -> 拒绝
    loadings(client, "T1", [loading("C1", 7, 12)])
    r = handover(client, "T1", "VH-1", 8, "A", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "before_start"
    assert event_ids_for("C1") == set()


def test_same_timestamp_existing_cargo_handover_rejected(client):
    make_cargo(client, "C1", holder="A")
    make_cargo(client, "C2", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 18), loading("C2", 8, 18)])
    own_hand(client, "h1", "C1", 12, "A", "X")  # C1 在 12:00 已有交接

    r = handover(client, "T1", "VH-1", 12, "X", "B")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"
    assert event_ids_for("C2") == set()  # C2 也不得产生事件


def test_same_timestamp_measurement_allowed(client):
    # 测温与交接同刻不互斥（测温先于交接处理）
    make_cargo(client, "C1", holder="A", limit="4")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 18)])
    own_meas(client, "m1", "C1", 12, "9")  # 同刻超限测温
    r = handover(client, "T1", "VH-1", 12, "A", "B")
    assert r.status_code == 201, r.text


def test_same_timestamp_two_vehicle_handovers_rejected(client):
    make_cargo(client, "C1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 18)])
    assert handover(client, "T1", "VH-1", 12, "A", "B").status_code == 201
    r = handover(client, "T1", "VH-2", 12, "B", "C")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"


def test_unknown_trip_404(client):
    r = handover(client, "NOPE", "VH-1", 12, "A", "B")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_path_body_trip_mismatch(client):
    make_trip(client, "T1")
    r = client.post(
        "/trips/T1/handovers",
        json={
            "handover_id": "VH-1", "trip_id": "T2",
            "handed_over_at": "2026-10-01T12:00:00+00:00",
            "giver": "A", "receiver": "B",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_handover_shape"


# ----------------------------------------------------------- 幂等 / 冲突


def test_idempotent_replay_same_content(client):
    make_cargo(client, "C1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 18)])

    first = handover(client, "T1", "VH-1", 12, "A", "B")
    assert first.status_code == 201
    events_after_first = event_ids_for("C1")

    second = handover(client, "T1", "VH-1", 12, "A", "B")
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["handover"]["handover_id"] == "VH-1"
    assert [i["event_id"] for i in body["handover"]["manifest"]] == [
        i["event_id"] for i in first.json()["manifest"]
    ]
    # 不重复产生事件
    assert event_ids_for("C1") == events_after_first


def test_same_id_different_content_conflict(client):
    make_cargo(client, "C1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 20)])
    assert handover(client, "T1", "VH-1", 12, "A", "B").status_code == 201

    r = handover(client, "T1", "VH-1", 13, "A", "B")  # 不同时刻
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "handover_id_conflict"

    r = handover(client, "T1", "VH-1", 12, "A", "C")  # 不同接收方
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "handover_id_conflict"


# ----------------------------------------------------------- 清单冻结


def test_loading_after_handover_cannot_change_manifest(client):
    make_cargo(client, "C1", holder="A")
    make_cargo(client, "C2", holder="A")
    make_trip(client, "T1")
    # 先只登记 C1 [08,18)，12:00 完成整车交接（清单仅 C1）
    loadings(client, "T1", [loading("C1", 8, 18)])
    assert handover(client, "T1", "VH-1", 12, "A", "B").status_code == 201

    # 事后补登 C2 [10,14)：会使 12:00 在车集合变成 {C1,C2} -> 整批拒绝
    r = loadings(client, "T1", [loading("C2", 10, 14)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "handover_manifest_conflict"
    # C2 无装载记录（整批回滚）
    detail = client.get("/trips/T1").json()
    assert {l["cargo_id"] for l in detail["loadings"]} == {"C1"}


def test_loading_unrelated_window_allowed(client):
    make_cargo(client, "C1", holder="A")
    make_cargo(client, "C2", holder="B")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 18)])
    handover(client, "T1", "VH-1", 12, "A", "B")

    # C2 的时段在交接时刻 12:00 之后才开始 -> 不改变冻结清单，允许
    r = loadings(client, "T1", [loading("C2", 13, 20)])
    assert r.status_code == 201, r.text


def test_loading_adjacent_window_allowed(client):
    # 新窗口在交接时刻刚好右开（loaded_at == 交接时刻）不改变该时刻集合
    make_cargo(client, "C1", holder="A")
    make_cargo(client, "C2", holder="B")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 12)])
    handover(client, "T1", "VH-1", 11, "A", "B")

    # C2 [11,?）会改变 11:00 集合 -> 拒绝
    assert loadings(client, "T1", [loading("C2", 11, 14)]).status_code == 409
    # C2 [12,14)：11:00 不在车上（且与 C1 的 [8,12) 首尾相接语义无关，不同货物）-> 允许
    assert loadings(client, "T1", [loading("C2", 12, 14)]).status_code == 201


def test_loading_on_other_trip_does_not_affect_frozen_manifest(client):
    make_cargo(client, "C1", holder="A")
    make_cargo(client, "C2", holder="A")
    make_trip(client, "T1")
    make_trip(client, "T2")
    loadings(client, "T1", [loading("C1", 8, 18)])
    handover(client, "T1", "VH-1", 12, "A", "B")

    # C2 登在另一车次 T2 上：不改变 T1 交接时刻清单 -> 允许
    r = loadings(client, "T2", [loading("C2", 10, 14)])
    assert r.status_code == 201, r.text


def test_freeze_checks_all_completed_handovers(client):
    make_cargo(client, "C1", holder="A")
    make_cargo(client, "C2", holder="B")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 20)])
    handover(client, "T1", "VH-1", 10, "A", "B")
    handover(client, "T1", "VH-2", 15, "B", "C")

    # C2 [09,12)：改变 VH-1（10:00 在车）但不影响 VH-2（15:00 不在车）-> 仍拒绝
    assert loadings(client, "T1", [loading("C2", 9, 12)]).status_code == 409
    # C2 [16,18)：两个交接时刻（10:00、15:00）都不在车 -> 允许
    assert loadings(client, "T1", [loading("C2", 16, 18)]).status_code == 201


# ----------------------------------------------------------- 责任兼容


def test_handover_same_time_as_measurement_responsibility(client):
    # 交接与测温同刻：责任片段自该时刻归接收方
    make_cargo(client, "C1", holder="A", limit="4")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 20)])
    own_meas(client, "m1", "C1", 10, "9")   # 开启越温
    handover(client, "T1", "VH-1", 12, "A", "B")
    own_meas(client, "m2", "C1", 14, "3")   # 闭合

    report = client.get("/cargo/C1/report").json()
    ex = report["excursions"][0]
    parties = [(s["responsible_party"], s["start"][11:19]) for s in ex["segments"]]
    # [10:00,12:00) A；[12:00,14:00) B
    assert parties == [("A", "10:00:00"), ("B", "12:00:00")]

    # B 对自己的片段按现有责任接口确认：快照校验、表态、历史均兼容
    iid = ex["interval_id"]
    snap = client.get(f"/cargo/C1/responsibilities/{iid}").json()["snapshot_id"]
    r = client.post(
        f"/cargo/C1/responsibilities/{iid}/stances",
        json={
            "snapshot_id": snap, "responsible_party": "B",
            "stance": "confirm", "stance_id": "st-b-1",
        },
    )
    assert r.status_code == 201, r.text
    view = client.get(f"/cargo/C1/responsibilities/{iid}").json()
    assert view["summary"] == "pending"  # A 尚未表态
    assert view["latest_stances"][0]["responsible_party"] == "B"


def test_derived_events_appear_in_readings_and_report(client):
    # 整车交接事件作为普通自有事件参与全部既有查询
    make_cargo(client, "C1", holder="A", limit="4")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("C1", 8, 20)])
    handover(client, "T1", "VH-1", 12, "A", "B")

    evs = client.get("/cargo/C1/events").json()
    assert len(evs) == 1 and evs[0]["event_type"] == "handover"
    readings = client.get("/cargo/C1/readings").json()["readings"]
    assert readings == []  # 交接不是测温
