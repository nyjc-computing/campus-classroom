# Feedback Design Taxonomy for Constructed-Response Assessment Platform

## Overview

This taxonomy provides a framework for designing feedback mechanisms in a Google Classroom Add-on focused on constructed-response questions. Options are organized by dimension, with research-backed guidance on when each option is most effective.

---

## 1. Feedback Timing

### 1.1 Timing Options

| Option | Definition | Implementation |
|--------|------------|----------------|
| **Immediate** | Feedback delivered within seconds of submission | Auto-generated on submit |
| **Brief delay** | Feedback delivered after 1-24 hours | Queued for teacher review or scheduled release |
| **End-of-session** | Feedback delivered after completing a set of questions | Batch feedback after assignment completion |
| **Delayed** | Feedback delivered after 24+ hours | Teacher-reviewed, scheduled release |
| **Staged** | Immediate confirmation + delayed elaboration | Hybrid: auto "correct/incorrect" + teacher comments later |

### 1.2 Timing Selection Matrix

| Context | Recommended Timing | Rationale |
|---------|-------------------|-----------|
| Factual recall / simple extraction | Immediate | Prevents error consolidation |
| Procedural practice (calculations, algorithms) | Immediate | Real-time correction aids skill building |
| Complex reasoning / analysis | Brief delay (1-24h) | Allows reflection before feedback |
| Transfer-focused learning | Delayed (24-48h) | Spaced retrieval enhances long-term retention |
| First exposure to concept | Immediate | Error correction critical early |
| Repeated practice on same concept | Delayed | Builds self-regulation |
| Homework/practice context | Immediate | Students engage more; prevents disengagement |
| High-stakes preparation | Staged | Immediate confirmation + detailed review later |

### 1.3 Timing Constraints

- **Maximum delay before motivation drops:** 10 days
- **Optimal window for learning impact:** 24-48 hours
- **Classroom vs lab effect:** Immediate works better in authentic settings where engagement with delayed feedback is not enforced

---

## 2. Feedback Format

### 2.1 Location/Placement Options

| Option | Definition | Best For |
|--------|------------|----------|
| **In-line annotation** | Comments anchored to specific text/response segments | Identifying specific errors; writing tasks |
| **Margin comments** | Comments alongside response, linked to regions | Longer responses; multiple issues |
| **Summary comment** | Single holistic comment at end of response | Overall performance; feed-forward guidance |
| **Hybrid** | In-line annotations + summary | Comprehensive feedback on substantial responses |
| **Overlay/highlight only** | Visual marking without text comment | Quick identification; indirect feedback |

### 2.2 Format Selection Matrix

| Response Type | Recommended Format | Notes |
|---------------|-------------------|-------|
| Short answer (1-2 sentences) | Summary comment | In-line overkill for brief responses |
| Extended response (paragraph+) | Hybrid (in-line + summary) | In-line for specifics, summary for synthesis |
| Fill-in-blank / cloze | Immediate inline | Direct positioning at error location |
| Multi-part question | Per-part annotation + overall summary | Clear correspondence between feedback and parts |
| Code/technical response | In-line with line numbers | Precision critical for debugging |

### 2.3 Modality Options

| Option | Pros | Cons | Best For |
|--------|------|------|----------|
| **Text** | Precise, referenceable, student can re-read | Time-consuming to write | Default for most contexts |
| **Audio** | 3x faster to produce; personal tone | Harder to reference during revision | Complex feedback; relationship-building |
| **Video** | Demonstrates process; highly personal | Production overhead; harder to skim | Worked examples; procedural feedback |
| **Structured rubric** | Consistent, scalable, transparent criteria | Less personalized | Standardized assessments |
| **Adaptive/templated** | Efficient; consistent quality | May feel impersonal | High-volume, pattern-based errors |

---

## 3. Feedback Type (Directness Spectrum)

### 3.1 Directness Options

| Type | Definition | Example | Cognitive Demand on Student |
|------|------------|---------|----------------------------|
| **Direct correction** | Provides the correct answer/form | "The answer is 42" | Low |
| **Direct + metalinguistic** | Correction with explanation of why | "The answer is 42 because velocity = distance/time" | Low-Medium |
| **Directive** | Tells student what to do without giving answer | "Recalculate using the correct formula" | Medium |
| **Indirect - coded** | Marks error with code/symbol indicating error type | "[CALC]" or "⚠️ formula error" | Medium-High |
| **Indirect - locating only** | Highlights/underlines error without explanation | Yellow highlight on incorrect segment | High |
| **Interactive/questioning** | Prompts reflection without indicating error | "What assumption did you make about the initial velocity?" | Highest |

### 3.2 Directness Selection Matrix

