# Investigating reviews and reputation

Read after the brief in `../SKILL.md`. Objective `investigate`. Use it when the
question is about one place or a few, or about a suspected group of them.

## Start with the limits
Google returns at most five reviews per place. State the sample size before any
finding. Signals from under 20 reviews are low confidence by the tool's own rule.

## Method
1. Get the places into a run (`maps search` by name, or their competitors by
   category), then `maps reviews fetch RUN`.
2. `maps reviews integrity RUN`. Read each signal against an innocent reading:
   - a burst of reviews: also an opening, a promotion, a news story;
   - repeated text: also short stock phrases, and translations of the same idiom;
   - many single-review authors: also a new business with real first customers;
   - rating far above the sample: also an unlucky sample of five.
3. `maps reviews contributors RUN`: reviewers shared between competing places
   are the strongest sign of a ring, and the rarest to see in small samples.
4. More reviews per place or reviewers' other reviews need a conditional
   provider that the owner has approved (`docs/account-protection.md`). Without
   one, say what the sample cannot show.

## Report
A table: signal, value, what it would mean, the innocent reading. Conclude in
the form "consistent with", "not consistent with" or "not enough to say".
Never write that reviews are fake. Point the owner to what would settle it:
reading the place's reviews on Maps sorted by newest and by lowest.

## Preferences worth recording
None. Record the finding as a case study instead.
