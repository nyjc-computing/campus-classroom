// Auto-growing textareas: any textarea with the .autosize class grows to
// fit its content, capped by CSS (textarea.autosize max-height in
// style.css). Typed input, builder cards inserted after load, and
// viewport rewraps are all handled here, so pages only mark the class —
// except after assigning .value to a textarea that was already in the
// page (programmatic value changes fire no events): call autosizeAll().
(function () {
    function autosize(el) {
        if (!el.offsetHeight && !el.scrollHeight) return;  // hidden — nothing to fit
        const border = el.offsetHeight - el.clientHeight;  // border-box compensation
        el.style.height = 'auto';
        el.style.height = Math.ceil(el.scrollHeight + border) + 'px';
    }

    function autosizeAll(root) {
        (root || document).querySelectorAll('textarea.autosize').forEach(autosize);
    }

    document.addEventListener('input', event => {
        if (event.target instanceof HTMLTextAreaElement
                && event.target.classList.contains('autosize')) {
            autosize(event.target);
        }
    });

    // Question-builder cards and JS-rendered pages insert their
    // textareas after load; the observer fires as a microtask, after the
    // synchronous prefills of .value, so it sizes the final content.
    new MutationObserver(mutations => {
        mutations.forEach(mutation => mutation.addedNodes.forEach(node => {
            if (!(node instanceof HTMLElement)) return;
            if (node instanceof HTMLTextAreaElement) {
                if (node.classList.contains('autosize')) autosize(node);
            } else {
                node.querySelectorAll('textarea.autosize').forEach(autosize);
            }
        }));
    }).observe(document.documentElement, {childList: true, subtree: true});

    // Line wrapping changes with the viewport; debounce the refit.
    let resizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => autosizeAll(), 150);
    });

    window.autosizeAll = autosizeAll;
    autosizeAll();
})();
