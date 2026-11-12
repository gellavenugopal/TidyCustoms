"""Relational schema.

Database-level guarantees (CHECK constraints) back up the application checks:
  * a bond balance can never go below zero or above the bond amount;
  * a lot's available quantity / value can never go below zero or above what
    was received, so no consumption can exceed available duty-saved stock even
    if a buggy caller skips the service-layer validation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .audit_context import current_username
from .db import Base

MONEY = Numeric(18, 2)
QTY = Numeric(18, 3)
QTY_PER = Numeric(18, 4)
RATE = Numeric(9, 4)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Stamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_by: Mapped[str | None] = mapped_column(String(64), default=current_username)


# ---------------------------------------------------------------- users / audit
class User(Base, Stamped):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="operator")  # admin|operator|viewer
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("role in ('admin','operator','viewer')", name="ck_user_role"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(8))  # CREATE | UPDATE | DELETE
    table_name: Mapped[str] = mapped_column(String(64), index=True)
    record_id: Mapped[str | None] = mapped_column(String(64))
    old_values: Mapped[str | None] = mapped_column(Text)  # JSON
    new_values: Mapped[str | None] = mapped_column(Text)  # JSON


# ---------------------------------------------------------------- master data
class Supplier(Base, Stamped):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    country: Mapped[str | None] = mapped_column(String(64))


class Item(Base, Stamped):
    """A component / raw material / accessory with a unique internal code."""

    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    description: Mapped[str] = mapped_column(String(255))
    uom: Mapped[str] = mapped_column(String(16), default="NOS")
    item_type: Mapped[str] = mapped_column(String(16), default="COMPONENT")  # COMPONENT|ACCESSORY
    hs_code: Mapped[str | None] = mapped_column(String(16))
    bcd_rate: Mapped[Decimal | None] = mapped_column(RATE)  # % of AV; NULL -> config default
    sws_rate: Mapped[Decimal | None] = mapped_column(RATE)  # % of BCD; NULL -> config default
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        CheckConstraint("item_type in ('COMPONENT','ACCESSORY')", name="ck_item_type"),
    )

    def __str__(self) -> str:
        return f"{self.code} - {self.description}"


class DescriptionMapping(Base, Stamped):
    """Supplier description -> internal item code. scope = supplier id or 'GLOBAL'."""

    __tablename__ = "description_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), default="GLOBAL")
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    normalized_description: Mapped[str] = mapped_column(String(512))
    sample_description: Mapped[str] = mapped_column(String(512))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    item: Mapped[Item] = relationship()
    supplier: Mapped[Supplier | None] = relationship()
    __table_args__ = (UniqueConstraint("scope", "normalized_description", name="uq_mapping_key"),)


class ExportMapping(Base, Stamped):
    """Shipping-bill description -> finished model or item, remembered from the
    export template so the same description is tagged automatically next time."""

    __tablename__ = "export_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    normalized_description: Mapped[str] = mapped_column(String(512), unique=True)
    sample_description: Mapped[str] = mapped_column(String(512))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("fg_models.id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    model: Mapped["FGModel | None"] = relationship()
    item: Mapped[Item | None] = relationship()
    __table_args__ = (
        CheckConstraint("(model_id IS NULL) <> (item_id IS NULL)", name="ck_export_mapping_one_target"),
    )


class FGModel(Base, Stamped):
    """A finished-goods model, e.g. 4343 Gen5 or 2121 detector."""

    __tablename__ = "fg_models"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    boms: Mapped[list["BOM"]] = relationship(back_populates="model", order_by="BOM.version")

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class BOM(Base, Stamped):
    __tablename__ = "boms"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("fg_models.id"))
    version: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[date] = mapped_column(Date)
    remarks: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[FGModel] = relationship(back_populates="boms")
    lines: Mapped[list["BOMLine"]] = relationship(
        back_populates="bom", cascade="all, delete-orphan", order_by="BOMLine.id"
    )
    __table_args__ = (
        UniqueConstraint("model_id", "version", name="uq_bom_version"),
        UniqueConstraint("model_id", "effective_from", name="uq_bom_effective"),
    )


class BOMLine(Base):
    __tablename__ = "bom_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    bom_id: Mapped[int] = mapped_column(ForeignKey("boms.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty_per_unit: Mapped[Decimal] = mapped_column(QTY_PER)
    bom: Mapped[BOM] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship()
    __table_args__ = (
        UniqueConstraint("bom_id", "item_id", name="uq_bom_line_item"),
        CheckConstraint("qty_per_unit > 0", name="ck_bom_qty_pos"),
    )


# ---------------------------------------------------------------- bonds
class Bond(Base, Stamped):
    __tablename__ = "bonds"
    id: Mapped[int] = mapped_column(primary_key=True)
    bond_no: Mapped[str] = mapped_column(String(64), unique=True)
    bond_amount: Mapped[Decimal] = mapped_column(MONEY)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    balance: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE | CLOSED
    remarks: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (
        CheckConstraint("bond_amount > 0", name="ck_bond_amount_pos"),
        CheckConstraint("balance >= 0", name="ck_bond_balance_nonneg"),
        CheckConstraint("balance <= bond_amount", name="ck_bond_balance_le_amount"),
        CheckConstraint("valid_to >= valid_from", name="ck_bond_validity"),
        CheckConstraint("status in ('ACTIVE','CLOSED')", name="ck_bond_status"),
    )

    def __str__(self) -> str:
        return self.bond_no


class BondLedgerEntry(Base, Stamped):
    """One debit or credit (or zero-value memo) against a bond."""

    __tablename__ = "bond_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    bond_id: Mapped[int] = mapped_column(ForeignKey("bonds.id"), index=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    # IMPORT_DEBIT | IMPORT_REVERSAL | CONSUMPTION_MEMO | EXPORT_CREDIT | DTA_CREDIT
    entry_type: Mapped[str] = mapped_column(String(24))
    source_type: Mapped[str] = mapped_column(String(16))  # IMPORT | PRODUCTION | EXPORT | DTA
    source_id: Mapped[int] = mapped_column(Integer)
    source_ref: Mapped[str] = mapped_column(String(64))
    debit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    memo_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    balance_after: Mapped[Decimal] = mapped_column(MONEY)
    narration: Mapped[str | None] = mapped_column(String(255))
    bond: Mapped[Bond] = relationship()
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="ck_ledger_nonneg"),
        Index("ix_ledger_source", "source_type", "source_id"),
    )


# ---------------------------------------------------------------- imports
class ImportInvoice(Base, Stamped):
    __tablename__ = "import_invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(64))
    invoice_date: Mapped[date] = mapped_column(Date)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    # BOND = duty-saved import under bond (debits the bond);
    # DUTY_PAID = cleared on payment of duty - no bond, no debit, no recredit on export
    import_type: Mapped[str] = mapped_column(String(16), default="BOND", server_default="BOND")
    bond_id: Mapped[int | None] = mapped_column(ForeignKey("bonds.id"))
    be_no: Mapped[str | None] = mapped_column(String(64))  # Bill of Entry (into-bond)
    be_date: Mapped[date | None] = mapped_column(Date)
    # Form III fields
    port_of_import: Mapped[str | None] = mapped_column(String(16))  # e.g. INBOM4
    be_invoice_serial: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    clearance_date: Mapped[date | None] = mapped_column(Date)  # out-of-charge date
    receipt_date: Mapped[date | None] = mapped_column(Date)  # receipt at importer's premises
    currency: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")  # DRAFT|POSTED|CANCELLED
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    posted_by: Mapped[str | None] = mapped_column(String(64))
    supplier: Mapped[Supplier] = relationship()
    bond: Mapped[Bond | None] = relationship()
    lines: Mapped[list["ImportLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="ImportLine.line_no"
    )
    __table_args__ = (
        UniqueConstraint("supplier_id", "invoice_no", name="uq_import_invoice"),
        CheckConstraint("status in ('DRAFT','POSTED','CANCELLED')", name="ck_import_status"),
        CheckConstraint("import_type in ('BOND','DUTY_PAID')", name="ck_import_type"),
        CheckConstraint("import_type = 'DUTY_PAID' OR bond_id IS NOT NULL", name="ck_import_bond_required"),
    )

    @property
    def duty_paid(self) -> bool:
        return self.import_type == "DUTY_PAID"

    @property
    def bond_label(self) -> str:
        return self.bond.bond_no if self.bond else "Duty paid (no bond)"

    @property
    def total_dsv(self) -> Decimal:
        return sum((l.duty_saved_value for l in self.lines), Decimal("0"))

    @property
    def total_duty(self) -> Decimal:
        """All duty heads of the lines - forgone (BOND) or paid (DUTY_PAID)."""
        return sum((l.duty_total for l in self.lines), Decimal("0"))


class ImportLine(Base):
    __tablename__ = "import_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("import_invoices.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(512))
    normalized_description: Mapped[str] = mapped_column(String(512))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    assessable_value: Mapped[Decimal] = mapped_column(MONEY)  # INR, for DTA duty
    duty_saved_value: Mapped[Decimal] = mapped_column(MONEY)  # INR, debited to bond
    # Form III fields: BE item serial, quantity actually received, duty forgone split
    be_item_serial: Mapped[int | None] = mapped_column(Integer)
    hs_code: Mapped[str | None] = mapped_column(String(16))  # CTH as on the BE
    qty_received: Mapped[Decimal | None] = mapped_column(QTY)  # NULL = all received
    bcd_forgone: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default="0")
    sws_forgone: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default="0")
    igst_forgone: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default="0")
    cess_forgone: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default="0")
    other_forgone: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default="0")
    invoice: Mapped[ImportInvoice] = relationship(back_populates="lines")
    item: Mapped[Item | None] = relationship()

    @property
    def received(self) -> Decimal:
        return self.qty if self.qty_received is None else self.qty_received

    @property
    def has_duty_split(self) -> bool:
        return any((self.bcd_forgone, self.sws_forgone, self.igst_forgone, self.cess_forgone, self.other_forgone))

    @property
    def duty_total(self) -> Decimal:
        """Sum of the duty heads; on a duty-paid import these hold the duty PAID."""
        if self.has_duty_split:
            return sum((self.bcd_forgone, self.sws_forgone, self.igst_forgone, self.cess_forgone,
                        self.other_forgone), Decimal("0"))
        return self.duty_saved_value

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_import_line_qty"),
        CheckConstraint("qty_received IS NULL OR (qty_received >= 0 AND qty_received <= qty)",
                        name="ck_import_line_received"),
        CheckConstraint("assessable_value >= 0", name="ck_import_line_av"),
        CheckConstraint("duty_saved_value >= 0", name="ck_import_line_dsv"),
    )


class ImportLot(Base, Stamped):
    """Stock created by a posted import line. FIFO unit. Duty-saved under a bond,
    or duty paid (bond_id NULL, duty_paid true, duty-saved value 0)."""

    __tablename__ = "import_lots"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_line_id: Mapped[int] = mapped_column(ForeignKey("import_lines.id"), unique=True)
    bond_id: Mapped[int | None] = mapped_column(ForeignKey("bonds.id"), index=True)
    duty_paid: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    lot_date: Mapped[date] = mapped_column(Date)
    qty_received: Mapped[Decimal] = mapped_column(QTY)
    qty_available: Mapped[Decimal] = mapped_column(QTY)
    dsv_total: Mapped[Decimal] = mapped_column(MONEY)
    dsv_available: Mapped[Decimal] = mapped_column(MONEY)
    av_total: Mapped[Decimal] = mapped_column(MONEY)
    av_available: Mapped[Decimal] = mapped_column(MONEY)
    import_line: Mapped[ImportLine] = relationship()
    bond: Mapped[Bond | None] = relationship()
    item: Mapped[Item] = relationship()

    @property
    def bond_label(self) -> str:
        return self.bond.bond_no if self.bond else "Duty paid (no bond)"

    __table_args__ = (
        CheckConstraint("qty_available >= 0", name="ck_lot_qty_nonneg"),
        CheckConstraint("duty_paid OR bond_id IS NOT NULL", name="ck_lot_bond_or_paid"),
        CheckConstraint("NOT duty_paid OR dsv_total = 0", name="ck_lot_paid_no_dsv"),
        CheckConstraint("qty_available <= qty_received", name="ck_lot_qty_le_received"),
        CheckConstraint("dsv_available >= 0 AND dsv_available <= dsv_total", name="ck_lot_dsv"),
        CheckConstraint("av_available >= 0 AND av_available <= av_total", name="ck_lot_av"),
        Index("ix_lot_fifo", "item_id", "lot_date", "id"),
    )

    @property
    def source_ref(self) -> str:
        inv = self.import_line.invoice
        return f"{inv.invoice_no}/{self.import_line.line_no}"


# ---------------------------------------------------------------- production
class ProductionBatch(Base, Stamped):
    __tablename__ = "production_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True)
    production_date: Mapped[date] = mapped_column(Date)
    model_id: Mapped[int] = mapped_column(ForeignKey("fg_models.id"))
    bom_id: Mapped[int] = mapped_column(ForeignKey("boms.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    restrict_bond_id: Mapped[int | None] = mapped_column(ForeignKey("bonds.id"))
    total_dsv: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_av: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    remarks: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[FGModel] = relationship()
    bom: Mapped[BOM] = relationship()
    allocations: Mapped[list["ConsumptionAllocation"]] = relationship(
        back_populates="batch", order_by="ConsumptionAllocation.id"
    )
    fg_lot: Mapped["FGLot"] = relationship(back_populates="batch", uselist=False)
    __table_args__ = (CheckConstraint("qty > 0", name="ck_batch_qty"),)


class FGLot(Base, Stamped):
    """Finished goods produced by one batch; FIFO unit for export / DTA."""

    __tablename__ = "fg_lots"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("production_batches.id"), unique=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("fg_models.id"), index=True)
    lot_date: Mapped[date] = mapped_column(Date)
    qty_produced: Mapped[Decimal] = mapped_column(QTY)
    qty_available: Mapped[Decimal] = mapped_column(QTY)
    batch: Mapped[ProductionBatch] = relationship(back_populates="fg_lot")
    model: Mapped[FGModel] = relationship()
    __table_args__ = (
        CheckConstraint("qty_available >= 0", name="ck_fglot_qty_nonneg"),
        CheckConstraint("qty_available <= qty_produced", name="ck_fglot_qty_le_produced"),
    )


class ConsumptionAllocation(Base):
    """Import-lot quantity consumed by a production batch (FIFO result).

    The *_remaining columns track how much of this consumption is still sitting
    in finished-goods stock (not yet exported or cleared to DTA)."""

    __tablename__ = "consumption_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("production_batches.id"), index=True)
    fg_lot_id: Mapped[int] = mapped_column(ForeignKey("fg_lots.id"), index=True)
    import_lot_id: Mapped[int] = mapped_column(ForeignKey("import_lots.id"), index=True)
    bond_id: Mapped[int | None] = mapped_column(ForeignKey("bonds.id"))  # NULL = duty-paid input
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    dsv: Mapped[Decimal] = mapped_column(MONEY)
    av: Mapped[Decimal] = mapped_column(MONEY)
    qty_remaining: Mapped[Decimal] = mapped_column(QTY)
    dsv_remaining: Mapped[Decimal] = mapped_column(MONEY)
    av_remaining: Mapped[Decimal] = mapped_column(MONEY)
    batch: Mapped[ProductionBatch] = relationship(back_populates="allocations")
    import_lot: Mapped[ImportLot] = relationship()
    item: Mapped[Item] = relationship()
    bond: Mapped[Bond | None] = relationship()

    @property
    def bond_label(self) -> str:
        return self.bond.bond_no if self.bond else "Duty paid (no bond)"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_cons_qty_pos"),
        CheckConstraint("qty_remaining >= 0 AND qty_remaining <= qty", name="ck_cons_qty_rem"),
        CheckConstraint("dsv_remaining >= 0 AND dsv_remaining <= dsv", name="ck_cons_dsv_rem"),
        CheckConstraint("av_remaining >= 0 AND av_remaining <= av", name="ck_cons_av_rem"),
    )


# ---------------------------------------------------------------- export
class ExportShipment(Base, Stamped):
    __tablename__ = "export_shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(64), unique=True)
    export_date: Mapped[date] = mapped_column(Date)
    shipping_bill_no: Mapped[str | None] = mapped_column(String(64))
    shipping_bill_date: Mapped[date | None] = mapped_column(Date)
    port_of_export: Mapped[str | None] = mapped_column(String(16))
    consignee: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    total_recredit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    lines: Mapped[list["ExportLine"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", order_by="ExportLine.line_no"
    )


class ExportLine(Base):
    """line_type FG = finished detector (from FG stock); ITEM = accessory/other goods
    exported as-is from duty-saved import stock (cables, spare batteries, ...)."""

    __tablename__ = "export_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("export_shipments.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    line_type: Mapped[str] = mapped_column(String(8))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("fg_models.id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    export_item_name: Mapped[str] = mapped_column(String(255))
    qty: Mapped[Decimal] = mapped_column(QTY)
    fob_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    dsv_recredited: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    # as on the shipping bill (INVSN / ITEMSN / HS code)
    sb_invoice_serial: Mapped[int | None] = mapped_column(Integer)
    sb_item_serial: Mapped[int | None] = mapped_column(Integer)
    hs_code: Mapped[str | None] = mapped_column(String(16))
    shipment: Mapped[ExportShipment] = relationship(back_populates="lines")
    model: Mapped[FGModel | None] = relationship()
    item: Mapped[Item | None] = relationship()
    __table_args__ = (
        CheckConstraint("line_type in ('FG','ITEM')", name="ck_export_line_type"),
        CheckConstraint("qty > 0", name="ck_export_qty"),
        CheckConstraint(
            "(line_type='FG' AND model_id IS NOT NULL AND item_id IS NULL) OR "
            "(line_type='ITEM' AND item_id IS NOT NULL AND model_id IS NULL)",
            name="ck_export_line_target",
        ),
    )


# ---------------------------------------------------------------- DTA
class DTAClearance(Base, Stamped):
    __tablename__ = "dta_clearances"
    id: Mapped[int] = mapped_column(primary_key=True)
    clearance_no: Mapped[str] = mapped_column(String(64), unique=True)
    clearance_date: Mapped[date] = mapped_column(Date)
    customer: Mapped[str | None] = mapped_column(String(128))
    be_no: Mapped[str | None] = mapped_column(String(64))  # ex-bond / DTA bill of entry
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING | PAID
    challan_no: Mapped[str | None] = mapped_column(String(64))
    paid_date: Mapped[date | None] = mapped_column(Date)
    total_av: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_bcd: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_sws: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_duty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_dsv: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    bond_recredited: Mapped[bool] = mapped_column(Boolean, default=False)
    lines: Mapped[list["DTALine"]] = relationship(
        back_populates="clearance", cascade="all, delete-orphan", order_by="DTALine.line_no"
    )
    __table_args__ = (CheckConstraint("status in ('PENDING','PAID')", name="ck_dta_status"),)


class DTALine(Base):
    __tablename__ = "dta_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    clearance_id: Mapped[int] = mapped_column(ForeignKey("dta_clearances.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    line_type: Mapped[str] = mapped_column(String(8))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("fg_models.id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    description: Mapped[str] = mapped_column(String(255))
    qty: Mapped[Decimal] = mapped_column(QTY)
    sale_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))  # info; GST base
    assessable_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    bcd: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    sws: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    duty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    dsv: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    clearance: Mapped[DTAClearance] = relationship(back_populates="lines")
    model: Mapped[FGModel | None] = relationship()
    item: Mapped[Item | None] = relationship()
    __table_args__ = (
        CheckConstraint("line_type in ('FG','ITEM')", name="ck_dta_line_type"),
        CheckConstraint("qty > 0", name="ck_dta_qty"),
    )


# ---------------------------------------------------------------- disposal trace
class DisposalAllocation(Base):
    """Trace from an export / DTA line back to the duty-saved import lots it used.

    For FG lines fg_lot_id and consumption_id are set; for ITEM lines the stock
    was drawn straight from the import lot. Duty columns are filled for DTA."""

    __tablename__ = "disposal_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    export_line_id: Mapped[int | None] = mapped_column(ForeignKey("export_lines.id"), index=True)
    dta_line_id: Mapped[int | None] = mapped_column(ForeignKey("dta_lines.id"), index=True)
    fg_lot_id: Mapped[int | None] = mapped_column(ForeignKey("fg_lots.id"))
    fg_qty: Mapped[Decimal | None] = mapped_column(QTY)
    consumption_id: Mapped[int | None] = mapped_column(ForeignKey("consumption_allocations.id"))
    import_lot_id: Mapped[int] = mapped_column(ForeignKey("import_lots.id"), index=True)
    bond_id: Mapped[int | None] = mapped_column(ForeignKey("bonds.id"))  # NULL = duty-paid input
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)  # component quantity
    dsv: Mapped[Decimal] = mapped_column(MONEY)
    av: Mapped[Decimal] = mapped_column(MONEY)
    bcd_rate: Mapped[Decimal | None] = mapped_column(RATE)
    sws_rate: Mapped[Decimal | None] = mapped_column(RATE)
    bcd: Mapped[Decimal | None] = mapped_column(MONEY)
    sws: Mapped[Decimal | None] = mapped_column(MONEY)
    import_lot: Mapped[ImportLot] = relationship()
    item: Mapped[Item] = relationship()
    bond: Mapped[Bond | None] = relationship()

    @property
    def bond_label(self) -> str:
        return self.bond.bond_no if self.bond else "Duty paid (no bond)"
    __table_args__ = (
        CheckConstraint(
            "(export_line_id IS NOT NULL AND dta_line_id IS NULL) OR "
            "(export_line_id IS NULL AND dta_line_id IS NOT NULL)",
            name="ck_disposal_one_source",
        ),
        CheckConstraint("qty >= 0 AND dsv >= 0 AND av >= 0", name="ck_disposal_nonneg"),
    )
