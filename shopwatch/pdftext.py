"""Text out of a PDF, standard library only, bounded against hostile files.

Terms and conditions are often published only as a PDF (developer.olx.bg publishes the
operator's name and ЕИК that way). The Deck has no pip, so this is a small extractor
rather than a dependency: it inflates FlateDecode streams, reads each font's ToUnicode
CMap, and decodes the strings shown by Tj / TJ / ' / " in page content streams.

Covers what office suites and Google Docs emit, including PDF 1.5 object streams. Not
covered, and reported as no text rather than guessed: encrypted PDFs, fonts without a
ToUnicode map, scanned images.
ponytail: upgrade path is pdfminer/pypdf if the host ever gets pip.
"""
from __future__ import annotations

import re
import zlib

MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_INFLATED = 32 * 1024 * 1024      # total across all streams: zip-bomb ceiling
MAX_OBJECTS = 20000

_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.DOTALL)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.DOTALL)
_REF = rb"(\d+)\s+\d+\s+R"


class PdfError(ValueError):
    pass


def _inflate(data: bytes, budget: list[int]) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(data, budget[0] + 1)
    if len(out) > budget[0]:
        raise PdfError("decompressed size limit exceeded")
    budget[0] -= len(out)
    return out


def _objects(pdf: bytes) -> dict[int, tuple[bytes, bytes | None]]:
    objs, budget = {}, [MAX_INFLATED]
    for i, m in enumerate(_OBJ.finditer(pdf)):
        if i >= MAX_OBJECTS:
            raise PdfError("too many objects")
        body = m.group(3)
        s = _STREAM.search(body)
        head, stream = (body[:s.start()], s.group(1)) if s else (body, None)
        if stream is not None and b"/FlateDecode" in head:
            try:
                stream = _inflate(stream, budget)
            except zlib.error:
                stream = None
        objs[int(m.group(1))] = (head, stream)
    # PDF 1.5 object streams: Word and others pack font dictionaries in here, so the
    # ToUnicode maps are invisible until the stream is unpacked.
    for head, stream in list(objs.values()):
        if not stream or not re.search(rb"/Type\s*/ObjStm", head):
            continue
        n = re.search(rb"/N\s+(\d+)", head)
        first = re.search(rb"/First\s+(\d+)", head)
        if not n or not first:
            continue
        nums = [int(x) for x in stream[:int(first.group(1))].split()]
        pairs = list(zip(nums[0::2], nums[1::2]))[:int(n.group(1))]
        base = int(first.group(1))
        for k, (num, off) in enumerate(pairs):
            end = base + pairs[k + 1][1] if k + 1 < len(pairs) else len(stream)
            if num not in objs and len(objs) < MAX_OBJECTS:
                objs[num] = (stream[base + off:end], None)
    return objs


def _cmap(data: bytes) -> tuple[dict[int, str], int]:
    """ToUnicode CMap -> ({code: text}, code width in bytes)."""
    table: dict[int, str] = {}
    width = 2
    m = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", data)
    if m:
        width = max(1, len(m.group(1)) // 2)

    def u(hexs: bytes) -> str:
        raw = bytes.fromhex(hexs.decode())
        return raw.decode("utf-16-be", "replace")
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.DOTALL):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            table[int(src, 16)] = u(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.DOTALL):
        for lo, hi, rest in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if hi - lo > 65535:
                continue
            if rest.startswith(b"["):
                for k, dst in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", rest)):
                    table[lo + k] = u(dst)
            else:
                start = bytes.fromhex(rest[1:-1].decode())
                base = int.from_bytes(start, "big")
                for k in range(hi - lo + 1):
                    b = (base + k).to_bytes(len(start), "big")
                    table[lo + k] = b.decode("utf-16-be", "replace")
    return table, width


def _literal(s: bytes) -> bytes:
    """Unescape a PDF literal string body (without the outer parentheses)."""
    out, i = bytearray(), 0
    esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(s):
        c = s[i]
        if c == 0x5C and i + 1 < len(s):             # backslash
            n = s[i + 1]
            if n in esc:
                out.append(esc[n]); i += 2
            elif 0x30 <= n <= 0x37:
                j = i + 1
                while j < len(s) and j < i + 4 and 0x30 <= s[j] <= 0x37:
                    j += 1
                out.append(int(s[i + 1:j], 8) & 0xFF); i = j
            elif n in (10, 13):
                i += 2
            else:
                out.append(n); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


