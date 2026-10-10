"""Shared profiling state preserving every source occurrence."""
import collections

class LogProfileState:
    def __init__(self, source, header=None):
        self.source = source
        self.header = list(header) if header is not None else []
        self.rows = 0
        self.widths = collections.Counter()
        self.types = collections.Counter()
        self.contexts = {}

    def add_batch(self, events):
        new = []
        observations = []
        for event in events:
            self.rows += 1
            self.widths[event.source_width or len(self.header)] += 1
            key = event.analysis_identity
            record = self.contexts.get(key)
            if record is None:
                record = {'id': 'SQL{:06d}'.format(len(self.contexts) + 1),
                          'sql': event.sql_text, 'user_name': event.user_name,
                          'count': 0, 'first_record': event.source_record,
                          'first_time': event.event_time, 'last_time': event.event_time,
                          'types': set()}
                self.contexts[key] = record
                new.append(record)
            record['count'] += 1
            record['types'].add(event.statement_type)
            record['first_time'] = min(record['first_time'], event.event_time)
            record['last_time'] = max(record['last_time'], event.event_time)
            self.types[event.statement_type] += 1
            observations.append({'source_record': event.source_record,
                                 'log_time': event.event_time, 'sql_id': record['id']})
        return [dict(record, types=sorted(record['types'])) for record in new], observations

    def profile(self):
        return {'source': self.source, 'header': self.header,
                'widths': dict(self.widths), 'types': dict(self.types), 'bad': [],
                'sqls': [dict(record, types=sorted(record['types']))
                         for record in self.contexts.values()]}
