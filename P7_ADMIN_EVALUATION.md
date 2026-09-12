# P7 — Admin WER/CER evaluation

Status: complete on 2026-09-12

## Important rule

Every evaluation fixes both sides of the comparison permanently: one immutable machine transcript
M and one admin-approved corrected version V, including its exact annotation revision and content
fingerprint. Reopening the source cannot alter that approved V. A later correction is saved as a new
V, and evaluating it creates a new report rather than changing history.

## Metric policy

- Machine and corrected text are grouped by connected time ranges, not mutable segment IDs. This
  keeps results stable when a reviewer splits or joins transcript segments.
- Reports include substitutions, insertions, deletions, reference denominators, error rates, and
  evaluated time coverage for both WER and CER.
- Strict v1 applies Unicode NFC, case folding, and whitespace normalization.
- Arabic-normalized v1 additionally removes Arabic diacritics and tatweel, normalizes common alef
  forms and alif maqsura, and removes punctuation. CER excludes whitespace.
- The chosen overlap policy—deduplicate repeated text, include both speakers, or exclude overlap
  ranges—is stored in the report. Speaker and verified-only scope are stored too.

## Admin workflow

- The Evaluations page is visible only to administrators.
- An administrator chooses an available M/V pair, normalization, overlap treatment, and speaker
  scope, then previews metrics and time-aligned differences.
- Clicking a difference auditions its exact media range.
- Saving creates an immutable report with downloadable JSON and permanent provenance.
- Opening the source for further correction preserves the evaluated approved V; the next Save creates
  or updates an unapproved successor under the P5 saving policy.

## Retention relationship

Archive or cleanup remains an explicit action on the Approvals page. An annotation revision used by
an evaluation report is registered as a permanent reference, so P5 retention cannot remove its
payload. The recommended verified-backup plus 10-day archive policy therefore remains safe without
keeping unrelated recoverable generations forever.

## Verification

Metric fixtures cover exact edit counts, Arabic normalization, temporal split/join behavior, overlap
exclusion, and coverage. Persistence fixtures confirm the exact M/V provenance and retention
reference. Admin-only API authorization is enforced by the shared administrator guard.
