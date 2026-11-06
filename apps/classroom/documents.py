"""campus.apps.classroom.documents

Document-question support (#62/#78): embed tokens, export nonces,
snapshot enforcement, and export-runtime injection for the `document`
question type — single-file interactive HTML docs stored in the
assignment (campus questionschemas registry, campus#533).

Security model (#78): documents are SERVED from a dedicated embed
origin (an EMBED_MODE=1 deployment of this app) inside frames sandboxed
`allow-scripts allow-same-origin` — safe only because the frame origin
differs from the app origin. Export serialization necessarily runs
client-side (the DOM lives in the browser), but the server owns the
trigger and acceptance: a stateless HMAC nonce stamped into every
snapshot, a size cap, and a scrub of active content at write.
Snapshots are attestations, not verified evidence: a client can
fabricate response_data.html exactly as it can fabricate response_text.
"""

import base64
import hmac
import json
import os
import re
import time
from hashlib import sha256

EMBED_MODE_ENV = "EMBED_MODE"
EMBED_ORIGIN_ENV = "EMBED_ORIGIN"
EMBED_PARENT_ORIGIN_ENV = "EMBED_PARENT_ORIGIN"
EMBED_TOKEN_SECRET_ENV = "EMBED_TOKEN_SECRET"

EMBED_TOKEN_TTL_SECONDS = 2 * 60 * 60   # embed URLs must outlive a work session
EXPORT_NONCE_TTL_SECONDS = 10 * 60      # challenge → save/submit window

DOCUMENT_TYPE_ID = "document"
NONCE_META_NAME = "campus-export-nonce"

# Deployed apps live in the campus.api hostedapps registry (#81,
# campus#1066): publishers deploy via campus-apphost, classroom
# questions reference `data: {slug}`, and the embed route serves the
# app's current bytes — replacing a slug's content updates referencing
# assignments in place. Bytes are content-addressed (sha256 keys) and
# served hash-verified by campus.api; the slug is only the address.

# Mirror the campus questionschemas caps (client-side hints + this
# app's enforcement must agree with what campus will 422 on).
MAX_DOCUMENT_CHARS = 2_000_000
MAX_SNAPSHOT_CHARS = 5_000_000


class DocumentResponseError(ValueError):
    """A document response failed snapshot enforcement (answers 422)."""


# --- configuration ----------------------------------------------------------

def embed_mode() -> bool:
    """True on the docs deployment (which may serve /embed routes)."""
    return os.environ.get(EMBED_MODE_ENV) == "1"


def embed_origin() -> str | None:
    """Public origin of the docs deployment, or None when unset
    (document questions then render a degrade notice on /a/)."""
    origin = os.environ.get(EMBED_ORIGIN_ENV, "").strip()
    return origin.rstrip("/") or None


def parent_origin() -> str | None:
    """Main-app origin (embed deployments): the only allowed framer and
    the postMessage target of the export runtime."""
    origin = os.environ.get(EMBED_PARENT_ORIGIN_ENV, "").strip()
    return origin.rstrip("/") or None


def token_secret() -> bytes:
    """HMAC key for embed tokens and export nonces. A dedicated secret
    is preferred (the docs deployment needs no Flask SECRET_KEY overlap);
    SECRET_KEY is the local-dev fallback."""
    secret = (
        os.environ.get(EMBED_TOKEN_SECRET_ENV)
        or os.environ.get("SECRET_KEY")
    )
    if not secret:
        raise RuntimeError(
            "EMBED_TOKEN_SECRET (or SECRET_KEY) is required for document "
            "embeds"
        )
    return secret.encode("utf-8")


# --- HMAC capabilities (embed tokens, export nonces) ------------------------
#
# Both are stateless `expiry.signature` strings over a domain-separated
# payload, so nothing is stored: the docs deployment verifies embed
# tokens with the same key the main app minted them with.

def _sign(payload: str) -> str:
    digest = hmac.new(token_secret(), payload.encode("utf-8"), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:32]


def _mint(domain: str, assignment_id: str, question_id: str,
          ttl: int) -> tuple[str, int]:
    exp = int(time.time()) + ttl
    token = f"{exp}.{_sign(f'{domain}|{assignment_id}|{question_id}|{exp}')}"
    return token, exp


