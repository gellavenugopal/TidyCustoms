"""Runtime settings (environment) and business rules (config/rules.toml)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_RULES_FILE = BASE_DIR / "config" / "rules.toml"

try:  # optional .env support for local development
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass


@dataclass(frozen=True)
class Rules:
    default_bcd_rate: Decimal = Decimal("10")
    sws_rate_on_bcd: Decimal = Decimal("10")
    round_duty_to_rupee: bool = True
    reject_import_outside_validity: bool = True
    allow_credit_after_expiry: bool = True
    dta_recredit_on: str = "payment"  # payment | clearance | never
    consumption_memo_entries: bool = True
    production_scope: str = "global"  # global | bond
    lot_must_predate_use: bool = True
    mapping_strip_punctuation: bool = True
    form3_sws_in_bcd_column: bool = True
    form3_item_export_as: str = "export"  # export | re_export
    form3_fg_stock_as: str = "pending"  # pending | export
    form3_default_port_of_import: str = "INBOM4"
    form3_default_port_of_export: str = "INBOM4"


def load_rules(path: str | os.PathLike | None = None) -> Rules:
    path = Path(path or rules_file())
    if not path.exists():
        return Rules()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    duty, bond, fifo, mapping, f3 = (raw.get(k, {}) for k in ("duty", "bond", "fifo", "mapping", "form3"))
    rules = Rules(
        default_bcd_rate=Decimal(str(duty.get("default_bcd_rate", 10))),
        sws_rate_on_bcd=Decimal(str(duty.get("sws_rate_on_bcd", 10))),
        round_duty_to_rupee=bool(duty.get("round_duty_to_rupee", True)),
        reject_import_outside_validity=bool(bond.get("reject_import_outside_validity", True)),
        allow_credit_after_expiry=bool(bond.get("allow_credit_after_expiry", True)),
        dta_recredit_on=str(bond.get("dta_recredit_on", "payment")),
        consumption_memo_entries=bool(bond.get("consumption_memo_entries", True)),
        production_scope=str(fifo.get("production_scope", "global")),
        lot_must_predate_use=bool(fifo.get("lot_must_predate_use", True)),
        mapping_strip_punctuation=bool(mapping.get("strip_punctuation", True)),
        form3_sws_in_bcd_column=bool(f3.get("sws_in_bcd_column", True)),
        form3_item_export_as=str(f3.get("item_export_as", "export")),
        form3_fg_stock_as=str(f3.get("fg_stock_as", "pending")),
        form3_default_port_of_import=str(f3.get("default_port_of_import", "INBOM4")),
        form3_default_port_of_export=str(f3.get("default_port_of_export", "INBOM4")),
    )
    if rules.form3_item_export_as not in ("export", "re_export"):
        raise ValueError("form3.item_export_as must be export or re_export")
    if rules.form3_fg_stock_as not in ("pending", "export"):
        raise ValueError("form3.fg_stock_as must be pending or export")
    if rules.dta_recredit_on not in ("payment", "clearance", "never"):
        raise ValueError("bond.dta_recredit_on must be payment, clearance or never")
    if rules.production_scope not in ("global", "bond"):
        raise ValueError("fifo.production_scope must be global or bond")
    return rules


_rules: Rules | None = None


def get_rules() -> Rules:
    global _rules
    if _rules is None:
        _rules = load_rules()
    return _rules


def set_rules(rules: Rules | None = None, **overrides) -> Rules:
    """Replace the active rules (used by tests and the CLI)."""
    global _rules
    base = rules or get_rules()
    _rules = replace(base, **overrides) if overrides else base
    return _rules


def data_dir() -> Path:
    """Where the live database and secret key live.

    EOU_DATA_DIR if set; on Windows %LOCALAPPDATA%\\EOU Bond Tracker - deliberately
    OUTSIDE OneDrive/Dropbox, because syncing a database while it is being written
    can corrupt it. Backups (which are safe to sync) go to BACKUP_DIR instead."""
    if os.environ.get("EOU_DATA_DIR"):
        return Path(os.environ["EOU_DATA_DIR"])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "EOU Bond Tracker"
    return BASE_DIR


def database_url() -> str:
    if not os.environ.get("DATABASE_URL"):
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(d / 'eoubond.db').as_posix()}"
    url = os.environ["DATABASE_URL"]
    # Heroku/Render style URLs
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def secret_key() -> str:
    """SECRET_KEY from the environment, else a random key generated once and kept
    in the data folder (so logins survive restarts on a local install)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = data_dir() / "secret_key"
    try:
        if path.exists():
            return path.read_text().strip()
        import secrets

        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        path.write_text(key)
        return key
    except OSError:  # read-only filesystem (some containers): fall back, sessions reset on restart
        return "dev-only-change-me"


def brand() -> dict:
    """Header branding, set in .env: BRAND_NAME, BRAND_TAGLINE, BRAND_MARK (2-3 letters)
    and BRAND_CREDIT (e.g. a "designed & developed by" line; blank = not shown)."""
    return dict(name=os.environ.get("BRAND_NAME") or "EOU Bond Tracker",
                tagline=os.environ.get("BRAND_TAGLINE") or "Duty-saved inventory under bond",
                mark=(os.environ.get("BRAND_MARK") or "EOU")[:3],
                credit=os.environ.get("BRAND_CREDIT") or "",
                credit_label=os.environ.get("BRAND_CREDIT_LABEL") or "Designed & developed by")


def rules_file() -> Path:
    return Path(os.environ.get("EOU_RULES_FILE") or DEFAULT_RULES_FILE)


def _sync_roots() -> list[Path]:
    """Folders that are copied off this PC (OneDrive etc.)."""
    roots = [os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    return [Path(r) for r in roots if r]


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def backup_dir() -> Path:
    """Where backups are written: BACKUP_DIR if set, else a "backups" folder next
    to the app. Backups must be somewhere that survives a format - OneDrive,
    Google Drive, Dropbox or a network folder."""
    return Path(os.environ.get("BACKUP_DIR") or (BASE_DIR / "backups"))


def backup_dir_is_safe() -> bool:
    """True when backups are copied off this PC: inside OneDrive (or another
    folder whose path names a sync service) or on a network share - and never in
    the same folder as the live database."""
    b = backup_dir()
    if data_dir().resolve() != BASE_DIR.resolve() and _under(b, data_dir()):
        return False  # next to the live database on this PC's local disk
    if str(b).startswith("\\\\"):
        return True
    if any(_under(b, r) for r in _sync_roots()):
        return True
    return any(s in str(b).lower() for s in ("onedrive", "google drive", "googledrive", "dropbox", "icloud"))


def backup_keep() -> int:
    return int(os.environ.get("BACKUP_KEEP") or "60")


def backup_interval_hours() -> float:
    return float(os.environ.get("BACKUP_INTERVAL_HOURS") or "24")
