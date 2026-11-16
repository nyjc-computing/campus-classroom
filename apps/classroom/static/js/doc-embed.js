// Interactive document embeds: maximize/restore runtime shared by the
// student answer sheet (share.html) and the staff assignment preview
// (view.html). The maximized box is a fixed overlay around the same
// iframe element — never re-parented, so in-document work survives the
// toggle. Top layer (Popover API) first: any transformed ancestor
// (e.g. the card's slide-in animation) traps position:fixed and would
// clip the overlay to the card; plain fixed is the fallback where the
// API is missing. One document maximized at a time; Escape restores.

function setDocMax(box, maximize) {
    if (maximize) {
        if (typeof box.showPopover === 'function') {
            box.setAttribute('popover', 'manual');
            try { box.showPopover(); } catch (e) { /* already shown */ }
        }
        box.classList.add('doc-maximized');
    } else {
        box.classList.remove('doc-maximized');
        if (typeof box.hidePopover === 'function') {
            try { box.hidePopover(); } catch (e) { /* not shown */ }
            box.removeAttribute('popover');
        }
    }
    document.body.classList.toggle(
        'doc-max-open',
        Boolean(document.querySelector('.doc-embed.doc-maximized')));
    const btn = box.querySelector('.doc-max-btn');
    if (btn) {
        btn.setAttribute('aria-expanded', String(maximize));
        btn.innerHTML = maximize
            ? '<i class="bi bi-fullscreen-exit me-1"></i>Restore'
            : '<i class="bi bi-arrows-fullscreen me-1"></i>Maximize';
    }
}

document.addEventListener('click', event => {
    const btn = event.target.closest('.doc-max-btn');
    if (!btn) return;
    const box = btn.closest('.doc-embed');
    document.querySelectorAll('.doc-embed.doc-maximized').forEach(other => {
        if (other !== box) setDocMax(other, false);
    });
    setDocMax(box, !box.classList.contains('doc-maximized'));
});

document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('.doc-embed.doc-maximized')
        .forEach(box => setDocMax(box, false));
});
