"""SQLAlchemy ORM 模型：货物与冷链事件（测温 / 交接）。"""
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventType(str, enum.Enum):
    """事件类型。成员名与 PG enum 字面量保持一致。"""

    measurement = "measurement"  # 测温
    handover = "handover"        # 交接


class StanceKind(str, enum.Enum):
    """责任方表态类型。"""

    confirm = "confirm"  # 确认担责
    dispute = "dispute"  # 异议（须附理由）


class Cargo(Base):
    __tablename__ = "cargo"

    cargo_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temp_upper_limit: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False
    )
    initial_holder: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Event(Base):
    """一条测温或交接事件。

    测温：temperature 有值，giver/receiver 为空。
    交接：giver/receiver 有值，temperature 为空。
    """

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "(event_type = 'measurement' AND temperature IS NOT NULL "
            "AND giver IS NULL AND receiver IS NULL) "
            "OR (event_type = 'handover' AND temperature IS NULL "
            "AND giver IS NOT NULL AND receiver IS NOT NULL AND giver <> receiver)",
            name="chk_event_shape",
        ),
        # 同一货物同一时刻只能有一笔测温（跨类型不互斥：测温与交接可同刻发生）
        Index(
            "ux_event_cargo_time_measurement",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'measurement'::event_type"),
            sqlite_where=text("event_type = 'measurement'"),
        ),
        # 同一货物同一时刻只能有一笔交接
        Index(
            "ux_event_cargo_time_handover",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'handover'::event_type"),
            sqlite_where=text("event_type = 'handover'"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type", create_constraint=True),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )
    giver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Stance(Base):
    """责任方对一个已闭合越温区间的一次表态（确认 / 异议）。

    表态是只追加（append-only）的：改口产生新编号的新记录，历次表态永久保留；
    晚到事件重建后若区间边界或责任片段变化，旧行不删除、不修改，
    其是否仍“有效”由查询时与当前快照比对得出（见 app/responsibility.py）。

    ``start_event_id`` 为区间稳定标识：货物自有测温开启的区间用测温事件ID；
    车载读数开启的区间用 ``onboard:<读数ID>``。因此这里**不**对 event 表建外键。
    """

    __tablename__ = "responsibility_stance"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'confirm' AND reason IS NULL) "
            "OR (kind = 'dispute' AND reason IS NOT NULL AND char_length(reason) > 0)",
            name="chk_stance_shape",
        ),
        Index("ix_stance_cargo_interval", "cargo_id", "start_event_id"),
    )

    stance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 区间稳定标识：自有测温事件ID，或 onboard:<车载读数ID>（长度容纳前缀）
    start_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    responsible_party: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[StanceKind] = mapped_column(
        Enum(StanceKind, name="stance_kind", create_constraint=True),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 提交时校验通过的快照标识；区间/片段变化后该标识与当前快照不再一致
    snapshot_id: Mapped[str] = mapped_column(String(71), nullable=False)
    # 表态时该责任方承担的片段坐标（规范化 JSON：[[start,end,party],...]）。
    # 晚到事件重建后据此逐片段判定表态是否仍有效（未变化片段继续有效）。
    attested_segments: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Trip(Base):
    """一辆冷链车的一次车次。"""

    __tablename__ = "trip"

    trip_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vehicle_no: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class CargoLoading(Base):
    """货物在某车次上的一个装载时段 [loaded_at, unloaded_at)（左闭右开）。

    同货物不得同时属于两个车次：同一货物的装载时段两两不得相交
    （首尾相接允许：上一段的卸载时刻可以是下一段的装载时刻）。
    """

    __tablename__ = "cargo_loading"
    __table_args__ = (
        CheckConstraint(
            "loaded_at < unloaded_at", name="chk_loading_window"
        ),
        UniqueConstraint(
            "trip_id", "cargo_id", name="ux_loading_trip_cargo"
        ),
        Index("ix_loading_cargo_window", "cargo_id", "loaded_at", "unloaded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        nullable=False,
    )
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    unloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class OnboardReading(Base):
    """车载测温设备在某车次上的一笔原始读数。

    读数只作用于“读数时刻仍在车上”的货物（依据 cargo_loading 左闭右开判断）；
    同一车次同一时刻至多一笔原始读数。原始读数始终保留可查，
    是否被某货物追责时间线采用由查询时按装载时段与同刻自有测温合并决定。
    """

    __tablename__ = "onboard_reading"
    __table_args__ = (
        Index(
            "ux_onboard_trip_time",
            "trip_id",
            "read_at",
            unique=True,
        ),
        Index("ix_onboard_trip", "trip_id", "read_at"),
    )

    reading_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        nullable=False,
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class VehicleHandover(Base):
    """一次整车交接单（操作员在某车次、某时刻把整车从交出方交给接收方）。

    交接单在写入时即按装载时段左闭右开规则确定“该时刻在车货物清单”，
    并把清单作为**不可变快照**保留（见 VehicleHandoverItem）。
    成功后为清单内每票货物在 ``event`` 表记一笔普通交接事件——
    整车交接不改变既有追责 / 责任模型，只是批量、原子地产生交接事件。

    同一交接单号全局唯一（幂等键）；同一车次同一时刻至多一笔整车交接。
    """

    __tablename__ = "vehicle_handover"
    __table_args__ = (
        # 同一车次同一时刻至多一笔整车交接（“同刻已有交接”整单拒绝的库级兜底）
        UniqueConstraint("trip_id", "handover_at", name="ux_handover_trip_time"),
        Index("ix_handover_trip", "trip_id", "handover_at"),
    )

    handover_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        nullable=False,
    )
    handover_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    giver: Mapped[str] = mapped_column(String(128), nullable=False)
    receiver: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class VehicleHandoverItem(Base):
    """整车交接单清单中的一票货物快照（交接单不可变内容的一部分）。

    每行对应：交接时刻该票货物按左闭右开规则确定的装载时段，
    以及成功后为该票货物写入的那笔普通交接事件（``event_id``）。
    后续即使有晚到数据，交接单返回的仍是这份快照。
    """

    __tablename__ = "vehicle_handover_item"
    __table_args__ = (
        UniqueConstraint("handover_id", "cargo_id", name="ux_handover_item_cargo"),
        Index("ix_handover_item_cargo", "cargo_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    handover_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("vehicle_handover.handover_id", ondelete="RESTRICT"),
        nullable=False,
    )
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 交接单写入时刻该票货物的装载时段快照（左闭右开）
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    unloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 为该票货物产生的普通交接事件ID（event 表主键）
    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
