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
