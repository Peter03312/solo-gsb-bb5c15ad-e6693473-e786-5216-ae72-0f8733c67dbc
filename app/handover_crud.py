"""整车交接的写入与查询。

一张整车交接单在**单事务**内完成写入，加锁顺序恒为
「车次行（FOR UPDATE）→ 按货物编号排序的在车货物行（FOR UPDATE）」，
与装载时段写入、车载读数写入完全一致，因此：

* 两张同车次交接单并发提交：车次行锁串行化，先提交者成功，
  后者在同刻唯一索引 / 清单校验处拒绝；
* 整车交接与清单内各货物的单货物事件写入、责任表态、装载补登记、
  车载读数写入全部互斥——整单与相关货物事件、装载、责任表态的
  并发提交保持一致。

整单校验（任一不过即整体回滚，**不产生任何货物事件**）
------------------------------------------------------
1. 车次必须存在（404 trip_not_found）；
2. 交接单号全局唯一：同单号同内容重传返回原结果（幂等），异内容 409
   handover_id_conflict；
3. 交出方与接收方不得相同（422 bad_handover_shape）；
4. 交接时刻在车货物清单按各货物在**该车次**上的装载时段左闭右开确定
   （``loaded_at <= t < unloaded_at``）；空车 → 422 empty_vehicle；
5. 同车次同刻已有交接单（409 same_timestamp_conflict）；
6. 逐票货物：交接时刻早于货物起始时间（422 before_start）；
   该票货物在该时刻已有交接事件（409 same_timestamp_conflict）；
   该票货物在交接时刻的保管方不是交出方（422 giver_mismatch）。

成功后：为清单内每票货物写一笔**普通**货物自有交接事件（event 表），
交接单与冻结清单（vehicle_handover_item）一并落库。普通交接事件与
单货物接口写入的事件完全同构，事件查询、追责重建、责任拆分、责任表态
均无需感知本单据。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import chain
from app.crud import ApiError, _conflict, _load_events, _unprocessable
from app.database import sqlite_write_lock
from app.models import (
    CargoLoading,
    Event,
    EventType,
    VehicleHandover,
    VehicleHandoverItem,
)
from app.timeutil import ensure_utc
from app.trip_crud import _load_cargos_locked_ordered, _load_trip_locked

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")


def _bad_shape(message: str) -> ApiError:
    return _unprocessable(message, code="bad_handover_shape")


@dataclass
class HandoverWriteResult:
    handover: VehicleHandover
    items: list[VehicleHandoverItem]
    replayed: bool


def derive_event_id(handover_id: str, cargo_id: str) -> str:
    """由交接单号 + 货物编号派生逐票普通交接事件ID。

    常规短编号使用可读形式 ``vh:<交接单>:<货物>``（前缀 vh: 与既有
    客户端事件ID空间区分）；过长时退化为内容哈希，保证不超过 64 字符
    且同一 (交接单, 货物) 稳定、不同组合不碰撞。
    """
    readable = f"vh:{handover_id}:{cargo_id}"
    if len(readable) <= 64 and _SAFE_ID.fullmatch(handover_id) and _SAFE_ID.fullmatch(cargo_id):
        return readable
    digest = hashlib.sha1(
        f"{handover_id}\n{cargo_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"vh1:{digest}"


# ----------------------------------------------------------- 读取辅助


def manifest_cargo_ids(db, trip_id: str, at) -> list[str]:
    """交接时刻 ``at`` 在该车次上的货物（装载时段左闭右开），按编号升序。"""
    rows = db.scalars(
        select(CargoLoading).where(CargoLoading.trip_id == trip_id)
    ).all()
    ids = [
        row.cargo_id
        for row in rows
        if ensure_utc(row.loaded_at) <= at < ensure_utc(row.unloaded_at)
    ]
    return sorted(set(ids))


def load_items(db, handover_id: str) -> list[VehicleHandoverItem]:
    rows = list(
        db.scalars(
            select(VehicleHandoverItem)
            .where(VehicleHandoverItem.handover_id == handover_id)
            .order_by(VehicleHandoverItem.cargo_id)
        ).all()
    )
    return rows


def list_handovers(db, trip_id: str) -> list[tuple]:
    """车次下全部交接单（按交接时刻升序），返回 (交接单, 冻结清单行) 列表。"""
    rows = list(
        db.scalars(
            select(VehicleHandover)
            .where(VehicleHandover.trip_id == trip_id)
            .order_by(VehicleHandover.handed_over_at, VehicleHandover.handover_id)
        ).all()
    )
    for h in rows:
        h.handed_over_at = ensure_utc(h.handed_over_at)
    return [(h, load_items(db, h.handover_id)) for h in rows]


def get_handover(db, handover_id: str) -> tuple | None:
    h = db.get(VehicleHandover, handover_id)
    if h is None:
        return None
    h.handed_over_at = ensure_utc(h.handed_over_at)
    return h, load_items(db, handover_id)


# ----------------------------------------------------------- 写入


def _same_content(existing: VehicleHandover, trip_id: str, at, giver: str, receiver: str) -> bool:
    """逐字段比对（handover_id 已相同）。时间归一化到 UTC。"""
    return (
        existing.trip_id == trip_id
        and ensure_utc(existing.handed_over_at) == at
        and existing.giver == giver
        and existing.receiver == receiver
    )


def _replay(db, existing: VehicleHandover) -> HandoverWriteResult:
    db.rollback()  # 仅释放车次行锁，未做任何修改
    existing.handed_over_at = ensure_utc(existing.handed_over_at)
    return HandoverWriteResult(
        handover=existing, items=load_items(db, existing.handover_id), replayed=True
    )


def _is_sqlite_lock_error(exc) -> bool:
    """SQLite 文件库下，写锁竞争有时以 IntegrityError/OperationalError 包装抛出。

    PostgreSQL 依赖行锁串行化，不会出现此类错误；这里仅对 SQLite 的
    “database is locked / disk I/O error” 识别后整体重试一次。
    """
    seen = []
    cur = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        text = str(cur).lower()
        if "database is locked" in text or "disk i/o error" in text:
            return True
        cur = getattr(cur, "orig", None) or getattr(cur, "__cause__", None)
    return False


def create_vehicle_handover(db, payload, _attempt: int = 0) -> HandoverWriteResult:
    with sqlite_write_lock:
        return _create_vehicle_handover(db, payload, _attempt)


def _create_vehicle_handover(db, payload, _attempt: int) -> HandoverWriteResult:
    at = ensure_utc(payload.handed_over_at)
    candidate = VehicleHandover(
        handover_id=payload.handover_id,
        trip_id=payload.trip_id,
        handed_over_at=at,
        giver=payload.giver,
        receiver=payload.receiver,
    )
    try:
        return _create_tx(db, candidate, at)
    except (OperationalError, IntegrityError) as exc:
        # SQLite 文件库无行级锁，busy timeout 耗尽时整体重试一次（PG 不触发）
        if not _is_sqlite_lock_error(exc) or _attempt >= 1:
            raise
        db.rollback()
        return _create_vehicle_handover(db, payload, _attempt=_attempt + 1)
    except _RetryTx:
        # 锁竞争导致读到不一致快照（仅 SQLite 可能），整体重试一次
        if _attempt >= 1:
            raise
        db.rollback()
        return _create_vehicle_handover(db, payload, _attempt=_attempt + 1)


class _RetryTx(Exception):
    """SQLite 读快照与并发提交不一致，需要结束快照后整体重试。"""


def _create_tx(db, candidate: VehicleHandover, at) -> HandoverWriteResult:
    try:
        # 先锁车次行（同时保证车次存在）：与装载 / 车载读数 / 另一张交接单互斥
        _load_trip_locked(db, candidate.trip_id)

        # 幂等优先：交接单号全局唯一
        existing = db.get(VehicleHandover, candidate.handover_id)
        if existing is not None:
            if _same_content(
                existing,
                candidate.trip_id,
                at,
                candidate.giver,
                candidate.receiver,
            ):
                return _replay(db, existing)
            raise _conflict(
                f"交接单号 {candidate.handover_id} 已存在且内容不同，拒绝覆盖",
                code="handover_id_conflict",
            )

        # 双方相同
        if candidate.giver == candidate.receiver:
            raise _bad_shape("交出方与接收方不能相同")

        # 交接时刻在车货物清单（左闭右开）
        manifest_ids = manifest_cargo_ids(db, candidate.trip_id, at)
        if not manifest_ids:
            raise _unprocessable(
                f"车次 {candidate.trip_id} 在交接时刻 {at.isoformat()} 没有在车货物，"
                "空车不允许整车交接",
                code="empty_vehicle",
            )

        # 同车次同刻至多一张交接单
        clash = db.scalars(
            select(VehicleHandover).where(
                VehicleHandover.trip_id == candidate.trip_id,
                VehicleHandover.handed_over_at == at,
            )
        ).first()
        if clash is not None:
            raise _conflict(
                f"车次 {candidate.trip_id} 在 {at.isoformat()} 已存在整车交接单 "
                f"{clash.handover_id}，同一时刻不允许重复交接",
                code="same_timestamp_conflict",
            )

        # 锁定全部在车货物行（固定排序）：与各货物事件 / 表态 / 装载互斥
        cargos = _load_cargos_locked_ordered(db, manifest_ids)

        # 并发兜底（SQLite 文件库下 FOR UPDATE 为空操作时）：持“锁”后复查
        # 交接单是否已被并发事务写入——同内容按幂等重传，异内容冲突。
        winner = db.get(VehicleHandover, candidate.handover_id)
        if winner is not None:
            if _same_content(
                winner, candidate.trip_id, at, candidate.giver, candidate.receiver
            ):
                return _replay(db, winner)
            raise _conflict(
                f"交接单号 {candidate.handover_id} 已存在且内容不同，拒绝覆盖",
                code="handover_id_conflict",
            )

        # 逐票校验（持锁期间读取最新事件链）
        derived: dict[str, Event] = {}
        for cid in manifest_ids:
            cargo = cargos[cid]
            if at < cargo.started_at:
                raise _unprocessable(
                    f"在车货物 {cid} 的交接时刻 {at.isoformat()} 早于其起始时间 "
                    f"{cargo.started_at.isoformat()}，整单拒绝",
                    code="before_start",
                )

            events = _load_events(db, cid)

            # 该票货物同刻已有交接（测温与交接可同刻，互不冲突）
            same_time_handover = next(
                (
                    e
                    for e in events
                    if e.event_type is EventType.handover and e.occurred_at == at
                ),
                None,
            )
            if same_time_handover is not None:
                # 并发重传：同刻交接事件若正是本交接单为本票货物派生的编号，
                # 说明同内容交接单已被并发事务提交（SQLite 读快照可能看不到
                # 交接单行）。回滚结束旧快照后全新读取，按幂等返回原结果。
                if (
                    same_time_handover.event_id
                    == derive_event_id(candidate.handover_id, cid)
                ):
                    db.rollback()
                    winner_row = db.get(VehicleHandover, candidate.handover_id)
                    if winner_row is not None and _same_content(
                        winner_row,
                        candidate.trip_id,
                        at,
                        candidate.giver,
                        candidate.receiver,
                    ):
                        return _replay(db, winner_row)
                    # 回滚后仍查不到：极少见的锁竞争脏读，整体重试
                    raise _RetryTx()
                raise _conflict(
                    f"货物 {cid} 在 {at.isoformat()} 已存在交接事件 "
                    f"{same_time_handover.event_id}，整车交接整单拒绝",
                    code="same_timestamp_conflict",
                )

            # 交接时刻的保管方必须是交出方（该时刻交接已生效后的保管方，
            # 与测温同刻时测温先于交接处理的语义一致）
            holder = chain.holder_at(cargo.initial_holder, events, at)
            if holder != candidate.giver:
                raise _unprocessable(
                    f"货物 {cid} 在 {at.isoformat()} 时刻的保管方为 {holder}，"
                    f"不是交出方 {candidate.giver}，整车交接整单拒绝",
                    code="giver_mismatch",
                )

            eid = derive_event_id(candidate.handover_id, cid)
            clash_event = db.get(Event, eid)
            if clash_event is not None:
                # 并发重传：事件为同内容交接单派生 -> 幂等；否则才是编号占用
                if winner is not None:
                    return _replay(db, winner)
                raise _conflict(
                    f"派生交接事件编号 {eid} 已被占用，请更换交接单号",
                    code="integrity_conflict",
                )
            derived[cid] = Event(
                event_id=eid,
                cargo_id=cid,
                event_type=EventType.handover,
                occurred_at=at,
                temperature=None,
                giver=candidate.giver,
                receiver=candidate.receiver,
            )

        # —— 全部校验通过：交接单 + 冻结清单 + 逐票普通交接事件一次提交 ——
        db.add(candidate)
        db.add_all(derived.values())
        db.flush()  # 让 event / 交接单先落库，满足清单行外键
        items = [
            VehicleHandoverItem(
                handover_id=candidate.handover_id,
                cargo_id=cid,
                event_id=derived[cid].event_id,
            )
            for cid in manifest_ids
        ]
        db.add_all(items)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError as exc:
        # SQLite 写锁竞争可能以 IntegrityError 包装抛出（非真实唯一约束冲突），
        # 交给外层整体重试；PG 不会触发
        if _is_sqlite_lock_error(exc):
            db.rollback()
            raise
        db.rollback()
        # 并发兜底：单号 / 同刻 / 逐票同刻交接唯一约束竞争
        winner = db.get(VehicleHandover, candidate.handover_id)
        if winner is not None:
            if _same_content(
                winner,
                candidate.trip_id,
                at,
                candidate.giver,
                candidate.receiver,
            ):
                return _replay(db, winner)
            raise _conflict(
                f"交接单号 {candidate.handover_id} 已存在且内容不同，拒绝覆盖",
                code="handover_id_conflict",
            )
        clash = db.scalars(
            select(VehicleHandover).where(
                VehicleHandover.trip_id == candidate.trip_id,
                VehicleHandover.handed_over_at == at,
            )
        ).first()
        if clash is not None:
            raise _conflict(
                f"车次 {candidate.trip_id} 在 {at.isoformat()} 已存在整车交接单 "
                f"{clash.handover_id}，同一时刻不允许重复交接",
                code="same_timestamp_conflict",
            )
        # 逐票交接同刻（SQLite 下货物行锁为空操作时由部分唯一索引兜底）
        for cid in manifest_cargo_ids(db, candidate.trip_id, at):
            existing_event = db.scalars(
                select(Event).where(
                    Event.cargo_id == cid,
                    Event.occurred_at == at,
                    Event.event_type == EventType.handover,
                )
            ).first()
            if existing_event is not None:
                raise _conflict(
                    f"货物 {cid} 在 {at.isoformat()} 已存在交接事件 "
                    f"{existing_event.event_id}，整车交接整单拒绝",
                    code="same_timestamp_conflict",
                )
        raise _conflict(
            f"整车交接单 {candidate.handover_id} 与既有数据冲突"
            "（编号 / 同刻 / 字段约束）",
            code="integrity_conflict",
        )

    return HandoverWriteResult(handover=candidate, items=items, replayed=False)
