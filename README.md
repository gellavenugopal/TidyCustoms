# EOU Bond Tracker

Duty-saved inventory under bond for an Export Oriented Unit that makes X-ray flat
panel detectors (4343 Gen5, 2121) and exports them with accessories (cables, spare
batteries, etc.).

It records bonds, imports under bond (with supplier-description → item-code
mapping), BOM-driven production with FIFO consumption, exports that recredit the
bond, DTA clearances with BCD + SWS duty computation, a bond ledger, reports with
Excel export, and an audit trail of every change.

Stack: Python 3.12, Flask, SQLAlchemy 2, Alembic, openpyxl. PostgreSQL in
production, SQLite for local development.

---

## 0. Run it on your Windows PC

1. Install **Python 3.12** from python.org with the Python launcher enabled.
2. Keep the program in `D:\Programs\TidyCustoms\EOU Tool`, or another folder
   of your choice. The default backup folder is only synced if this folder is synced.
3. Double-click **`start.bat`**. The first run on a PC takes a few minutes (it
   sets up Python packages and the database); later runs start in seconds. Your
   browser opens http://localhost:5000. Keep the black window open while you
   work; close it to stop the app.
4. On first start the app asks you to **create the administrator** - or to
   **restore from a backup** if you are reinstalling.
5. Sign in as the super-admin, open **Clients**, and add each client. The
   existing records are assigned to the editable **Existing data** client.
   Select a client in the header, then use **Users** to create users or assign
   existing users a role for that client.

Where things are kept:

| What | Where | Synced by OneDrive? |
|---|---|---|
| Program files | this folder | only if you place it in a synced folder |
| Backups | `backups\` in this folder (or `BACKUP_DIR` in `.env`) | only if the backup folder is synced |
| Live database, secret key | `%LOCALAPPDATA%\EOU Bond Tracker\` | no - on purpose (syncing a live database can corrupt it) |
| Python environment | `%LOCALAPPDATA%\EOU Bond Tracker\venv` | no - `start.bat` rebuilds it on any PC |

If the program folder is not in OneDrive, set `BACKUP_DIR` in
`.env` to a OneDrive or network folder; the **Backup** page warns you when
backups are not being copied off the PC.

**Running it in the background (no window)** - after the first run of
`start.bat` on a PC:

| File | What it does |
|---|---|
| `start-background.vbs` | Starts the app hidden and opens the browser. If it's already running, only opens the browser. |
| `stop.bat` | Stops the app (safe - each save is all-or-nothing). |
| `autostart-on.bat` | Starts it hidden every time you log in to Windows (Startup-folder shortcut, no admin rights). |
| `autostart-off.bat` | Undoes `autostart-on.bat`. |

With no window, messages and errors go to
`%LOCALAPPDATA%\EOU Bond Tracker\app.log` (a fresh log is started after 5 MB);
if the app doesn't start, the starter says so and opens the log.

Other PCs in the office can use it too: set `HOST=0.0.0.0` in `.env`, allow
port 5000 in Windows Firewall, and open `http://<this-pc-name>:5000` on the other PCs.

## Backup & restore (survives a format)

* **Automatic:** while the app runs, a full backup is written to `BACKUP_DIR`
  every 24 hours and at every start (newest 60 kept). Change with
  `BACKUP_INTERVAL_HOURS` and `BACKUP_KEEP` in `.env`.
* **Client backup:** a client admin can download and restore the selected
  client's business records. It excludes accounts and other clients and can
  only be restored to the same client. Client restore leaves users and business
  rules unchanged.
* **System backup:** a super-admin can download and restore all clients, users,
  permissions and rules. Automatic backups and `backup-now.bat` are system
  backups. Restoring one replaces the entire system and signs everyone out.
  Older single-client backups are system backups; their records become the
  **Existing data** client and existing administrators become super-admins.
* Restore requires typing `RESTORE` and saves a safety backup before changing
  data. Backups have a checksum; a damaged file is refused.

**After a format / on a new PC:** install Python → recover the program and
your backups → double-click `start.bat` → the app opens on
**Restore from a backup** → pick the newest system backup from the `backups` folder.
Everything comes back, and you sign in with your usual username.
(Command-line alternative:
`"%LOCALAPPDATA%\EOU Bond Tracker\venv\Scripts\python.exe" -m eoubond.cli restore <file.zip>`.)

