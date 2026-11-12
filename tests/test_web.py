"""End-to-end through the web layer: login, roles, CSRF, the import -> map ->
post -> produce -> export -> DTA flow, every page and every Excel export."""
import io
import re
from datetime import date

import pytest
from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from eoubond import db
from eoubond.models import Bond, Client, ClientAccess, DescriptionMapping, User
from eoubond.web import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app(f"sqlite:///{(tmp_path / 'web.db').as_posix()}", create_all=True, testing=True)
    with db.SessionLocal() as s:
        admin = User(username="admin", password_hash=generate_password_hash("adminpass1"), role="admin",
                     is_superadmin=True)
        viewer = User(username="viewer", password_hash=generate_password_hash("viewpass1"), role="viewer")
        s.add_all([admin, viewer])
        s.flush()
        s.add(ClientAccess(user_id=viewer.id, client_id=1, role="viewer"))
        s.commit()
    yield app
    db.engine.dispose()


def _csrf(client):
    page = client.get("/login").get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', page).group(1)


def login(client, user="admin", pw="adminpass1"):
    tok = _csrf(client)
    r = client.post("/login", data=dict(username=user, password=pw, _csrf=tok))
    assert r.status_code == 302
    return tok


def post(client, url, tok, **data):
    return client.post(url, data={"_csrf": tok, **data}, follow_redirects=True)


