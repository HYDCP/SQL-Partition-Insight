"""Regression checks for user-supplied partition metadata imports."""

import _support
import csv
from pathlib import Path
import tempfile
import unittest

from sql_partition_analysis.partitioning.importer import load_metadata


class ImportPartitionMetadataTests(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'metadata.txt'
            source.write_text(text, encoding='utf-8')
            return load_metadata(source)

    def load_rows(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'metadata.txt'
            with source.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['SCHEMA_NAME', 'TAB_NAME', 'TAB_PARTI_FIELD'])
                writer.writerows(rows)
            return load_metadata(source)

    def test_identical_duplicates_merge(self):
        result = self.load_rows([
            ['rdm', 't', 'PARTITION BY RANGE(data_dt)'],
            ['rdm', 't', 'PARTITION BY RANGE(data_dt)'],
            ['rdm_qry', 'n', 'NULL'],
            ['rdm_qry', 'n', 'NULL'],
        ])
        self.assertEqual(result['source']['rows'], 4)
        self.assertEqual(result['source']['unique_tables'], 2)
        self.assertEqual(result['source']['duplicate_rows'], 2)
        self.assertEqual(len(result['tables']), 1)
        self.assertEqual(result['non_partitioned_tables'], ['rdm_qry.n'])

    def test_conflicting_duplicates_rejected(self):
        for second in ['NULL', 'PARTITION BY RANGE(row_date)', 'PARTITION BY LIST(data_dt)']:
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, 'Conflicting partition definitions'):
                self.load_rows([
                    ['rdm', 't', 'PARTITION BY RANGE(data_dt)'],
                    ['rdm', 't', second],
                ])

    def test_invalid_csv_rejected(self):
        for text in [
            'schema,table,definition\nrdm,t,NULL\n',
            'SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD\nrdm,t\n',
            'SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD\nrdm,t,NULL,unexpected\n',
            'SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD\n,t,NULL\n',
            'SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD\nrdm,,NULL\n',
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load_text(text)

    def test_unknown_definitions_and_expressions_rejected(self):
        for definition in ['', 'PARTITION BY UNKNOWN(data_dt)', 'PARTITION BY RANGE(date_trunc(\'day\', data_dt))', 'PARTITION BY RANGE(t.data_dt)']:
            with self.subTest(definition=definition), self.assertRaises(ValueError):
                self.load_rows([['rdm', 't', definition]])

    def test_bom_header_and_quoted_multicolumn_csv(self):
        result = self.load_text('\ufeffSCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD\nrdm,t,"PARTITION BY RANGE(data_dt, region)"\n')
        self.assertEqual(result['tables']['rdm.t']['partition_columns'], ['data_dt', 'region'])
        self.assertEqual(result['version'], 1)


if __name__ == '__main__':
    unittest.main()
