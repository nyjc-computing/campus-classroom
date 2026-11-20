// Staff-authored markdown for JS-rendered display surfaces
// (assignments/view.html, submissions/view.html): question prompts,
// group passages, question text, assignment descriptions.
//
// Server-rendered surfaces (share.html) use the matching Jinja `md`
// filter (apps/classroom/text_render.py) — escape-first python-markdown
// with nl2br. This helper mirrors that contract: escape-first parsing
// is not available client-side, so `marked` output is sanitized with
// DOMPurify instead (same guarantee: raw HTML in teacher text never
// executes in a student's browser). {breaks: true} matches nl2br.
//
// CDN fallback: if jsdelivr is unreachable (school networks), degrade
// to the previous verbatim rendering — escape + <br> — so prompts stay
// readable and linebreaks survive (issue #115).
(function () {
    'use strict';

    function escapeHtmlBr(text) {
        return text
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
            .replace(/\n/g, '<br>');
    }

    window.campusMarkdown = function (text) {
        if (!text) {
            return '';
        }
        if (window.marked && window.DOMPurify) {
            try {
                return window.DOMPurify.sanitize(
                    window.marked.parse(text, { breaks: true, gfm: true })
                );
            } catch (err) {
                console.error('Markdown rendering failed:', err);
            }
        }
        return escapeHtmlBr(text);
    };
})();