_TOKENS = re.compile(
    rb"\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)"   # literal string (one nesting level)
    rb"|<[0-9A-Fa-f\s]*>"                             # hex string
    rb"|/[^\s/<>\[\]()]+"                            # name
    rb"|-?\d*\.?\d+"                                 # number
    rb"|[A-Za-z'\"*]+"                               # operator
    rb"|\[|\]", re.DOTALL)


def _widths(head: bytes) -> tuple[dict[int, float], float]:
    """Glyph widths (1/1000 em) from a CIDFont /W array or a simple font's /Widths."""
    w: dict[int, float] = {}
    m = re.search(rb"/W\s*\[((?:[^\[\]]|\[[^\]]*\])*)\]", head)
    if m:
        toks = re.findall(rb"\[[^\]]*\]|-?\d*\.?\d+", m.group(1))
        i = 0
        while i < len(toks) - 1 and len(w) < 70000:
            first = int(float(toks[i]))
            if toks[i + 1].startswith(b"["):
                for k, v in enumerate(re.findall(rb"-?\d*\.?\d+", toks[i + 1])):
                    w[first + k] = float(v)
                i += 2
            elif i + 2 < len(toks):
                last = int(float(toks[i + 1]))
                for c in range(first, min(last, first + 65535) + 1):
                    w[c] = float(toks[i + 2])
                i += 3
            else:
                break
    m = re.search(rb"/FirstChar\s+(\d+).*?/Widths\s*\[([^\]]*)\]", head, re.DOTALL)
    if m:
        for k, v in enumerate(re.findall(rb"-?\d*\.?\d+", m.group(2))):
            w[int(m.group(1)) + k] = float(v)
    dw = re.search(rb"/DW\s+(-?\d*\.?\d+)", head)
    return w, float(dw.group(1)) if dw else (0.0 if w else 500.0)


def _decode(raw: bytes, font) -> tuple[str, float]:
    """Text and total advance (1/1000 em) of one shown string."""
    if not font:
        return raw.decode("latin-1"), 500.0 * len(raw)
    table, width, w, dw = font
    codes = [int.from_bytes(raw[i:i + width], "big")
             for i in range(0, len(raw) - width + 1, width)]
    return "".join(table.get(c, "") for c in codes), sum(w.get(c, dw) for c in codes)


def _run(content: bytes, fonts: dict) -> str:
    """Decode one content stream, rebuilding lines and word gaps from glyph positions.

    Generators disagree about spaces: Google Docs emits a space glyph, Word omits it and
    just moves the pen. So spaces come from geometry - a gap wider than a quarter em
    between where the last glyph ended and where the next starts - and line breaks from
    a change of baseline. Breaking on BT/ET or Td instead turns "ЕИК" into "Е И К".
    """
    out: list[str] = []
    font = None
    stack: list = []
    size, scale = 1.0, 1.0
    y, last_y = 0.0, None
    line_x = x = 0.0
    target: float | None = None

    def emit(text: str, units: float) -> None:
        nonlocal last_y, x, target
        if last_y is not None and abs(y - last_y) > 1:
            out.append("\n")
        elif target is not None and out and target - x > 0.25 * size * scale \
                and not out[-1][-1:].isspace() and not text[:1].isspace():
            out.append(" ")
        if target is not None:
            x, target = target, None
        last_y = y
        out.append(text)
        x += units / 1000 * size * scale

    for tok in _TOKENS.findall(content):
        if tok.startswith(b"("):
            stack.append(("s", _literal(tok[1:-1])))
        elif tok.startswith(b"<"):
            h = re.sub(rb"\s", b"", tok[1:-1])
            stack.append(("s", bytes.fromhex((h + b"0" * (len(h) % 2)).decode())))
        elif tok.startswith(b"/"):
            stack.append(("n", tok[1:]))
        elif tok == b"[":
            stack.append(("[", None))
        elif tok == b"]":
            arr = []
            while stack and stack[-1][0] != "[":
                arr.append(stack.pop())
            if stack:
                stack.pop()
            stack.append(("a", arr[::-1]))
        elif re.fullmatch(rb"-?\d*\.?\d+", tok):
            stack.append(("d", float(tok)))
        else:                                          # operator
            nums = [v for k, v in stack if k == "d"]
            if tok == b"Tf":
                names = [v for k, v in stack if k == "n"]
                font = fonts.get(names[-1]) if names else None
                size = abs(nums[-1]) if nums else size
            elif tok in (b"Tj", b"'", b'"'):
                strs = [v for k, v in stack if k == "s"]
                if tok != b"Tj":
                    y += 1000                                # next line
                    target = line_x
                if strs:
                    emit(*_decode(strs[-1], font))
            elif tok == b"TJ":
                arrs = [v for k, v in stack if k == "a"]
                for k, v in (arrs[-1] if arrs else []):
                    if k == "s":
                        emit(*_decode(v, font))
                    elif k == "d":                           # kerning: moves the pen
                        target = (x if target is None else target) - v / 1000 * size * scale
            elif tok == b"T*":
                y += 1000
                target = line_x
            elif tok in (b"Td", b"TD") and len(nums) >= 2:
                line_x += nums[-2] * scale
                y += nums[-1]
                target = line_x
            elif tok == b"Tm" and len(nums) >= 6:
                scale = abs(nums[-6]) or 1.0
                line_x, y = nums[-2], nums[-1]
                target = line_x
            elif tok == b"BT":
                line_x, target = 0.0, 0.0
            stack = []
    return "".join(out)


