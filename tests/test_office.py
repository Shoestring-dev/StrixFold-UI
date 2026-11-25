"""Office files in StrixFold UI's chat (a spreadsheet was refused): Excel sheets read as CSV tables with
their row numbers (shared and rich strings, formats as shown, cached formula values, merged and empty cells, hidden
sheets, a cap with a note), Word paragraphs and tables, PowerPoint slides in their order, CSV in other encodings,
and a plain refusal with what to do for older Office files, password-protected files, PDFs, archives, a DTD (entity
expansion) and a zip bomb. Every file here is synthetic (tests/office_fixtures.py)."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import io
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import office_fixtures as F  # noqa: E402
import officetext as O  # noqa: E402
import studio  # noqa: E402


class Excel(unittest.TestCase):
    def test_a_workbook_as_tables(self):
        d = O.read(F.xlsx(), "report.xlsx")
        self.assertEqual((d.kind, d.detail, d.truncated), ("xlsx", "2 sheets · 6 rows", False))
        self.assertEqual(d.text.split("\n\n")[0].splitlines(), [
            '## Sheet "Sales": 5 rows, columns B-F, as CSV with the sheet\'s row numbers',
            "row,B,C,D,E,F",
            "1,Region,Item,Sales,Share,Date",
            '2,North,Widgets,"$1,234.50",45.67%,2025-10-20',      # currency and percent formats, a custom date
            "3,North,Gadgets,$99.00,,2025-10-21",                  # the merged region repeats down; a built-in date as ISO
            '5,South,Gizmos,-$20.00,"inline, ""quoted""",',         # row 4 empty: left out (row numbers say so)
            '6,Total,,"$1,313.50",=NOW(),TRUE',                    # a cached formula value; one never calculated: its formula
        ])
        self.assertIn('## Sheet "Notes" (hidden): 1 row', d.text)
        self.assertNotIn("ignored", d.text)  # the phonetic guide of a rich string

    def test_a_long_sheet_is_cut_with_a_note(self):
        d = O.read(F.xlsx(long_rows=5000), "big.xlsx", limit=20_000)
        self.assertTrue(d.truncated)
        self.assertLessEqual(len(d.text), 21_000)
        self.assertRegex(d.text, r"\(truncated after [\d,]+ rows of 5,000: the rest did not fit\)$")
        self.assertRegex(d.detail, r"^3 sheets · 5,006 rows · first [\d,]+ read$")
        self.assertIn("1,1,entry number 1 with some words", d.text)

    def test_number_and_date_formats_as_shown(self):
        cases = [(1234.5, "#,##0.00", "1,234.50"), (0.125, "0.0%", "12.5%"), (-5, "#,##0;(#,##0)", "(5)"),
                 (-5, "#,##0.00", "-5.00"), (12345.678, "0.00E+00", "1.23E+04"), (1_500_000, '0.0,,"M"', "1.5M"),
                 (1234.5, "[$€-407] #,##0.00", "€ 1,234.50"), (3, "General", "3"), (0.1 + 0.2, "General", "0.3"),
                 (0.5, "#.00", ".50"), (45950, "d-mmm-yy", "20-Oct-25"), (45950, "dddd, mmmm d, yyyy", "Monday, October 20, 2025"),
                 (0.5, "h:mm AM/PM", "12:00 PM"), (1.5, "[h]:mm:ss", "36:00:00"), (45950.75, "yyyy-mm-dd hh:mm", "2025-10-20 18:00"),
                 (0.0104166667, "mm:ss", "15:00"), (7, '0 "units"', "7 units"), (42, "@", "42")]
        for x, code, want in cases:
            self.assertEqual(O.show_number(x, code), want, (x, code))
        self.assertEqual(O.show_number(1, "yyyy-mm-dd", date1904=True), "1904-01-02")
        self.assertEqual(O.show_number(1, "yyyy-mm-dd"), "1900-01-01")


class WordAndPowerPoint(unittest.TestCase):
    def test_a_document(self):
        d = O.read(F.docx(), "plan.docx")
        self.assertEqual(d.text, "# Quarterly plan\n\n# Goals\n\n- Ship the widget\n\n- Hire two people\n\nBefore\ttab\n\n"
                                 "| Name | Role |\n|---|---|\n| Ada | Lead\\|Eng |")  # a deleted tracked change is left out
        self.assertEqual(d.detail, "15 words · 1 table")

    def test_a_deck_in_its_order(self):
        d = O.read(F.pptx(), "deck.pptx")
        self.assertEqual(d.text, "## Slide 1\nOpening title\nSubtitle\n\n## Slide 2\nSecond slide\nDetails here")
        self.assertEqual(d.detail, "2 slides")


