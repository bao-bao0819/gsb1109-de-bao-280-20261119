from os import path
from beangulp import mimetypes
from beangulp.importers import csvbase
from beangulp.testing import main


class Importer(csvbase.Importer):
    date = csvbase.Date("Posting Date", "%m/%d/%Y")
    narration = csvbase.Columns("Description", "Check or Slip #", sep="; ")
    amount = csvbase.Amount("Amount")
    balance = csvbase.Amount("Balance")

    # Statements whose dates carry only a month and a day can be given
    # the period they cover, so the year is derived from the statement
    # rather than from the date the file happens to be imported. Rows
    # whose dates do not fit the period are reported and skipped.
    # period = ("2016-03-01", "2016-03-31")

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != "text/csv":
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return head.startswith(
            'Details,Posting Date,"Description",Amount,Type,Balance,Check or Slip #,'
        )

    def filename(self, filepath):
        return "csvbank." + path.basename(filepath)


if __name__ == "__main__":
    main(Importer("Assets:US:CSVBank", "USD"))
