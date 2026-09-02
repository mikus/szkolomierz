"""Pure logic shared by the analysis notebook and the standalone scripts.

Only logic with no I/O belongs here. The end-to-end data checks live in
`scripts/validate_export.py`, which deliberately re-derives everything
independently and must NOT import this package — that independence is what makes
it a real check rather than a restatement of the pipeline.
"""