class Refusals(unittest.TestCase):
    def refused(self, data, name):
        with self.assertRaises(O.Refused) as cm:
            O.read(data, name)
        return str(cm.exception)

    def test_what_to_do_instead(self):
        cfb = O.CFB + b"\x00" * 600
        self.assertEqual(self.refused(cfb, "report.xls"), "an older Excel file (.xls). Save it as .xlsx or .csv and attach it again.")
        self.assertIn("Save it as .docx", self.refused(cfb, "a.doc"))
        self.assertIn("Save it as .pptx", self.refused(cfb, "a.ppt"))
        self.assertIn("password-protected", self.refused(cfb, "locked.xlsx"))
        self.assertIn("PDFs aren't read here yet", self.refused(b"%PDF-1.7\n...", "report.pdf"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.txt", "hello")
        self.assertEqual(self.refused(buf.getvalue(), "files.zip"), "a .zip archive. Attach the files inside it instead.")
        self.assertIn(".xlsb", self.refused(F.pack({"_rels/.rels": F.rels([("rId1", "officeDocument", "xl/workbook.bin")]),
                                                    "xl/workbook.bin": b"\x00"}), "a.xlsb"))
        self.assertIsNone(O.read(b"plain text", "a.txt"))
        self.assertIn("can't be read", O.refusal(b"\x01\x02", "a.bin"))

    def test_no_entities_and_no_zip_bombs(self):
        laughs = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
                  f'<sst xmlns="{F.S}"><si><t>&lol2;</t></si></sst>')
        self.assertEqual(self.refused(F.xlsx(extra={"xl/sharedStrings.xml": laughs}), "a.xlsx"), "the file could not be read safely")
        old = O.MAX_INFLATE
        O.MAX_INFLATE = 1 << 20
        self.addCleanup(setattr, O, "MAX_INFLATE", old)
        bomb = F.xlsx(extra={"xl/worksheets/sheet2.xml": '<?xml version="1.0"?><worksheet><sheetData>' + " " * (4 << 20) + "</sheetData></worksheet>"})
        self.assertIn("too much to read here", self.refused(bomb, "a.xlsx"))
        self.assertIn("could not be read", self.refused(F.pack({"_rels/.rels": F.rels([("rId1", "officeDocument", "xl/workbook.xml")]),
                                                               "xl/workbook.xml": "<workbook><sheets"}), "broken.xlsx"))


class Store(unittest.TestCase):
    def test_chat_attachments(self):
        s = studio.Store(os.path.join(tempfile.mkdtemp(prefix="hs-office-"), "S"))
        u = s.add_upload(F.xlsx(), "report.xlsx", text_ok=True)
        self.assertEqual((u["kind"], u["name"], u["detail"]), ("text", "report.xlsx", "2 sheets · 6 rows"))
        self.assertIn("1,Region,Item,Sales,Share,Date", s.upload_text(u["id"]))
        self.assertEqual(u["chars"], len(s.upload_text(u["id"])))
        small = s.add_upload(F.xlsx(long_rows=5000), "big.xlsx", text_ok=True, text_limit=30_000)
        self.assertRegex(small["detail"], r"first [\d,]+ read$")
        self.assertLess(small["chars"], 31_000)
        csv = s.add_upload("name,score\nSam,9\n\nAda,10\n".encode(), "scores.csv", text_ok=True)
        self.assertEqual(csv["detail"], "3 rows")
        win = s.add_upload("name,city\nRené,Zürich\n".encode("cp1252"), "excel-windows.csv", text_ok=True)
        self.assertEqual(s.upload_text(win["id"]), "name,city\nRené,Zürich\n")
        u16 = s.add_upload("a\tb\n1\t2\n".encode("utf-16"), "sheet.tsv", text_ok=True)
        self.assertEqual((s.upload_text(u16["id"]), u16["detail"]), ("a\tb\n1\t2\n", "2 rows"))
        self.assertEqual(s.upload_text(s.add_upload("caf\xe9".encode("cp1252"), "notes.txt", text_ok=True)["id"]), "caf\u00e9")
        old = s.add_upload(O.CFB + b"\x00" * 600, "report.xls", text_ok=True)  # nothing refused: its details, and what to do
        self.assertEqual(old["detail"], "608 bytes · content not readable")
        self.assertIn("Save it as .xlsx or .csv", s.upload_text(old["id"]))
        with self.assertRaises(ValueError):
            s.add_upload(F.xlsx(), "a.xlsx")  # Studio's references: media only


if __name__ == "__main__":
    unittest.main()
