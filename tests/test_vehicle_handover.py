"""整车交接 API 集成测试。

覆盖：
* 在车清单按装载时段左闭右开确定（装车时刻含、卸车时刻不含）；
* 成功后为清单内每票货物记一笔**普通交接事件**（事件查询 / 追责 / 责任接口兼容）；
* 交接与测温同刻：责任片段从该时刻归接收方；
* 整单拒绝（不留任何货物事件）：空车、双方相同、保管方非交出方、
  早于货物起始、同刻已有逐票交接、同车次同刻已有整车交接；
* 幂等：同单号同内容重传返回原交接单与原逐票事件编号；异内容冲突；
* 交接单保留当时货物清单快照；
* 补登记装载若改变任一已完成交接时刻的在车集合 -> 整批拒绝
  （不覆盖交接时刻 / 覆盖但货物在清单内 / 恰在卸车时刻首尾相接 等允许）；
* 未知车次 / 路径车次不一致 / 单号查询 404。
"""
from tests.conftest import event_ids_for


def make_cargo(client, cargo_id, limit="4", holder="A",
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


def make_trip(client, trip_id="T1", vehicle="京A-0001", note=None):
    r = client.post("/trips", json={"trip_id": trip_id, "vehicle_no": vehicle, "note": note})
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


def vehicle_handover(client, hid, trip_id, hh, giver, receiver, day="2026-10-01"):
    return client.post(
        f"/trips/{trip_id}/handovers",
        json={
            "handover_id": hid,
            "trip_id": trip_id,
            "handover_at": f"{day}T{hh:02d}:00:00+00:00",
            "giver": giver,
            "receiver": receiver,
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


def reading(client, rid, trip_id, hh, temp, day="2026-10-01"):
    return client.post(
        f"/trips/{trip_id}/readings",
        json={
            "reading_id": rid,
            "trip_id": trip_id,
            "read_at": f"{day}T{hh:02d}:00:00+00:00",
            "temperature": str(temp),
        },
    )


def _setup_two_cargo_trip(client):
    # G1 [02,10)；G2 [03,09)，起始保管方均为 A
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_trip(client, "T1")
    r = loadings(client, "T1", [loading("G1", 2, 10), loading("G2", 3, 9)])
    assert r.status_code == 201, r.text


# ----------------------------------------------------------- 成功路径


def test_vehicle_handover_success_creates_one_handover_event_per_cargo(client):
    _setup_two_cargo_trip(client)
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["handover_id"] == "VH-1"
    assert body["trip_id"] == "T1"
    assert body["giver"] == "A" and body["receiver"] == "B"
    assert [m["cargo_id"] for m in body["manifest"]] == ["G1", "G2"]

    # 每票货物各有一笔普通交接事件，编号即清单返回的 event_id
    ev1 = {e["event_id"]: e for e in client.get("/cargo/G1/events").json()}
    ev2 = {e["event_id"]: e for e in client.get("/cargo/G2/events").json()}
    ids = {m["cargo_id"]: m["event_id"] for m in body["manifest"]}
    for eid, cid in ((ids["G1"], "G1"), (ids["G2"], "G2")):
        ev = ev1[eid] if cid == "G1" else ev2[eid]
        assert ev["event_type"] == "handover"
        assert ev["giver"] == "A" and ev["receiver"] == "B"
        assert ev["occurred_at"].startswith("2026-10-01T05:00:00")

    # 两票货物当前保管方都已变为 B
    assert client.get("/cargo/G1/report").json()["current_holder"] == "B"
    assert client.get("/cargo/G2/report").json()["current_holder"] == "B"


def test_manifest_follows_half_open_loading_windows(client):
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_trip(client, "T1")
    # G1 [02,09)；G2 [09,12)：09 时刻 G1 已卸（右开）、G2 刚装（左闭）
    loadings(client, "T1", [loading("G1", 2, 9), loading("G2", 9, 12)])
    r = vehicle_handover(client, "VH-1", "T1", 9, "A", "B")
    assert r.status_code == 201, r.text
    assert [m["cargo_id"] for m in r.json()["manifest"]] == ["G2"]
    # 只有 G2 产生交接事件
    assert event_ids_for("G1") == set()
    assert len(event_ids_for("G2")) == 1


def test_handover_at_exact_loading_boundary_includes_loaded_cargo(client):
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 5, 10)])
    # 恰在装车时刻 05:00（左闭，在车上）
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 201
    assert [m["cargo_id"] for m in r.json()["manifest"]] == ["G1"]


