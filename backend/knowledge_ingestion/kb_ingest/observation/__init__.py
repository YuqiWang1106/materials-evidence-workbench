"""A private, read-only timeline of one document-processing attempt.

The pipeline appends allowlisted events (stages, tables, windows, model calls, reader steps) to a journal beside
the attempt's artifacts; a snapshot projects that journal and the attempt's own records for the task page. It is a
diagnostic side channel: it takes no part in model dispatch, request settlement, cancellation or publication.
"""