def _verify(token: str, domain: str, assignment_id: str, question_id: str) -> bool:
    exp, _, sig = (token or "").partition(".")
    if not sig or not exp.isdigit():
        return False
    if int(exp) < time.time():
        return False
    expected = _sign(f"{domain}|{assignment_id}|{question_id}|{int(exp)}")
    return hmac.compare_digest(sig, expected)


def mint_embed_token(assignment_id: str, question_id: str) -> str:
    """Capability token for one document of one assignment (no user
    binding: embed requests carry no Campus session cookie, so the token
    IS the credential — short-lived, single-question scope)."""
    return _mint("embed", assignment_id, question_id, EMBED_TOKEN_TTL_SECONDS)[0]


def verify_embed_token(token: str, assignment_id: str, question_id: str) -> bool:
    return _verify(token, "embed", assignment_id, question_id)


def mint_export_nonce(assignment_id: str, question_id: str) -> tuple[str, int]:
    """Server-triggered export (#78): the nonce an exported snapshot
    must stamp before this app will accept it."""
    return _mint("export", assignment_id, question_id, EXPORT_NONCE_TTL_SECONDS)


def verify_export_nonce(nonce: str, assignment_id: str, question_id: str) -> bool:
    return _verify(nonce, "export", assignment_id, question_id)


# --- snapshot enforcement ---------------------------------------------------

_NONCE_META_RE = re.compile(
    r"<meta[^>]*name\s*=\s*[\"']" + NONCE_META_NAME + r"[\"'][^>]*>",
    re.IGNORECASE,
)
_NONCE_CONTENT_RE = re.compile(r"content\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)

_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL
)
_UNCLOSED_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL
)
_EVENT_ATTR_RE = re.compile(
    r"""\son[a-z]{3,}\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE
)
_JS_URL_ATTR_RE = re.compile(
    r"""(\s(?:href|src|action|formaction|poster|xlink:href)\s*=\s*)
        (?:"\s*javascript:[^"]*"|'\s*javascript:[^']*'|javascript:[^\s>]*)""",
    re.IGNORECASE | re.VERBOSE,
)


def _snapshot_nonce(html: str) -> str | None:
    meta = _NONCE_META_RE.search(html)
    if meta is None:
        return None
    content = _NONCE_CONTENT_RE.search(meta.group(0))
    return content.group(1) if content else None


def sanitize_snapshot(html: str) -> str:
    """Scrub active content from an exported snapshot.

    Defense in depth: the review view renders snapshots in a
    script-disabled sandbox, and the harness-authored source was
    sanitised upstream — this keeps the STORED artifact clean no matter
    what produced it (write-side guarantee from #78).
    """
    html = _SCRIPT_RE.sub("", html)
    html = _UNCLOSED_SCRIPT_RE.sub("", html)
    html = _EVENT_ATTR_RE.sub("", html)
    html = _JS_URL_ATTR_RE.sub(lambda m: m.group(1) + '""', html)
    return html


def enforce_document_responses(assignment, responses) -> None:
    """Validate + sanitise the document responses in a submission
    payload, in place (this app is the enforcement layer before the
    Campus API; its schema is the backstop).

    Raises DocumentResponseError when a response to a document question
    does not carry a fresh, nonce-stamped snapshot. Text responses pass
    through untouched (legacy byte-compatibility, #62).
    """
    types_by_id = {}
    for question in getattr(assignment, "questions", None) or []:
        qid = question_id(question)
        if qid:
            types_by_id[qid] = question_type_id(question)

    assignment_id = str(getattr(assignment, "id", ""))
    for response in responses or []:
        if not isinstance(response, dict):
            continue
        qid = response.get("question_id")
        if types_by_id.get(qid) != DOCUMENT_TYPE_ID:
            continue

        data = response.get("response_data")
        html = data.get("html") if isinstance(data, dict) else None
        if response.get("response_text") and not html:
            raise DocumentResponseError(
                f"{qid}: document questions submit an exported document, "
                "not typed text."
            )
        if not html or not html.strip():
            raise DocumentResponseError(
                f"{qid}: the exported document snapshot is missing."
            )
        if len(html) > MAX_SNAPSHOT_CHARS:
            raise DocumentResponseError(
                f"{qid}: the exported document exceeds the size limit."
            )
        nonce = _snapshot_nonce(html)
        if not nonce or not verify_export_nonce(nonce, assignment_id, qid):
            raise DocumentResponseError(
                f"{qid}: the snapshot is missing a fresh export "
                "authorization. Reload the page and save again."
            )
        response["response_data"] = {"html": sanitize_snapshot(html)}
        response["question_type"] = DOCUMENT_TYPE_ID
        response.pop("response_text", None)


