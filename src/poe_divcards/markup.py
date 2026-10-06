"""Parser for the markup used in Path of Exile item texts.

Card rewards and flavour texts are stored with inline styles::

    <uniqueitem>{Headhunter}\r\n<corrupted>{Corrupted}
    <gemitem>{Minion Gem}\r\n<default>{Quality:} <augmented>{+20%}
    <size:31>{A wolf does not bite his mate as he does his prey...}

``<tag>{content}`` blocks can be nested. They are flattened into lines of
segments, each with the innermost style and the active font size, which a web
client can render directly (one ``<span class="style">`` per segment).

``<<name>>`` is an inline image drawn by the game, such as the Harbinger-script
glyphs of The Messenger's flavour text (``<<HBG04>><<HBGAt>>``): it becomes a
segment with ``glyph`` set and empty text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Spaces between the tag and its brace occur in game texts ("<brequelmutated> {Foulborn}").
_TAG = re.compile(r"<([^<>{}]+)>[ \t]*\{")
_GLYPH = re.compile(r"<<([A-Za-z0-9_]+)>>")


@dataclass(slots=True)
class Segment:
    text: str
    style: str | None = None
    size: int | None = None
    glyph: str | None = None

    def to_json(self) -> dict:
        out: dict = {"text": self.text}
        if self.style:
            out["style"] = self.style
        if self.size:
            out["size"] = self.size
        if self.glyph:
            out["glyph"] = self.glyph
        return out


def parse(text: str) -> list[list[Segment]]:
    """Split ``text`` into lines of styled segments.

    Unbalanced markup is kept as plain text instead of raising: the strings come
    from the game and a malformed one must not break a whole build.
    """
    lines: list[list[Segment]] = [[]]

    def emit(chunk: str, style: str | None, size: int | None) -> None:
        parts = chunk.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for n, part in enumerate(parts):
            if n:
                lines.append([])
            if not part:
                continue
            line = lines[-1]
            if line and line[-1].glyph is None and line[-1].style == style and line[-1].size == size:
                line[-1].text += part
            else:
                line.append(Segment(part, style, size))

    def walk(pos: int, style: str | None, size: int | None, nested: bool) -> int:
        """Consume text from ``pos`` until the closing brace of the current block."""
        start = pos
        while pos < len(text):
            ch = text[pos]
            if ch == "<" and (g := _GLYPH.match(text, pos)):
                emit(text[start:pos], style, size)
                lines[-1].append(Segment("", style, size, glyph=g.group(1)))
                pos = start = g.end()
                continue
            if ch == "<":
                m = _TAG.match(text, pos)
                if m:
                    emit(text[start:pos], style, size)
                    tag = m.group(1).strip()
                    if tag.lower().startswith("size:"):
                        try:
                            inner_style, inner_size = style, int(tag[5:])
                        except ValueError:
                            inner_style, inner_size = style, size
                    else:
                        inner_style, inner_size = tag.lower(), size
                    pos = walk(m.end(), inner_style, inner_size, True)
                    start = pos
                    continue
            elif ch == "}" and nested:
                emit(text[start:pos], style, size)
                return pos + 1
            pos += 1
        emit(text[start:pos], style, size)
        return pos

    walk(0, None, None, False)
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines


def to_plain(text: str) -> str:
    """Text without markup, lines joined with ``\\n``."""
    return "\n".join("".join(s.text for s in line) for line in parse(text))


def to_json(lines: list[list[Segment]]) -> list[list[dict]]:
    return [[s.to_json() for s in line] for line in lines]
