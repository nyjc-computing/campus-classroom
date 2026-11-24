// Shared single-file HTML upload widget for the /apps publisher forms
// (apps_add, apps_edit): one drop zone that accepts click-to-browse
// AND drag-and-drop, with the validation both pages enforce (HTML
// extension, size cap) and the same status copy.
//
// Usage:
//   const upload = initAppFileUpload({
//       zone: '#app-drop',        // drop/click target
//       input: '#app-file',       // the (hidden) <input type="file">
//       status: '#app-file-status',
//       maxBytes: 2000000,
//       emptyText: 'No file selected.',
//       onFile: (name, html) => {...},  // accepted file, read as text
//   });
//   upload.clear();  // back to the empty state

function initAppFileUpload(options) {
    const zone = document.querySelector(options.zone);
    const input = document.querySelector(options.input);
    const status = document.querySelector(options.status);
    const maxBytes = options.maxBytes;
    const emptyText = options.emptyText || 'No file selected.';
    let currentFile = null;

    // A drop outside the zone must not navigate the page away (the
    // browser's default for file drops is "open the file").
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => e.preventDefault());

    function setStatus(text, isError) {
        status.textContent = text;
        status.classList.toggle('text-danger', Boolean(isError));
    }

    function kb(bytes) {
        return Math.max(1, Math.round(bytes / 1024));
    }

    function accept(file) {
        if (!file) return;
        const name = file.name || '';
        const lower = name.toLowerCase();
        const looksHtml = lower.endsWith('.html') || lower.endsWith('.htm')
            || file.type === 'text/html';
        if (!looksHtml) {
            currentFile = null;
            input.value = '';
            setStatus(`"${name}" is not an HTML file — pick a .html or .htm file.`, true);
            return;
        }
        if (file.size > maxBytes) {
            currentFile = null;
            input.value = '';
            setStatus(`"${name}" is ${kb(file.size)} KB — apps are limited to 2 MB.`, true);
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            currentFile = file;
            const html = String(reader.result || '');
            setStatus(`${name} · ${kb(file.size)} KB ready.`, false);
            options.onFile(name, html);
        };
        reader.onerror = () => setStatus(`Could not read "${name}".`, true);
        reader.readAsText(file);
    }

    zone.addEventListener('click', () => input.click());
    zone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            input.click();
        }
    });
    input.addEventListener('change', () => accept(input.files && input.files[0]));

    zone.addEventListener('dragover', (e) => {
        e.preventDefault();
        zone.classList.add('dragover');
    });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        zone.classList.remove('dragover');
        accept(e.dataTransfer.files && e.dataTransfer.files[0]);
    });

    setStatus(emptyText, false);
    return {
        clear() {
            currentFile = null;
            input.value = '';
            setStatus(emptyText, false);
        },
        get file() {
            return currentFile;
        },
    };
}
