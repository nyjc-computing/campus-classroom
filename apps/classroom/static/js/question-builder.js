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

// Recalculate all question IDs from the tree structure
function recalculateAllIds() {
    document.querySelectorAll('#questions-container > .question-tree').forEach((tree, treeIndex) => {
        updateTreeIds(tree, generateQuestionId(1, treeIndex));
    });
}

// Set the tree's own question ID, then recurse into its subquestions
function updateTreeIds(tree, id) {
    const card = tree.querySelector(':scope > .question-card');
    if (!card) return;

    card.querySelector('.question-id-badge').textContent = id;
    card.dataset.level = id.split('.').length;

    const subquestions = card.nextElementSibling;
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
        <div class="question-card" draggable="true" data-level="${level}">
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
                <textarea class="form-control question-prompt" rows="2" placeholder="Context or passage for the question..."></textarea>
            </div>
            <div class="mb-0">
                <label class="form-label small text-muted">Question <span class="text-danger">*</span></label>
                <textarea class="form-control question-text" rows="2" required placeholder="Enter your question here..."></textarea>
            </div>
        </div>
    `;
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

        stack.push({ depth, card });
    });

    recalculateAllIds();
}

// Drag and drop setup
let draggedElement = null;

function setupDragAndDrop(card) {
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
        // Demote: become the last subpart of the target
        let subquestions = targetCard.nextElementSibling;
        if (!subquestions || !subquestions.classList.contains('subquestions')) {
            subquestions = document.createElement('div');
            subquestions.className = 'subquestions';
            targetCard.after(subquestions);
        }
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

        const question = {
            id: badge.textContent,
            prompt: prompt,
            question: questionText
        };

        // Collect subquestions
        const subquestions = card.nextElementSibling;
        if (subquestions && subquestions.classList.contains('subquestions')) {
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
        flat.push({
            id: q.id,
            prompt: q.prompt,
            question: q.question
        });

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

    const incomplete = questions.find(q => !q.question);
    if (incomplete) {
        alert(`Question ${incomplete.id} is missing its question text.`);
        return null;
    }

    return questions;
}
