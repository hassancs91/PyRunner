/* Whole-row links for tables.
   Mark a <tr data-href="/some/url"> and the entire row navigates on click,
   while clicks on real controls inside the row (links, buttons, inputs) keep
   their own behaviour. Ctrl/Cmd-click and middle-click open a new tab, and a
   click that ends a text selection does nothing so copying stays possible. */
(function () {
    var CONTROLS = "a, button, input, select, textarea, label, summary, [data-no-row-link]";

    function rowFor(event) {
        var row = event.target.closest ? event.target.closest("tr[data-href]") : null;
        if (!row) return null;
        if (event.target.closest(CONTROLS)) return null;
        return row;
    }

    document.addEventListener("click", function (event) {
        var row = rowFor(event);
        if (!row) return;
        var selection = window.getSelection ? String(window.getSelection()) : "";
        if (selection) return;
        var href = row.getAttribute("data-href");
        if (event.ctrlKey || event.metaKey || event.shiftKey) {
            window.open(href, "_blank", "noopener");
        } else {
            window.location.href = href;
        }
    });

    document.addEventListener("auxclick", function (event) {
        if (event.button !== 1) return;
        var row = rowFor(event);
        if (!row) return;
        event.preventDefault();
        window.open(row.getAttribute("data-href"), "_blank", "noopener");
    });

    // Keyboard: rows carry tabindex="0" so Enter opens them like a link.
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Enter") return;
        var row = event.target.closest ? event.target.closest("tr[data-href]") : null;
        if (!row || event.target !== row) return;
        window.location.href = row.getAttribute("data-href");
    });
})();