The same backup file also restores into the cloud version (PostgreSQL), and a
cloud backup restores on a PC. Backups from an older version of the app
restore into a newer one; a backup from a newer version is refused until you
update the app.

The live database stays out of OneDrive by default (see the table above);
don't point `DATABASE_URL` or `EOU_DATA_DIR` into OneDrive.

---

## 1. Quick start for developers (SQLite)

```bash
python -m venv .venv
```
```bash
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
```
```bash
pip install -r requirements.txt
```
```bash
python -m eoubond.cli upgrade                       # create / migrate the database
```
```bash
python -m eoubond.cli create-user admin --role admin # prompts for a password
```
```bash
python -m eoubond.cli seed-demo                     # optional demo data
```
```bash
python -m eoubond.cli run                           # http://127.0.0.1:5000
```

On Windows, the SQLite file is `%LOCALAPPDATA%\EOU Bond Tracker\eoubond.db`
by default; on other platforms it is in the project folder. Set `DATABASE_URL`
(see `.env.example`) to use PostgreSQL instead.

On Windows, `gunicorn` and `psycopg[binary]` in `requirements.txt` can be skipped
for SQLite-only development: `pip install Flask SQLAlchemy alembic openpyxl python-dotenv pytest`.

### Run the tests

```bash
python -m pytest -q
```

Tests (install `requirements-dev.txt` for the formula recalculation tests) cover the formula workbooks (recalculated and compared with the app), backup/restore (full round trip after total loss, damaged files, older/newer backups, safety copy, automatic backups, first-run restore), the Form III return (column identities, row selection, rule switches, .xls layout), FIFO allocation, bond debit/credit and validity, description
mapping, BOM versioning and BOM-driven consumption, atomic rollback on shortage,
export recredit (FG and accessories, split across bonds), DTA duty computation
and rounding, the monthly summary and ledger running balance, Excel export, the
audit trail, the database CHECK constraints, the Alembic migration, and the whole
flow through the web UI (login, roles, CSRF).

Local business documents and document tests containing real transaction data
are excluded from the public repository. Keep them separately and do not
commit them without removing sensitive data.

### Docker (app + PostgreSQL)

```bash
docker compose up --build
```
```bash
docker compose exec app python -m eoubond.cli create-user admin --role admin
```

Then open http://localhost:8000. For hosting see [docs/CLOUD_HOSTING.md](docs/CLOUD_HOSTING.md).

---

## 2. How it works

### Flow

```
Import invoice (draft) ── map every description to an item code ── POST
   │  creates one ImportLot per line, DEBITS the bond by the duty-saved value
   ▼
Production batch (model, qty, date)
   │  BOM version effective on the date × qty = component requirement
   │  each component drawn FIFO from ImportLots  → ConsumptionAllocation rows
   │  creates an FGLot; zero-value MEMO entry in the bond ledger
   ▼
Export shipment                          DTA clearance
   │  FG line: FIFO over FGLots,            │  same FIFO draw as exports
   │    releasing each lot's share of       │  duty = BCD + SWS on the inputs'
   │    consumed import lots                │    assessable value (no GST)
   │  ITEM line (accessory): FIFO over      │  status PENDING → PAID (challan)
   │    ImportLots directly                 │  on payment: CREDIT bond(s)
   │  CREDIT each bond by the duty-saved
   │    value of the inputs that left
```

Every disposal (export or DTA) is traced back to the exact import lots it used in
`disposal_allocations`, so each recredit and each duty line can be audited to the
import invoice and line.

### Bond balance

`balance = bond amount − debits + credits`. Ledger entries:

| Entry | Source | Effect |
|---|---|---|
| IMPORT_DEBIT | import invoice | − duty-saved value of the invoice |
| IMPORT_REVERSAL | cancelled import | + same amount (only if no stock from it was used) |
| CONSUMPTION_MEMO | production batch | 0 (shows duty-saved value moved RM → FG) |
| EXPORT_CREDIT | export shipment | + duty-saved value of inputs exported, per bond |
| DTA_CREDIT | DTA clearance | + duty-saved value of inputs cleared, per bond |

### Atomicity and enforcement

* Each service runs inside the request's transaction and never commits; the web
  layer commits on success and rolls back on any error, so a FIFO allocation and
  its ledger entries are all-or-nothing.
