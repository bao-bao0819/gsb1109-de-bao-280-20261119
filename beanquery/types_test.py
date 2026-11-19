import unittest

from beanquery import types
from beanquery.sources import beancount


class TestName(unittest.TestCase):
    def test_transactions_names(self):
        table = beancount.TransactionsTable
        columns = {name: types.name(column.dtype) for name, column in table.columns.items()}
        self.assertEqual(columns, {
            'meta': 'metadata',
            'date': 'date',
            'flag': 'str',
            'payee': 'str',
            'narration': 'str',
            'tags': 'set',
            'links': 'set',
            'accounts': 'set[str]',
        })

    def test_postings_other_names(self):
        table = beancount.PostingsTable
        names = {name: types.name(table.columns[name].dtype)
                 for name in ('other_accounts', 'other_amounts', 'other_postings', 'other_currencies')}
        self.assertEqual(names, {
            'other_accounts': 'set[str]',
            'other_amounts': 'dict',
            'other_postings': 'list',
            'other_currencies': 'set[str]',
        })
