# Eval datasets

Synthetic, labeled data for `pulse eval` (implementation plan, Phase 8). **Nothing here is real.** Every
review, sentence, name, phone number, email address and id was written for this project, from templates in
`make_datasets.py`. No text was copied from real Play Store reviews.

| File | Rows | What it is |
|------|------|------------|
| `reviews_labeled.jsonl` | ~290 | Groww-style reviews `{id, rating, text, date, lang, gold_theme}`. `gold_theme` is one of the taxonomy hints (`src/groww_pulse/themes/taxonomy.py`) or `other`. The mix is uneven on purpose: about 35% *App Performance & Bugs*, 10% Hindi (`hi`) / Hinglish (`hi-Latn`), 5% noise ("good", "👍"), 5% near-duplicates (`dup*` ids), plus a few off-topic reviews. Dates fall in the 10-week window that ends in 2026-W43. |
| `pii_seeded.jsonl` | ~215 | Sentences `{text, spans: [{start, end, type}]}` with seeded PII of the types `email`, `phone`, `pan`, `aadhaar`, `upi`, `account` and `name`. Indian formats (`+91 98765 43210`, `098765-43210`, Aadhaar with and without spaces, `name@okhdfcbank`) and Indian names in English script. About 40 **negatives** have no spans and must survive scrubbing unchanged (`5 days`, `₹5000`, `version 7.12.3`). |
| `injection.jsonl` | ~40 | Reviews that carry instructions (`injection` = `ignore_rules`, `add_url`, `write_hindi`, `name_person`, `five_actions`, `format_break`, `role_play`), mixed with normal reviews (`injection: null`). |

## Regenerating

```bash
python evals/datasets/make_datasets.py
```

The generator uses a fixed seed, so it reproduces the committed files byte for byte. Commit the script and
the regenerated files together.

## Adding cases

- **A new review theme or phrasing:** add phrases to `THEMES` (or a new theme that is also in `TAXONOMY`), then
  regenerate. `tests/test_eval_datasets.py` checks the mix, the schema and that the labeled set holds no PII
  according to the scrubber.
- **A PII format the scrubber missed:** add a template to `PII_TEMPLATES` or a generator to `ENTITY`, or a
  fixed sentence next to the "formats the plan names" block. Keep the value invented. Spans are computed from
  the inserted value, so they stay correct.
- **A false positive:** add the sentence to `NEGATIVES`.
- **A new injection:** add it under a kind in `INJECTIONS`. Use invented names and `.example` domains only.

Real reviewer text, or real personal data, must never be added here.