* Requirements are checked for **all** components/lines before anything is
  touched, so a shortage reports every short item at once.
* PostgreSQL: bond and lot rows are locked with `SELECT … FOR UPDATE`.
  SQLite: every transaction starts with `BEGIN IMMEDIATE`, which serialises writers.
* Database CHECK constraints (32 in total) are a second line of defence even if
  someone bypasses the application: lot `qty_available` / value between 0 and
  what was received; bond `balance` between 0 and `bond_amount`; consumption
  remainders within what was consumed; positive quantities; valid status values.

### Audit trail

An SQLAlchemy `after_flush` hook records changes in the same transaction:
user, timestamp (UTC), table, record id, old and new values (password hashes
masked). Client business activity appears under **Reports → Audit trail**.
Client, user and permission changes appear under **Admin audit**, accessible
only to super-admins.

### Roles

* **super-admin** – manage all clients, users, roles and system backups
* **admin** – manage data and users for the selected client
* **operator** – master data and transaction entry/posting
* **viewer** – read-only screens and Excel downloads

Roles are assigned separately for each client. A client admin cannot reset or
disable a user shared with another client; only a super-admin can do that.
Business records, reports and upload previews are isolated by selected client.
Business rules in `config/rules.toml` are shared by all clients.

---

## 3. Screens

| Area | What you can do |
|---|---|
| Dashboard | Bond balances and utilisation, pending DTA duty, draft imports with unmapped lines |
| Bonds | Create, edit (amount / validity / status), select the working bond (top bar), view ledger / monthly / stock |
| Imports | Enter invoice + lines → draft; resolve unmapped descriptions (remember per supplier or for all suppliers); post (debits bond); cancel an untouched import |
| Production | Check availability per component, post a batch; view FIFO consumption per lot |
| Exports | Finished detectors and accessories (each with its export invoice name); view the lot-by-lot recredit trace |
| DTA | Record a clearance, view the duty computation per line and per input, export it to Excel, mark paid |
| Reports | Stock by bond (RM + inside FG), FG stock, bond ledger (date filter), monthly debit/credit summary, pending DTA duty, import / production / export registers, audit trail – each with **Export to Excel**, plus a single workbook with all reports |
| Form III | Quarterly return in the filed layout (see below): pick the quarter, review the six sheets and the "check before filing" list, download the .xls |
| Masters | Items (unique codes, item-level BCD/SWS rates), suppliers, models, BOM versions, description mappings |

### Uploading Bills of Entry and Shipping Bills

**Imports → Upload BoE PDFs / Excel** and **Exports → Upload SB PDFs / Excel**:

1. **PDFs → Excel.** Upload one or more ICEGATE PDFs; every item is extracted
   into one pre-filled template. Yellow cells are still needed.
   * Bill of Entry: BE No / date / port (page header), bond and bond debit
     (Part I), invoice no / date / supplier (Part II), CTH, description, qty,
     UQC, assessable value and BCD/SWS rates (Part III), duty forgone per item
     (Part IV-G: "BCD AMT FG" = BCD + SWS, "OTH AMT FG" = IGST). Item duty is
     reconciled to the bond debit printed in Part I.
   * Shipping Bill: the Shipping Bill Extractor's own parser
     (`Tools/shipping-bill-extractor`), unchanged apart from import paths and
     passing the invoice serial through - port, SB no/date, invoice no/date,
     INVSN, ITEMSN, HS code, description, qty, UQC, FOB (INR); IGST is
     reconciled to the bill's Part I totals as before.
2. **Tag and fill in Excel.** Imports: the unique **Part No** per row (sheet
   *Parts* lists existing codes; a new code becomes a new part on upload;
   part numbers printed in the description are suggested).
   Exports: **FG + model code** or **ITEM + Part No**.
   Tags are remembered for the same description next time. The *Check* sheet
   reconciles each BoE's duty to its bond debit (and each SB's item count / FOB)
   with formulas and links back to the rows.
3. **Upload the template → preview → confirm.** The preview is a full dry run
   (nothing saved): per invoice / shipment it shows what will be created, new
   parts and suppliers, the duty forgone or bond recredit, and any error with
   its Excel row. Confirm creates the good ones; fix the rest and upload the
   same file again - documents already in the tracker are skipped.
   Imports are saved as drafts (or posted, if ticked); exports are posted.

### Bills of Material from Excel

