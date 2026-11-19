"""PDF generado por el adaptador a partir de texto con formato (markdown).

El agente envía el contenido como texto y el bot arma el PDF con una
plantilla fija, para que el formato sea siempre el mismo. La plantilla actual
es provisional (encabezado con título, agente y fecha; pie con páginas) y se
reemplazará por el «formato acordado» cuando se defina.

Las fuentes base de PDF cubren el español (acentos, ñ, ¿, ¡); los caracteres
fuera de Latin-1 (emojis, símbolos) se sustituyen.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import markdown
from fpdf import FPDF, FontFace, TextStyle

MAX_CONTENT_CHARS = 200_000
TZ = ZoneInfo("America/Santiago")
BRAND = (0, 61, 121)  # azul corporativo provisional

_REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    "•": "-", "…": "...", " ": " ", "→": "->", "←": "<-", "✓": "v",
}


class PdfContentError(ValueError):
    pass


def to_latin1(text: str) -> str:
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "ignore").decode("latin-1")


def safe_filename(title: str) -> str:
    base = re.sub(r"[^A-Za-z0-9áéíóúÁÉÍÓÚñÑ _-]+", "", title).strip().replace(" ", "_")[:80]
    return (base or "documento") + ".pdf"


class _Template(FPDF):
    def __init__(self, title: str, subtitle: str):
        super().__init__(format="A4")
        self._title = title
        self._subtitle = subtitle
        self.set_margins(18, 30, 18)
        self.set_auto_page_break(True, margin=20)

    def header(self):
        self.set_fill_color(*BRAND)
        self.rect(0, 0, self.w, 20, style="F")
        self.set_xy(18, 5)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 13)
        self.cell(0, 6, self._title, new_x="LMARGIN", new_y="NEXT")
        self.set_x(18)
        self.set_font("Helvetica", "", 8)
        self.cell(0, 5, self._subtitle)
        self.set_text_color(0, 0, 0)
        self.set_y(28)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, f"Agentes ICP · Página {self.page_no()} de {{nb}}", align="C")


def render_pdf(title: str, content_md: str, agent_name: str, now: datetime | None = None) -> bytes:
    title = (title or "").strip()[:150]
    if not title:
        raise PdfContentError("falta el título del documento")
    if not isinstance(content_md, str) or not content_md.strip():
        raise PdfContentError("falta el contenido del documento")
    if len(content_md) > MAX_CONTENT_CHARS:
        raise PdfContentError(f"el contenido supera {MAX_CONTENT_CHARS} caracteres")
    now = (now or datetime.now(TZ)).astimezone(TZ)
    subtitle = f"{agent_name} · {now:%d-%m-%Y %H:%M} (hora de Chile)"
    pdf = _Template(to_latin1(title), to_latin1(subtitle))
    pdf.set_title(to_latin1(title))
    pdf.set_author("Agentes ICP")
    pdf.add_page()
    for kind, block in _split_tables(to_latin1(content_md)):
        if kind == "table":
            _table(pdf, block)
        elif block.strip():
            html = markdown.markdown(block, extensions=["sane_lists"])
            # Las imágenes externas no se descargan.
            html = re.sub(r"<img[^>]*>", "", html)
            pdf.set_font("Helvetica", "", 10.5)
            pdf.write_html(html, font_family="helvetica", tag_styles=_STYLES,
                           li_prefix_color=BRAND, ul_bullet_char="-")
    return bytes(pdf.output())


_STYLES = {
    "h1": TextStyle(font_family="helvetica", font_style="B", font_size_pt=16, color=BRAND, t_margin=4, b_margin=2),
    "h2": TextStyle(font_family="helvetica", font_style="B", font_size_pt=13, color=BRAND, t_margin=4, b_margin=1.5),
    "h3": TextStyle(font_family="helvetica", font_style="B", font_size_pt=11.5, color=(60, 60, 60), t_margin=3, b_margin=1),
    "p": TextStyle(font_family="helvetica", font_size_pt=10.5, t_margin=1, b_margin=1.5),
    "li": TextStyle(font_family="helvetica", font_size_pt=10.5, l_margin=6, t_margin=0.6),
    "a": FontFace(color=BRAND, emphasis="UNDERLINE"),
}
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_tables(text: str) -> list[tuple[str, list[str] | str]]:
    """Separa las tablas markdown (filas | a | b |) del resto del texto."""
    blocks: list[tuple[str, list[str] | str]] = []
    lines = text.splitlines()
    i, buffer = 0, []
    while i < len(lines):
        if _TABLE_ROW.match(lines[i]) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            if buffer:
                blocks.append(("text", "\n".join(buffer)))
                buffer = []
            rows = [lines[i]]
            i += 2
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(lines[i])
                i += 1
            blocks.append(("table", rows))
            continue
        buffer.append(lines[i])
        i += 1
    if buffer:
        blocks.append(("text", "\n".join(buffer)))
    return blocks


def _cells(row: str) -> list[str]:
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    return [re.sub(r"\*\*|__|`", "", c) for c in cells]


def _table(pdf: FPDF, rows: list[str]) -> None:
    data = [_cells(r) for r in rows]
    width = max(len(r) for r in data)
    data = [r + [""] * (width - len(r)) for r in data]
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9.5)
    with pdf.table(
        headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=BRAND),
        line_height=5.5, padding=1.5, cell_fill_color=(240, 244, 250), cell_fill_mode="ROWS",
        borders_layout="HORIZONTAL_LINES", text_align="LEFT",
    ) as table:
        for r in data:
            row = table.row()
            for c in r:
                row.cell(c)
    pdf.ln(3)
