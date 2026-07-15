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
    ['interval', 'daily', 'weekly', 'monthly', 'cron'].forEach(function (m) {
        var el = document.getElementById(m + '-options');
        if (el) el.classList.toggle('hidden', m !== mode);
    });
    // The timezone select lives outside the panels (one control, one name) and
    // applies to every clock-based mode — cron expressions included.
    var tz = document.getElementById('timezone-option');
    if (tz) tz.classList.toggle('hidden', ['daily', 'weekly', 'monthly', 'cron'].indexOf(mode) === -1);
    if (mode === 'cron') updateCronPreview();
}

// Live cron validation + "next runs" preview.
(function () {
    var debounceTimer = null;

    function els() {
        return {
            input: document.getElementById('id_cron_expression'),
            wrap: document.getElementById('cron-options'),
            preview: document.getElementById('cron-preview'),
            status: document.getElementById('cron-preview-status'),
            runs: document.getElementById('cron-preview-runs'),
        };
    }

    window.updateCronPreview = function () {
        var e = els();
        if (!e.input || !e.wrap || !e.preview) return;

        var expr = e.input.value.trim();
        if (!expr) {
            e.preview.classList.add('hidden');
            return;
        }

        var tzSelect = document.getElementById('id_timezone');
        var url = e.wrap.getAttribute('data-preview-url') +
            '?expression=' + encodeURIComponent(expr) +
            (tzSelect ? '&timezone=' + encodeURIComponent(tzSelect.value) : '');

        fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                e.preview.classList.remove('hidden');
                if (!data.valid) {
                    e.status.textContent = data.error || 'Invalid cron expression.';
                    e.status.className = 'font-medium text-fail';
                    e.runs.innerHTML = '';
                    return;
                }
                e.status.textContent = 'Next runs (' + (data.timezone || 'UTC') + '):';
                e.status.className = 'font-medium text-ok';
                e.runs.innerHTML = '';
                (data.runs || []).forEach(function (run) {
                    var li = document.createElement('li');
                    li.textContent = run;
                    e.runs.appendChild(li);
                });
            })
            .catch(function () {
                e.preview.classList.add('hidden');
            });
    };

    document.addEventListener('DOMContentLoaded', function () {
        var input = document.getElementById('id_cron_expression');
        if (!input) return;
        input.addEventListener('input', function () {
            if (debounceTimer) clearTimeout(debounceTimer);
            debounceTimer = setTimeout(updateCronPreview, 350);
        });
        // The preview is computed in the selected timezone, so re-run it when
        // that changes while cron mode is showing.
        var tzSelect = document.getElementById('id_timezone');
        if (tzSelect) {
            tzSelect.addEventListener('change', function () {
                var current = document.querySelector('input[name="run_mode"]:checked');
                if (current && current.value === 'cron') updateCronPreview();
            });
        }
        // Show preview immediately if cron mode is the current selection.
        var checked = document.querySelector('input[name="run_mode"]:checked');
        if (checked && checked.value === 'cron') updateCronPreview();
    });
})();

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
