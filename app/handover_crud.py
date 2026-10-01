"""整车交接的写入与查询逻辑。

一次整车交接
------------
操作员提交「车次、交接时刻、交出方、接收方、唯一交接单号」，服务端：

1. **锁车次行**（``SELECT ... FOR UPDATE``）：与同车次的装载登记 / 车载读数 /
   其他整车交接互斥——在车清单依据的装载时段在本事务内不会变化；
2. 按装载时段**左闭右开**（``loaded_at <= handover_at < unloaded_at``）确定
   交接时刻仍在该车次上的货物清单（快照）；
3. 按货物编号排序锁定清单内全部货物行（固定加锁顺序，杜绝死锁），
   与这些货物的单票事件写入、责任表态、装载登记、车载读数写入串行；
4. 对每票货物做整链校验（复用单票事件规则）：
   - 交接时刻不得早于货物起始时间；
   - 交接时刻的保管方必须是交出方（晚到插入若使既有交接失效同样拒绝）；
   - 同一货物同一时刻至多一笔交接（同刻已有交接 -> 整单拒绝）；
5. 任一校验失败（含空车、双方相同、同刻已有车次交接）**整单回滚**，
   不产生任何货物事件；
6. 校验通过后在同一事务内为清单内每票货物写入一笔**普通交接事件**
   （``event_type='handover'``），并把交接单与清单快照落库。

幂等
----
* 同交接单号 + 同内容（车次 / 时刻 / 交出方 / 接收方，时刻按同一时刻归一化）
  重传：返回原交接单与原逐票事件编号（``replayed=true``，不重复写入）；
* 同交接单号 + 异内容：409 ``handover_id_conflict``。

清单冻结
--------
交接单永久保留写入时的在车货物清单（``vehicle_handover_item`` 快照）。
后续在该车次上补登记装载时段时（见 trip_crud），若新区间会覆盖任一已完成
交接时刻且会引入清单外货物（即改变该时刻的在车集合），整批装载拒绝
（``409 loading_manifest_frozen``）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import crud
from app.crud import ApiError, _conflict, _unprocessable, _validate_prospective
from app.models import CargoLoading, Event, EventType, VehicleHandover, VehicleHandoverItem
from app.schemas import VehicleHandoverCreate
from app.timeutil import ensure_utc
from app.trip_crud import _load_cargos_locked_ordered, _load_trip_locked


# ----------------------------------------------------------- 结果 / 辅助


@dataclass
class HandoverWriteResult:
    handover: VehicleHandover
    items: list[VehicleHandoverItem]
    replayed: bool


def _event_id_for(handover_id: str, cargo_id: str) -> str:
    """由交接单号与货物编号派生逐票交接事件ID（确定性、跨重试稳定）。"""
    h = hashlib.sha256(f"{handover_id}\n{cargo_id}".encode("utf-8")).hexdigest()[:8]
    c = hashlib.sha256(cargo_id.encode("utf-8")).hexdigest()[:8]
    return f"hv:{h}:{c}"


def _same_handover_content(existing: VehicleHandover, at, payload: VehicleHandoverCreate) -> bool:
    """逐字段比对（handover_id 已相同）；时间归一化到 UTC。"""
    return (
        existing.trip_id == payload.trip_id
        and ensure_utc(existing.handover_at) == at
        and existing.giver == payload.giver
        and existing.receiver == payload.receiver
    )


def _load_items(db, handover_id: str) -> list[VehicleHandoverItem]:
    """读取交接单清单快照（按插入顺序，即创建时 (装车时刻, 货物编号) 排序）。"""
    rows = list(
        db.scalars(
            select(VehicleHandoverItem)
            .where(VehicleHandoverItem.handover_id == handover_id)
            .order_by(VehicleHandoverItem.id)
        ).all()
    )
    for r in rows:
        r.loaded_at = ensure_utc(r.loaded_at)
        r.unloaded_at = ensure_utc(r.unloaded_at)
    return rows


def _manifest_rows(db, trip_id: str, at) -> list[CargoLoading]:
    """按左闭右开确定交接时刻仍在该车次上的装载时段（即当时在车货物）。"""
    rows = list(
        db.scalars(
            select(CargoLoading).where(CargoLoading.trip_id == trip_id)
        ).all()
    )
    on_board = []
    for row in rows:
        if ensure_utc(row.loaded_at) <= at < ensure_utc(row.unloaded_at):
            row.loaded_at = ensure_utc(row.loaded_at)
            row.unloaded_at = ensure_utc(row.unloaded_at)
            on_board.append(row)
    on_board.sort(key=lambda r: (r.loaded_at, r.cargo_id))
    return on_board


# ----------------------------------------------------------- 写入


def create_vehicle_handover(db, payload: VehicleHandoverCreate) -> HandoverWriteResult:
    payload.handover_at = ensure_utc(payload.handover_at)
    # SQLite 文件库无行级锁，busy timeout 耗尽时整体重试一次
    try:
        return _create_tx(db, payload)
    except OperationalError:
        db.rollback()
        return _create_tx(db, payload)


def _create_tx(db, payload: VehicleHandoverCreate) -> HandoverWriteResult:
    at = payload.handover_at
    try:
        # 形状校验：交出方 / 接收方必须不同
        if payload.giver == payload.receiver:
            raise _unprocessable(
                "整车交接的交出方与接收方不能相同",
                code="bad_handover_shape",
            )

        # 1) 先锁车次行：串行化同车次的交接 / 装载 / 读数，同时保证车次存在
        _load_trip_locked(db, payload.trip_id)

        # 2) 幂等优先：交接单号全局唯一
        existing = db.get(VehicleHandover, payload.handover_id)
        if existing is not None:
            if _same_handover_content(existing, at, payload):
                db.rollback()  # 仅释放车次行锁，未做任何修改
                existing.handover_at = ensure_utc(existing.handover_at)
                items = _load_items(db, payload.handover_id)
                return HandoverWriteResult(
                    handover=existing, items=items, replayed=True
                )
            raise _conflict(
                f"交接单号 {payload.handover_id} 已存在且内容不同，拒绝覆盖",
                code="handover_id_conflict",
            )

        # 3) 同车次同刻至多一笔整车交接
        clash = db.scalars(
            select(VehicleHandover).where(
                VehicleHandover.trip_id == payload.trip_id,
                VehicleHandover.handover_at == at,
            )
        ).first()
        if clash is not None:
            raise _conflict(
                f"车次 {payload.trip_id} 在 {at.isoformat()} 已存在整车交接 "
                f"{clash.handover_id}，同一时刻不允许重复交接",
                code="same_timestamp_conflict",
            )

        # 4) 左闭右开确定在车货物清单
        manifest = _manifest_rows(db, payload.trip_id, at)
        if not manifest:
            raise _unprocessable(
                f"车次 {payload.trip_id} 在 {at.isoformat()} 没有在车货物"
                "（空车交接整单拒绝）",
                code="empty_vehicle",
            )

        # 5) 固定顺序锁定清单内全部货物行：
        #    整车交接与这些货物的单票事件 / 表态 / 装载 / 读数写入串行一致。
        cargo_ids = [row.cargo_id for row in manifest]
        cargos = _load_cargos_locked_ordered(db, cargo_ids)

        # 6) 逐票货物整链校验（复用单票交接规则；任一不过则整单拒绝）
        candidates: dict[str, Event] = {}
        # 先一次性查明清单货物在交接时刻是否已有逐票交接（测温与交接可同刻）
        existing_handovers_at = {
            row.cargo_id
            for row in db.scalars(
                select(Event).where(
                    Event.cargo_id.in_(cargo_ids),
                    Event.event_type == EventType.handover,
                    Event.occurred_at == at,
                )
            ).all()
        }
        for row in manifest:
            cid = row.cargo_id
            cargo = cargos[cid]
            if at < cargo.started_at:
                raise _unprocessable(
                    f"货物 {cid} 的交接时刻 {at.isoformat()} 早于货物起始时间 "
                    f"{cargo.started_at.isoformat()}，整车交接整单拒绝",
                    code="before_start",
                )

            if cid in existing_handovers_at:
                raise _conflict(
                    f"货物 {cid} 在 {at.isoformat()} 已存在一笔交接，"
                    "同一时刻不允许重复交接，整车交接整单拒绝",
                    code="same_timestamp_conflict",
                )

            generated_id = _event_id_for(payload.handover_id, cid)
            if db.get(Event, generated_id) is not None:
                # 派生ID与既有事件碰撞（实际不会发生）：交由约束冲突处理
                raise _conflict(
                    f"为货物 {cid} 派生的交接事件编号 {generated_id} 已被占用",
                    code="integrity_conflict",
                )

            candidate = Event(
                event_id=generated_id,
                cargo_id=cid,
                event_type=EventType.handover,
                occurred_at=at,
                giver=payload.giver,
                receiver=payload.receiver,
            )
            events = crud._load_events(db, cid)
            # 整链校验：交接时刻保管方必须是交出方（含晚到插入使既有交接失效）
            _validate_prospective(cargo, events + [candidate])
            candidates[cid] = candidate

        # 7) 整单写入：逐票普通交接事件 + 交接单 + 清单快照（同一事务提交）
        for row in manifest:
            db.add(candidates[row.cargo_id])
        db.flush()  # 先落事件，清单项外键 event_id 才有保障

        handover = VehicleHandover(
            handover_id=payload.handover_id,
            trip_id=payload.trip_id,
            handover_at=at,
            giver=payload.giver,
            receiver=payload.receiver,
        )
        db.add(handover)
        db.flush()

        items = [
            VehicleHandoverItem(
                handover_id=payload.handover_id,
                cargo_id=row.cargo_id,
                loaded_at=row.loaded_at,
                unloaded_at=row.unloaded_at,
                event_id=candidates[row.cargo_id].event_id,
            )
            for row in manifest
        ]
        db.add_all(items)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        # 并发兜底：单号 / 同车次同刻 / 逐票事件同刻 唯一约束
        winner = db.get(VehicleHandover, payload.handover_id)
        if winner is not None:
            if _same_handover_content(winner, at, payload):
                winner.handover_at = ensure_utc(winner.handover_at)
                return HandoverWriteResult(
                    handover=winner, items=_load_items(db, payload.handover_id),
                    replayed=True,
                )
            raise _conflict(
                f"交接单号 {payload.handover_id} 已存在且内容不同，拒绝覆盖",
                code="handover_id_conflict",
            )
        clash = db.scalars(
            select(VehicleHandover).where(
                VehicleHandover.trip_id == payload.trip_id,
                VehicleHandover.handover_at == at,
            )
        ).first()
        if clash is not None:
            raise _conflict(
                f"车次 {payload.trip_id} 在 {at.isoformat()} 已存在整车交接，"
                "同一时刻不允许重复交接",
                code="same_timestamp_conflict",
            )
        raise _conflict(
            f"整车交接 {payload.handover_id} 与既有数据冲突"
            "（交接单号 / 同刻交接 / 逐票交接事件编号）",
            code="integrity_conflict",
        )

    db.refresh(handover)
    return HandoverWriteResult(handover=handover, items=items, replayed=False)


# ----------------------------------------------------------- 查询


def get_vehicle_handover(db, handover_id: str) -> HandoverWriteResult:
    handover = db.get(VehicleHandover, handover_id)
    if handover is None:
        raise ApiError(
            404, "handover_not_found", f"交接单 {handover_id} 不存在"
        )
    handover.handover_at = ensure_utc(handover.handover_at)
    return HandoverWriteResult(
        handover=handover, items=_load_items(db, handover_id), replayed=False
    )


def list_vehicle_handovers(db, trip_id: str | None = None) -> list[VehicleHandover]:
    stmt = select(VehicleHandover)
    if trip_id is not None:
        stmt = stmt.where(VehicleHandover.trip_id == trip_id)
    stmt = stmt.order_by(VehicleHandover.handover_at, VehicleHandover.handover_id)
    rows = list(db.scalars(stmt).all())
    for r in rows:
        r.handover_at = ensure_utc(r.handover_at)
    return rows
