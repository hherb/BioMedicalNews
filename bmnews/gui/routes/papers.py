"""Paper list and reading pane routes."""

from __future__ import annotations

import logging

from bmlib.fulltext import FullTextError, FullTextService
from bmlib.fulltext.models import FullTextSourceEntry
from flask import Blueprint, abort, current_app, render_template, request
from markupsafe import escape

from bmnews.constants import DEFAULT_CONTACT_EMAIL, DEFAULT_PAGE_SIZE
from bmnews.db.operations import (
    get_paper_with_score,
    get_papers_filtered,
    save_fulltext,
)
from bmnews.metadata import parse_metadata

logger = logging.getLogger(__name__)

papers_bp = Blueprint("papers", __name__)


@papers_bp.route("/papers")
def paper_list() -> str:
    """Render the first page of the paper list, honouring sort and filters.

    Returns:
        The ``paper_list`` HTMX fragment.
    """
    conn = current_app.config["BMNEWS_DB"]
    sort = request.args.get("sort", "date")
    source = request.args.get("source", "")
    tier = request.args.get("tier", "")
    design = request.args.get("design", "")
    search_term = request.args.get("q", "").strip()

    papers, total = get_papers_filtered(
        conn, sort=sort, source=source, quality_tier=tier,
        study_design=design, search=search_term,
        limit=DEFAULT_PAGE_SIZE, offset=0, with_total=True,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=total, offset=0, limit=DEFAULT_PAGE_SIZE,
        sort=sort, source=source, tier=tier, design=design,
        search=search_term,
    )


@papers_bp.route("/papers/more")
def paper_list_more() -> str:
    """Render a further page of the paper list for infinite scrolling.

    Returns:
        The ``paper_list`` HTMX fragment in append mode.
    """
    conn = current_app.config["BMNEWS_DB"]
    sort = request.args.get("sort", "date")
    source = request.args.get("source", "")
    tier = request.args.get("tier", "")
    design = request.args.get("design", "")
    search_term = request.args.get("q", "").strip()
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", DEFAULT_PAGE_SIZE, type=int)

    # Clamp so a hand-edited URL cannot ask for a negative offset or an
    # unbounded page.
    offset = max(0, offset)
    limit = max(1, min(limit, DEFAULT_PAGE_SIZE * 5))

    papers = get_papers_filtered(
        conn, sort=sort, source=source, quality_tier=tier,
        study_design=design, search=search_term, limit=limit, offset=offset,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=None, offset=offset, limit=limit,
        sort=sort, source=source, tier=tier, design=design,
        search=search_term, append=True,
    )


@papers_bp.route("/papers/<int:paper_id>")
def paper_detail(paper_id: int) -> str:
    """Render the reading pane for a single paper.

    Args:
        paper_id: Id of the paper to display.

    Returns:
        The ``reading_pane`` HTMX fragment.

    Raises:
        werkzeug.exceptions.NotFound: If no such paper exists.
    """
    conn = current_app.config["BMNEWS_DB"]
    paper = get_paper_with_score(conn, paper_id)
    if paper is None:
        abort(404)
    _enrich_paper_metadata(paper)
    return render_template("fragments/reading_pane.html", paper=paper)


@papers_bp.route("/search")
def search() -> str:
    """Render papers matching a keyword search of titles and abstracts.

    Falls back to the unfiltered list when the query is blank.

    Returns:
        The ``paper_list`` HTMX fragment.
    """
    conn = current_app.config["BMNEWS_DB"]
    q = request.args.get("q", "").strip()
    if not q:
        return paper_list()

    papers, total = get_papers_filtered(
        conn, search=q, limit=DEFAULT_PAGE_SIZE, offset=0, with_total=True,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=total, offset=0, limit=DEFAULT_PAGE_SIZE,
        sort="combined", source="", tier="", design="", search=q,
    )


# Marker stored in ``papers.fulltext_source`` when the ``fulltext_html``
# column holds a link or path rather than actual HTML.
_LINK_SOURCES = {
    "pdf_cached": (
        "fulltext-pdf", "PDF cached locally:", "Open PDF &#x2197;", True,
    ),
    "unpaywall_pdf": (
        "fulltext-pdf", "PDF available from open-access source:",
        "Open PDF &#x2197;", False,
    ),
    "publisher_url": (
        "fulltext-external", "Full text available at publisher website:",
        "Open Publisher Page &#x2197;", False,
    ),
}

