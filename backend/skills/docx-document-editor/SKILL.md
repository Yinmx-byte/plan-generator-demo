---
name: docx-document-editor
description: Edit or revise an already generated maintenance-plan DOCX by preserving unchanged sections and updating only the requested personnel, risks, implementation steps, rollback steps, scripts, or evidence.
---

# DOCX Document Editor Skill

Use this skill when the user asks to modify, revise, replace, update, or re-evaluate an already generated maintenance plan document.

## Principles

1. Treat the previous document text as the baseline.
2. Preserve all sections and content that the user did not ask to change.
3. Apply the user's revision request precisely.
4. If the user asks to change personnel, update the implementation plan/personnel table and keep the rest stable.
5. If the user asks to re-evaluate risks according to another document or new evidence, update only the risk assessment, pre-control measures, emergency handling, implementation checks, and rollback constraints that are affected.
6. If the user asks to update scripts, keep the script safe, auditable, and aligned with the selected maintenance type skill and RAG evidence.
7. Output the same JSON document schema expected by the document renderer. Do not output Markdown or explanatory prose.

## Required Output

Return one JSON object with a `document` field. `document.sections` must contain the complete revised document, not only the changed fragment, because the renderer creates a complete DOCX from the JSON.

Include an `evidence` object with:

- `revision_mode`: true
- `changed_parts`: list of section names or field names changed
- `preserved_parts`: short list of major sections intentionally preserved
- `revision_request`: the user's edit request

