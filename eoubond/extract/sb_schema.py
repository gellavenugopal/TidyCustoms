"""
Data model for a Shipping Bill and its line items.

The column order defined in LineItem (via `TEMPLATE_COLUMNS`) is the
contractual output format. Do not reorder these without updating
export.py and the front-end template checker together.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# Exact column order required by the CA workflow template.
TEMPLATE_COLUMNS = [
    "PORT CODE",
    "SB NO",
    "SB DATE",
    "INV NO",
    "INV DATE",
    "ITESN",
    "HSN/SAC",
    "Description of goods",
    "Quantity",
    "UQC",
    "FOB value (INR)",
    "IGST Value",
    "IGST AMOUNT",
    "DBK CLAIM",
    "RODTEP AMT",
]


class LineItem(BaseModel):
    """One row of the output = one item line on the shipping bill."""

    port_code: Optional[str] = Field(None, description="Port Code from the shipping bill header")
    sb_no: str = Field(..., description="Shipping Bill Number")
    sb_date: str = Field(..., description="Shipping Bill Date, DD-MM-YYYY")
    inv_no: Optional[str] = Field(
        None, description="Invoice number, from '2.INVOICE No. & Dt.' in the A.REF (Part II) section"
    )
    inv_date: Optional[str] = Field(
        None, description="Invoice date, from '2.INVOICE No. & Dt.' in the A.REF (Part II) section"
    )
    invsn: Optional[str] = Field(None, description="Invoice Sr. No. (INVSN) - EOU Tool addition")
    item_serial_no: str = Field(..., description="Item Sl./Sr. No. within the bill (ITESN)")
    hsn_sac: str = Field(..., description="HSN/SAC code of the item")
    description: str = Field(..., description="Description of goods")
    quantity: Optional[float] = Field(None, description="Quantity of goods")
    uqc: Optional[str] = Field(None, description="Unit Quantity Code, e.g. NOS, KGS, PCS")
    fob_value_inr: Optional[float] = Field(None, description="FOB value in INR")
    igst_value: Optional[float] = Field(None, description="Taxable value for IGST in INR")
    igst_amount: Optional[float] = Field(None, description="IGST amount in INR")
    dbk_claim: Optional[float] = Field(
        None, description="'1.DBK CLAIM' from the Part I D. EX.PR. summary box"
    )
    rodtep_amt: Optional[float] = Field(
        None, description="'5.RODTEP AMT' from the Part I D. EX.PR. summary box"
    )

    # Internal bookkeeping (not part of the exported columns)
    extraction_confidence: Optional[str] = Field(
        default=None, description="'high' | 'medium' | 'low' — model's self-reported confidence"
    )
    missing_fields: List[str] = Field(default_factory=list)

    @field_validator("sb_no", "sb_date", "item_serial_no", "hsn_sac", "description")
    @classmethod
    def not_blank_required(cls, v: str) -> str:
        if v is None or str(v).strip() == "":
            raise ValueError("required field is blank")
        return str(v).strip()

    def as_row(self) -> dict:
        """Return dict keyed exactly by TEMPLATE_COLUMNS, in order."""
        return {
            "PORT CODE": self.port_code,
            "SB NO": self.sb_no,
            "SB DATE": self.sb_date,
            "INV NO": self.inv_no,
            "INV DATE": self.inv_date,
            "ITESN": self.item_serial_no,
            "HSN/SAC": self.hsn_sac,
            "Description of goods": self.description,
            "Quantity": self.quantity,
            "UQC": self.uqc,
            "FOB value (INR)": self.fob_value_inr,
            "IGST Value": self.igst_value,
            "IGST AMOUNT": self.igst_amount,
            "DBK CLAIM": self.dbk_claim,
            "RODTEP AMT": self.rodtep_amt,
        }


class ExtractionResult(BaseModel):
    """Full result of processing one uploaded document."""

    source_filename: str
    success: bool
    line_items: List[LineItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    raw_model_notes: Optional[str] = Field(
        default=None, description="Any free-text caveats the extraction model returned"
    )

    # Reconciliation: cross-checks the sum of every extracted line item's
    # IGST VALUE / IGST AMOUNT against the shipping bill's own Part I
    # summary totals ("4.IGST VALUE" and "2. IGST AMT"). A mismatch here is
    # a strong, document-verified signal that extraction is incomplete or
    # wrong for this file -- catches exactly the class of bug where a page
    # silently contributes zero or partial rows without raising an error.
    header_igst_value: Optional[float] = Field(
        default=None, description="'4.IGST VALUE' total from the bill's Part I summary"
    )
    header_igst_amount: Optional[float] = Field(
        default=None, description="'2. IGST AMT' total from the bill's Part I summary"
    )
    extracted_igst_value_sum: Optional[float] = Field(
        default=None, description="Sum of igst_value across all extracted line items"
    )
    extracted_igst_amount_sum: Optional[float] = Field(
        default=None, description="Sum of igst_amount across all extracted line items"
    )
    igst_reconciled: Optional[bool] = Field(
        default=None,
        description="True if extracted sums match the header totals within rounding tolerance; "
        "None if header totals could not be located on the bill's first page.",
    )
