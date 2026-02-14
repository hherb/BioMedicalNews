document.addEventListener("DOMContentLoaded", function () {
    if (typeof Split !== "undefined") {
        Split({
            columnGutters: [{
                track: 1,
                element: document.getElementById("gutter"),
            }],
        });
    }

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

    // Tab switching
    document.body.addEventListener("click", function (evt) {
        var tab = evt.target.closest(".tab");
        if (tab) {
            document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
            tab.classList.add("active");
        }
    });
});
