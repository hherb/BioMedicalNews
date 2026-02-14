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

    // Tab switching
    document.body.addEventListener("click", function (evt) {
        var tab = evt.target.closest(".tab");
        if (tab) {
            document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
            tab.classList.add("active");
        }
    });
});