| Student Proficiency | Error Type | Recommended Directness |
|--------------------|------------|----------------------|
| Novice | First encounter with concept | Direct + metalinguistic |
| Novice | Repeated error (same type) | Directive |
| Intermediate | Surface-level error (typo, minor) | Indirect - locating |
| Intermediate | Conceptual error | Directive or Interactive |
| Advanced | Any error type | Indirect or Interactive |
| Any | Pattern across multiple responses | Direct correction on first instance, indirect on subsequent |

### 3.3 Error Type Mapping

| Error Category | Effective Feedback Type |
|---------------|------------------------|
| **Factual/content error** | Direct correction + brief explanation |
| **Procedural error** | Directive with strategy hint |
| **Conceptual misconception** | Interactive questioning + metalinguistic explanation |
| **Structural/organizational** | Directive or suggestion |
| **Missing element** | Direct indication of what's missing |
| **Incomplete reasoning** | Interactive questioning |

---

## 4. Feedback Content (Hattie-Timperley Framework)

### 4.1 Feedback Levels

| Level | Focus | Example | Effectiveness |
|-------|-------|---------|---------------|
| **Task** | Correctness of this specific response | "Your calculation is incorrect" | Useful but limited transfer |
| **Process** | Strategies used to complete task | "Try breaking this into smaller steps first" | High - promotes learning |
| **Self-regulation** | Student's monitoring and self-assessment | "How confident were you before checking? What will you do differently next time?" | Highest - builds independence |
| **Self** | Personal attributes of student | "You're so smart!" | Ineffective/potentially harmful |

### 4.2 Three Questions Framework (Feed Up / Feed Back / Feed Forward)

| Component | Question Answered | Example |
|-----------|------------------|---------|
| **Feed Up** | "Where am I going?" | "The goal is to explain the relationship between pressure and volume" |
| **Feed Back** | "How am I going?" | "You've correctly identified the inverse relationship but haven't explained the molecular mechanism" |
| **Feed Forward** | "Where to next?" | "Try describing what happens to gas molecules when volume decreases" |

**Critical insight:** Feed Forward is most desired by students and has highest impact, but is least commonly provided.

### 4.3 Content Structure Template

```
[WHAT WORKED] Specific identification of what was done well and why
[WHAT'S MISSING/INCORRECT] Specific identification of gap or error
[WHY IT MATTERS] Brief explanation connecting to learning goal
[NEXT STEP] Concrete, actionable guidance for improvement
```

---

## 5. Feedback Scope

### 5.1 Scope Options

| Option | Definition | Trade-off |
|--------|------------|-----------|
| **Comprehensive** | Address all errors/issues | Thorough but overwhelming; time-intensive |
| **Selective/focused** | Address 2-5 priority issues | Manageable for student; may miss important errors |
| **Targeted** | Address single pre-specified skill/concept | Efficient; clear focus; ignores other issues |
| **Progressive** | Comprehensive initially, increasingly selective | Builds independence over time |

### 5.2 Scope Selection Matrix

| Context | Recommended Scope |
|---------|------------------|
| Formative practice (low stakes) | Selective (2-3 key issues) |
| Summative assessment | Comprehensive (but prioritized) |
| Skill-building sequence | Targeted (one skill per round) |
| Revision cycle | Progressive (comprehensive → selective) |
| Time-constrained teacher | Targeted or selective with rubric |

---

## 6. Tone and Language

### 6.1 Tone Options

| Tone | Characteristics | Effect on Students |
|------|-----------------|-------------------|
| **Neutral-informational** | Factual, objective, neither praising nor criticizing | Safe default; clear communication |
| **Warm-supportive** | Encouraging, acknowledging effort | Builds confidence; may lack clarity |
| **Coaching** | Growth-oriented, "we" language, collaborative | Builds relationship; may be too informal |
| **Evaluative** | Judgmental language ("good," "poor," "weak") | Clear standards; can demotivate |

### 6.2 Language Guidelines

**Effective feedback language:**
- Specific and concrete (not vague: "good job")
- Action-oriented (verbs: "revise," "consider," "try")
- Linked to criteria/standards
- Neutral-to-positive in tone
- Avoids rhetorical questions (students interpret as criticism, not genuine questions)

**Avoid:**
- Generic praise without specifics ("Great work!")
- Negative personal attributions ("You didn't understand...")
- Overwhelming quantity
- Jargon without explanation

---

## 7. Automation Considerations

### 7.1 Automation Spectrum

| Level | Description | Feasibility | Quality |
|-------|-------------|-------------|---------|
| **Fully automated** | AI/NLP generates all feedback | High volume; scalable | Variable; may miss nuance |
| **Assisted** | AI drafts feedback; teacher reviews/edits | Balanced efficiency/quality | Good with human oversight |
| **Templated** | Pre-written feedback mapped to common patterns | Fast; consistent | Limited personalization |
| **Rubric-driven** | Auto-score against rubric; teacher adds comments | Efficient scoring; personal comments | Hybrid quality |
| **Fully manual** | Teacher writes all feedback | Time-intensive | Highest personalization |

