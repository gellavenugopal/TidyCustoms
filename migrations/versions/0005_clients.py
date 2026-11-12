"""Client ownership and per-client user roles.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa
import secrets

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLES = (
    "audit_log", "suppliers", "items", "description_mappings", "export_mappings",
    "fg_models", "boms", "bom_lines", "bonds", "bond_ledger", "import_invoices",
    "import_lines", "import_lots", "production_batches", "fg_lots",
    "consumption_allocations", "export_shipments", "export_lines", "dta_clearances",
    "dta_lines", "disposal_allocations",
)

UNIQUE = {
    "suppliers": (("name",), "uq_supplier_client_name"),
    "items": (("code",), "uq_item_client_code"),
    "description_mappings": (("scope", "normalized_description"), "uq_mapping_key"),
    "export_mappings": (("normalized_description",), "uq_export_mapping_client_key"),
    "fg_models": (("code",), "uq_model_client_code"),
    "bonds": (("bond_no",), "uq_bond_client_no"),
    "production_batches": (("batch_no",), "uq_batch_client_no"),
    "export_shipments": (("invoice_no",), "uq_export_client_invoice"),
    "dta_clearances": (("clearance_no",), "uq_dta_client_no"),
}
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _unique_name(table, cols):
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table):
        if tuple(constraint["column_names"]) == cols:
            return constraint["name"] or f"uq_{table}_{cols[0]}"
    raise RuntimeError(f"Missing unique constraint on {table}: {cols}")


def upgrade():
    op.create_table("system_state", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("session_epoch", sa.String(64), nullable=False))
    op.get_bind().execute(sa.text("INSERT INTO system_state (id, session_epoch) VALUES (1, :epoch)"),
                          {"epoch": secrets.token_urlsafe(32)})
    op.create_table("admin_audit_log",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("ts", sa.DateTime(), nullable=False),
                    sa.Column("username", sa.String(64)),
                    sa.Column("action", sa.String(8), nullable=False),
                    sa.Column("table_name", sa.String(64), nullable=False),
                    sa.Column("record_id", sa.String(64)),
                    sa.Column("old_values", sa.Text()),
                    sa.Column("new_values", sa.Text()))
    op.create_index("ix_admin_audit_log_ts", "admin_audit_log", ["ts"])
    op.create_index("ix_admin_audit_log_table_name", "admin_audit_log", ["table_name"])
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("iec_code", sa.String(32)),
        sa.Column("gstin", sa.String(32)),
        sa.Column("address", sa.Text()),
        sa.Column("backup_id", sa.String(64), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(64)),
    )
    op.get_bind().execute(sa.text("INSERT INTO clients (id, name, backup_id, active, created_at, created_by) "
                                  "VALUES (1, 'Existing data', :backup_id, true, CURRENT_TIMESTAMP, 'migration')"),
                          {"backup_id": secrets.token_urlsafe(32)})
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("is_superadmin", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute(sa.text("UPDATE users SET is_superadmin = true WHERE role = 'admin'"))
    op.create_table(
        "client_access",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.CheckConstraint("role in ('admin','operator','viewer')", name="ck_access_role"),
    )
    op.execute(sa.text("INSERT INTO client_access (user_id, client_id, role) SELECT id, 1, role FROM users"))
    for table in TABLES:
        spec = UNIQUE.get(table)
        old_name = _unique_name(table, spec[0]) if spec else None
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.add_column(sa.Column("client_id", sa.Integer(), server_default="1", nullable=False))
            batch.create_foreign_key(f"fk_{table}_client", "clients", ["client_id"], ["id"])
            batch.create_index(f"ix_{table}_client_id", ["client_id"])
            if spec:
                batch.drop_constraint(old_name, type_="unique")
                batch.create_unique_constraint(spec[1], ["client_id", *spec[0]])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("SELECT setval(pg_get_serial_sequence('clients', 'id'), 1, true)"))


def downgrade():
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT COUNT(*) FROM clients WHERE id <> 1")).scalar():
        raise RuntimeError("Cannot downgrade while additional clients exist")
    for table in reversed(TABLES):
        spec = UNIQUE.get(table)
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            if spec:
                batch.drop_constraint(spec[1], type_="unique")
                batch.create_unique_constraint(f"uq_{table}_{spec[0][0]}", list(spec[0]))
            batch.drop_index(f"ix_{table}_client_id")
            batch.drop_constraint(f"fk_{table}_client", type_="foreignkey")
            batch.drop_column("client_id")
    op.drop_table("client_access")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_superadmin")
    op.drop_table("clients")
    op.drop_table("system_state")
    op.drop_table("admin_audit_log")
