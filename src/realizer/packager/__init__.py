"""Packager capability: the append-only realization ledger and the snapshot projection (extracted phase by phase).

So far only the read side of the ledger lives here: the path layout, loading and the latest-event lookup the Analyzer
needs to know the history a run starts from.
"""