# --- export-runtime injection (embed deployments) ---------------------------

def export_runtime_js() -> str:
    """The injected snapshot serializer served with every document."""
    path = os.path.join(
        os.path.dirname(__file__), "static", "js", "export-runtime.js"
    )
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def inject_export_runtime(html: str, question_id: str, parent: str | None) -> str:
    """Append the export runtime (and its config) to a document. The
    runtime self-identifies via window.CAMPUS_EMBED and only accepts
    export requests from the main-app origin."""
    config = json.dumps({"qid": question_id, "parentOrigin": parent})
    bootstrap = (
        "<script>window.CAMPUS_EMBED=" + config + ";</script>\n"
        "<script>\n" + export_runtime_js() + "\n</script>"
    )
    close = re.search(r"</body\s*>", html, re.IGNORECASE)
    if close:
        return html[:close.start()] + bootstrap + html[close.start():]
    return html + bootstrap


# --- classroom-side registry helpers (#62) ----------------------------------
#
# Campus owns the authoritative schemas (campus.api.questionschemas);
# this table is the classroom UI contract: the answer-field renderer each
# registered type uses on /a/ and in review. Types without a renderer
# (campus may know more than this app renders yet) fall back to a
# "not supported here" notice rather than a wrong input.

RENDER_TEXTAREA = "textarea"       # multi-line free text (free-response)
RENDER_TEXT = "text"               # single-line input (short-answer)
RENDER_DOCUMENT = "document"       # embedded doc + export flow

RENDERERS = {
    "free-response": RENDER_TEXTAREA,
    "short-answer": RENDER_TEXT,
    DOCUMENT_TYPE_ID: RENDER_DOCUMENT,
}


def renderer_for(question) -> str:
    """The renderer key for a question's type, or "" when this app has
    no renderer for it yet."""
    return RENDERERS.get(question_type_id(question), "")


def question_id(question) -> str | None:
    """The question's id; tolerant of both model objects and the
    dict-shaped questions some payloads/harnesses carry."""
    if isinstance(question, dict):
        return question.get("id")
    return getattr(question, "id", None)


def question_type_id(question) -> str:
    """The question's registry type id (campus default: free-response)."""
    if isinstance(question, dict):
        return question.get("type") or "free-response"
    return getattr(question, "type", None) or "free-response"


def is_document(question) -> bool:
    return question_type_id(question) == DOCUMENT_TYPE_ID


def embed_url_for(assignment_id: str, question_id: str) -> str | None:
    """Tokened /embed URL on the docs deployment, or None when this
    deployment has no embed origin configured."""
    origin = embed_origin()
    if not origin:
        return None
    token = mint_embed_token(assignment_id, question_id)
    return f"{origin}/embed/{assignment_id}/{question_id}?t={token}"


def sanitize_app_slug(slug: str) -> str | None:
    """Normalise a publisher-provided slug (the app's address).

    Lower-cases, maps runs of disallowed characters to '-', and
    enforces the hostedapps slug charset (single segment — campus.api
    rejects slashes). Returns None when nothing usable remains (the
    caller answers 400).
    """
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", slug.strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    if not cleaned or len(cleaned) > 512:
        return None
    return cleaned


def question_app_slug(question) -> str | None:
    """The files slug a document question references, when it uses the
    registry-reference shape (data.slug; legacy questions carry
    data.html instead)."""
    data = getattr(question, "data", None)
    if isinstance(data, dict):
        slug = data.get("slug")
        if isinstance(slug, str) and slug:
            return slug
    return None