### 7.2 What Can Be Automated Effectively

| Component | Automation Potential | Notes |
|-----------|---------------------|-------|
| Correctness verification | High | For responses with determinable right answers |
| Keyword/concept detection | High | Checking for required elements |
| Pattern-based error identification | Medium-High | Common errors have recognizable signatures |
| Task-level feedback | Medium | "Your answer includes X but is missing Y" |
| Process-level feedback | Low-Medium | Requires inference about student strategy |
| Self-regulation prompts | Medium | Can be templated: "How confident were you?" |
| Feed-forward suggestions | Low-Medium | Often requires understanding student's specific gap |

### 7.3 Hybrid Approach Recommendation

| Phase | Automation | Human |
|-------|------------|-------|
| **Immediate confirmation** | ✓ Correct/incorrect signal | |
| **Element identification** | ✓ What's present/missing | |
| **Error classification** | ✓ Common patterns | ✓ Novel errors |
| **Task feedback** | ✓ Template-based | ✓ Review/customize |
| **Process feedback** | | ✓ Teacher expertise |
| **Feed-forward** | ✓ Draft suggestions | ✓ Personalize |

---

## 8. Question Type × Feedback Design Matrix

### 8.1 Recommended Configurations by Question Type

| Question Type | Timing | Format | Directness | Content Focus |
|--------------|--------|--------|------------|---------------|
| **Fill-in-blank** | Immediate | Inline | Direct | Task |
| **Short answer (factual)** | Immediate | Summary | Direct + metalinguistic | Task |
| **Short answer (reasoning)** | Staged | Summary | Directive | Process |
| **Extended response** | Brief delay | Hybrid | Interactive | Process + Self-regulation |
| **Multi-step problem** | Staged | Per-step inline | Progressive (direct → directive) | Process |
| **Code/programming** | Immediate (syntax) + Delayed (logic) | Inline with line refs | Direct (syntax), Interactive (logic) | Process |
| **Diagram/drawing** | Brief delay | Overlay + summary | Directive | Task + Process |
| **Ordering/sequencing** | Immediate | Visual + summary | Direct | Task |

---

## 9. Implementation Priority Recommendations

### 9.1 MVP Features (Essential)

1. **Timing control** - Allow teachers to set immediate vs delayed release
2. **Basic annotation** - Ability to highlight and comment on specific parts
3. **Summary comment field** - Always available regardless of annotations
4. **Feed-forward prompt** - Template or reminder to include "next steps"
5. **Correctness indicator** - Simple correct/incorrect/partial signal

### 9.2 Phase 2 Features (High Value)

1. **Staged feedback** - Immediate confirmation + delayed elaboration workflow
2. **Feedback templates** - Pre-written common feedback mapped to error patterns
3. **Rubric integration** - Score against criteria with attached comments
4. **Scope limiting** - "Focus on top 3 issues" mode
5. **Feedback preview** - Teacher sees aggregate student responses before writing feedback

### 9.3 Phase 3 Features (Advanced)

1. **Automated element detection** - NLP checks for required concepts
2. **Pattern recognition** - Flag common errors across class
3. **Feedback analytics** - Track which feedback types lead to improvement
4. **Student feedback engagement** - Track whether students view/engage with feedback
5. **Adaptive directness** - Automatically adjust based on student proficiency signals

---

## 10. Validation Metrics

### 10.1 Feedback Quality Indicators

| Metric | How to Measure | Target |
|--------|----------------|--------|
| **Specificity** | % of feedback with concrete examples/references | >80% |
| **Actionability** | % with clear next steps | >70% |
| **Feed-forward presence** | % containing "where to next" guidance | >60% |
| **Student engagement** | % of feedback viewed; time spent | >90% view rate |
| **Revision impact** | Score change after feedback on similar items | Positive delta |

### 10.2 System-Level Metrics

| Metric | Purpose |
|--------|---------|
| Time to feedback delivery | Ensure within optimal window |
| Teacher feedback creation time | Efficiency of interface |
| Feedback completion rate | Teacher adoption |
| Student improvement on repeated concepts | Learning impact |
| Student help-seeking after feedback | Self-regulation development |

---

## References

Key sources informing this taxonomy:
- Hattie & Timperley (2007) - Power of Feedback; three questions framework
- Shute (2008) - Focus on Formative Feedback
- Wisniewski et al. (2020) - Meta-analysis on feedback
- Butler et al. (2007) - Type and timing of feedback
- Kulik & Kulik (1988) - Meta-analysis on feedback timing
- Ellis (2009) - Written corrective feedback typology
- Van der Kleij et al. (2015) - Feedback in computer-based environments
