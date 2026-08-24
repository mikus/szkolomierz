"""Pure functions extracted from the analysis notebook so they can be unit-tested.

Only logic with no I/O belongs here. The end-to-end data checks live in
`scripts/validate_export.py`, which deliberately re-derives everything
independently and must NOT import this package — that independence is what makes
it a real check rather than a restatement of the pipeline.
"""