def test_requires_login_and_csrf(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.post("/login", data=dict(username="admin", password="adminpass1")).status_code == 400  # no CSRF
    tok = login(c)
    assert c.get("/").status_code == 200
    assert post(c, "/logout", tok).status_code == 200


def test_viewer_cannot_post(app):
    c = app.test_client()
    tok = login(c, "viewer", "viewpass1")
    r = post(c, "/bonds", tok, bond_no="X", bond_amount="100", valid_from="2026-01-01", valid_to="2026-12-31")
    assert "Viewers cannot edit" in r.get_data(as_text=True)
    with db.SessionLocal() as s:
        assert s.query(Bond).count() == 0


@pytest.mark.parametrize(("username", "role", "expected"), [
    ("viewer", "viewer", {"/items"}),
    ("operator", "operator", {"/items", "/settings/templates"}),
    ("manager", "admin", {"/items", "/settings/templates", "/users", "/admin/backup"}),
    ("admin", "superadmin", {"/items", "/settings/templates", "/users", "/admin/backup",
                             "/clients", "/admin/audit"}),
])
def test_sidebar_groups_respect_roles(app, username, role, expected):
    if role in ("operator", "admin"):
        with db.SessionLocal() as s:
            user = User(username=username, password_hash=generate_password_hash("password1"), role=role)
            s.add(user)
            s.flush()
            s.add(ClientAccess(user_id=user.id, client_id=1, role=role))
            s.commit()
    c = app.test_client()
    passwords = {"viewer": "viewpass1", "admin": "adminpass1"}
    login(c, username, passwords.get(username, "password1"))
    html = c.get("/").get_data(as_text=True)
    workspace = re.search(r'<nav class="sidebar-nav" aria-label="Workspace">(.*?)</nav>', html, re.S).group(1)
    settings = re.search(r'<nav class="sidebar-nav settings-nav" aria-label="Settings">(.*?)</nav>',
                         html, re.S).group(1)
    assert all(f'href="{path}"' in workspace for path in
               ("/", "/bonds", "/imports", "/production", "/exports", "/dta", "/reports", "/returns/form3"))
    settings_paths = {"/items", "/settings/templates", "/users", "/admin/backup", "/clients", "/admin/audit"}
    assert {path for path in settings_paths if f'href="{path}"' in settings} == expected
    assert 'href="/settings/templates"' not in workspace
    assert 'action="/select-client"' in html and 'action="/select-bond"' in html
    assert 'action="/logout"' in html
    if role == "viewer":
        assert c.get("/settings/templates").status_code == 302
        assert c.get("/items").status_code == 200
    else:
        assert c.get("/settings/templates").status_code == 200


def test_settings_templates_and_existing_workflow_links(app):
    c = app.test_client()
    login(c)
    html = c.get("/settings/templates").get_data(as_text=True)
    assert re.search(r'<details class="settings-group"\s+open>', html)
    assert re.search(r'<a href="/settings/templates" class="on" aria-current="page">Templates</a>', html)
    for kind in ("imports", "exports", "boms"):
        assert f'href="/{kind}/upload"' in html
        assert f'href="/{kind}/upload/blank"' in html
        assert c.get(f"/{kind}/upload").status_code == 200
        download = c.get(f"/{kind}/upload/blank")
        assert download.status_code == 200
        load_workbook(io.BytesIO(download.data))
    for page, kind in (("/", "imports"), ("/", "exports"), ("/imports", "imports"),
                       ("/exports", "exports"), ("/models", "boms")):
        assert f'href="/{kind}/upload"' in c.get(page).get_data(as_text=True)


def test_superadmin_navigation_without_active_client(app):
    with db.SessionLocal() as s:
        s.get(Client, 1).active = False
        s.commit()
    c = app.test_client()
    login(c)
    html = c.get("/clients").get_data(as_text=True)
    assert 'href="/clients" class="on" aria-current="page"' in html
    assert 'href="/admin/audit"' in html
    assert 'aria-label="Workspace"' not in html
    assert 'href="/settings/templates"' not in html


def test_full_flow_and_all_pages(app):
    c = app.test_client()
    tok = login(c)
    assert "Bond created" in post(c, "/bonds", tok, bond_no="B1", bond_amount="1000000",
                                  valid_from="2026-01-01", valid_to="2026-12-31").get_data(as_text=True)
    post(c, "/suppliers", tok, name="Acme CN", country="CN")
    for code, desc, typ in [("TFT", "TFT panel", "COMPONENT"), ("BAT", "Battery", "ACCESSORY")]:
        post(c, "/items", tok, code=code, description=desc, uom="NOS", item_type=typ)
    post(c, "/models", tok, code="4343-GEN5", name="4343 Gen5")
    with db.SessionLocal() as s:
        from eoubond.models import FGModel, Item, Supplier
        tft, bat = s.query(Item).filter_by(code="TFT").one(), s.query(Item).filter_by(code="BAT").one()
        model, sup, bond = s.query(FGModel).one(), s.query(Supplier).one(), s.query(Bond).one()
    r = c.post(f"/models/{model.id}/bom/new", data={"_csrf": tok, "effective_from": "2026-01-01",
                                                   "item_id": [tft.id, bat.id], "qty": ["1", "1"]},
               follow_redirects=True)
    assert "BOM version created" in r.get_data(as_text=True)

    # Import with an unmapped description
    r = c.post("/imports/new", data={"_csrf": tok, "invoice_no": "INV-1", "invoice_date": "2026-02-01",
                                     "supplier_id": sup.id, "bond_id": bond.id,
                                     "description": ["Panel, TFT 43cm", "Li-ion battery"], "item_id": ["", str(bat.id)],
                                     "qty": ["10", "10"], "av": ["100000", "20000"], "dsv": ["30000", "6000"]},
               follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "unmapped" in html
    inv_id = int(re.search(r"/imports/(\d+)", r.request.path).group(1))
    r = post(c, f"/imports/{inv_id}", tok, action="post")
    assert "Map every description" in r.get_data(as_text=True)
    with db.SessionLocal() as s:
        from eoubond.models import ImportLine
        line_id = s.query(ImportLine).filter(ImportLine.item_id.is_(None)).one().id
    post(c, f"/imports/{inv_id}", tok, action="resolve", **{f"map_{line_id}": str(tft.id), f"scope_{line_id}": "supplier"})
    assert "bond debited" in post(c, f"/imports/{inv_id}", tok, action="post").get_data(as_text=True)
    with db.SessionLocal() as s:
        assert s.query(DescriptionMapping).count() == 2
        assert str(s.get(Bond, bond.id).balance) == "964000.00"

    r = post(c, "/production/new", tok, action="post", batch_no="P1", production_date="2026-03-01",
             model_id=str(model.id), qty="4")
    assert "components consumed FIFO" in r.get_data(as_text=True)
    r = c.post("/exports/new", data={"_csrf": tok, "invoice_no": "EX-1", "export_date": "2026-03-05",
                                     "target": [f"FG:{model.id}", f"ITEM:{bat.id}"], "qty": ["3", "2"],
                                     "export_item_name": ["", "Spare battery"], "fob_value": ["", ""]},
               follow_redirects=True)
    assert "bond recredited" in r.get_data(as_text=True)
    r = c.post("/dta/new", data={"_csrf": tok, "clearance_no": "D-1", "clearance_date": "2026-03-06",
                                 "target": [f"FG:{model.id}"], "qty": ["1"], "description": [""], "sale_value": [""]},
               follow_redirects=True)
    assert "DTA clearance recorded" in r.get_data(as_text=True)
    dta_id = int(re.search(r"/dta/(\d+)", r.request.path).group(1))
    assert "Duty marked paid" in post(c, f"/dta/{dta_id}", tok, challan_no="CH1",
                                      paid_date="2026-03-07").get_data(as_text=True)

    # Over-export is rejected with a readable message and no partial effect
    r = c.post("/exports/new", data={"_csrf": tok, "invoice_no": "EX-2", "export_date": "2026-03-08",
                                     "target": [f"FG:{model.id}"], "qty": ["5"], "export_item_name": [""],
                                     "fob_value": [""]}, follow_redirects=True)
    assert "Insufficient duty-saved stock" in r.get_data(as_text=True)

    for url in ["/", "/bonds", "/imports", "/production", "/exports", "/dta", "/items", "/suppliers", "/models",
                "/mappings", "/reports", "/users", f"/dta/{dta_id}", "/imports/new", "/production/new",
                "/exports/new", "/dta/new", f"/bonds/{bond.id}/edit", "/production/1", "/exports/1"]:
        assert c.get(url, follow_redirects=True).status_code == 200, url
    for name in ["stock", "ledger", "monthly", "pending-dta", "imports", "production", "exports", "audit"]:
        assert c.get(f"/reports/{name}").status_code == 200, name
        x = c.get(f"/reports/{name}?format=xlsx")
        assert x.status_code == 200 and x.mimetype.endswith("sheet"), name
        load_workbook(io.BytesIO(x.data))
    wb = load_workbook(io.BytesIO(c.get("/reports/all.xlsx").data))
    assert "Monthly summary" in wb.sheetnames
    load_workbook(io.BytesIO(c.get(f"/dta/{dta_id}?format=xlsx").data))
    page = c.get("/returns/form3?quarter_end=2026-03-31").get_data(as_text=True)
    assert "Details of Goods" in page and "Panel, TFT 43cm" in page
    import xlrd
    x = c.get("/returns/form3?quarter_end=2026-03-31&format=xls")
    assert x.status_code == 200
    assert xlrd.open_workbook(file_contents=x.data).sheet_by_index(0).nrows == 3  # header + 2 BE items


def test_migration_builds_schema(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from eoubond import config

    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "migrations"))
    command.upgrade(cfg, "head")
    eng = db.make_engine(url)
    assert {"bonds", "import_lots", "bond_ledger", "audit_log", "disposal_allocations"} <= set(inspect(eng).get_table_names())
    command.downgrade(cfg, "base")
    eng.dispose()


def test_migrations_keep_data_in_a_populated_database(tmp_path, monkeypatch):
    """Migrations that rebuild SQLite tables must work when other tables point at
    them (the empty-database test above can't catch that)."""
    import sqlite3
    from datetime import date

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.orm import Session

    from eoubond import config
    from eoubond.services import exports, production
    from tests.conftest import World

    path = tmp_path / "full.db"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "migrations"))
    command.upgrade(cfg, "head")
    eng = db.make_engine(url)
    with Session(bind=eng) as s:
        w = World(s)
        w.import_("I-1", date(2026, 2, 1), [w.line(w.tft, 4, 4000, 400), w.line(w.csi, 4, 1, 1),
                                            w.line(w.pcb, 8, 1, 1), w.line(w.bat, 4, 1, 1)])
        production.produce(s, batch_no="P1", model_id=w.m4343.id, qty_units=2, production_date=date(2026, 3, 1))
        exports.post_export(s, invoice_no="E-1", export_date=date(2026, 3, 2),
                            lines=[dict(line_type="FG", model_id=w.m4343.id, qty=1)])
        s.commit()
    eng.dispose()

    def counts():
        c = sqlite3.connect(path)
        out = {t: c.execute(f"select count(*) from {t}").fetchone()[0]
               for t in ("import_lots", "consumption_allocations", "disposal_allocations", "bond_ledger")}
        assert c.execute("pragma foreign_key_check").fetchall() == []
        c.close()
        return out

    before = counts()
    assert before["disposal_allocations"] > 0
    command.downgrade(cfg, "0002")  # rebuilds the referenced tables
    command.upgrade(cfg, "head")
    assert counts() == before
