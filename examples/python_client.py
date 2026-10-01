"""冷链追责服务 Python 调用示例（仅依赖 httpx）。

运行：python examples/python_client.py
默认地址 http://127.0.0.1:8000，可用环境变量 BASE_URL 覆盖。
"""
import os

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
CARGO = "COLD-DEMO-2"


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n== {title} [{resp.status_code}] ==")
    print(resp.text)


def demo_vehicle_handover(api: httpx.Client) -> None:
    """整车交接链路：在车清单自动确定、逐票事件、整单拒绝、幂等、清单冻结。"""
    trip = "PY-TRIP-HO"
    c1, c2 = "PY-HO-CARGO-1", "PY-HO-CARGO-2"
    for cid, limit in ((c1, "4.0"), (c2, "7.0")):
        show(
            f"创建货物 {cid}",
            api.post(
                "/cargo",
                json={
                    "cargo_id": cid,
                    "started_at": "2026-10-01T08:00:00+08:00",
                    "temp_upper_limit": limit,
                    "initial_holder": "北京冷库",
                },
            ),
        )
    show("登记车次", api.post("/trips", json={"trip_id": trip, "vehicle_no": "京A·P0001"}))
    show(
        "登记装载（左闭右开）",
        api.post(
            f"/trips/{trip}/loadings",
            json={
                "loadings": [
                    {
                        "cargo_id": c1,
                        "loaded_at": "2026-10-01T10:00:00+08:00",
                        "unloaded_at": "2026-10-01T18:00:00+08:00",
                    },
                    {
                        "cargo_id": c2,
                        "loaded_at": "2026-10-01T11:00:00+08:00",
                        "unloaded_at": "2026-10-01T17:00:00+08:00",
                    },
                ]
            },
        ),
    )
    body = {
        "handover_id": "PY-HO-1",
        "trip_id": trip,
        "handed_over_at": "2026-10-01T12:00:00+08:00",
        "giver": "北京冷库",
        "receiver": "济南冷库",
    }
    show("整车交接 12:00（清单=C1,C2，逐票返回普通交接事件编号）",
         api.post(f"/trips/{trip}/handovers", json=body))
    show("同单号同内容重传 -> 200 replayed（返回原交接单/原事件编号）",
         api.post(f"/trips/{trip}/handovers", json=body))
    show(
        "同单号异内容 -> 409 handover_id_conflict",
        api.post(
            f"/trips/{trip}/handovers",
            json={**body, "receiver": "青岛冷库"},
        ),
    )
    show(
        "双方相同 -> 422 bad_handover_shape",
        api.post(
            f"/trips/{trip}/handovers",
            json={**body, "handover_id": "PY-HO-BAD", "receiver": "北京冷库"},
        ),
    )
    show(
        "保管方不符 -> 422 giver_mismatch（12:30 保管方已是济南冷库）",
        api.post(
            f"/trips/{trip}/handovers",
            json={
                "handover_id": "PY-HO-BAD2",
                "trip_id": trip,
                "handed_over_at": "2026-10-01T12:30:00+08:00",
                "giver": "北京冷库",
                "receiver": "青岛冷库",
            },
        ),
    )
    show(
        "补登记装载改变 12:00 在车集合 -> 409 handover_manifest_conflict",
        api.post(
            "/cargo",
            json={
                "cargo_id": "PY-HO-CARGO-NEW",
                "started_at": "2026-10-01T08:00:00+08:00",
                "temp_upper_limit": "4.0",
                "initial_holder": "北京冷库",
            },
        ),
    )
    show(
        "补登记装载改变 12:00 在车集合 -> 409 handover_manifest_conflict",
        api.post(
            f"/trips/{trip}/loadings",
            json={
                "loadings": [
                    {
                        "cargo_id": "PY-HO-CARGO-NEW",
                        "loaded_at": "2026-10-01T11:30:00+08:00",
                        "unloaded_at": "2026-10-01T12:30:00+08:00",
                    }
                ]
            },
        ),
    )
    show("车次交接单列表", api.get(f"/trips/{trip}/handovers"))
    show("按单号直查交接单（冻结清单 + 逐票事件编号）",
         api.get("/handovers/PY-HO-1"))
    show("既有事件接口兼容：C1 事件列表含派生的普通交接事件",
         api.get(f"/cargo/{c1}/events"))


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as api:
        show(
            "创建货物",
            api.post(
                "/cargo",
                json={
                    "cargo_id": CARGO,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )

        # 测温：温度高于上限 -> 开启越温区间
        show(
            "超限测温",
            api.post(
                "/events",
                json={
                    "event_id": "p-t1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T10:00:00+08:00",
                    "temperature": "8.2",
                },
            ),
        )

        # 交接：必须带交出方与接收方；交出方须为当时保管方
        show(
            "合法交接 北京->上海",
            api.post(
                "/events",
                json={
                    "event_id": "p-h1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T12:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "上海冷库",
                },
            ),
        )

        # 错误交出方 -> 422，整体拒绝
        show(
            "非法交接（交出方不是保管方，拒绝）",
            api.post(
                "/events",
                json={
                    "event_id": "p-bad",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T13:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "广州冷库",
                },
            ),
        )

        # 不超限测温 -> 闭合区间
        show(
            "恢复测温（闭合区间）",
            api.post(
                "/events",
                json={
                    "event_id": "p-t2",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T15:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )

        show("追责报告", api.get(f"/cargo/{CARGO}/report"))
        show("事件列表（按时间）", api.get(f"/cargo/{CARGO}/events"))

        # ----- 责任确认（仅针对已闭合越温区间）-----
        resp = api.get(f"/cargo/{CARGO}/responsibilities")
        show("责任确认：已闭合区间列表", resp)
        intervals = resp.json().get("intervals", [])
        if not intervals:
            print("（没有已闭合越温区间，跳过责任确认演示）")
            return

        interval = intervals[0]
        iid = interval["interval_id"]       # 区间标识 = 开启测温事件ID
        snap = interval["snapshot_id"]      # 当前快照标识（区间起止 + 责任片段）
        print(f"区间标识={iid} 结束测温事件={interval['end_event_id']} "
              f"快照={snap} 初始汇总={interval['summary']}")

        # 起始保管方确认担责
        show(
            "北京冷库 确认",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 异议必须带理由；忘记理由 -> 422 dispute_reason_required
        show(
            "上海冷库 异议但未带理由（拒绝示例）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "stance_id": "py-stance-bad",
                },
            ),
        )

        # 合法异议
        show(
            "上海冷库 异议（带理由）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "reason": "温控参数由交出方远程设定，接收方未实际接管",
                    "stance_id": "py-stance-2",
                },
            ),
        )

        show(
            "区间责任确认视图（此时汇总 has_dispute）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # 同编号同内容重传 -> 200 replayed，返回原记录
        show(
            "表态幂等重传",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 改口：新编号，历次表态保留
        show(
            "上海冷库 改口为确认（新编号）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-3",
                },
            ),
        )
        show(
            "表态历史（历次表态，含 superseded/is_current 标记）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}/stances"),
        )
        show(
            "最终区间视图（汇总 all_confirmed）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # —— 整车交接链路（本轮新增）——
        demo_vehicle_handover(api)


if __name__ == "__main__":
    main()
