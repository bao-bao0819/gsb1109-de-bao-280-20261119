#!/usr/bin/env python3
"""Build the local minimal mixed-PDF counterexample.

Run with the system Python that ships Pillow (/usr/local/bin/python3):

    /usr/local/bin/python3 casework/tools/make_pdf.py

The output casework/pdf/mixed_statement.pdf has four pages that isolate
each way an amount can disappear:

  page 1  full-page raster image, no text layer
          amount 1,250.00 (identity TRANSFER OUT 7110) exists only as pixels
  page 2  full-page raster image plus a native header-only text layer
          amount 1,250.00 (identity TRANSFER IN 7111) exists only as pixels;
          the text layer passes the >=20 alnum char "has text" test but
          contains no amount at all
  page 3  native digital text, no image, amount 1,250.00 (identity
          ATM FEE 7112) is fully extractable but the row layout violates
          the statement plugin's column assumptions, so the rule rejects it
  page 4  native digital text whose rows match the legal template
          (different amounts) -- positive control proving the plugin emits
          entries when its assumptions hold

The number 1,250.00 therefore appears three times with three different
transaction identities; only one occurrence is in a text layer.
"""

import io
import pathlib

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "pdf" / "mixed_statement.pdf"

PAGE_W, PAGE_H = 612, 792


def page_image(lines):
    image = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(image)
    y = 60
    for line in lines:
        draw.text((54, y), line, fill="black")
        y += 34
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def escape(text):
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def text_stream(lines, font_size=12, top=PAGE_H - 72, leading=18):
    tokens = ["BT", f"/F1 {font_size} Tf", f"72 {top} Td"]
    for index, line in enumerate(lines):
        if index:
            tokens.append(f"0 -{leading} Td")
        tokens.append(f"({escape(line)}) Tj")
    tokens.append("ET")
    return ("\n".join(tokens)).encode("latin-1")


class PdfBuilder:
    def __init__(self):
        self.objects = []

    def add(self, body):
        self.objects.append(body)
        return len(self.objects)

    def write(self, path):
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, body in enumerate(self.objects, start=1):
            offsets.append(len(out))
            out.extend(f"{number} 0 obj\n".encode())
            out.extend(body)
            out.extend(b"\nendobj\n")
        xref = len(out)
        out.extend(f"xref\n0 {len(self.objects) + 1}\n".encode())
        out.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            out.extend(f"{offset:010d} 00000 n \n".encode())
        out.extend(
            f"trailer\n<< /Size {len(self.objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n".encode()
        )
        path.write_bytes(out)


def jpeg_object(builder, jpeg, width, height):
    body = (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
        f"/Length {len(jpeg)} >>\nstream\n"
    ).encode() + jpeg + b"\nendstream"
    return builder.add(body)


def stream_object(builder, data):
    return builder.add(
        b"<< /Length " + str(len(data)).encode() + b" >>\nstream\n"
        + data + b"\nendstream"
    )


def main():
    builder = PdfBuilder()
    catalog = builder.add(b"")          # 1, patched later
    pages = builder.add(b"")            # 2, patched later
    font = builder.add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )

    page_specs = []

    # Page 1: full-page image, no text layer.
    jpeg1 = page_image([
        "ACME Bank",
        "Statement page 1 (scanned sheet, no text layer)",
        "",
        "Posting Ref   Description              Amount",
        "7110          TRANSFER OUT             1,250.00",
        "",
        "The amount above exists only in the raster image.",
    ])
    image1 = jpeg_object(builder, jpeg1, PAGE_W, PAGE_H)
    content1 = stream_object(builder, b"q 612 0 0 792 0 0 cm /Im0 Do Q\n")
    page1 = builder.add(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
        f"/Resources << /XObject << /Im0 {image1} 0 R >> >> "
        f"/Contents {content1} 0 R >>".encode()
    )
    page_specs.append(page1)

    # Page 2: full-page image plus header-only native text.
    jpeg2 = page_image([
        "ACME Bank",
        "Statement page 2 (scan with OCR-style header text layer)",
        "",
        "Posting Ref   Description              Amount",
        "7111          TRANSFER IN              1,250.00",
        "",
        "The amount above exists only in the raster image;",
        "the native text layer carries the header line only.",
    ])
    image2 = jpeg_object(builder, jpeg2, PAGE_W, PAGE_H)
    header = text_stream([
        "ACME Bank Statement Page Two Header Only",
    ], font_size=14, top=PAGE_H - 42, leading=20)
    content2 = stream_object(
        builder,
        b"q 612 0 0 792 0 0 cm /Im0 Do Q\n" + header + b"\n",
    )
    page2 = builder.add(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
        f"/Resources << /Font << /F1 {font} 0 R >> "
        f"/XObject << /Im0 {image2} 0 R >> >> "
        f"/Contents {content2} 0 R >>".encode()
    )
    page_specs.append(page2)

    # Page 3: native text, amount present but columns do not match the rule.
    body3 = text_stream([
        "ACME Bank",
        "Statement page 3 (digital text, shifted column layout)",
        "",
        "Date       Amount    Description",
        "2026-03-04 1,250.00  ATM FEE 7112",
        "",
        "The amount token is extractable but the row order is",
        "ISO-date then amount then description: rejected by the",
        "plugin, which requires MM/DD/YYYY, description, amount-last.",
    ], font_size=12, leading=20)
    content3 = stream_object(builder, body3 + b"\n")
    page3 = builder.add(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
        f"/Resources << /Font << /F1 {font} 0 R >> >> "
        f"/Contents {content3} 0 R >>".encode()
    )
    page_specs.append(page3)

    # Page 4: native text matching the legal template (positive control).
    body4 = text_stream([
        "ACME Bank",
        "Statement page 4 (digital text, legal column layout)",
        "",
        "Date       Description              Amount",
        "03/05/2026 COFFEE SHOP 7113         48.20",
        "03/06/2026 OFFICE SUPPLIES 7114     -130.75",
    ], font_size=12, leading=20)
    content4 = stream_object(builder, body4 + b"\n")
    page4 = builder.add(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
        f"/Resources << /Font << /F1 {font} 0 R >> >> "
        f"/Contents {content4} 0 R >>".encode()
    )
    page_specs.append(page4)

    kids = " ".join(f"{number} 0 R" for number in page_specs)
    builder.objects[catalog - 1] = (
        f"<< /Type /Catalog /Pages 2 0 R >>".encode()
    )
    builder.objects[pages - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_specs)} >>".encode()
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    builder.write(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