def extract_text(pdf: bytes) -> str:
    """Best-effort text of every page. Raises PdfError on a file it will not parse."""
    if not pdf.startswith(b"%PDF"):
        raise PdfError("not a PDF")
    if len(pdf) > MAX_PDF_BYTES:
        raise PdfError("PDF too large")
    if b"/Encrypt" in pdf[-4096:] or re.search(rb"/Encrypt\s+\d+\s+\d+\s+R", pdf):
        raise PdfError("encrypted PDF")
    objs = _objects(pdf)

    def resolve(ref: bytes | None) -> bytes:
        return objs.get(int(ref), (b"", None))[0] if ref else b""

    cmaps: dict[int, tuple] = {}
    texts = []
    for num, (head, _) in objs.items():
        if not re.search(rb"/Type\s*/Page\b", head):
            continue
        res = head
        m = re.search(rb"/Resources\s+" + _REF, head)
        if m:
            res = resolve(m.group(1))
        fdict = re.search(rb"/Font\s*<<(.*?)>>", res, re.DOTALL)
        if not fdict:
            m = re.search(rb"/Font\s+" + _REF, res)
            fdict = re.search(rb"<<(.*)>>", resolve(m.group(1)), re.DOTALL) if m else None
        fonts = {}
        for name, ref in re.findall(rb"/([^\s/<>]+)\s+" + _REF, fdict.group(1) if fdict else b""):
            fhead = resolve(ref)
            tu = re.search(rb"/ToUnicode\s+" + _REF, fhead)
            if tu:
                key = int(tu.group(1))
                if key not in cmaps and objs.get(key, (b"", None))[1]:
                    cmaps[key] = _cmap(objs[key][1])
                if cmaps.get(key):
                    desc = re.search(rb"/DescendantFonts\s*\[\s*" + _REF, fhead)
                    w, dw = _widths(resolve(desc.group(1)) if desc else fhead)
                    fonts[name] = (*cmaps[key], w, dw)
        refs = []
        c = re.search(rb"/Contents\s*(\[[^\]]*\]|" + _REF + rb")", head)
        if c:
            refs = re.findall(_REF, c.group(1))
        content = b"\n".join(objs.get(int(r), (b"", None))[1] or b"" for r in refs)
        texts.append(_run(content, fonts))
    return re.sub(r"[ \t]+", " ", "\n".join(texts)).strip()


if __name__ == "__main__":                            # self-check on a tiny hand-made PDF
    cm = (b"/CIDInit /ProcSet findresource begin begincmap 1 begincodespacerange <0000> <FFFF>"
          b" endcodespacerange 2 beginbfchar <0001> <0415> <0002> <0418> endbfchar"
          b" 1 beginbfrange <0003> <0003> <041A> endbfrange endcmap")
    content = b"BT /F1 12 Tf [<0001><0002>(\\000\\003)] TJ ET"
    doc = (b"%PDF-1.4\n1 0 obj << /Type /Page /Resources << /Font << /F1 2 0 R >> >>"
           b" /Contents 4 0 R >> endobj\n2 0 obj << /Type /Font /ToUnicode 3 0 R >> endobj\n"
           b"3 0 obj << /Length 1 >> stream\n" + cm + b"\nendstream endobj\n"
           b"4 0 obj << /Length 1 /Filter /FlateDecode >> stream\n" + zlib.compress(content)
           + b"\nendstream endobj\n")
    assert extract_text(doc) == "ЕИК", repr(extract_text(doc))
    print("pdftext self-check ok")
