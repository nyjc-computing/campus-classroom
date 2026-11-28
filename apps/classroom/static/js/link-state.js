// #180: state panels for the assignments listing.
//
// The listing API names the bridge state in its body envelope's
// link_state field ("ok" | "not-connected" | "reconnect-needed" |
// "reauth-needed" | "unavailable" — #184); these helpers render the
// matching empty state or degraded banner, so an unconnected student
// never sees a bare "No assignments yet". CAMPUS_SCRIPT_ROOT
// (base.html) keeps every link namespace-aware behind campus-gateway;
// CAMPUS_LOGIN_URL carries the re-auth target.

function classroomConnectUrl() {
    return `${window.CAMPUS_SCRIPT_ROOT || ''}/connect`;
}

function classroomEmptyStateHtml(linkState) {
    if (linkState === 'not-connected' || linkState === 'reconnect-needed') {
        const reconnect = linkState === 'reconnect-needed';
        return `
            <div class="text-center py-5">
                <h5 class="text-warning">${reconnect
                    ? 'Your Google Classroom connection needs refreshing'
                    : 'You\u2019re not connected to Google Classroom yet'}</h5>
                <p class="text-muted">
                    ${reconnect
                        ? 'Your connection is missing permissions it needs to see your classes\u2019 assignments. Reconnect once to refresh them.'
                        : 'Connect your school Google account once, and assignments your teachers send to Classroom will appear here.'}
                </p>
                <a href="${classroomConnectUrl()}" target="_blank" rel="noopener" class="btn btn-primary">${reconnect
                    ? 'Reconnect Google Classroom'
                    : 'Connect Google Classroom'}</a>
                <p class="text-muted small mt-2 mb-0">Opens in a new tab — reload this page after connecting.</p>
            </div>`;
    }
    if (linkState === 'reauth-needed') {
        const next = encodeURIComponent(
            window.location.pathname + window.location.search);
        return `
            <div class="text-center py-5">
                <h5 class="text-muted">Your Campus session has expired</h5>
                <p class="text-muted">Sign in again to see assignments from your Google Classroom classes.</p>
                <a href="${window.CAMPUS_LOGIN_URL}?next=${next}" class="btn btn-primary">Sign in again</a>
            </div>`;
    }
    if (linkState === 'unavailable') {
        return `
            <div class="text-center py-5">
                <h5 class="text-muted">Couldn't check Google Classroom just now</h5>
                <p class="text-muted">Assignments shared with your classes may be missing from this list. Try reloading in a minute.</p>
            </div>`;
    }
    return `
        <div class="text-center py-5">
            <h5 class="text-muted">No assignments shared with you yet</h5>
            <p class="text-muted">
                You're connected to Google Classroom — assignments your teachers
                send to your classes will appear here. Assignments you create
                appear here too.
            </p>
        </div>`;
}

function classroomDegradedBannerHtml(linkState) {
    if (linkState === 'not-connected' || linkState === 'reconnect-needed') {
        const reconnect = linkState === 'reconnect-needed';
        return `
            <div class="alert alert-warning d-flex justify-content-between align-items-center" role="alert">
                <span>${reconnect
                    ? 'Your Google Classroom connection is missing permissions — assignments shared with your classes are hidden.'
                    : 'Only showing assignments you created — connect Google Classroom to see the ones shared with your classes.'}</span>
                <a href="${classroomConnectUrl()}" target="_blank" rel="noopener" class="btn btn-sm btn-warning">${reconnect
                    ? 'Reconnect' : 'Connect'}</a>
            </div>`;
    }
    if (linkState === 'reauth-needed') {
        const next = encodeURIComponent(
            window.location.pathname + window.location.search);
        return `
            <div class="alert alert-warning" role="alert">
                Your Campus session expired — assignments shared with your classes are hidden.
                <a href="${window.CAMPUS_LOGIN_URL}?next=${next}">Sign in again</a> to see them.
            </div>`;
    }
    return `
        <div class="alert alert-warning" role="alert">
            Couldn't check Google Classroom just now — assignments shared with your classes
            may be missing. Try reloading in a minute.
        </div>`;
}
