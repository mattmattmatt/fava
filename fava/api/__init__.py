"""This module provides the data required by Fava's reports."""

import datetime
import os

from beancount import loader
from beancount.core import getters, realization
from beancount.core.flags import FLAG_UNREALIZED
from beancount.core.account_types import get_account_sign
from beancount.core.compare import hash_entry
from beancount.core.data import (get_entry, iter_entry_dates, Open, Close,
                                 Document, Balance, TxnPosting, Transaction,
                                 Event, Custom)
from beancount.ops import prices, summarize
from beancount.parser.options import get_account_types
from beancount.reports.context import render_entry_context
from beancount.utils.encryption import is_encrypted_file
from beancount.utils.misc_utils import filter_type

from fava.util import date
from fava.api.budgets import BudgetModule
from fava.api.charts import ChartModule
from fava.api.watcher import Watcher
from fava.api.file import FileModule
from fava.api.filters import (AccountFilter, FromFilter, PayeeFilter,
                              TagFilter, TimeFilter)
from fava.api.helpers import (get_final_holdings, aggregate_holdings_by,
                              entry_at_lineno, FavaAPIException)
from fava.api.fava_options import parse_options
from fava.api.misc import FavaMisc
from fava.api.query_shell import QueryShell


def _list_accounts(root_account, active_only=False):
    """List of all sub-accounts of the given root."""
    accounts = [child_account.account
                for child_account in
                realization.iter_children(root_account)
                if not active_only or child_account.txn_postings]

    return accounts if active_only else accounts[1:]


MODULES = {
    'budgets': BudgetModule,
    'charts': ChartModule,
    'file': FileModule,
    'misc': FavaMisc,
    'query_shell': QueryShell,
}

MODULE_NAMES = list(MODULES.keys())


