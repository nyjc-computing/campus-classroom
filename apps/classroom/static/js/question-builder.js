// Shared nested question builder for assignment creation and editing.
// Question IDs follow the PRD §6.6 hierarchy: qN, qN.letter, qN.letter.roman.

const LEVEL1_NUMBERS = Array.from({ length: 99 }, (_, i) => String(i + 1));
const LEVEL2_LETTERS = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z'];
const LEVEL3_ROMAN = ['i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x',
                       'xi', 'xii', 'xiii', 'xiv', 'xv', 'xvi', 'xvii', 'xviii', 'xix', 'xx'];

function generateQuestionId(level, index) {
    switch(level) {
        case 1: return 'q' + LEVEL1_NUMBERS[index];
        case 2: return LEVEL2_LETTERS[index];
        case 3: return LEVEL3_ROMAN[index];
        default: return (index + 1).toString();
    }
}

// Question types (#62/#78): campus validates type + data server-side;
// document questions reference a deployed app by slug (#81).

// Recalculate all question IDs from the tree structure
function recalculateAllIds() {
    document.querySelectorAll('#questions-container > .question-tree').forEach((tree, treeIndex) => {
        updateTreeIds(tree, generateQuestionId(1, treeIndex));
    });
}

// Set the tree's own question ID, then recurse into its subquestions.
// Also syncs group mode: a card with subparts is a passage/group node —
// its own question+type fields hide (they live in the subparts).
function updateTreeIds(tree, id) {
    const card = tree.querySelector(':scope > .question-card');
    if (!card) return;

    card.querySelector('.question-id-badge').textContent = id;
    card.dataset.level = id.split('.').length;

    const subquestions = card.nextElementSibling;
    const hasSubparts = !!(subquestions
        && subquestions.classList.contains('subquestions')
        && subquestions.querySelector(':scope > .question-tree'));
    card.classList.toggle('question-group', hasSubparts);

    if (subquestions && subquestions.classList.contains('subquestions')) {
        const childLevel = id.split('.').length + 1;
        subquestions.querySelectorAll(':scope > .question-tree').forEach((subTree, subIndex) => {
            updateTreeIds(subTree, id + '.' + generateQuestionId(childLevel, subIndex));
        });
    }
}

// Add a new question card
function createQuestionCard(level = 1) {
    return `
        <div class="question-card" data-level="${level}">
            <div class="question-card-header">
                <div class="d-flex align-items-center">
                    <span class="drag-handle" title="Drag to reorder">
                        <i class="bi bi-grip-vertical"></i>
                    </span>
                    <span class="question-id-badge">q${level === 1 ? '1' : ''}</span>
                </div>
                <div class="question-actions">
                    ${level < 3 ? `
                        <button type="button" class="btn btn-sm btn-outline-secondary" onclick="addSubpart(this)" title="Add subpart">
                            <i class="bi bi-plus"></i> Subpart
                        </button>
                    ` : ''}
                    <button type="button" class="btn btn-sm btn-outline-danger" onclick="removeQuestion(this)" title="Remove">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
            </div>
            <div class="mb-2">
                <label class="form-label small text-muted">Prompt (optional)</label>
                <textarea class="form-control autosize question-prompt" rows="2" placeholder="Context or passage for the question..."></textarea>
            </div>
            <div class="question-group-hint">
                Passage/group — the answerable questions are the subparts below.
            </div>
            <div class="question-leaf-fields">
                <div class="mb-0">
                    <label class="form-label small text-muted">Question <span class="text-danger">*</span></label>
                    <textarea class="form-control autosize question-text" rows="2" required placeholder="Enter your question here..."></textarea>
                </div>
                <div class="mb-2 mt-2">
                    <label class="form-label small text-muted">Type</label>
                    <select class="form-select form-select-sm question-type" onchange="onQuestionTypeChange(this)">
                        <option value="free-response">Free response</option>
                        <option value="document">Interactive document</option>
                    </select>
                </div>
                <div class="question-doc-config d-none mb-0">
                    <div class="doc-app-actions">
                        <button type="button" class="btn btn-sm btn-outline-primary"
                                onclick="toggleAppPicker(this)">Embed Existing App</button>
                        <button type="button"
                                class="btn btn-sm btn-outline-primary doc-apphost-link"
                                data-apphost-path="/apps/add"
                                onclick="openAppUpload(this)">Upload App</button>
                        <a class="doc-apphost-link doc-myapps-link" target="_blank"
                           rel="noopener" data-apphost-path="/apps/myapps"
                           href="#">My Apps</a>
                    </div>
                    <select class="form-select form-select-sm question-doc-app d-none mt-2"
                            onchange="onDocAppChange(this)">
                        <option value="">Loading deployed apps…</option>
                    </select>
                    <div class="form-text question-doc-status">No app embedded yet.</div>
                </div>
            </div>
        </div>
    `;
}

