"""Formato de texto para Teams (Markdown: un salto simple se ve como espacio)."""

from teams_people import teams_text


def test_bullets_on_their_own_lines_become_a_markdown_list():
    text = "Vista previa | Noticias del 19\n• Coquimbo: crecidas. [Leer fuente](https://a)\n• Mundo: CIJ.\nSelección limitada."
    assert teams_text(text) == (
        "Vista previa | Noticias del 19\n\n- Coquimbo: crecidas. [Leer fuente](https://a)\n- Mundo: CIJ.\n\nSelección limitada."
    )


def test_inline_bullets_are_split():
    assert teams_text("Titular • Chile: a • Mundo: b") == "Titular\n\n- Chile: a\n- Mundo: b"


def test_plain_lines_become_paragraphs_and_lists_are_kept():
    assert teams_text("Hola\nlínea dos\r\n\r\n\r\n- uno\n- dos\n1. tres") == "Hola\n\nlínea dos\n\n- uno\n- dos\n1. tres"
    assert teams_text("") == "" and teams_text("solo") == "solo"


def test_mentions_and_links_are_untouched():
    text = "Aviso para <at>Ana Díaz</at> en [el informe](https://x.cl/a-b)."
    assert teams_text(text) == text
