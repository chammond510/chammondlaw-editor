# Exemplar Style Anchors

## Purpose

The exemplar system now distinguishes between three roles:

- `matter_exemplar`
  Prior filings and work product used for substance, research context, and drafting examples.
- `style_anchor`
  A canonical formatting and structure master used to drive export fidelity.
- `section_template`
  Reusable blocks such as signature blocks, exhibit lists, or standard opening sections.

## USCIS Cover Letter Anchor

The current default USCIS cover-letter style anchor is the DOCX file in:

- `/Users/chrishammond/chammondlaw-editor/Exemplars/Cadeau I-612 Cover Letter.docx`

If no user-owned database style anchor exists, DOCX export for `cover_letter` documents falls back to that filesystem exemplar automatically.

## Export Behavior

For `cover_letter` exports, the app now:

1. Resolves a `style_anchor` for the current user and document.
2. Opens the exemplar DOCX as the export template.
3. Preserves the exemplar's page setup, styles, and footer/page numbering.
4. Rebuilds the letter body using exemplar-derived formatting samples.
5. Renders exhibit lists as a stable two-column exhibit table.
6. Appends the signature block using exemplar font styling and left-aligned closing-block layout.

Non-cover-letter exports still use the generic `python-docx` renderer.

## Importing Exemplars

To import local files from the `Exemplars/` folder into the exemplar bank for a real app user:

```bash
./.venv/bin/python manage.py import_exemplars --username YOUR_USERNAME --default
```

That command imports the local files as `style_anchor` exemplars for the `uscis_cover_letter` family by default.

## Template Seeding

`seed_templates` now gives generated USCIS cover-letter document types a structure closer to the master exemplar:

- mailing method
- date
- USCIS address block
- `RE:` line
- party-identification lines
- salutation
- sectioned body
- exhibit categories
- closing line