**Masters → Models & BOMs → Upload BOMs from Excel.** The template lists every
model's current BOM, one row per component (model code, effective date, Part
No, qty per finished unit). One file can hold every finished good.

* **Change a BOM:** edit its rows and give them a **new Effective from** date -
  that becomes the next version; earlier versions stay unchanged, so past
  production keeps the BOM it was made with.
* **New model:** rows with its code, *Model name*, a date and its components.
* **New part:** a Part No not yet in the item master is created (give it a
  description and UOM).
* Preview first; unchanged BOMs, and re-uploads of the same file, are skipped.
  A different BOM on an *existing* model + date is refused (versions are never
  overwritten), as is the same part twice in one BOM.

### Exports entered by hand

**Exports → Excel template with stock on hand** downloads the export template
pre-filled with every finished model and part currently in stock, with the
quantity available and a *Stock check* formula. Fill in the SB details and the
quantity shipped on the rows you exported; rows left without SB No and Qty are
ignored on upload.

### Duty-paid imports

An import is either **Under bond** (duty forgone - debits the bond) or **Duty
paid** (cleared on payment of duty - no bond). Choose it on the import screen,
or in the *Import type* column of the BoE template (pre-filled DUTY PAID when a
BoE carries no bond).

* A duty-paid import **never debits a bond**; its duty heads record the duty
  *paid*, and its stock carries **zero duty-saved value**.
* When that stock is exported - as-is or inside a finished detector - **nothing
  is recredited** for it; only the bonded inputs are recredited.
* A DTA sale of duty-paid stock owes **no further customs duty**.
* Duty-paid goods are not under bond, so they are **left out of Form III**, and
  *Stock by bond* shows them as "Duty paid (no bond)".

**Import register** shows every import line with **BCD, SWS, IGST, Cess, Other**
and total, split into *duty forgone (bond debit)* and *duty paid*. **Export
register** shows the same heads for the imported inputs in each export line
(traced FIFO), split into *bond recredit* and *duty-paid inputs*.

### Look and branding

The interface follows a light "grid canvas" style with navy feature panels
(Lexend headings, IBM Plex Sans text; system fonts when offline). It is
light-only by choice - it stays on a white / light background even when the
computer is in dark mode. Modules and Masters sections are buttons with icons. The dashboard shows KPIs, bond
utilisation, monthly debits vs credits, duty saved in stock by item, where the
stock sits, alerts and recent activity - every chart has a hover tooltip and a
table view. Header branding is set in `.env`: `BRAND_NAME`, `BRAND_TAGLINE`,
`BRAND_MARK` (2-3 letters), and `BRAND_CREDIT` / `BRAND_CREDIT_LABEL` for a
"Designed & developed by" line (hidden when blank).

### Working Excel files (formulas and links)

**Reports → Working file** (also what the Excel button on Stock, Bond ledger,
Monthly summary and Pending DTA downloads) is built so the workings are visible:
every summary figure is a formula over the source rows on another sheet
(SUM, SUMIF on a key column such as `Bond|Month`, VLOOKUP, ROUND, IF), green
cells are formulas, blue cells link to the rows behind a figure, and *Read me*
explains each sheet. The *Bonds* sheet reconciles: utilised = duty saved in
stock + DTA awaiting recredit + goods not received, with the unexplained
difference shown (0). **Form III → Working file** shows the same columns as
the filing file with M, R, S–V, Y, Z, AC as formulas over a *Movements* sheet.
Tests recalculate these workbooks and compare every figure with the app.

### Form III quarterly return

