"""Paper list and reading pane routes."""

from __future__ import annotations

import json

from flask import Blueprint, current_app, render_template, request, abort

from bmlib.fulltext import FullTextService, FullTextError
from bmnews.db.operations import (
    get_papers_filtered,
    get_paper_with_score,
    save_fulltext,
)

papers_bp = Blueprint("papers", __name__)


@papers_bp.route("/papers")
def paper_list():
    conn = current_app.config["BMNEWS_DB"]
    sort = request.args.get("sort", "combined")
    source = request.args.get("source", "")
    tier = request.args.get("tier", "")
    design = request.args.get("design", "")

    papers, total = get_papers_filtered(
        conn, sort=sort, source=source, quality_tier=tier,
        study_design=design, limit=20, offset=0, with_total=True,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=total, offset=0, limit=20,
        sort=sort, source=source, tier=tier, design=design,
    )


@papers_bp.route("/papers/more")
def paper_list_more():
    conn = current_app.config["BMNEWS_DB"]
    sort = request.args.get("sort", "combined")
    source = request.args.get("source", "")
    tier = request.args.get("tier", "")
    design = request.args.get("design", "")
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 20, type=int)

    papers = get_papers_filtered(
        conn, sort=sort, source=source, quality_tier=tier,
        study_design=design, limit=limit, offset=offset,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=None, offset=offset, limit=limit,
        sort=sort, source=source, tier=tier, design=design,
        append=True,
    )


@papers_bp.route("/papers/<int:paper_id>")
def paper_detail(paper_id: int):
    conn = current_app.config["BMNEWS_DB"]
    paper = get_paper_with_score(conn, paper_id)
    if paper is None:
        abort(404)
    return render_template("fragments/reading_pane.html", paper=paper)


@papers_bp.route("/search")
def search():
    conn = current_app.config["BMNEWS_DB"]
    q = request.args.get("q", "").strip()
    if not q:
        return paper_list()

    papers, total = get_papers_filtered(
        conn, search=q, limit=20, offset=0, with_total=True,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=total, offset=0, limit=20,
        sort="combined", source="", tier="", design="",
    )


@papers_bp.route("/papers/<int:paper_id>/fulltext", methods=["POST"])
def paper_fulltext(paper_id: int):
    """Fetch and display full text for a paper."""
    conn = current_app.config["BMNEWS_DB"]
    paper = get_paper_with_score(conn, paper_id)
    if paper is None:
        abort(404)

    # Check if already cached in DB
    if paper.get("fulltext_html"):
        source = paper.get("fulltext_source", "")
        if source == "unpaywall_pdf":
            url = paper["fulltext_html"]
            return (
                '<div class="fulltext-pdf">'
                "<p>PDF available from open-access source:</p>"
                f'<a href="{url}" target="_blank" '
                'class="btn btn-primary">Open PDF &#x2197;</a></div>'
            )
        if source == "publisher_url":
            url = paper["fulltext_html"]
            return (
                '<div class="fulltext-external">'
                "<p>Full text available at publisher website:</p>"
                f'<a href="{url}" target="_blank" '
                'class="btn btn-primary">Open Publisher Page &#x2197;</a></div>'
            )
        return render_template("fragments/fulltext_content.html", paper=paper)

    # Extract identifiers
    pmc_id = paper.get("pmcid") or ""
    doi = paper.get("doi") or ""
    pmid = paper.get("pmid") or ""

    # Also try metadata_json
    if not pmc_id or not pmid:
        meta = json.loads(paper.get("metadata_json") or "{}")
        pmc_id = pmc_id or meta.get("pmcid", "")
        pmid = pmid or meta.get("pmid", "")

    email = current_app.config.get("BMNEWS_EMAIL", "bmnews@example.com")
    service = FullTextService(email=email)

    try:
        result = service.fetch_fulltext(
            pmc_id=pmc_id or None, doi=doi or None, pmid=pmid,
        )
    except FullTextError:
        return (
            '<div class="fulltext-unavailable">'
            "<p>Full text is not available for this paper.</p></div>"
        )

    if result.source == "europepmc" and result.html:
        save_fulltext(
            conn, paper_id=paper_id, html=result.html, source="europepmc",
        )
        paper["fulltext_html"] = result.html
        paper["fulltext_source"] = "europepmc"
        return render_template("fragments/fulltext_content.html", paper=paper)

    if result.source == "unpaywall" and result.pdf_url:
        link_html = (
            f'<a href="{result.pdf_url}" target="_blank" '
            'class="btn btn-primary">Open PDF &#x2197;</a>'
        )
        save_fulltext(
            conn, paper_id=paper_id, html=result.pdf_url, source="unpaywall_pdf",
        )
        return (
            f'<div class="fulltext-pdf">'
            f"<p>PDF available from open-access source:</p>{link_html}</div>"
        )

    if result.web_url:
        link_html = (
            f'<a href="{result.web_url}" target="_blank" '
            'class="btn btn-primary">Open Publisher Page &#x2197;</a>'
        )
        save_fulltext(
            conn, paper_id=paper_id, html=result.web_url, source="publisher_url",
        )
        return (
            f'<div class="fulltext-external">'
            f"<p>Full text available at publisher website:</p>{link_html}</div>"
        )

    return (
        '<div class="fulltext-unavailable">'
        "<p>Full text is not available for this paper.</p></div>"
    )
