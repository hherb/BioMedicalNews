"""bmlib's Markdown subset, and the two shapes bmnews renders it in.

Since bmlib 0.8.0 the PubMed fetcher stores titles and abstracts as Markdown:
sections as ``**LABEL:** text``, inline markup as ``**strong**`` / ``*em*`` /
``~sub~`` / ``^sup^``, and prose taken from the document backslash-escaped over
``\\ ` * ~ ^``.

**No other fetcher does.** medRxiv, bioRxiv, Europe PMC and OpenAlex store
prose exactly as the source sent it, unescaped, so a converter pointed at them
does not find markup — it invents it. ``CYP2C19*2 and CYP2C19*17`` pairs into
``CYP2C19<em>2 and CYP2C19</em>17``, ``HLA-B*57:01`` and ``5'-UTR``/``3'-UTR``
likewise, and ``4.2∙10^4∙10^(4.32R_S/r)`` loses an exponent. Star alleles, HLA
types, algorithm names and scientific notation all hug non-whitespace on both
sides, which is exactly the condition a delimiter must meet to pair, so the
flanking rule below does not save them. Measured against a 4,214-abstract
corpus, converting unconditionally changed 18 abstracts, every one of them for
the worse.

That is why nothing here converts without being told the paper's sources.
:func:`is_markdown_source` is the gate, and it is not optional decoration: it
is the only thing that makes "bmlib guarantees the flanking rule" a true
statement about the input rather than an aspiration.

This module is deliberately free of any GUI or database import — both layers
need it, and the knowledge of what bmlib emits belongs to neither.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Sources whose stored prose is bmlib Markdown rather than raw source text.
MARKDOWN_SOURCES = frozenset({"pubmed"})

# Prose the document itself carried, escaped by bmlib so it cannot be read as
# markup — the star alleles in ``CYP2C19 (\*1, \*2)``, an ``AUC \~ 0.80``.
# These are held aside before any marker matching, or the conversion pairs
# exactly the characters the escaping exists to protect.
_ESCAPED = re.compile(r"\\([\\`*~^])")

# Emphasis delimiters must hug non-whitespace. That is CommonMark's flanking
# rule, and bmlib's emitter guarantees it — ``_walk_formatting`` moves a run's
# edge whitespace *outside* the markers for this reason, so ``**Randomised **``
# never reaches us.
#
# The rule narrows the damage a stray marker can do; it does not remove it, and
# it is not what keeps non-Markdown sources safe. Only the source gate is. A
# spaced ``~50 to ~60`` cannot pair, but ``~50-~60`` can, and ``significant*,``
# followed by a later ``effect*`` pairs across the whole clause between them.
#
# Sub/sup go further and forbid inner whitespace: bmlib does emit a multi-word
# run (``<sub>1c adjusted</sub>`` becomes ``~1c adjusted~``), but pairing across
# a space costs far more on the sources that are not Markdown than the rare
# phrase-length subscript is worth, so those render with their markers showing.
#
# Ordering is load-bearing twice over. ``***x***`` must match before ``**x**``,
# or the strong rule takes the first two asterisks and the emphasis rule the
# third, yielding crossed ``<strong><em>x</strong></em>``. And ``**x**`` must
# match before ``*x*``, or the emphasis rule eats one asterisk of each pair and
# leaves the other stranded as ``<em>*x</em>*``.
_STRONG_EM = re.compile(r"\*\*\*(?!\s)(.+?)(?<!\s)\*\*\*")
_STRONG = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_EM = re.compile(r"\*(?!\s)(.+?)(?<!\s)\*")
_SUB = re.compile(r"~([^\s~]+)~")
_SUP = re.compile(r"\^([^\s^]+)\^")

_HTML_RULES = (
    (_STRONG_EM, r"<strong><em>\1</em></strong>"),
    (_STRONG, r"<strong>\1</strong>"),
    (_EM, r"<em>\1</em>"),
    (_SUB, r"<sub>\1</sub>"),
    (_SUP, r"<sup>\1</sup>"),
)

# The flat forms, for the surfaces that cannot carry a tag — the plain-text
# digest, both plain-text notification bodies, and any title, which is rendered
# as text everywhere bmnews shows one.
#
# Subscript drops its markers because the conventional inline form of ``CO~2~``
# is ``CO2``. Superscript *keeps* its caret because dropping it would turn
# ``10^6^`` into ``106`` — a different number, which is worse than a marker on
# screen.
_TEXT_RULES = (
    (_STRONG_EM, r"\1"),
    (_STRONG, r"\1"),
    (_EM, r"\1"),
    (_SUB, r"\1"),
    (_SUP, r"^\1"),
)


def is_markdown_source(sources: Iterable[str] | None) -> bool:
    """Whether a paper's stored prose is bmlib Markdown.

    A publication merged from several sources lists them all, and bmlib's merge
    keeps whichever abstract was stored first, so a paper fed by both PubMed and
    medRxiv cannot be told apart per field. This answers True for it anyway, on
    the cheaper error: an unconverted PubMed abstract shows the reader literal
    backslashes on every line it escaped, whereas converting a medRxiv one only
    risks a marker pairing that the flanking rule usually prevents.

    Args:
        sources: The paper's ``sources`` list, or None when the query did not
            select it.

    Returns:
        True when at least one source stores Markdown.
    """
    if not sources:
        return False
    return any(str(source).lower() in MARKDOWN_SOURCES for source in sources)


def _convert(text: str, rules: tuple[tuple[re.Pattern[str], str], ...]) -> str:
    """Apply one rule set, with bmlib's backslash escapes held aside.

    NUL is the sentinel the held values go behind, so any already in the text is
    dropped first: it is not displayable either way, and left in place a source
    carrying one — JSON permits ``\\u0000``, unlike the XML PubMed arrives as —
    could be read back as a sentinel and index out of the list.
    """
    text = text.replace("\x00", "")
    held: list[str] = []

    def _hold(match: re.Match) -> str:
        held.append(match.group(1))
        return f"\x00{len(held) - 1}\x00"

    text = _ESCAPED.sub(_hold, text)
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)


def markdown_to_html(text: str) -> str:
    """Convert bmlib's Markdown subset to the tags the reading pane speaks.

    Call only on text that is already HTML-escaped, and only for a paper
    :func:`is_markdown_source` accepts. ``escape()`` leaves ``\\ ` * ~ ^``
    alone, so the markers survive it intact and the tags emitted here are the
    only unescaped HTML this function introduces.

    Args:
        text: HTML-escaped Markdown.

    Returns:
        The same text with the marker set replaced by ``<strong>``, ``<em>``,
        ``<sub>`` and ``<sup>``, and bmlib's backslash escapes resolved.
    """
    return _convert(text, _HTML_RULES)


def markdown_to_text(text: str) -> str:
    """Flatten bmlib's Markdown subset for a surface that cannot carry tags.

    Call only for a paper :func:`is_markdown_source` accepts.

    Args:
        text: Markdown, not HTML-escaped.

    Returns:
        The text with emphasis markers removed, ``~sub~`` flattened, ``^sup^``
        reduced to a single caret, and bmlib's backslash escapes resolved.
    """
    return _convert(text, _TEXT_RULES)
