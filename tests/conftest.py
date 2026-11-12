from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from eoubond import config, db
from eoubond.audit_context import set_current_user
from eoubond.services import bonds, imports, masters


@pytest.fixture(autouse=True)
def _rules():
    config.set_rules(config.Rules())  # defaults, independent of rules.toml edits
    set_current_user("tester")
    yield
    config.set_rules(config.Rules())


@pytest.fixture
def session():
    engine = db.init_engine("sqlite://", create_all=True)
    s = Session(bind=engine, expire_on_commit=False)
    yield s
    s.close()
    db.Base.metadata.drop_all(engine)
    engine.dispose()


class World:
    """Minimal master data: two bonds, a supplier, detector components, accessories,
    two models with BOMs."""

    def __init__(self, s: Session):
        self.s = s
        self.bond = bonds.create_bond(s, bond_no="B-2026-01", bond_amount=1_000_000,
                                      valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31))
        self.bond2 = bonds.create_bond(s, bond_no="B-2026-02", bond_amount=500_000,
                                       valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31))
        self.sup = masters.create_supplier(s, name="Shenzhen Imaging Co", country="CN")
        mk = lambda code, desc, t="COMPONENT", **kw: masters.create_item(s, code=code, description=desc, item_type=t, **kw)
        self.tft = mk("TFT-4343", "TFT sensor array 43x43", bcd_rate=Decimal("7.5"))
        self.csi = mk("CSI-4343", "CsI scintillator 43x43")
        self.pcb = mk("PCB-RO", "Readout PCB assembly")
        self.bat = mk("BAT-LI", "Li-ion battery pack", "ACCESSORY")
        self.cab = mk("CAB-TETHER", "Tether cable 10m", "ACCESSORY")
        self.m4343 = masters.create_model(s, code="4343-GEN5", name="4343 Gen5 detector")
        self.bom1 = masters.create_bom_version(s, model_id=self.m4343.id, effective_from=date(2026, 1, 1), lines=[
            dict(item_id=self.tft.id, qty_per_unit=1), dict(item_id=self.csi.id, qty_per_unit=1),
            dict(item_id=self.pcb.id, qty_per_unit=2), dict(item_id=self.bat.id, qty_per_unit=1)])
        s.flush()

    def import_(self, no, d, lines, bond=None, post=True):
        inv = imports.create_import(self.s, invoice_no=no, invoice_date=d, supplier_id=self.sup.id,
                                    bond_id=(bond or self.bond).id, lines=lines)
        if post:
            imports.post_import(self.s, inv.id)
        return inv

    def line(self, item, q, av, dsv, desc=None):
        return dict(description=desc or item.description, qty=q, assessable_value=av,
                    duty_saved_value=dsv, item_id=item.id)


@pytest.fixture
def world(session):
    return World(session)


def D(x):
    return Decimal(str(x))