// --- question types (#62/#78) ------------------------------------------------

// Deployed apps (#81): document questions reference an app by slug; the
// list comes from campus.api files (apps/ prefix) via /api/v1/apps.
// The upload flow lives on the apphost (embed) deployment —
// window.APPHOST_ORIGIN, set by the page — and returning from it
// refreshes the list, auto-embedding the new app in the card that
// launched the upload.
let APPS_PROMISE = null;
let KNOWN_APP_SLUGS = null;      // slugs seen at the last load (focus diff)
let APPS_REFRESHING = false;
let UPLOAD_REQUESTOR = null;     // card whose Upload App button was clicked

// ensureAppsLoaded plus slug bookkeeping: every load updates the baseline
// the Upload App focus handler diffs against. Use this everywhere; the
// raw promise would re-baseline out from under the diff.
function loadAppsAndTrack() {
    return ensureAppsLoaded().then(apps => {
        KNOWN_APP_SLUGS = new Set(apps.map(app => app.slug));
        return apps;
    });
}

function ensureAppsLoaded() {
    if (!APPS_PROMISE) {
        APPS_PROMISE = fetch('/api/v1/apps', {
            headers: {'X-Requested-With': 'XMLHttpRequest'},
        })
            .then(response => {
                if (!response.ok) {
                    // A dead Campus grant (#98) answers 401 here: re-auth
                    // is the only remedy, so redirect before the error path.
                    return response.json().catch(() => ({})).then(data => {
                        if (handleSessionExpiry(response, data)) return null;
                        throw new Error('Failed to load deployed apps');
                    });
                }
                return response.json();
            })
            .then(apps => {
                if (!apps) return null;   // page is navigating to /login
                window.CAMPUS_APPS = apps;
                refreshAllAppSelects();
                return apps;
            })
            .catch(error => {
                APPS_PROMISE = null;   // allow retry on the next document question
                throw error;
            });
    }
    return APPS_PROMISE;
}

function populateAppSelect(select) {
    const current = select.value || select.dataset.appSlug || '';
    const apps = window.CAMPUS_APPS || [];
    select.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = apps.length
        ? '— Choose a deployed app —'
        : 'No apps published yet — click Upload App to publish one.';
    select.appendChild(placeholder);
    apps.forEach(app => {
        const option = document.createElement('option');
        option.value = app.slug;
        option.textContent = (app.name && app.name !== app.slug)
            ? `${app.name} (${app.slug})`
            : app.slug;
        select.appendChild(option);
    });
    if (current) select.value = current;
}

function refreshAllAppSelects() {
    document.querySelectorAll('.question-doc-app').forEach(populateAppSelect);
}

function onQuestionTypeChange(select) {
    const card = select.closest('.question-card');
    const isDocument = select.value === 'document';
    card.querySelector('.question-doc-config').classList.toggle('d-none', !isDocument);
    if (isDocument) {
        // The apphost links need the origin; the picker needs options —
        // populate right away when the apps list is already loaded,
        // since ensureAppsLoaded's one-shot chain never refreshes a
        // select created later (late-added document questions, demoted
        // subparts, edit-mode populate).
        refreshDocAppLinks(card);
        if (window.CAMPUS_APPS) {
            populateAppSelect(card.querySelector('.question-doc-app'));
        }
        loadAppsAndTrack()
            .then(() => updateDocStatus(card.querySelector('.question-doc-app')))
            .catch(error => {
                const status = card.querySelector('.question-doc-status');
                if (status) status.textContent = error.message;
            });
    }
}

// Apphost convenience links (Upload App / My Apps) point at the embed
// (apphost) deployment; without a configured origin there is nowhere
// to send them, so they hide and only the embed picker remains.
function refreshDocAppLinks(card) {
    const origin = (window.APPHOST_ORIGIN || '').replace(/\/$/, '');
    card.querySelectorAll('.doc-apphost-link').forEach(link => {
        if (origin) {
            link.href = origin + link.dataset.apphostPath;
            link.hidden = false;
        } else {
            link.hidden = true;
            link.removeAttribute('href');
        }
    });
}

