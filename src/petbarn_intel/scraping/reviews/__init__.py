"""Bazaarvoice review/Q&A sampling for the seed set.

Deliberately a *sample*, not a full sync (explicit user decision -- see the
plan's "do not limit... reviews" discussion resolved down to a stratified
cap via `Settings.reviews_per_product`, default 150): recent + low-rated +
most-helpful buckets, deduplicated by review id and tagged with
`sample_reasons`. `reviews_repo.review_stats()` always computes its
distribution/average over every review actually stored, so the sample
never silently biases the numbers an agent reports -- it only bounds how
much *text* gets pulled in for qualitative summarization.
"""
