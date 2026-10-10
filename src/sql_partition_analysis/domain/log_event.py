"""Source-independent log contract; one event is one source occurrence."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LogEvent:
    sql_text: str
    raw_sql: str
    event_time: str
    user_name: str
    statement_type: str
    source_record: int
    engine_type: str = 'adb_pg'
    instance_id: str = ''
    current_database: str = ''
    current_schema: str = ''
    session_id: str = ''
    query_id: str = ''
    execution_status: str = 'unknown'
    source_width: int = 0

    @property
    def analysis_identity(self):
        # Query IDs identify executions; session and name-resolution context
        # must remain separate when future adapters provide them.
        return (self.sql_text, self.user_name, self.engine_type, self.instance_id,
                self.current_database, self.current_schema, self.session_id)