**Form III** (top bar) builds the quarterly return in the same layout as the filed
`Form3_Q4.xls`: the same six sheets ("Details of Goods", "Re-Export", "Goods
cleared on payment", "Job Worker Unit", "Unit Transfer", "End Use Recipient"),
the same column headings in the same order, BE and clearance dates as
`DD-MMM-YY` text, the receipt date as a real date, written as `.xls`.

* One "Details of Goods" row per Bill of Entry item. Quantities are cumulative
  from import to the quarter end, so on every row: imported = received + not
  received; used for intended purpose = domestic + export + end-use + service;
  pending = received − cleared on payment − re-exported − used. (These identities
  were checked against all 154 rows of the filed Q4 return.)
* Rows: BE items imported in the quarter, plus older BE items that still had
  stock at the start of the quarter.
* Duty columns come from the duty forgone split entered on the import line
  (BCD, SWS, IGST, cess, other). With the default rule, SWS is reported inside
  "BCD Forgone", as in the filed return.
* The screen lists what to fix before filing: missing BE number, date, port,
  clearance or receipt date, lines with no duty split, unpaid DTA clearances.

Import entry now records the Form III fields: BE number and date, port of import,
invoice serial number on the BE, OOC date, receipt date, the BE item serial, the
quantity received, and the duty forgone per head. Exports record the port of export.

---

## 4. Configuration

* Environment: `DATABASE_URL`, `SECRET_KEY`, `SESSION_COOKIE_SECURE`,
  `EOU_RULES_FILE` (see `.env.example`).
* Business rules and rates: [config/rules.toml](config/rules.toml). Restart the
  app after editing it. Item-specific BCD and SWS rates are set on the item master
  and override the defaults.

---

## 5. Assumptions

The request left these rules open. Each is implemented as described, flagged in
the code, and – where it is a rule rather than a design choice – switchable in
`config/rules.toml`. **Please confirm each with your customs consultant / DC
office before go-live.**

### Rates and duty

| ID | Assumption | Where to change |
|---|---|---|
| A-DTA-1 | Default BCD is **10 %** of assessable value. Each item can have its own BCD rate (e.g. TFT panels 7.5 % in the demo). Real rates depend on the tariff heading and any exemption notification. | `duty.default_bcd_rate`, item master |
| A-DTA-2 | SWS is **10 % of BCD** (not of assessable value). Items exempt from SWS can be given 0. | `duty.sws_rate_on_bcd`, item master |
| A-DTA-3 | BCD and SWS are each summed per DTA line and **rounded to the nearest rupee** per line. The input-wise working keeps paise. | `duty.round_duty_to_rupee` |
| A-DTA-4 | Duty on a DTA clearance is computed on the **assessable value of the duty-saved imported inputs** actually consumed (traced FIFO), at the rates in the item master **on the day the clearance is recorded**. Rates are snapshotted on each trace row, so later rate changes don't alter past computations. If your clearance is assessed on the finished goods' value instead, or at the rate on the import date, this needs a change. | code: `services/dta.py` |
| A-DTA-5 | Only BCD + SWS are computed. **GST / IGST and compensation cess are excluded** (payable on the domestic supply in the normal way). Interest, penalties and any anti-dumping / safeguard duty are not computed. | – |
| A-DTA-6 | The "sale value" on a DTA line is informational (GST base); it does not enter the customs duty computation. | – |

### Bond

| ID | Assumption | Where to change |
|---|---|---|
| A-BOND-1 | An import is rejected if its **invoice date** is outside the bond's validity, or the bond is CLOSED, or the duty-saved value exceeds the balance. (Bill of Entry date is recorded but not used for the check.) | `bond.reject_import_outside_validity` |
| A-BOND-2 | Export and DTA recredits are **allowed after the bond's validity ends** (the liability is being discharged). | `bond.allow_credit_after_expiry` |
| A-BOND-3 | A DTA clearance recredits the bond **when its duty is marked paid**. Alternatives: at clearance, or never. | `bond.dta_recredit_on` |
| A-BOND-4 | Production does not change the bond balance (the liability moves from raw material to finished goods). It writes a **zero-value memo entry** so the ledger still links to the production batch as the requirement asks. | `bond.consumption_memo_entries` |
| A-BOND-5 | The bond is debited with the **duty-saved value entered on the import line** (the total duty foregone: BCD + SWS + IGST etc. as per the Bill of Entry). The app does not recompute it. | – |
| A-BOND-6 | Recredit on export = the duty-saved value of the imported inputs in the goods exported. When one export uses inputs from several bonds, one credit entry is made per bond. | – |
| A-BOND-7 | Raising the bond amount (enhanced bond) raises the balance by the same difference; it can't be reduced below the amount already utilised. | – |
| A-BOND-8 | One bond per import invoice. | – |

### FIFO, lots and stock

| ID | Assumption | Where to change |
|---|---|---|
| A-FIFO-1 | Production consumes from **all bonds**, oldest lot first, unless the user picks a bond on the production screen. A stricter "bond must be named" mode is available. | `fifo.production_scope` |
| A-FIFO-2 | FIFO order is **lot date (import invoice date / production date), then entry order**. A lot can't be used before its own date. | `fifo.lot_must_predate_use` |
| A-FIFO-3 | Each import line is one lot. A draw that partly uses a lot takes a **pro-rata share of its value**; the draw that empties a lot takes the exact remaining value, so rounding never leaves paise stranded. Lots split freely. Quantities are kept to 3 decimals, money to 2. | code: `services/stock.py` |
| A-FIFO-4 | Finished goods are their own FIFO layer: each production batch is one FG lot carrying the import lots it consumed. Exporting or clearing *k* of *n* units releases *k/n* of each consumed lot's remaining value (exact remainder on the last unit). | – |
| A-FIFO-5 | Accessories and any goods outside the finished-goods list are exported (or cleared) **as-is from import stock**, under the name typed on the export line. The same item can also be a BOM component (e.g. the battery in the 4343 Gen5). Accessories that are themselves manufactured would need their own model + BOM. | – |
| A-FIFO-6 | Scrap, wastage, rejection, inter-unit transfer and re-export of unused raw material are not modelled. Re-export of raw material can be recorded as an ITEM export line. | – |

### Form III quarterly return

| ID | Assumption | Where to change |
|---|---|---|
| A-F3-1 | SWS is reported inside "BCD Forgone" (the filed return shows BCD Forgone = BCD × 1.1 on every row) and "Other Custom Duties" holds only other duties. | `form3.sws_in_bcd_column` |
| A-F3-2 | Accessories and other goods exported as-is (e.g. spare batteries, chargers) count as "used for manufacturing goods that are exported" (col V), as the filed return does for batteries and chargers, not as re-exports. The alternative puts them in col T and on the Re-Export sheet. | `form3.item_export_as` |
| A-F3-3 | Inputs built into finished detectors that are not yet exported or sold stay "pending in stock in the importer's premises" (col Z) until the detector leaves. **Confirmed by the user (Nov 2026)** as the treatment their return uses. Alternative (not used): count them as used for export as soon as they are consumed. | `form3.fg_stock_as` |
| A-F3-4 | Quantities are cumulative from import to the quarter end. Listed BE items: imported in the quarter, or with stock pending at the start of the quarter. The "Re-Export" and "Goods cleared on payment" sheets list only events dated in the quarter. | code: `services/form3.py` |
| A-F3-5 | Job work, unit transfer, end-use supply and output services aren't tracked, so their quantity columns are 0 and those three sheets have headers only. | – |
| A-F3-6 | Only the quantity received becomes usable stock; the bond stays debited with the duty forgone on the full BE quantity (the short quantity is reported in "Quantity Not Received"). | – |
| A-F3-7 | On "Goods cleared on payment", IGST, cess and interest are 0 (customs duty paid = BCD + SWS, per A-DTA-5); the challan is the payment reference entered on the DTA clearance. Add interest manually if any was paid. | – |
| A-F3-8 | Imports entered without a BE item serial use the line number; the invoice serial defaults to 1. Imports without a duty split report the whole duty forgone under "Other Custom Duties" and are flagged. | – |
| A-F3-9 | The quarter selector shows calendar quarters with both labels (e.g. "calendar Q4 2025 / FY 2025-26 Q3"), since the filed `Form3_Q4.xls` covers Oct–Dec. | – |

### Document upload

| ID | Assumption | Where to change |
|---|---|---|
| A-DOC-1 | BoE parsing is verified on 66 ICEGATE BEs: one EOU BE (INHYD4, Part IV-G duty forgone) and 65 MOOWR warehousing BEs (INVTZ1 / INMAA1 / INMAA4 / INMUN1; assessed and OOC copies; up to 3 invoices and 13 items, items spanning pages; WH + RE bonds on one BE; Health cess; items exempted by notification). Every one reconciles to its bond debit. To re-check a folder: set `EOU_BOE_SAMPLES` and run `pytest tests/test_boe_rules.py`. | `extract/boe_parser.py` |
| A-DOC-2 | Duty per item comes from the first of: Part IV-G (EOU; "BCD AMT FG" = BCD + SWS, split using the BCD printed in Part III's second duty block, "OTH AMT FG" = IGST); Part III "Amount" rows (warehousing BEs: assessed duty kept under bond; second-block amounts such as Health cess / AIDC go to Cess); Part III "Duty Fg" rows (items exempted by notification). | – |
| A-DOC-2b | Bond reconciliation per bond code: WH (warehouse) bond = 3 × the assessed duty; EI (EOU) bond = duty forgone; RE (end-use) bond = BCD + SWS forgone. **Decided (Nov 2026): the tracker debits every bond - including a warehouse bond - with the duty itself, not 3 × duty.** The 3 × rule is used only to check the BoE extraction. For a MOOWR warehouse bond, enter the bond amount as bond value ÷ 3 so the balance reflects the duty that can still be covered. | – |
| A-DOC-3 | The bond printed on the BoE is the customs bond registration no. If it isn't a tracker bond number, the template pre-fills the only active bond valid on the BE date (or leaves it yellow). | – |
| A-DOC-4 | An unknown Part No on the import template creates a new part (UOM = UQC, HS = CTH, type COMPONENT). Exports never create models or parts. | – |
| A-DOC-5 | One draft import per BE + invoice serial; one export shipment per SB + INVSN (the export invoice number must be unique). Uploading again skips documents already in the tracker; it does not update them. | – |
| A-DOC-6 | The SB parser recognises invoice numbers in the pattern `XXXX/yy-yy/nnnnn` (as in the extractor). Other formats come through blank (yellow) for you to fill in. | – |

### Duty-paid stock

| ID | Assumption | Where to change |
|---|---|---|
| A-DP-1 | Duty-paid and bonded lots of the same part form **one FIFO pool** (oldest lot first, whichever kind). An export or production that crosses both recredits only the bonded share. If you need to choose which kind is used first, say so - it would be a rule in `config/rules.toml`. | `services/stock.py` |
| A-DP-2 | Duty heads per export line are the import line's heads × (quantity used ÷ quantity imported). | `services/reports.py` |
| A-DP-3 | A BoE with no bond in Part I is pre-filled as DUTY PAID in the template; check it, since a missing bond can also mean the PDF wasn't read fully. | – |
| A-BOM-2 | A BOM upload is skipped when its components equal the current version, even with a new date (no duplicate versions). | – |

### Master data and mapping

| ID | Assumption | Where to change |
|---|---|---|
| A-MAP-1 | Supplier descriptions are normalised (upper case, punctuation → space, whitespace collapsed) before lookup. | `mapping.strip_punctuation` |
| A-MAP-2 | A mapping can be **supplier-specific** (default when resolving) or **global**. The supplier-specific one wins. Resolving a line also maps every other unmapped draft line with the same description. Posted imports keep the item they were posted with even if a mapping changes later. | – |
| A-BOM-1 | BOM versions are immutable: a change is a new version with a new effective date. Production uses the latest version effective on the production date, unless the user picks a version (which must already be effective). | – |
| A-INV-1 | Import invoice fields: invoice no (unique per supplier), invoice date, supplier, bond, Bill of Entry no/date, currency; per line: supplier description, qty, assessable value (INR), duty-saved value (INR). Foreign-currency value and exchange rate are not stored separately – enter INR values from the Bill of Entry. | – |
| A-POST-1 | Posted transactions are not edited. Corrections: a draft can be deleted; a posted import can be cancelled while its stock is unused (reversal entry). Reversal of production / export / DTA is not implemented in this version. | – |

---

## 6. Project layout

```
config/rules.toml          business rules and rates
eoubond/
  models.py                schema (SQLAlchemy) incl. CHECK constraints
  audit.py                 audit-trail hook
  services/                business logic (no web code)
    bonds.py               bond master + ledger postings
    stock.py               FIFO engine (import lots, FG lots)
    imports.py             import draft / mapping / post / cancel
    production.py          BOM-driven consumption
    exports.py             export + recredit
    dta.py                 DTA clearance + BCD/SWS computation
    reports.py             all reports
    form3.py               quarterly Form III return (+ .xls writer)
  excel.py                 report → .xlsx
  web/                     Flask views, templates, static
  cli.py                   upgrade / create-user / seed-demo / run
migrations/                Alembic migrations
tests/                     pytest suite
docs/CLOUD_HOSTING.md      deployment guide
```

## 7. Adding a migration

After changing `models.py`:

```bash
python -m alembic revision --autogenerate -m "describe the change"
```

Review the generated file in `migrations/versions/`, then `python -m eoubond.cli upgrade`.
