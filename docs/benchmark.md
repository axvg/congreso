# GovTrack benchmark

Reverse-engineered from live pages on 2026-08-14. This is the bar. The rubric at
the bottom is what the critic applies to two unlabelled pages.

## Page inventories

### Bill page — `/congress/bills/{congress}/{type}{number}`

Blocks in order: breadcrumb · H1 (number + short title) · track/contact/list actions ·
position widget (6-point support scale) · tab bar (tabs *disappear* when the data
is absent) · official long title · **sponsor card** (photo, party, district in
words) + bill-text thumbnail + length in pages · property panel (Introduced /
Status / Law / Cosponsors with party split / Prognosis / Source) · position
statements · incorporated legislation with % overlap · **History** table where
every row carries a one-sentence explanation of what that stage means and its base
rate · **Next steps** · citation generator · provenance ("updated one day after
events occur").

Sub-tabs: Summary (CRS, with attribution and date) · Cosponsors (party split
headline, sort by name/date/committee, `Original Cosponsor` distinction,
withdrawals, committee role per cosponsor, **plus a second table showing which
relevant chairs/ranking members did *not* sign on**) · Details (committees, votes,
related bills with relation type, subject areas) · Text (every version by date and
stage, compare-to-previous, compare-to-another-bill, on-site HTML) · Study Guide.

Low-activity bill: tabs shrink, `Text: Not available yet.`, prognosis still
renders, Next steps carries the weight. ~8 populated blocks with one action.
Nothing renders as a blank card.

### Legislator page — `/congress/members/{slug}/{id}`

Header with pronunciation · bio paragraph (tenure, next election, age, caucuses,
outbound links) · **contact decision tree** (constituent / not / unsure) ·
ideology–leadership scatter of the whole chamber · committee membership with role
badges · enacted legislation count + "does 16 not sound like a lot?" context +
published definition of enacted · issue-area mix as percentages linking facets ·
recent bills · key votes with position and outcome · **missed votes phrased
comparatively** ("1.0%… better than the median of 2.0%") with percentile bands and
a numbers table · primary sources.

### Roll-call vote page — `/congress/votes/{congress}-{year}/{chamber}{num}`

Breadcrumb · alerts/compare · H1 = the question voted on · date + "On the
Amendment in the House" · **explainer prose that decodes the procedure** · tally
table (totals, %, per-party columns, outcome, **threshold required**) · seat
diagram ordered by ideology · selected caucuses · population representativeness
(Senate) · cartogram hexmap (House) · procedural notes · **CSV export** ·
**statistically notable votes** (defectors, above the full roll) · full roster ·
study guide.

Roster mechanics worth copying: columns Vote / District / Party / Legislator;
clicking a header re-sorts with multi-key secondary sorts (Party sorts internally
by ideology, so defectors cluster at the ends); group header rows are inserted
automatically to follow the sort.

## MUST for v1

1. Plain-language status sentence, not a status code.
2. Action history where every row is annotated with what the stage means.
3. Explicit next-steps list, present even on a bill with one action.
4. Sponsor card: photo, party, district in words; name links everywhere.
5. Cosponsor table: party split, join dates, original-vs-later, committee role.
6. Full roll-call roster, sortable on every column, with grouping that follows sort.
7. Tally by party with percentages, outcome, and threshold required.
8. Explainer prose that decodes the procedure being voted on.
9. Stable guessable permalinks.
10. Bidirectional linkage bill ⇄ vote ⇄ legislator ⇄ subject.
11. Provenance with the upstream source and its lag.
12. Full text on-site as HTML, every version listed by date and stage.
13. Graceful degradation: sections omitted rather than rendered empty.
14. CSV export of the roll call.
15. Faceted search driving the same URL params the on-page links use.

SHOULD: comparative missed-vote stat · enacted count with context · related bills ·
subject tags · text diff · alerts/RSS · prognosis with factors · who-didn't-cosponsor ·
statistically notable votes.

## Where GovTrack is beatable

1. **No visual progress tracker on the bill page.** Status is a text row; future
   stages are literally a run of em-dashes. The only graphical status artifact is
   the embeddable widget offered to *other* sites. A stepper with completed /
   current / remaining states and dates on completed nodes is an immediate win —
   more so for a bicameral chamber (Diputados → Senado → autógrafa →
   observación/promulgación).
2. **The roster is a jQuery DOM sort cloned into three fake columns on every sort
   and every resize.** No filter box, no search by name, no party filter, no
   sticky header, and sort state is not in the URL — you cannot link someone to
   "this vote sorted by party".
3. **The homepage is nearly dead.** One editorial teaser, two votes, six
   unlabelled "trending" links. "Coming up" — the most valuable module for a
   legislature tracker — is not on it. Nothing answers "what is Congress doing
   right now".
4. **Model outputs presented as authoritative bare numbers.** "30% chance of being
   enacted" with no interval; the ideology chart's axes are deliberately
   unlabelled while a raw float leaks into a hidden sort column.
5. **Bill text is a 1.7 MB single-page dump.** No table of contents, no section
   anchors, no in-text search, no change summary on the diff.

## Blind-comparison rubric

Two unlabelled pages of the same type. 0 / 1 / 2 each. Criteria 1, 2, 5, 6, 7
count double.

| # | Criterion | 0 | 1 | 2 |
|---|---|---|---|---|
| 1 | Status legible without scrolling: current stage *and* what happens next | neither | one | both, remaining stages as distinct visual states |
| 2 | Action/status items carrying a one-sentence explanation | none | some | every row |
| 3 | Of 5 named entities, how many link to a page on this site | ≤1 | 2–3 | 4–5 |
| 4 | Provenance named, dated, linked, lag stated | absent | named | named + linked + last-updated |
| 5 | Empty-state blocks on the low-activity example | ≥2 blank | 1 | 0 blank; omitted or explained |
| 6 | Roll-call table affordances (per-column sort, group headers, text filter, party filter, find-my-legislator, sticky header, URL state) | ≤2 | 3–4 | ≥5 with URL-persisted state |
| 7 | Tally shows totals, %, per-party, absences, threshold, outcome | ≤2 of 6 | 3–4 | 5–6 |
| 8 | Identify members who broke with their bloc without reading the roster | no | via sort | explicit outliers block or ordered visual |
| 9 | Statistics given a peer baseline rather than bare | 0 | 1–2 | ≥3 |
| 10 | Machine-readable export + clean guessable URL | neither | one | both + stated citation |
| 11 | Primary document: on-site HTML, sectioned, anchored, versioned, diffable | PDF link only | on-site, no nav | all four |
| 12 | At 375px: horizontal overflow, pinch-zoom tables, sub-44px targets | ≥3 problems | 1–2 | none |

Tie-break within 2 points: **time-to-first-fact** — read for 10 seconds and write
the single most important fact. The page where it was in the first viewport wins.