// Embed Existing App: reveal the app picker (hidden again once a
// choice is made — the status line shows the embedded app).
function toggleAppPicker(button) {
    const card = button.closest('.question-card');
    const select = card.querySelector('.question-doc-app');
    if (select.classList.contains('d-none')) {
        if (window.CAMPUS_APPS) populateAppSelect(select);
        loadAppsAndTrack().catch(error => {
            const status = card.querySelector('.question-doc-status');
            if (status) status.textContent = error.message;
        });
        select.classList.remove('d-none');
        select.focus();
    } else {
        select.classList.add('d-none');
    }
}

function onDocAppChange(select) {
    select.classList.add('d-none');
    updateDocStatus(select);
}

// Upload App: the campus-apphost upload form, in a new tab. When the
// user comes back, the focus handler below re-fetches the registry and
// auto-embeds the newly published app into this card.
function openAppUpload(button) {
    const origin = (window.APPHOST_ORIGIN || '').replace(/\/$/, '');
    if (!origin) return;
    UPLOAD_REQUESTOR = button.closest('.question-card');
    window.open(origin + button.dataset.apphostPath, '_blank', 'noopener');
}

// Returning from the Upload App tab: re-fetch the registry and, if this
// card launched the upload and is still unembedded, embed the newest
// newly published app in it.
window.addEventListener('focus', () => {
    if (!window.APPHOST_ORIGIN || APPS_REFRESHING) return;
    if (!document.querySelector('.question-doc-config:not(.d-none)')) return;
    const before = KNOWN_APP_SLUGS;
    const requestor = UPLOAD_REQUESTOR;
    UPLOAD_REQUESTOR = null;
    APPS_REFRESHING = true;
    APPS_PROMISE = null;   // a cached list defeats the point of the refresh
    loadAppsAndTrack().then(apps => {
        APPS_REFRESHING = false;
        if (!requestor || !before) return;
        const fresh = apps.filter(app => !before.has(app.slug));
        if (!fresh.length) return;
        const newest = fresh.slice().sort((a, b) =>
            String(b.updated_at || '').localeCompare(String(a.updated_at || '')))[0];
        const select = requestor.querySelector('.question-doc-app');
        if (select && !select.value && !select.dataset.appSlug) {
            select.value = newest.slug;
            select.dataset.appSlug = newest.slug;
            updateDocStatus(select);
        }
    }).catch(() => { APPS_REFRESHING = false; });
});

function setQuestionType(card, typeId) {
    const select = card.querySelector('.question-type');
    select.value = typeId || 'free-response';
    onQuestionTypeChange(select);
}

function updateDocStatus(select) {
    const status = select.closest('.question-card').querySelector('.question-doc-status');
    if (!status) return;
    if (select.value && select.selectedIndex > 0) {
        const option = select.options[select.selectedIndex];
        status.textContent = `Embeds ${option.textContent} — served live from the registry.`;
        return;
    }
    const hasApps = (window.CAMPUS_APPS || []).length > 0;
    status.textContent = hasApps
        ? 'No app embedded yet — click Embed Existing App to choose one.'
        : 'No apps published yet — click Upload App to publish one.';
}

// Add top-level question
function addTopLevelQuestion() {
    const container = document.getElementById('questions-container');
    const tree = document.createElement('div');
    tree.className = 'question-tree';
    tree.innerHTML = createQuestionCard(1);
    container.appendChild(tree);

    setupDragAndDrop(tree.querySelector('.question-card'));
    recalculateAllIds();
}

