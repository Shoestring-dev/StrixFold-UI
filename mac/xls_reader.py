"""xls_reader.py FILE LIMIT: an Excel 97-2003 workbook (.xls) read as StrixFold UI's chat reads a workbook (officetext's CSV
tables, formats as shown, merged cells, hidden sheets, the text limit), by xlrd 2.0.1: pure Python, pinned by hash in
its own venv (tools/setup_office_reader.sh), run by filetext.py as a separate process with a timeout. JSON on stdout:
{"text", "detail", "truncated"} or {"error"}. It reads cells only: no macro or formula is run (a formula's cached
value is what the file holds)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import officetext as O  # noqa: E402

import xlrd  # noqa: E402


def main(path, limit):
    book = xlrd.open_workbook(path, formatting_info=True, on_demand=True, logfile=open(os.devnull, "w"))
    fmt = lambda xf: book.format_map[book.xf_list[xf].format_key].format_str if 0 <= xf < len(book.xf_list) else "General"  # noqa: E731
    used, cut, out = 0, False, []
    for i in range(book.nsheets):
        sh = book.sheet_by_index(i)
        state = "visible" if sh.visibility == 0 else "hidden"
        rows, n_rows = {}, 0
        for r in range(sh.nrows):
            cur = {}
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                t, v = cell.ctype, cell.value
                if t in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    continue
                if t == xlrd.XL_CELL_TEXT:
                    val = v
                elif t == xlrd.XL_CELL_BOOLEAN:
                    val = "TRUE" if v else "FALSE"
                elif t == xlrd.XL_CELL_ERROR:
                    val = xlrd.error_text_from_code.get(v, "#ERR")
                else:  # a number or a date serial, as its format shows it
                    try:
                        val = O.show_number(float(v), fmt(cell.xf_index), book.datemode == 1)
                    except (ValueError, OverflowError, KeyError, IndexError, ZeroDivisionError):
                        val = O.general(float(v))
                if val != "":
                    cur[c] = val
            if not cur:
                continue
            n_rows += 1
            if not cut:
                rows[r + 1] = cur
                used += sum(len(x) for x in cur.values()) + 2 * len(cur) + 8
                cut = used > limit
        for r1, r2, c1, c2 in getattr(sh, "merged_cells", []):  # a merge down one column repeats its value
            if c2 - c1 == 1 and r2 - r1 > 1 and c1 in rows.get(r1 + 1, {}):
                for r in range(r1 + 2, r2 + 1):
                    if r in rows:
                        rows[r].setdefault(c1, rows[r1 + 1][c1])
        out.append((sh.name, state, rows, n_rows))
        book.unload_sheet(i)
    d = O.render_sheets(out, "xls")
    return {"text": d.text, "detail": d.detail, "truncated": d.truncated}


if __name__ == "__main__":
    try:
        res = main(sys.argv[1], int(sys.argv[2]))
    except Exception as e:  # noqa: BLE001 - any failure is "could not be read", never a traceback with cell data
        res = {"error": type(e).__name__}
    sys.stdout.write(json.dumps(res) + "\n")