def test_handover_manifest_is_a_snapshot_queryable_afterward(client):
    _setup_two_cargo_trip(client)
    body = vehicle_handover(client, "VH-1", "T1", 5, "A", "B").json()
    got = client.get("/handovers/VH-1")
    assert got.status_code == 200
    assert got.json() == body
    # 列表接口
    lst = client.get("/handovers").json()
    assert [h["handover_id"] for h in lst] == ["VH-1"]
    lst_t = client.get("/handovers?trip_id=T1").json()
    assert [h["handover_id"] for h in lst_t] == ["VH-1"]
    assert client.get("/handovers?trip_id=NOPE").status_code == 404
    assert client.get("/handovers/NOPE").status_code == 404
    # 清单项带装载时段快照
    m = body["manifest"][0]
    assert m["loaded_at"].startswith("2026-10-01T02:00:00")
    assert m["unloaded_at"].startswith("2026-10-01T10:00:00")


def test_second_handover_chain_continues_after_first(client):
    _setup_two_cargo_trip(client)
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    # 06:00 再交接 B -> C：此刻保管方已是 B，成功
    r = vehicle_handover(client, "VH-2", "T1", 6, "B", "C")
    assert r.status_code == 201, r.text
    assert client.get("/cargo/G1/report").json()["current_holder"] == "C"


# ----------------------------------------------------------- 整单拒绝


def test_empty_vehicle_rejected_without_events(client):
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")  # 不登记装载
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "empty_vehicle"
    assert event_ids_for("G1") == set()
    # 交接单也不存在
    assert client.get("/handovers/VH-1").status_code == 404


def test_giver_equal_receiver_rejected(client):
    _setup_two_cargo_trip(client)
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "A")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_handover_shape"
    assert event_ids_for("G1") == set() and event_ids_for("G2") == set()


def test_giver_not_current_holder_of_any_cargo_rejects_entire_order(client):
    _setup_two_cargo_trip(client)
    # 交出方 X 不是任何一票货物的当前保管方 A -> 整单拒绝
    r = vehicle_handover(client, "VH-1", "T1", 5, "X", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "giver_mismatch"
    assert event_ids_for("G1") == set() and event_ids_for("G2") == set()


def test_one_cargo_holder_mismatch_rejects_whole_order(client):
    # G1 的保管方是 A，G2 的保管方已经是 X；交出方 A 只对 G1 合法 -> 整单拒绝
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="X")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10), loading("G2", 3, 9)])
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "giver_mismatch"
    assert event_ids_for("G1") == set() and event_ids_for("G2") == set()


def test_late_single_handover_making_vehicle_order_invalid_rejects_entire_order(client):
    # G1 已有一笔更晚的交接 A->X，再提交整车 A->B 时 G1 在该时刻保管方
    # 虽仍为 A，但整链重算后既有交接 A->X 的交出方失效 -> 整单拒绝
    _setup_two_cargo_trip(client)
    own_hand(client, "late-h", "G1", 6, "A", "X")
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "giver_mismatch"
    # 两票都不产生事件；既有事件保持不变
    assert event_ids_for("G1") == {"late-h"}
    assert event_ids_for("G2") == set()


def test_before_cargo_start_rejects_entire_order(client):
    make_cargo(client, "G1", holder="A", start="2026-10-01T06:00:00+00:00")
    make_cargo(client, "G2", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10), loading("G2", 3, 9)])
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "before_start"
    assert event_ids_for("G1") == set() and event_ids_for("G2") == set()


def test_existing_single_handover_at_same_instant_rejects_order(client):
    _setup_two_cargo_trip(client)
    # G1 在 05:00 已有单票交接 A->B（G2 没有）：整车交接与 G1 同刻 -> 整单拒绝
    own_hand(client, "h-g1", "G1", 5, "A", "B")
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "C")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"
    assert event_ids_for("G1") == {"h-g1"}
    assert event_ids_for("G2") == set()


def test_duplicate_vehicle_handover_same_instant_rejected(client):
    _setup_two_cargo_trip(client)
    assert vehicle_handover(client, "VH-1", "T1", 5, "A", "B").status_code == 201
    # 同车次同刻、不同单号 -> 拒绝
    r = vehicle_handover(client, "VH-2", "T1", 5, "B", "C")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"
    # 每票货物仍只有一笔事件
    assert len(event_ids_for("G1")) == 1
    assert len(event_ids_for("G2")) == 1


