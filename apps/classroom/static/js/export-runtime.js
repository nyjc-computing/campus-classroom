// Campus Classroom document-export runtime (#78).
// Injected into served documents on the embed origin; the /a/ answer
// sheet requests an export at save/submit via postMessage. The runtime
// serializes the LIVE DOM into a script-free snapshot: form-control
// state is synced into attributes (outerHTML loses .value/.checked/
// .selected), same-origin child iframes become srcdoc snapshots,
// canvases become images, then scripts (including this runtime) and
// inline event handlers are stripped, and the server-minted export
// nonce is stamped as a meta tag before the HTML is posted back.
(function () {
    'use strict';
    var CFG = window.CAMPUS_EMBED || {};

    function syncFormState(live, clone) {
        var inputs = live.querySelectorAll('input, textarea, select');
        var clones = clone.querySelectorAll('input, textarea, select');
        for (var i = 0; i < inputs.length && i < clones.length; i++) {
            var el = inputs[i];
            var copy = clones[i];
            if (el.tagName === 'TEXTAREA') {
                copy.textContent = el.value;
            } else if (el.tagName === 'SELECT') {
                for (var j = 0; j < el.options.length && j < copy.options.length; j++) {
                    if (el.options[j].selected) {
                        copy.options[j].setAttribute('selected', 'selected');
                    } else {
                        copy.options[j].removeAttribute('selected');
                    }
                }
            } else if (el.type === 'checkbox' || el.type === 'radio') {
                if (el.checked) copy.setAttribute('checked', 'checked');
                else copy.removeAttribute('checked');
            } else if (el.type !== 'password') {
                copy.setAttribute('value', el.value);
            }
        }
    }

    function captureChildFrames(live, clone) {
        var frames = live.querySelectorAll('iframe');
        var clones = clone.querySelectorAll('iframe');
        for (var i = 0; i < frames.length && i < clones.length; i++) {
            try {
                var doc = frames[i].contentDocument;
                if (doc && doc.documentElement) {
                    clones[i].setAttribute('srcdoc', serializeDocument(doc));
                }
            } catch (err) { /* cross-origin child: keep its src */ }
        }
    }

    function captureCanvases(live, clone) {
        var canvases = live.querySelectorAll('canvas');
        var clones = clone.querySelectorAll('canvas');
        for (var i = 0; i < canvases.length && i < clones.length; i++) {
            try {
                var img = clone.ownerDocument.createElement('img');
                img.setAttribute('alt', 'canvas snapshot');
                img.setAttribute('src', canvases[i].toDataURL());
                clones[i].replaceWith(img);
            } catch (err) { /* tainted canvas: leave it empty */ }
        }
    }

    function stripActiveContent(root) {
        root.querySelectorAll('script').forEach(function (el) { el.remove(); });
        root.querySelectorAll('*').forEach(function (el) {
            Array.prototype.forEach.call(el.attributes, function (attr) {
                var name = attr.name.toLowerCase();
                if (name.indexOf('on') === 0 && name.length > 4) {
                    el.removeAttribute(attr.name);
                }
            });
        });
    }

    function serializeDocument(doc) {
        var clone = doc.documentElement.cloneNode(true);
        syncFormState(doc, clone);
        captureChildFrames(doc, clone);
        captureCanvases(doc, clone);
        stripActiveContent(clone);
        return '<!DOCTYPE html>' + clone.outerHTML;
    }

    function stampNonce(clone, nonce) {
        var head = clone.querySelector('head');
        if (!head) {
            head = clone.ownerDocument.createElement('head');
            clone.insertBefore(head, clone.firstChild);
        }
        var stale = head.querySelector('meta[name="campus-export-nonce"]');
        if (stale) stale.remove();
        var meta = clone.ownerDocument.createElement('meta');
        meta.setAttribute('name', 'campus-export-nonce');
        meta.setAttribute('content', nonce);
        head.appendChild(meta);
    }

    function exportNow(nonce) {
        var clone = document.documentElement.cloneNode(true);
        syncFormState(document, clone);
        captureChildFrames(document, clone);
        captureCanvases(document, clone);
        stripActiveContent(clone);   // removes this runtime too
        stampNonce(clone, nonce);
        return '<!DOCTYPE html>' + clone.outerHTML;
    }

    window.addEventListener('message', function (event) {
        if (CFG.parentOrigin && event.origin !== CFG.parentOrigin) return;
        var data = event.data || {};
        if (data.action !== 'campus-export' || !data.nonce) return;
        var reply;
        try {
            reply = { action: 'campus-exported', qid: CFG.qid, html: exportNow(String(data.nonce)) };
        } catch (err) {
            reply = { action: 'campus-export-error', qid: CFG.qid, message: String(err) };
        }
        event.source.postMessage(reply, CFG.parentOrigin || '*');
    });
})();