// A parent with subparts is a passage/group node: its own question+type
// moves into a new FIRST subpart, so every answerable question lives at
// leaf level (adding a subpart to "q1" makes its content q1.a and the
// fresh subpart q1.b). No-op for an empty free-response card.
function demoteCardContent(card, subquestions, level) {
    const questionText = card.querySelector('.question-text').value;
    const typeId = card.querySelector('.question-type').value;
    if (!questionText.trim() && typeId !== 'document') return;

    const demotedTree = document.createElement('div');
    demotedTree.className = 'question-tree';
    demotedTree.innerHTML = createQuestionCard(level);
    const demotedCard = demotedTree.querySelector('.question-card');
    demotedCard.querySelector('.question-text').value = questionText;
    // setQuestionType first: it populates the app picker once the apps
    // list is loaded, so the slug below lands on real options.
    setQuestionType(demotedCard, typeId);
    if (typeId === 'document') {
        // dataset.appSlug is the pending slug while the apps list is
        // still loading (edit-mode populate); prefer the picked value.
        const parentSelect = card.querySelector('.question-doc-app');
        const slug = parentSelect.value || parentSelect.dataset.appSlug || '';
        const slugSelect = demotedCard.querySelector('.question-doc-app');
        slugSelect.value = slug;
        slugSelect.dataset.appSlug = slug;
        updateDocStatus(slugSelect);
    }

    card.querySelector('.question-text').value = '';
    card.querySelector('.question-type').value = 'free-response';
    // The leaf may have had its doc config on screen ("Embeds X — served
    // live from the registry"); leaving it up reads as a dangling app
    // attachment on a passage/group card (#120), so fold it away and
    // clear the embed state the subpart now owns.
    onQuestionTypeChange(card.querySelector('.question-type'));
    const clearedSelect = card.querySelector('.question-doc-app');
    clearedSelect.value = '';
    delete clearedSelect.dataset.appSlug;
    updateDocStatus(clearedSelect);
    subquestions.insertBefore(demotedTree, subquestions.firstChild);
    setupDragAndDrop(demotedCard);
}

// Same content bar demoteCardContent uses for moving leaf fields down:
// question text or an embedded document app is worth preserving.
function cardHasLeafContent(card) {
    const text = card.querySelector('.question-text').value.trim();
    const typeId = card.querySelector('.question-type').value;
    return !!(text || typeId === 'document');
}

// Merge a subpart's content back into its parent — the reverse of
// demoteCardContent (#120) — so deleting a parent's LAST subpart can
// carry the content up instead of dropping it.
function mergeCardContent(subCard, parentCard) {
    const text = subCard.querySelector('.question-text').value;
    const typeId = subCard.querySelector('.question-type').value;
    const subSelect = subCard.querySelector('.question-doc-app');
    const slug = subSelect.value || subSelect.dataset.appSlug || '';
    const subPrompt = subCard.querySelector('.question-prompt').value;

    // A prompt visible on the group parent is never clobbered; an empty
    // one is filled from the subpart. The parent's question field is
    // hidden (and empty) while grouped, so the subpart's text takes its
    // place outright.
    const parentPrompt = parentCard.querySelector('.question-prompt');
    if (!parentPrompt.value.trim() && subPrompt.trim()) {
        parentPrompt.value = subPrompt;
    }
    parentCard.querySelector('.question-text').value = text;
    // setQuestionType first: it populates the app picker once the apps
    // list is loaded, so the slug below lands on real options.
    setQuestionType(parentCard, typeId);
    if (typeId === 'document' && slug) {
        const parentSelect = parentCard.querySelector('.question-doc-app');
        parentSelect.value = slug;
        parentSelect.dataset.appSlug = slug;
        updateDocStatus(parentSelect);
    }
}

// Add subpart to existing question
function addSubpart(button) {
    const card = button.closest('.question-card');
    let subquestions = card.nextElementSibling;

    if (!subquestions || !subquestions.classList.contains('subquestions')) {
        subquestions = document.createElement('div');
        subquestions.className = 'subquestions';
        card.after(subquestions);
    }

    const newLevel = parseInt(card.dataset.level || 1) + 1;
    demoteCardContent(card, subquestions, newLevel);

    const tree = document.createElement('div');
    tree.className = 'question-tree';
    tree.innerHTML = createQuestionCard(newLevel);
    subquestions.appendChild(tree);

    setupDragAndDrop(tree.querySelector('.question-card'));
    recalculateAllIds();
}

