"""Paper list and reading pane routes."""

from __future__ import annotations

from flask import Blueprint, current_app, render_template, request, abort

from bmnews.db.operations import get_papers_filtered, get_paper_with_score

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