_UNAVAILABLE_HTML = (
    '<div class="fulltext-unavailable">'
    "<p>Full text is not available for this paper.</p></div>"
)


def _link_fragment(source: str, target: str) -> str:
    """Build the HTML fragment linking out to a PDF or publisher page.

    Args:
        source: One of the keys of :data:`_LINK_SOURCES`.
        target: URL, or filesystem path when *source* is ``pdf_cached``.

    Returns:
        An HTML fragment with *target* escaped for safe attribute use.
    """
    css_class, caption, label, is_path = _LINK_SOURCES[source]
    href = f"file://{target}" if is_path else target
    return (
        f'<div class="{css_class}">'
        f"<p>{caption}</p>"
        f'<a href="{escape(href)}" target="_blank" '
        f'class="btn btn-primary">{label}</a></div>'
    )


@papers_bp.route("/papers/<int:paper_id>/fulltext", methods=["POST"])
def paper_fulltext(paper_id: int) -> str:
    """Fetch, cache and display the full text for a paper.

    Returns cached content when the paper already has full text stored,
    otherwise resolves it through bmlib's Europe PMC → Unpaywall → DOI chain
    and persists whatever was found.

    Args:
        paper_id: Id of the paper to retrieve full text for.

    Returns:
        An HTML fragment: inline full text, an outbound link, or a message
        saying nothing is available.

    Raises:
        werkzeug.exceptions.NotFound: If no such paper exists.
    """
    conn = current_app.config["BMNEWS_DB"]
    paper = get_paper_with_score(conn, paper_id)
    if paper is None:
        abort(404)

    # Check if already cached in DB
    if paper.get("fulltext_html"):
        source = paper.get("fulltext_source", "")
        if source in _LINK_SOURCES:
            return _link_fragment(source, paper["fulltext_html"])
        return render_template("fragments/fulltext_content.html", paper=paper)

    # Parse metadata for identifiers and source-provided fulltext URLs.
    # Bad JSON in the DB must not turn into a 500.
    meta = parse_metadata(paper.get("metadata_json"))
    pmc_id = paper.get("pmcid") or meta.get("pmcid", "")
    doi = paper.get("doi") or ""
    pmid = paper.get("pmid") or meta.get("pmid", "")

    raw_sources = meta.get("fulltext_sources") or []
    sources = []
    for entry in raw_sources:
        try:
            sources.append(FullTextSourceEntry.from_dict(entry))
        except (TypeError, ValueError, KeyError):
            logger.debug("Skipping malformed fulltext source: %r", entry)

    email = current_app.config.get("BMNEWS_EMAIL") or DEFAULT_CONTACT_EMAIL
    service = FullTextService(email=email)
    try:
        result = service.fetch_fulltext(
            fulltext_sources=sources or None,
            pmc_id=pmc_id or None, doi=doi or None, pmid=pmid,
            identifier=doi or None,
        )
    except FullTextError:
        return _UNAVAILABLE_HTML
    except Exception:
        # A network blip or an unexpected upstream shape should degrade to
        # "unavailable" rather than showing the user a traceback page.
        logger.exception("Unexpected error retrieving fulltext for paper %s", paper_id)
        return _UNAVAILABLE_HTML

    # Inline HTML (JATS-parsed or cached HTML)
    if result.html:
        save_fulltext(
            conn, paper_id=paper_id, html=result.html, source=result.source,
        )
        paper["fulltext_html"] = result.html
        paper["fulltext_source"] = result.source
        return render_template("fragments/fulltext_content.html", paper=paper)

    # Otherwise store whichever link we resolved and render it.
    for value, source in (
        (result.file_path, "pdf_cached"),
        (result.pdf_url, "unpaywall_pdf"),
        (result.web_url, "publisher_url"),
    ):
        if value:
            save_fulltext(conn, paper_id=paper_id, html=str(value), source=source)
            return _link_fragment(source, str(value))

    return _UNAVAILABLE_HTML


def _enrich_paper_metadata(paper: dict) -> None:
    """Lift the journal name out of ``metadata_json`` into a top-level key.

    Args:
        paper: Paper dict, modified in place to gain a ``journal`` key.
    """
    paper["journal"] = parse_metadata(paper.get("metadata_json")).get("journal", "")
