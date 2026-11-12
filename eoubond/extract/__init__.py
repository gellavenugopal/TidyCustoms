"""PDF extraction for ICEGATE documents.

pdf_utils.py, sb_parser.py (= local_parser.py), sb_schema.py (= schema.py) and
sb_header_extractor.py are copied from the Shipping Bill Extractor tool
(Tools/shipping-bill-extractor/app). Changes are marked "EOU Tool": import
paths, and passing the invoice serial (INVSN) through to each line item.
boe_parser.py is new and follows the same watermark-safe approach.
"""