// Remove question (with all of its subparts)
function removeQuestion(button) {
    const card = button.closest('.question-card');
    const tree = card.closest('.question-tree');

    const subquestions = card.nextElementSibling;
    const hasSubparts = subquestions && subquestions.classList.contains('subquestions')
        && subquestions.querySelectorAll('.question-card').length > 0;
    if (hasSubparts && !confirm('Delete this question and all its subparts?')) {
        return;
    }

    // Deleting a parent's LAST subpart turns that parent back into a
    // plain question — offer to carry the subpart's content up with it
    // (the reverse of demoteCardContent) instead of dropping it (#120).
    // Cancel keeps the old behaviour: delete subpart and content.
    const container = tree.parentElement;
    const parentTree = container && container.classList.contains('subquestions')
        ? container.closest('.question-tree') : null;
    const parentCard = parentTree
        ? parentTree.querySelector(':scope > .question-card') : null;
    if (parentCard
            && container.querySelectorAll(':scope > .question-tree').length === 1
            && cardHasLeafContent(card)
            && confirm(`${card.querySelector('.question-id-badge').textContent} `
                + `is the last subpart of `
                + `${parentCard.querySelector('.question-id-badge').textContent}. `
                + 'OK: merge its question, type and app back into the parent '
                + 'and remove the subpart. Cancel: delete it with its '
                + 'content.')) {
        mergeCardContent(card, parentCard);
    }

    if (subquestions && subquestions.classList.contains('subquestions')) {
        subquestions.remove();
    }
    card.remove();

    // Clean up containers left empty up the chain
    let node = tree;
    const topLevel = document.getElementById('questions-container');
    while (node && node !== topLevel) {
        const parent = node.parentElement;
        if (!node.querySelector('.question-card')) {
            node.remove();
        }
        node = parent;
    }

    recalculateAllIds();
}

// Reset the builder to empty
function resetBuilder() {
    document.getElementById('questions-container').innerHTML = '';
}

// Populate the builder from a flat questions array (dotted IDs, document order)
function populateBuilderFromFlat(flatQuestions) {
    resetBuilder();
    const container = document.getElementById('questions-container');
    const stack = [];  // nearest ancestors not yet closed: {depth, card}

    (flatQuestions || []).forEach(q => {
        const depth = (q.id || '').split('.').length;
        while (stack.length > 0 && stack[stack.length - 1].depth >= depth) {
            stack.pop();
        }

        let parentElement = container;
        if (stack.length > 0) {
            const parentCard = stack[stack.length - 1].card;
            let subquestions = parentCard.nextElementSibling;
            if (!subquestions || !subquestions.classList.contains('subquestions')) {
                subquestions = document.createElement('div');
                subquestions.className = 'subquestions';
                parentCard.after(subquestions);
            }
            parentElement = subquestions;
        }

        const tree = document.createElement('div');
        tree.className = 'question-tree';
        tree.innerHTML = createQuestionCard(depth);
        parentElement.appendChild(tree);

        const card = tree.querySelector('.question-card');
        setupDragAndDrop(card);
        card.querySelector('.question-prompt').value = q.prompt || '';
        card.querySelector('.question-text').value = q.question || '';
        const typeId = q.type || 'free-response';
        setQuestionType(card, typeId);
        if (typeId === 'document') {
            const appSelect = card.querySelector('.question-doc-app');
            const slug = (q.data && q.data.slug) || '';
            appSelect.dataset.appSlug = slug;   // kept until apps load
            appSelect.value = slug;
            // Once the options exist, show what's embedded in the status.
            loadAppsAndTrack().then(() => updateDocStatus(appSelect))
                .catch(() => {});
        }

        stack.push({ depth, card });
    });

    recalculateAllIds();
}

// Drag and drop setup
let draggedElement = null;

function setupDragAndDrop(card) {
    // Reorder drags arm per gesture: a card left draggable="true"
    // intercepts text-selection drags inside its own inputs (#121).
    card.addEventListener('mousedown', (e) => {
        card.draggable = !!e.target.closest('.drag-handle');
    });
    card.addEventListener('dragstart', handleDragStart);
    card.addEventListener('dragend', handleDragEnd);
    card.addEventListener('dragover', handleDragOver);
    card.addEventListener('dragenter', handleDragEnter);
    card.addEventListener('dragleave', handleDragLeave);
    card.addEventListener('drop', handleDrop);
}

function handleDragStart(e) {
    draggedElement = this;
    this.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
}

function handleDragEnd(e) {
    this.classList.remove('dragging');
    document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
    draggedElement = null;
    recalculateAllIds();
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
}

function handleDragEnter(e) {
    e.preventDefault();
    const draggedTree = draggedElement && draggedElement.closest('.question-tree');
    if (draggedElement && this !== draggedElement && !(draggedTree && draggedTree.contains(this))) {
        this.classList.add('drag-over');
    }
}

function handleDragLeave(e) {
    this.classList.remove('drag-over');
}

function questionDepth(card) {
    return card.querySelector('.question-id-badge').textContent.split('.').length;
}

function handleDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    this.classList.remove('drag-over');

    if (!draggedElement || this === draggedElement) {
        return false;
    }

    const sourceCard = draggedElement;
    const sourceTree = sourceCard.closest('.question-tree');
    const targetCard = this;
    const targetTree = targetCard.closest('.question-tree');

    // Refuse drops into the dragged question's own subtree
    if (sourceTree.contains(targetCard)) {
        return false;
    }

    const sourceDepth = questionDepth(sourceCard);
    const targetDepth = questionDepth(targetCard);

    if (sourceDepth === targetDepth) {
        // Reorder: take over the target's spot (whole group moves together)
        targetTree.before(sourceTree);
    } else if (sourceDepth < targetDepth) {
        // Demote: become the last subpart of the target — and when the
        // target still holds its own question+type, that content
        // demotes into the first subpart (groups are passage nodes).
        let subquestions = targetCard.nextElementSibling;
        if (!subquestions || !subquestions.classList.contains('subquestions')) {
            subquestions = document.createElement('div');
            subquestions.className = 'subquestions';
            targetCard.after(subquestions);
        }
        demoteCardContent(targetCard, subquestions, targetDepth + 1);
        subquestions.appendChild(sourceTree);
    } else {
        // Promote: follow the target as a sibling at the target's level
        targetTree.after(sourceTree);
    }

    cleanupEmptyContainers();
    recalculateAllIds();
    return false;
}

function cleanupEmptyContainers() {
    document.querySelectorAll('#questions-container .subquestions').forEach(sub => {
        if (!sub.querySelector('.question-card')) {
            sub.remove();
        }
    });
    document.querySelectorAll('#questions-container .question-tree').forEach(tree => {
        if (!tree.querySelector(':scope > .question-card')) {
            tree.remove();
        }
    });
}

// Collect all questions from the tree
function collectQuestions(container) {
    const questions = [];
    const trees = container.querySelectorAll(':scope > .question-tree');

    trees.forEach(tree => {
        const card = tree.querySelector(':scope > .question-card');
        if (!card) return;

        const badge = card.querySelector('.question-id-badge');
        const prompt = card.querySelector('.question-prompt').value.trim();
        const questionText = card.querySelector('.question-text').value.trim();
        const typeId = card.querySelector('.question-type').value;

        const subquestions = card.nextElementSibling;
        const isGroup = !!(subquestions
            && subquestions.classList.contains('subquestions')
            && subquestions.querySelectorAll(':scope > .question-tree').length > 0);

        // Group nodes carry only the passage; the answerable question+
        // type pairs live in their subparts.
        const question = {
            id: badge.textContent,
            prompt: prompt,
            question: isGroup ? '' : questionText
        };
        if (!isGroup && typeId === 'document') {
            question.type = typeId;
            question.data = { slug: card.querySelector('.question-doc-app').value };
        }

        // Collect subquestions
        if (isGroup) {
            const subQuestions = collectQuestions(subquestions);
            if (subQuestions.length > 0) {
                question.subquestions = subQuestions;
            }
        }

        questions.push(question);
    });

    return questions;
}

// Flatten nested questions into a single array.
// IDs from the badges are already full dotted paths (recalculateAllIds runs after every change).
function flattenQuestions(questions) {
    const flat = [];

    questions.forEach(q => {
        const entry = {
            id: q.id,
            prompt: q.prompt,
            question: q.question
        };
        if (q.type) {
            entry.type = q.type;
            entry.data = q.data;
        }
        flat.push(entry);

        if (q.subquestions) {
            flat.push(...flattenQuestions(q.subquestions));
        }
    });

    return flat;
}

// Collect flattened questions for saving; alerts and returns null when invalid
function collectValidatedQuestions() {
    const container = document.getElementById('questions-container');
    const questions = flattenQuestions(collectQuestions(container));

    if (questions.length === 0) {
        alert('Please add at least one question');
        return null;
    }

    // Only leaf questions carry answerable text — group nodes (parents
    // of dotted children) hold just the passage.
    const parents = new Set(questions
        .filter(q => q.id.includes('.'))
        .map(q => q.id.slice(0, q.id.lastIndexOf('.'))));
    const incomplete = questions.find(q => !q.question && !parents.has(q.id));
    if (incomplete) {
        alert(`Question ${incomplete.id} is missing its question text.`);
        return null;
    }

    const badDoc = questions.find(q =>
        q.type === 'document' && !q.data.slug);
    if (badDoc) {
        alert(`Question ${badDoc.id}: no app embedded — click `
              + 'Embed Existing App or Upload App on the question.');
        return null;
    }

    return questions;
}
