from sqlalchemy import Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.types import TypeDecorator


class LongText(TypeDecorator):
    """LONGTEXT on MariaDB, TEXT on SQLite.

    SQLite has no LONGTEXT and does not enforce TEXT length, so TEXT
    handles arbitrarily large JSON blobs there.  On MariaDB this renders
    as LONGTEXT, matching migration 0003.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(LONGTEXT())
        return dialect.type_descriptor(Text())
