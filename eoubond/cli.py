"""Command line: python -m eoubond.cli <command>

  upgrade               apply database migrations (alembic upgrade head)
  create-user NAME      create a user (prompts for password unless --password)
  seed-demo             load demo masters and transactions (empty database only)
  serve                 start the app for daily use (waitress server + automatic backups)
  run                   start the development web server
  backup [--out DIR]    write a full backup .zip now
  restore FILE          replace ALL data with a backup .zip (asks for confirmation)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import date, timedelta

from . import config, db


def upgrade() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "migrations"))
    command.upgrade(cfg, "head")
    print("Database is at the latest migration.")


def create_user(username: str, password: str | None, role: str, full_name: str | None) -> None:
    from sqlalchemy import select
    from werkzeug.security import generate_password_hash

    from .audit_context import acting_as
    from .models import User

    password = password or getpass.getpass("Password: ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters")
    db.init_engine()
    with acting_as("cli"), db.SessionLocal() as s:
        if s.scalar(select(User).where(User.username == username)):
            sys.exit(f"User {username} already exists")
        s.add(User(username=username, full_name=full_name, role=role,
                   password_hash=generate_password_hash(password)))
        s.commit()
    print(f"Created {role} user {username}")


def seed_demo() -> None:
    """Demo data: 4343 Gen5 and 2121 detectors, accessories, two bonds, imports
    with messy supplier descriptions, production, an export and a DTA sale."""
    from sqlalchemy import select

    from .audit_context import acting_as
    from .models import Bond
    from .services import bonds, dta, exports, imports, masters, production

    db.init_engine()
    with acting_as("demo-seed"), db.SessionLocal() as s:
        if s.scalar(select(Bond)):
            sys.exit("Database already has data; seed-demo only runs on an empty database")
        t = date.today()
        start = date(t.year, 1, 1)
        d = lambda n: min(start + timedelta(days=n), t)  # noqa: E731
        b1 = bonds.create_bond(s, bond_no="EOU/B-17/2025-26", bond_amount=25_000_000,
                               valid_from=date(t.year - 1, 4, 1), valid_to=date(t.year + 1, 3, 31))
        b2 = bonds.create_bond(s, bond_no="EOU/B-18/2026-27", bond_amount=10_000_000,
                               valid_from=start, valid_to=date(t.year + 2, 3, 31))
        sup1 = masters.create_supplier(s, name="Shenzhen Imaging Components Co", country="China")
        sup2 = masters.create_supplier(s, name="Hamamatsu Sensor Trading", country="Japan")
        it = {}
        for code, desc, typ, uom, bcd in [
            ("TFT-4343", "a-Si TFT sensor panel 43x43 cm", "COMPONENT", "NOS", "7.5"),
            ("TFT-2121", "a-Si TFT sensor panel 21x21 cm", "COMPONENT", "NOS", "7.5"),
            ("CSI-4343", "CsI(Tl) scintillator plate 43x43", "COMPONENT", "NOS", None),
            ("CSI-2121", "CsI(Tl) scintillator plate 21x21", "COMPONENT", "NOS", None),
            ("PCB-RO-G5", "Readout electronics board Gen5", "COMPONENT", "NOS", None),
            ("ASIC-RO", "Readout ASIC chip", "COMPONENT", "NOS", "0"),
            ("HSG-CF43", "Carbon fibre housing 4343", "COMPONENT", "NOS", None),
            ("HSG-AL21", "Aluminium housing 2121", "COMPONENT", "NOS", None),
            ("BAT-LI-G5", "Li-ion battery pack 7.4V", "ACCESSORY", "NOS", "20"),
            ("CAB-TETH-10", "Tether cable 10 m", "ACCESSORY", "NOS", None),
            ("CHG-DOCK", "Battery charger dock", "ACCESSORY", "NOS", None),
        ]:
            it[code] = masters.create_item(s, code=code, description=desc, item_type=typ, uom=uom, bcd_rate=bcd)
        m43 = masters.create_model(s, code="4343-GEN5", name="4343 Gen5 wireless flat panel detector")
        m21 = masters.create_model(s, code="2121", name="2121 flat panel detector")
        L = lambda *pairs: [dict(item_id=it[c].id, qty_per_unit=q) for c, q in pairs]  # noqa: E731
        masters.create_bom_version(s, model_id=m43.id, effective_from=date(t.year - 1, 4, 1), remarks="Initial release",
                                   lines=L(("TFT-4343", 1), ("CSI-4343", 1), ("PCB-RO-G5", 1), ("ASIC-RO", 8),
                                           ("HSG-CF43", 1), ("BAT-LI-G5", 1)))
        masters.create_bom_version(s, model_id=m43.id, effective_from=start, remarks="ECN-0142: 2 batteries per unit",
                                   lines=L(("TFT-4343", 1), ("CSI-4343", 1), ("PCB-RO-G5", 1), ("ASIC-RO", 8),
                                           ("HSG-CF43", 1), ("BAT-LI-G5", 2)))
        masters.create_bom_version(s, model_id=m21.id, effective_from=date(t.year - 1, 4, 1),
                                   lines=L(("TFT-2121", 1), ("CSI-2121", 1), ("ASIC-RO", 4), ("HSG-AL21", 1)))
        # Known supplier wording, saved as mappings
        for desc, code in [("TFT ARRAY PANEL, 43*43CM (A-SI)", "TFT-4343"), ("Scintillator CsI 430x430mm", "CSI-4343"),
                           ("RO BOARD G5 ASSY", "PCB-RO-G5"), ("IC, ASIC Readout 256ch", "ASIC-RO"),
                           ("Battery Li-ion 7.4V 3400mAh", "BAT-LI-G5"), ("Cable - tether 10M", "CAB-TETH-10"),
                           ("CF Housing 4343", "HSG-CF43")]:
            masters.save_mapping(s, description=desc, item_id=it[code].id, supplier_id=sup1.id)

        def imp(no, day, sup, bond, lines, port="INBOM4"):
            # duty forgone split as on the BE: BCD 10%, SWS 10% of BCD, IGST 18% on (AV + BCD + SWS);
            # the demo's "ds" figure is ignored in favour of the split total
            def split(av):
                bcd = round(av * 0.10, 2)
                sws = round(bcd * 0.10, 2)
                return dict(bcd_forgone=bcd, sws_forgone=sws, igst_forgone=round((av + bcd + sws) * 0.18, 2))
            inv = imports.create_import(
                s, invoice_no=no, invoice_date=d(day), supplier_id=sup.id, bond_id=bond.id,
                be_no=str(6_620_000 + day * 37), be_date=d(day), currency="USD", port_of_import=port,
                clearance_date=d(day + 1), receipt_date=d(day + 2),
                lines=[dict(description=a, qty=q, assessable_value=av, **split(av)) for a, q, av, _ds in lines])
            return inv

        inv1 = imp("SZ-INV-001", 5, sup1, b1, [
            ("TFT ARRAY PANEL, 43*43CM (A-SI)", 20, 3_200_000, 1_066_000),
            ("Scintillator CsI 430x430mm", 20, 1_400_000, 466_000),
            ("RO BOARD G5 ASSY", 20, 600_000, 200_000),
            ("IC, ASIC Readout 256ch", 200, 400_000, 72_000),
            ("CF Housing 4343", 20, 160_000, 53_000),
            ("Battery Li-ion 7.4V 3400mAh", 60, 300_000, 132_000)])
        imports.post_import(s, inv1.id)
        inv2 = imp("SZ-INV-014", 40, sup1, b2, [
            ("tft array panel 43x43cm a-si", 10, 1_650_000, 550_000),   # new wording -> mapped below
            ("Cable - tether 10M", 30, 90_000, 30_000),
            ("Battery Li-ion 7.4V 3400mAh", 20, 100_000, 44_000)])
        imports.resolve_line(s, inv2.lines[0].id, it["TFT-4343"].id)
        imports.post_import(s, inv2.id)
        # A draft with an unmapped description, left for the user to resolve
        imp("HSC-2291", 45, sup2, b2, port="INHYD4", lines=[("Flat panel sensor 21cm sq. a-Si", 10, 900_000, 300_000),
                                       ("Scint. plate CsI 21x21", 10, 380_000, 126_000)])
        s.flush()

        production.produce(s, batch_no="P-G5-001", model_id=m43.id, qty_units=12, production_date=d(20))
        production.produce(s, batch_no="P-G5-002", model_id=m43.id, qty_units=8, production_date=d(50))
        exports.post_export(s, invoice_no="EXP-0101", export_date=d(55), shipping_bill_no="5521907",
                            shipping_bill_date=d(55), consignee="MedImaging GmbH", country="Germany", lines=[
                                dict(line_type="FG", model_id=m43.id, qty=14, fob_value=14 * 610_000,
                                     export_item_name="4343 Gen5 wireless FPD"),
                                dict(line_type="ITEM", item_id=it["CAB-TETH-10"].id, qty=14, fob_value=14 * 6_000,
                                     export_item_name="Tether cable 10 m"),
                                dict(line_type="ITEM", item_id=it["BAT-LI-G5"].id, qty=6, fob_value=6 * 9_000,
                                     export_item_name="Spare battery pack")])
        clr = dta.post_dta(s, clearance_no="DTA-001", clearance_date=d(60), customer="City Diagnostics Pvt Ltd",
                           lines=[dict(line_type="FG", model_id=m43.id, qty=2, sale_value=2 * 750_000,
                                       description="4343 Gen5 wireless FPD")])
        dta.post_dta(s, clearance_no="DTA-002", clearance_date=d(62), customer="City Diagnostics Pvt Ltd",
                     lines=[dict(line_type="ITEM", item_id=it["BAT-LI-G5"].id, qty=4, sale_value=4 * 11_000)])
        dta.mark_paid(s, clr.id, challan_no="ICEGATE-CH-778812", paid_date=d(63))
        s.commit()
    print("Demo data loaded.")


def serve(host: str, port: int, auto_backup: bool = True) -> None:
    from . import backup as bk
    from .web import create_app

    app = create_app()
    print(f"EOU Bond Tracker {__import__('eoubond').__version__} - database: {config.database_url().split('@')[-1]}")
    print(f"Backups folder: {config.backup_dir()}")
    if not config.backup_dir_is_safe():
        print("  WARNING: backups are inside the app folder and would be lost if this PC is formatted.")
        print("  Set BACKUP_DIR in .env to a OneDrive / network folder (see README).")
    if auto_backup:
        bk.start_auto_backup_thread()
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    print(f"Open {url} in your browser. To stop: close this window, or double-click stop.bat "
          "when it runs in the background.", flush=True)
    try:
        from waitress import serve as wserve

        wserve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port)


def restore(path: str, yes: bool, restore_rules: bool) -> None:
    from pathlib import Path

    from . import backup as bk

    db.init_engine()
    blob = Path(path).read_bytes()
    try:
        m = bk.describe(blob)
    except bk.BackupError as exc:
        sys.exit(str(exc))
    print(f"Backup taken {m['created_at']} (app {m['app_version']}, {sum(m['row_counts'].values())} rows)")
    if not yes and input("This REPLACES ALL current data. Type RESTORE to continue: ").strip() != "RESTORE":
        sys.exit("Cancelled")
    res = bk.restore_backup(blob, restore_rules=restore_rules, restored_by="cli")
    print(f"Restored {res['rows']} rows." + (f" Previous data saved to {res['safety_backup']}" if res['safety_backup'] else ""))
    if res["rules_restored"]:
        print("Business rules (config/rules.toml) restored from the backup; previous file kept as rules.toml.bak")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m eoubond.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("upgrade")
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--password")
    cu.add_argument("--role", default="admin", choices=["admin", "operator", "viewer"])
    cu.add_argument("--full-name")
    sub.add_parser("seed-demo")
    r = sub.add_parser("run")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 5000))
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1",
                    help="0.0.0.0 to let other PCs on the office network connect")
    sv.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 5000))
    sv.add_argument("--no-auto-backup", action="store_true")
    b = sub.add_parser("backup")
    b.add_argument("--out", help="folder (default: BACKUP_DIR)")
    rs = sub.add_parser("restore")
    rs.add_argument("file")
    rs.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    rs.add_argument("--keep-rules", action="store_true", help="keep the current rules.toml")
    a = p.parse_args(argv)
    if a.cmd == "serve":
        serve(a.host, a.port, auto_backup=not a.no_auto_backup)
    elif a.cmd == "backup":
        from . import backup as bk

        db.init_engine()
        print(f"Backup written: {bk.write_backup(a.out)}")
    elif a.cmd == "restore":
        restore(a.file, a.yes, not a.keep_rules)
    elif a.cmd == "upgrade":
        upgrade()
    elif a.cmd == "create-user":
        create_user(a.username, a.password, a.role, a.full_name)
    elif a.cmd == "seed-demo":
        seed_demo()
    elif a.cmd == "run":
        from .web import create_app

        create_app().run(host=a.host, port=a.port, debug=os.environ.get("FLASK_DEBUG") == "1")


if __name__ == "__main__":
    main()
