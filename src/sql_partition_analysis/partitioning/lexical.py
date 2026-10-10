"""PostgreSQL token boundaries shared by parsing and evidence recovery.

The pinned SQLGlot tokenizer probes a dollar-quote tag before falling back to
a positional parameter. Dollar-quote tags have no quote-escape processing; an
apostrophe pair later in ``$1, ''`` must not raise a missing-dollar error during
that speculative probe. Keep source text and token offsets unchanged.
"""
from sqlglot.dialects.postgres import Postgres


class LexicalPostgres(Postgres):
    class Tokenizer(Postgres.Tokenizer):
        STRING_ESCAPES_ALLOWED_IN_RAW_STRINGS = False


def tokenize_postgres(text):
    return LexicalPostgres().tokenize(text)
