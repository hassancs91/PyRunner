/* Multi-module editor for the Library edit form.
 *
 * The whole module map ({filename: code}) round-trips through the hidden
 * <input id="modules_json"> that backs the Django form. That field is rendered
 * pre-populated with the current modules, so if Monaco never loads (CDN blocked,
 * JS error) an accidental submit posts the UNCHANGED map — the server compares
 * content and writes no revision. Degrades to a no-op, never to data loss.
 *
 * Explicit save only: nothing here autosaves. A revision is a save, not a
 * keystroke.
 */
(function () {
    var root = document.getElementById('module-editor');
    if (!root) return;

    var hidden = document.getElementById('modules_json');
    var tabsBox = document.getElementById('module-tabs');
    var container = document.getElementById('module-editor-container');
    var emptyNote = document.getElementById('module-empty-note');

    var modules = {};      // filename -> code (the source of truth pre-Monaco)
    var models = {};       // filename -> monaco model (once Monaco is up)
    var active = null;
    var editor = null;

    try {
        modules = JSON.parse(hidden.value || '{}');
    } catch (e) {
        // Only reachable when the server re-rendered a payload it already
        // rejected as malformed (it echoes the POST back). Starting empty is the
        // recoverable state — but say so rather than silently discarding it, or
        // the editor just looks blank for no reason.
        console.error('Library editor: could not parse module data, starting empty.', e);
        modules = {};
    }

    var FILENAME_RE = /^[A-Za-z_][A-Za-z0-9_]*\.py$/;

    function names() {
        return Object.keys(modules).sort();
    }

    function serialize() {
        // Pull live text out of Monaco before writing the field.
        if (editor && active && models[active]) {
            modules[active] = models[active].getValue();
        }
        Object.keys(models).forEach(function (f) {
            if (modules.hasOwnProperty(f)) modules[f] = models[f].getValue();
        });
        hidden.value = JSON.stringify(modules);
    }

    function renderTabs() {
        tabsBox.innerHTML = '';
        names().forEach(function (filename) {
            var tab = document.createElement('button');
            tab.type = 'button';
            tab.className = 'group inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-mono rounded-t-md border-b-2 transition-colors ' +
                (filename === active
                    ? 'border-ok text-text bg-panel-hi/60'
                    : 'border-transparent text-muted hover:text-text hover:bg-panel-hi/30');
            tab.onclick = function () { switchTo(filename); };

            var label = document.createElement('span');
            label.textContent = filename;
            tab.appendChild(label);

            // __init__.py is auto-generated when absent, so it must stay
            // removable like any other module — no special-casing here.
            var x = document.createElement('span');
            x.textContent = '×';
            x.className = 'opacity-0 group-hover:opacity-100 hover:text-fail transition-opacity';
            x.title = 'Remove module';
            x.onclick = function (e) { e.stopPropagation(); removeModule(filename); };
            tab.appendChild(x);

            tabsBox.appendChild(tab);
        });
        emptyNote.classList.toggle('hidden', names().length > 0);
    }

    function switchTo(filename) {
        if (editor && active && models[active]) modules[active] = models[active].getValue();
        active = filename;
        if (editor && models[filename]) editor.setModel(models[filename]);
        renderTabs();
    }

    function addModule() {
        var filename = window.prompt('New module filename (e.g. helpers.py)');
        if (!filename) return;
        filename = filename.trim();
        if (!FILENAME_RE.test(filename)) {
            window.alert('Module filenames must look like helpers.py — letters, digits and underscores, ending in .py, with no folders.');
            return;
        }
        if (modules.hasOwnProperty(filename)) {
            window.alert('That module already exists.');
            return;
        }
        modules[filename] = '';
        if (editor) models[filename] = monaco.editor.createModel('', 'python');
        switchTo(filename);
    }

    function removeModule(filename) {
        if (!window.confirm('Remove ' + filename + ' from this library? It is deleted when you save.')) return;
        delete modules[filename];
        var doomed = models[filename];
        delete models[filename];
        if (active === filename) active = names()[0] || null;
        // Detach BEFORE disposing, and pass null when nothing is left: disposing a
        // model the editor still holds (i.e. removing the last module) leaves
        // Monaco pointing at a dead model and it throws on the next interaction.
        if (editor) editor.setModel(active ? models[active] : null);
        if (doomed) doomed.dispose();
        renderTabs();
    }

    function renameModule() {
        if (!active) return;
        var next = window.prompt('Rename module', active);
        if (!next || next === active) return;
        next = next.trim();
        if (!FILENAME_RE.test(next)) {
            window.alert('Module filenames must look like helpers.py — letters, digits and underscores, ending in .py, with no folders.');
            return;
        }
        if (modules.hasOwnProperty(next)) {
            window.alert('A module with that name already exists.');
            return;
        }
        var code = (editor && models[active]) ? models[active].getValue() : modules[active];
        var previous = active;
        var doomed = models[previous];

        modules[next] = code;
        delete modules[previous];
        delete models[previous];
        if (editor) models[next] = monaco.editor.createModel(code, 'python');
        // Switch the editor onto the new model FIRST, then dispose the old one —
        // same reason as removeModule.
        switchTo(next);
        if (doomed) doomed.dispose();
    }

    document.getElementById('module-add').onclick = addModule;
    document.getElementById('module-rename').onclick = renameModule;

    // Serialize on submit — this is what actually gets saved.
    root.closest('form').addEventListener('submit', serialize);

    renderTabs();
    active = names()[0] || null;

    if (typeof require === 'undefined') return;
    require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });
    require(['vs/editor/editor.main'], function () {
        function monacoTheme() {
            return document.documentElement.classList.contains('dark') ? 'vs-dark' : 'vs';
        }

        names().forEach(function (filename) {
            models[filename] = monaco.editor.createModel(modules[filename], 'python');
        });

        editor = monaco.editor.create(container, {
            model: active ? models[active] : null,
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

        new MutationObserver(function () {
            monaco.editor.setTheme(monacoTheme());
        }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    });
})();
