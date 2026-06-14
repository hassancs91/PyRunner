/* Shared behaviour for the script create / edit forms. */

// Collapsible card sections (Schedule, Notifications).
function toggleSection(sectionId) {
    var content = document.getElementById(sectionId + '-content');
    var chevron = document.getElementById(sectionId + '-chevron');
    if (content) content.classList.toggle('hidden');
    if (chevron) chevron.classList.toggle('rotate-180');
}

// Show the option group that matches the selected schedule run mode.
function toggleScheduleOptions(mode) {
    ['interval', 'daily', 'weekly', 'monthly'].forEach(function (m) {
        var el = document.getElementById(m + '-options');
        if (el) el.classList.toggle('hidden', m !== mode);
    });
}

// Monaco code editor — initialised over the hidden <textarea id="id_code">.
(function () {
    if (typeof require === 'undefined') return;
    require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });

    require(['vs/editor/editor.main'], function () {
        var textarea = document.getElementById('id_code');
        var container = document.getElementById('code-editor-container');
        if (!textarea || !container) return;

        textarea.style.display = 'none';

        function monacoTheme() {
            return document.documentElement.classList.contains('dark') ? 'vs-dark' : 'vs';
        }

        var editor = monaco.editor.create(container, {
            value: textarea.value,
            language: 'python',
            theme: monacoTheme(),
            fontSize: 14,
            fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            lineNumbers: 'on',
            tabSize: 4,
            insertSpaces: true,
            automaticLayout: true,
            padding: { top: 14, bottom: 14 },
            renderLineHighlight: 'all',
            smoothScrolling: true,
        });

        editor.onDidChangeModelContent(function () {
            textarea.value = editor.getValue();
        });

        // Keep the editor theme in sync with the app's dark/light toggle.
        new MutationObserver(function () {
            monaco.editor.setTheme(monacoTheme());
        }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    });
})();