def test_unknown_trip_and_path_body_mismatch(client):
    r = client.post(
        "/trips/NOPE/handovers",
        json={"handover_id": "VH-1", "trip_id": "NOPE",
              "handover_at": "2026-10-01T05:00:00Z", "giver": "A", "receiver": "B"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"

    make_trip(client, "T1")
    r2 = client.post(
        "/trips/T1/handovers",
        json={"handover_id": "VH-1", "trip_id": "T2",
              "handover_at": "2026-10-01T05:00:00Z", "giver": "A", "receiver": "B"},
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "bad_handover_shape"


# ----------------------------------------------------------- 幂等


def test_same_id_same_content_replay_returns_original(client):
    _setup_two_cargo_trip(client)
    body1 = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert body1.status_code == 201
    first = body1.json()
    # 完全重传
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 200
    payload = r.json()
    assert payload["replayed"] is True
    assert payload["handover"] == first
    # 不重复产生货物事件
    assert len(event_ids_for("G1")) == 1
    assert len(event_ids_for("G2")) == 1


def test_replay_with_different_timezone_spelling_is_same_instant(client):
    _setup_two_cargo_trip(client)
    first = vehicle_handover(client, "VH-1", "T1", 5, "A", "B").json()
    r = client.post(
        "/trips/T1/handovers",
        json={
            "handover_id": "VH-1",
            "trip_id": "T1",
            "handover_at": "2026-10-01T13:00:00+08:00",  # == 05:00Z
            "giver": "A",
            "receiver": "B",
        },
    )
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["handover"]["manifest"] == first["manifest"]


def test_same_id_different_content_conflict(client):
    _setup_two_cargo_trip(client)
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    # 异内容（时刻不同）
    r = vehicle_handover(client, "VH-1", "T1", 6, "A", "B")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "handover_id_conflict"
    # 异内容（接收方不同）
    r2 = vehicle_handover(client, "VH-1", "T1", 5, "A", "C")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "handover_id_conflict"
    # 原交接单不变
    got = client.get("/handovers/VH-1").json()
    assert got["receiver"] == "B"
    assert got["handover_at"].startswith("2026-10-01T05:00:00")
    assert len(event_ids_for("G1")) == 1


# ----------------------------------------------------------- 同刻测温 / 责任拆分


def test_handover_and_measurement_same_instant_segment_goes_to_receiver(client):
    # G1 04:00 车载 8℃ 开区间；05:00 整车交接 A->B，且 05:00 同刻有车载测温 8℃；
    # 06:00 车载 2℃ 闭合。交接时刻起片段归接收方 B（同刻测温先于交接处理）。
    make_cargo(client, "G1", limit="4", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    reading(client, "r1", "T1", 4, 8)
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    reading(client, "r2", "T1", 5, 8)   # 同刻车载测温
    reading(client, "r3", "T1", 6, 2)
    rep = client.get("/cargo/G1/report").json()
    segments = rep["excursions"][0]["segments"]
    assert [s["responsible_party"] for s in segments] == ["A", "B"]
    # 交接时刻 05:00 起归接收方
    assert segments[0]["end"].startswith("2026-10-01T05:00:00")
    assert segments[1]["start"].startswith("2026-10-01T05:00:00")


def test_own_measurement_at_handover_instant_still_allowed(client):
    # 同刻货物自有测温与整车交接互不冲突；自有测温照常进入时间线
    make_cargo(client, "G1", limit="4", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    reading(client, "r1", "T1", 4, 8)
    own_meas(client, "m5", "G1", 5, 9)
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 201, r.text
    reading(client, "r2", "T1", 6, 2)
    seg = client.get("/cargo/G1/report").json()["excursions"][0]["segments"]
    assert [s["responsible_party"] for s in seg] == ["A", "B"]


def test_responsibility_stances_revalidated_after_vehicle_handover(client):
    # 先有已闭合区间 [04,06) 全归 A 并有确认表态；晚到 05:00 整车交接 A->B
    # 重建后区间被拆成 A/B 两段：旧表态对 A 段坐标变化（[04,06)->[04,05)）而失效，
    # 须以新快照重新表态；旧表态历史仍可查（现有快照规则不变）。
    make_cargo(client, "G1", limit="4", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    reading(client, "r1", "T1", 4, 8)
    reading(client, "r2", "T1", 6, 2)
    iv = client.get("/cargo/G1/responsibilities").json()["intervals"][0]
    stale_snap = iv["snapshot_id"]
    client.post(
        "/cargo/G1/responsibilities/onboard:r1/stances",
        json={"snapshot_id": stale_snap, "responsible_party": "A",
              "stance": "confirm", "stance_id": "s-a-1"},
    )
    # 晚到整车交接，责任片段变化
    r = vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    assert r.status_code == 201, r.text
    new_iv = client.get("/cargo/G1/responsibilities").json()["intervals"][0]
    assert [s["responsible_party"] for s in new_iv["segments"]] == ["A", "B"]
    assert new_iv["snapshot_id"] != stale_snap
    # 旧快照表态被拒
    r2 = client.post(
        "/cargo/G1/responsibilities/onboard:r1/stances",
        json={"snapshot_id": stale_snap, "responsible_party": "B",
              "stance": "confirm", "stance_id": "s-b-bad"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "stale_snapshot"
    # 以新快照，B 可表态；旧表态历史保留且 is_current=false
    r3 = client.post(
        "/cargo/G1/responsibilities/onboard:r1/stances",
        json={"snapshot_id": new_iv["snapshot_id"], "responsible_party": "B",
              "stance": "confirm", "stance_id": "s-b-1"},
    )
    assert r3.status_code == 201, r3.text
    hist = client.get("/cargo/G1/responsibilities/onboard:r1/stances").json()
    old = [h for h in hist["history"] if h["stance_id"] == "s-a-1"][0]
    assert old["is_current"] is False


# ----------------------------------------------------------- 清单冻结


def test_late_loading_changing_completed_handover_set_rejected(client):
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_trip(client, "T1")
    # 先只登记 G1 [02,10)
    assert loadings(client, "T1", [loading("G1", 2, 10)]).status_code == 201
    # 05:00 整车交接：清单只有 G1
    body = vehicle_handover(client, "VH-1", "T1", 5, "A", "B").json()
    assert [m["cargo_id"] for m in body["manifest"]] == ["G1"]
    # 事后给 G2 补登记 [03,06)：会覆盖 05:00 交接时刻并引入清单外货物 -> 整批拒绝
    r = loadings(client, "T1", [loading("G2", 3, 6)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_manifest_frozen"
    # G2 装载未写入；G1 交接单快照不变
    assert client.get("/trips/T1").json()["loadings"] == [
        {"cargo_id": "G1",
         "loaded_at": "2026-10-01T02:00:00Z",
         "unloaded_at": "2026-10-01T10:00:00Z",
         "trip_id": "T1"}
    ]
    got = client.get("/handovers/VH-1").json()
    assert [m["cargo_id"] for m in got["manifest"]] == ["G1"]


def test_late_loading_on_another_trip_at_handover_time_allowed(client):
    # 交接在 T1 上完成；之后在另一车次 T2 上给 G2 登记同一时刻附近的时段，
    # 不影响 T1 交接时刻的在车集合（G2 当时不在 T1），允许。
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_trip(client, "T1")
    make_trip(client, "T2")
    loadings(client, "T1", [loading("G1", 2, 10)])
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    # G2 在 T2 的 [03,06)：与 T1 交接单无关 -> 允许
    r = loadings(client, "T2", [loading("G2", 3, 6)])
    assert r.status_code == 201, r.text


def test_late_loading_not_covering_handover_instant_allowed(client):
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_cargo(client, "G3", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    # G3 [06,08)：在交接时刻之后，不覆盖 05:00 -> 允许
    assert loadings(client, "T1", [loading("G3", 6, 8)]).status_code == 201
    # G2 [03,05)：右开，05:00 已不在车（不覆盖交接时刻）-> 允许
    assert loadings(client, "T1", [loading("G2", 3, 5)]).status_code == 201


def test_late_loading_starting_exactly_at_handover_instant_rejected(client):
    # 补登记 G2 [05,07)：左闭，05:00 即在车 -> 改变交接时刻集合，拒绝
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    r = loadings(client, "T1", [loading("G2", 5, 7)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_manifest_frozen"


def test_batch_late_loading_partial_freeze_leaves_no_records(client):
    # 同批中 G3 合法（不覆盖交接时刻）但 G2 会改变集合 -> 整批回滚
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_cargo(client, "G3", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    r = loadings(client, "T1", [loading("G3", 6, 8), loading("G2", 3, 6)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_manifest_frozen"
    detail = client.get("/trips/T1").json()
    assert [l["cargo_id"] for l in detail["loadings"]] == ["G1"]


def test_late_loading_rejected_if_it_changes_any_one_of_multiple_handovers(client):
    # 两笔已完成交接 05:00（清单 G1）、08:00（清单仍只有 G1）；
    # 新货物补登记的时段只要覆盖任一已完成交接时刻就会被拒。
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_cargo(client, "G3", holder="C")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 12)])
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    vehicle_handover(client, "VH-2", "T1", 8, "B", "C")
    # G2 [06,08)：对 08:00 右开不覆盖，也不覆盖 05:00 -> 允许
    assert loadings(client, "T1", [loading("G2", 6, 8)]).status_code == 201
    # G3 [07,09)：覆盖 08:00 交接时刻且不在清单 -> 整批拒绝
    r = loadings(client, "T1", [loading("G3", 7, 9)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_manifest_frozen"


def test_freeze_check_runs_before_future_handovers_only_at_instant(client):
    # 交接在 05:00；G2 补登记 [03,06) 被拒；改为 [05,05) 非法窗口也被拒（invalid_loading_window）
    make_cargo(client, "G1", holder="A")
    make_cargo(client, "G2", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    vehicle_handover(client, "VH-1", "T1", 5, "A", "B")
    r = client.post(
        "/trips/T1/loadings",
        json={"loadings": [{
            "cargo_id": "G2",
            "loaded_at": "2026-10-01T06:00:00Z",
            "unloaded_at": "2026-10-01T05:00:00Z",
        }]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"
