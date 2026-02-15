document.addEventListener("DOMContentLoaded", function () {
    var splitInstance = null;

    function initSplitGutter(id) {
        var el = document.getElementById(id);
        if (el && typeof Split !== "undefined") {
            return Split({
                columnGutters: [{ track: 1, element: el }],
            });
        }
        return null;
    }

    // Init the papers gutter on page load
    splitInstance = initSplitGutter("gutter");

    // Re-init split gutters after HTMX swaps (e.g. settings tab)
    document.body.addEventListener("htmx:afterSettle", function () {
        var settingsGutter = document.getElementById("settings-gutter");
        if (settingsGutter && !settingsGutter._splitInit) {
            initSplitGutter("settings-gutter");
            settingsGutter._splitInit = true;
        }
    });

    // Highlight selected card
    document.body.addEventListener("htmx:afterOnLoad", function (evt) {
        if (evt.detail.target && evt.detail.target.id === "reading-pane") {
            document.querySelectorAll(".paper-card.selected").forEach(function (el) {
                el.classList.remove("selected");
            });
            if (evt.detail.elt && evt.detail.elt.classList.contains("paper-card")) {
                evt.detail.elt.classList.add("selected");
            }
        }
    });

    // Refresh paper list when pipeline status indicates new data
    document.body.addEventListener("htmx:afterSettle", function (evt) {
        if (evt.detail.target && evt.detail.target.id === "status-right") {
            var marker = document.querySelector("#status-right [data-refresh-list]");
            if (marker) {
                marker.removeAttribute("data-refresh-list");
                var paperList = document.getElementById("paper-list");
                if (paperList) {
                    htmx.trigger(paperList, "refreshPapers");
                }
            }
        }
    });

    // Tab switching
    document.body.addEventListener("click", function (evt) {
        var tab = evt.target.closest(".tab");
        if (tab) {
            document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
            tab.classList.add("active");
        }
    });
});

function toggleFulltext() {
    var el = document.getElementById("fulltext-display");
    var btn = document.querySelector(".fulltext-toggle");
    if (el.style.display === "none") {
        el.style.display = "block";
        btn.textContent = "Hide Full Text";
    } else {
        el.style.display = "none";
        btn.textContent = "Show Full Text";
    }
}
