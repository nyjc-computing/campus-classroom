// Shared helper for displaying campus API errors to users.
// Handles the error-spec envelope ({error: {code, message, errors: [...]}}),
// the legacy string form ({error: "..."}) and non-JSON error bodies.

function formatApiError(data, fallback) {
    if (!data || !data.error) {
        return fallback;
    }
    if (typeof data.error === 'string') {
        return data.error;
    }
    const error = data.error;
    let message = error.message || error.code || fallback;
    const fieldErrors = Array.isArray(error.errors) ? error.errors : [];
    if (fieldErrors.length > 0 && (fieldErrors[0].field || fieldErrors[0].message)) {
        const first = fieldErrors[0];
        message = (first.field ? first.field + ': ' : '') + (first.message || first.code);
    }
    return message;
}

// A dead Campus grant (#98): every API call answers 401
// campus_session_expired because the stored grant behind the app
// session is gone or unrefreshable — no retry can succeed until the
// browser re-enters the Campus OAuth flow. Redirects the top-level
// page through /login, which returns to the current URL (query
// included) after sign-in. Returns true when it took over, so callers
// can skip their usual error UI.
function handleSessionExpiry(response, data) {
    const code = (data && data.error && data.error.code)
        || (data && data.code) || '';
    if (response.status !== 401 && code !== 'campus_session_expired') {
        return false;
    }
    const next = encodeURIComponent(location.pathname + location.search);
    window.location.assign('/login?next=' + next);
    return true;
}
