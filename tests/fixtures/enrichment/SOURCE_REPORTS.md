# Synthetic report excerpts for deterministic review

These are invented test inputs, not published studies or medical evidence. The
example.org URLs in the JSON fixture identify these synthetic documents; they
are not claims that external reports exist. No real full text is hosted.

## Report 1, version 1 — fixture DOI 10.0000/fixture1

Identity section: registry identifiers NCT-FIXTURE and WHO-FIXTURE describe the
same investigation S. Methods: children under five; education arm receives a
weekly nutrition education session, comparator receives usual practice. Protocol
summary: height measurement was planned.

Table 2, row 1: all children, 12 months after randomization. Stunting prevalence
(HAZ < -2), education minus usual practice: -1.2 percentage points, 95% confidence
interval [-3.2, 0.8]. Difference reported as not statistically significant.

## Report 2, version 1 — fixture DOI 10.0000/fixture2

Identity section: follow-up of investigation S (NCT-FIXTURE; WHO-FIXTURE).
Table 2, row 2: all children, 24 months after randomization. Stunting prevalence
(HAZ < -2), education minus usual practice: -1.2 percentage points, 95% confidence
interval [-3.2, 0.8]; reported not statistically significant.
Table 2, row 3: girls, 24 months after randomization; same comparison and measure:
0 percentage points, 95% confidence interval [-3.2, 0.8]; reported not statistically
significant. This exact zero is distinct from rows 1–2 and from unavailable data.

## Mutation scenarios used in tests

A corrected row 1 changes its estimate to -0.5 with an explicit revision link.
Withdrawals label each affected version retracted; independent follow-up support
survives unless its version is also withdrawn. A multi-source finding is withheld
until a reviewer verifies that the remaining source supports every field. A
second synthetic investigation is explicitly added to the document identity
section for the one-paper/two-studies case; it is never inferred from the title.