class BeancountReportAPI():
    """Provides methods to access and filter beancount entries.

    Attributes:
        account_types: The names for the five base accounts.
        active_payees: All payees found in the file.
        active_tags: All tags found in the file.
        active_years: All years that contain some entry.
        all_accounts: A list of all account names.
        all_accounts_active: A list of all active account names.

    """

    __slots__ = [
        '_default_format_string', '_format_string',
        'account_types', 'active_payees', 'active_tags', 'active_years',
        'all_accounts_active', 'all_entries',
        'all_root_account', 'beancount_file_path',
        'date_first', 'date_last', 'entries',
        'errors', 'fava_options', 'filters', 'is_encrypted', 'options',
        'price_map', 'root_account', 'watcher'] + MODULE_NAMES

    def __init__(self, beancount_file_path):
        self.beancount_file_path = beancount_file_path
        self.is_encrypted = is_encrypted_file(beancount_file_path)
        self.filters = {
            'account': AccountFilter(),
            'from': FromFilter(),
            'payee': PayeeFilter(),
            'tag': TagFilter(),
            'time': TimeFilter(),
        }

        for name, mod in MODULES.items():
            setattr(self, name, mod(self))

        self.watcher = Watcher()
        self.load_file()

    def load_file(self):
        """Load self.beancount_file_path and compute things that are independent
        of how the entries might be filtered later"""
        # use the internal function to disable cache
        if not self.is_encrypted:
            self.all_entries, self.errors, self.options = \
                loader._load([(self.beancount_file_path, True)],
                             None, None, None)
            include_path = os.path.dirname(self.beancount_file_path)
            self.watcher.update(self.options['include'], [
                os.path.join(include_path, path)
                for path in self.options['documents']])
        else:
            self.all_entries, self.errors, self.options = \
                loader.load_file(self.beancount_file_path)
        self.price_map = prices.build_price_map(self.all_entries)
        self.account_types = get_account_types(self.options)
        self.all_root_account = realization.realize(self.all_entries,
                                                    self.account_types)
        if self.options['render_commas']:
            self._format_string = '{:,f}'
            self._default_format_string = '{:,.2f}'
        else:
            self._format_string = '{:f}'
            self._default_format_string = '{:.2f}'

        self.active_years = list(getters.get_active_years(self.all_entries))
        self.active_tags = getters.get_all_tags(self.all_entries)
        self.active_payees = getters.get_all_payees(self.all_entries)

        self.all_accounts_active = _list_accounts(
            self.all_root_account, active_only=True)

        self.fava_options, errors = parse_options(
            filter_type(self.all_entries, Custom))
        self.errors.extend(errors)

        for mod in MODULE_NAMES:
            getattr(self, mod).load_file()

        self.filter(True)

    # pylint: disable=attribute-defined-outside-init
    def filter(self, force=False, **kwargs):
        """Set and apply (if necessary) filters."""
        changed = False
        for filter_name, value in kwargs.items():
            if self.filters[filter_name].set(value):
                changed = True

        if not (changed or force):
            return

        self.entries = self.all_entries

        for filter_class in self.filters.values():
            self.entries = filter_class.apply(self.entries, self.options)

        self.root_account = realization.realize(self.entries,
                                                self.account_types)

        self.date_first, self.date_last = \
            getters.get_min_max_dates(self.entries, (Transaction))
        if self.date_last:
            self.date_last = self.date_last + datetime.timedelta(1)

        if self.filters['time']:
            self.date_first = self.filters['time'].begin_date
            self.date_last = self.filters['time'].end_date

    def changed(self):
        """Check if the file needs to be reloaded. """
        # We can't reload an encrypted file, so act like it never changes.
        if self.is_encrypted:
            return False
        changed = self.watcher.check()
        if changed:
            self.load_file()
        return changed

    def quantize(self, value, currency):
        """Quantize the value to the right number of decimal digits.

        Uses the DisplayContext generated by beancount."""
        if not currency:
            return self._default_format_string.format(value)
        return self._format_string.format(
            self.options['dcontext'].quantize(value, currency))

    def _interval_tuples(self, interval):
        """Calculates tuples of (begin_date, end_date) of length interval for the
        period in which entries contains transactions.  """
        return date.interval_tuples(self.date_first, self.date_last, interval)

    def get_account_sign(self, account_name):
        """Get account sign."""
        return get_account_sign(account_name, self.account_types)

    @property
    def root_account_closed(self):
        closing_entries = summarize.cap_opt(self.entries, self.options)
        return realization.realize(closing_entries)

    def interval_balances(self, interval, account_name, accumulate=False):
        """accumulate is False for /changes and True for /balances"""
        min_accounts = [account
                        for account in _list_accounts(self.all_root_account)
                        if account.startswith(account_name)]

        interval_tuples = list(reversed(self._interval_tuples(interval)))

        interval_balances = [
            realization.realize(list(iter_entry_dates(
                self.entries,
                self.date_first if accumulate else begin_date,
                end_date)), min_accounts)
            for begin_date, end_date in interval_tuples]

        return interval_balances, interval_tuples

    def account_journal(self, account_name, with_journal_children=False):
        real_account = realization.get_or_create(self.root_account,
                                                 account_name)

        if with_journal_children:
            postings = realization.get_postings(real_account)
        else:
            postings = real_account.txn_postings

        return realization.iterate_with_balance(postings)

    def events(self, event_type=None):
        """List events (possibly filtered by type)."""
        events = list(filter_type(self.entries, Event))

        if event_type:
            return filter(lambda e: e.type == event_type, events)

        return events

    def holdings(self, aggregation_key=None):
        holdings_list = get_final_holdings(
            self.entries,
            (self.account_types.assets, self.account_types.liabilities),
            self.price_map,
            self.date_last
        )

        if aggregation_key:
            holdings_list = aggregate_holdings_by(holdings_list,
                                                  aggregation_key)
        return holdings_list

    def context(self, entry_hash):
        """Context for an entry.

        Arguments:
            entry_hash: Hash of entry.

        Returns:
            A tuple ``(entry, context)`` of the (unique) entry with the given
            ``entry_hash`` and its context.

        """
        try:
            entry = next(entry for entry in self.all_entries
                         if entry_hash == hash_entry(entry))
        except StopIteration:
            return None, None

        ctx = render_entry_context(self.all_entries, self.options, entry)
        return entry, ctx.split("\n", 2)[2]

    def commodity_pairs(self):
        """List pairs of commodities.

        Returns:
            A list of pairs of commodities. Pairs of operating currencies will
            be given in both directions not just in the one found in file.

        """
        fw_pairs = self.price_map.forward_pairs
        bw_pairs = []
        for currency_a, currency_b in fw_pairs:
            if (currency_a in self.options['operating_currency'] and
                    currency_b in self.options['operating_currency']):
                bw_pairs.append((currency_b, currency_a))
        return sorted(fw_pairs + bw_pairs)

    def prices(self, base, quote):
        """List all prices."""
        all_prices = prices.get_all_prices(self.price_map,
                                           "{}/{}".format(base, quote))

        if self.filters['time']:
            return [(date, price) for date, price in all_prices
                    if (date >= self.filters['time'].begin_date and
                        date < self.filters['time'].end_date)]
        else:
            return all_prices

    def last_entry(self, account_name):
        """Get last entry of an account.

        Args:
            account_name: An account name.

        Returns:
            The last entry of the account if it is not a Close entry.
        """
        account = realization.get_or_create(self.all_root_account,
                                            account_name)

        last = realization.find_last_active_posting(account.txn_postings)

        if last is None or isinstance(last, Close):
            return

        return get_entry(last)

    @property
    def postings(self):
        """All postings contained in some transaction."""
        return [posting for entry in filter_type(self.entries, Transaction)
                for posting in entry.postings]

    def abs_path(self, file_path):
        """Make a path absolute.

        Args:
            file_path: A file path.

        Returns:
            The absolute path of `file_path`, assuming it is relative to
            the directory of the beancount file.

        """
        if not os.path.isabs(file_path):
            return os.path.join(os.path.dirname(
                os.path.realpath(self.beancount_file_path)), file_path)
        return file_path

    def statement_path(self, filename, lineno, metadata_key):
        """Returns the path for a statement found in the specified entry."""
        entry = entry_at_lineno(self.all_entries, filename, lineno)
        value = entry.meta[metadata_key]

        paths = [value]
        paths.extend([os.path.join(posting.account.replace(':', '/'), value)
                      for posting in entry.postings])
        paths.extend([os.path.join(document_root,
                                   posting.account.replace(':', '/'), value)
                      for posting in entry.postings
                      for document_root in self.options['documents']])

        for path in [self.abs_path(p) for p in paths]:
            if os.path.isfile(path):
                return path

        raise FavaAPIException('Statement not found.')

    def document_path(self, path):
        """Get absolute path of a document.

        Returns:
            The absolute path of ``path`` if it points to a document.

        Raises:
            FavaAPIException: If ``path`` is not the path of one of the
                documents.

        """
        for entry in filter_type(self.entries, Document):
            if entry.filename == path:
                return self.abs_path(path)

        raise FavaAPIException(
            'File "{}" not found in document entries.'.format(path))

    def _last_balance_or_transaction(self, account_name):
        real_account = realization.get_or_create(self.all_root_account,
                                                 account_name)

        for txn_posting in reversed(real_account.txn_postings):
            if not isinstance(txn_posting, (TxnPosting, Balance)):
                continue

            if isinstance(txn_posting, TxnPosting) and \
               txn_posting.txn.flag == FLAG_UNREALIZED:
                continue
            return txn_posting

    def account_uptodate_status(self, account_name):
        """Status of the last balance or transaction.

        Args:
            account_name: An account name.

        Returns:
            A status string for the last balance or transaction of the account.

            - 'green':  A balance check that passed.
            - 'red':    A balance check that failed.
            - 'yellow': Not a balance check.
        """
        last_posting = self._last_balance_or_transaction(account_name)

        if last_posting:
            if isinstance(last_posting, Balance):
                if last_posting.diff_amount:
                    return 'red'
                else:
                    return 'green'
            else:
                return 'yellow'

    def account_metadata(self, account_name):
        """Metadata of the account.

        Args:
            account_name: An account name.

        Returns:
            Metadata of the Open entry of the account.
        """
        real_account = realization.get_or_create(self.root_account,
                                                 account_name)
        for posting in real_account.txn_postings:
            if isinstance(posting, Open):
                return posting.meta
        return {}
