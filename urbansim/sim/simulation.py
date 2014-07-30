from __future__ import print_function

import inspect
from collections import Callable, namedtuple

import pandas as pd
import toolz
import time
import logging

logger = logging.getLogger(__name__)

from ..utils.misc import column_map

_TABLES = {}
_COLUMNS = {}
_MODELS = {}
_BROADCASTS = {}
_INJECTABLES = {}


def clear_sim():
    """
    Clear any stored state from the simulation.

    """
    _TABLES.clear()
    _COLUMNS.clear()
    _MODELS.clear()
    _BROADCASTS.clear()
    _INJECTABLES.clear()


# for errors that occur during simulation runs
class SimulationError(Exception):
    pass


class DataFrameWrapper(object):
    """
    Wraps a DataFrame so it can provide certain columns and handle
    computed columns.

    Parameters
    ----------
    name : str
        Name for the table.
    frame : pandas.DataFrame

    """
    def __init__(self, name, frame):
        self.name = name
        self._frame = frame

    @property
    def columns(self):
        """
        Columns in this table.

        """
        return self.local_columns + _list_columns_for_table(self.name)

    @property
    def local_columns(self):
        """
        Columns that are part of the wrapped DataFrame.

        """
        return list(self._frame.columns)

    @property
    def index(self):
        """
        Table index.

        """
        return self._frame.index

    def to_frame(self, columns=None):
        """
        Make a DataFrame with the given columns.

        Parameters
        ----------
        columns : sequence, optional
            Sequence of the column names desired in the DataFrame.
            If None all columns are returned, including registered columns.

        Returns
        -------
        frame : pandas.DataFrame

        """
        extra_cols = _columns_for_table(self.name)

        if columns:
            local_cols = [c for c in self._frame.columns
                          if c in columns and c not in extra_cols]
            extra_cols = toolz.keyfilter(lambda c: c in columns, extra_cols)
            df = self._frame[local_cols].copy()
        else:
            df = self._frame.copy()

        for name, col in extra_cols.items():
            df[name] = col()

        return df

    def update_col(self, column_name, series):
        """
        Add or replace a column in the underlying DataFrame.

        Parameters
        ----------
        column_name : str
            Column to add or replace.
        series : pandas.Series or sequence
            Column data.

        """
        self._frame[column_name] = series

    def __setitem__(self, key, value):
        return self.update_col(key, value)

    def get_column(self, column_name):
        """
        Returns a column as a Series.

        Parameters
        ----------
        column_name : str

        Returns
        -------
        column : pandas.Series

        """
        return self.to_frame(columns=[column_name])[column_name]

    def __getitem__(self, key):
        return self.get_column(key)

    def __getattr__(self, key):
        return self.get_column(key)

    def __len__(self):
        return len(self._frame)


class TableFuncWrapper(object):
    """
    Wrap a function that provides a DataFrame.

    Parameters
    ----------
    name : str
        Name for the table.
    func : callable
        Callable that returns a DataFrame.

    """
    def __init__(self, name, func):
        self.name = name
        self._func = func
        self._arg_list = set(inspect.getargspec(func).args)
        self._columns = []
        self._index = None
        self._len = 0

    @property
    def columns(self):
        """
        Columns in this table. (May contain only computed columns
        if the wrapped function has not been called yet.)

        """
        return self._columns + _list_columns_for_table(self.name)

    @property
    def local_columns(self):
        """
        Only the columns contained in the DataFrame returned by the
        wrapped function. (No registered columns included.)

        """
        if self._columns:
            return self._columns
        else:
            self._call_func()
            return self._columns

    @property
    def index(self):
        """
        Index of the underlying table. Will be None if that index is
        unknown.

        """
        return self._index

    def _call_func(self):
        """
        Call the wrapped function and return the result. Also updates
        attributes like columns, index, and length.

        """
        kwargs = _collect_injectables(self._arg_list)
        frame = self._func(**kwargs)
        self._columns = list(frame.columns)
        self._index = frame.index
        self._len = len(frame)
        return frame

    def to_frame(self, columns=None):
        """
        Make a DataFrame with the given columns.

        Parameters
        ----------
        columns : sequence, optional
            Sequence of the column names desired in the DataFrame.
            If None all columns are returned.

        Returns
        -------
        frame : pandas.DataFrame

        """
        frame = self._call_func()
        return DataFrameWrapper(self.name, frame).to_frame(columns)

    def get_column(self, column_name):
        """
        Returns a column as a Series.

        Parameters
        ----------
        column_name : str

        Returns
        -------
        column : pandas.Series

        """
        return self.to_frame(columns=[column_name])[column_name]

    def __getitem__(self, key):
        return self.get_column(key)

    def __getattr__(self, key):
        return self.get_column(key)

    def __len__(self):
        return self._len


class TableSourceWrapper(TableFuncWrapper):
    """
    Wraps a function that returns a DataFrame. After the function
    is evaluated the returned DataFrame replaces the function in the
    table registry.

    Parameters
    ----------
    name : str
    func : callable

    """
    def convert(self):
        """
        Evaluate the wrapped function, store the returned DataFrame as a
        table, and return the new DataFrameWrapper instance created.

        """
        frame = self._call_func()
        return add_table(self.name, frame)

    def to_frame(self, columns=None):
        """
        Make a DataFrame with the given columns. The first time this
        is called the registered table will be replaced with the DataFrame
        returned by the wrapped function.

        Parameters
        ----------
        columns : sequence, optional
            Sequence of the column names desired in the DataFrame.
            If None all columns are returned.

        Returns
        -------
        frame : pandas.DataFrame

        """
        return self.convert().to_frame(columns)


class _ColumnFuncWrapper(object):
    """
    Wrap a function that returns a Series.

    Parameters
    ----------
    table_name : str
        Table with which the column will be associated.
    column_name : str
        Name for the column.
    func : callable
        Should return a Series that has an
        index matching the table to which it is being added.

    """
    def __init__(self, table_name, column_name, func):
        self.table_name = table_name
        self.name = column_name
        self._func = func
        self._arg_list = set(inspect.getargspec(func).args)

    def __call__(self):
        kwargs = _collect_injectables(self._arg_list)
        return self._func(**kwargs)


class _SeriesWrapper(object):
    """
    Wrap a Series for the purpose of giving it the same interface as a
    `_ColumnFuncWrapper`.

    Parameters
    ----------
    table_name : str
        Table with which the column will be associated.
    column_name : str
        Name for the column.
    func : callable
        Should return a Series that has an
        index matching the table to which it is being added.

    """
    def __init__(self, table_name, column_name, series):
        self.table_name = table_name
        self.name = column_name
        self._column = series

    def __call__(self):
        return self._column


class _InjectableFuncWrapper(object):
    """
    Wraps a function that will be called (with injection) to provide
    an injectable value elsewhere.

    Parameters
    ----------
    name : str
    func : callable

    """
    def __init__(self, name, func):
        self.name = name
        self._func = func
        self._arg_list = set(inspect.getargspec(func).args)

    def __call__(self):
        kwargs = _collect_injectables(self._arg_list)
        return self._func(**kwargs)


class _ModelFuncWrapper(object):
    """
    Wrap a model function for dependency injection.

    Parameters
    ----------
    model_name : str
    func : callable

    """
    def __init__(self, model_name, func):
        self.name = model_name
        self._func = func
        self._arg_list = set(inspect.getargspec(func).args)

    def __call__(self):
        kwargs = _collect_injectables(self._arg_list)
        return self._func(**kwargs)


def list_tables():
    """
    List of table names.

    """
    return list(_TABLES.keys())


def list_columns():
    """
    List of (table name, registered column name) pairs.

    """
    return list(_COLUMNS.keys())


def list_models():
    """
    List of registered model names.

    """
    return list(_MODELS.keys())


def list_injectables():
    """
    List of registered injectables.

    """
    return list(_INJECTABLES.keys())


def list_broadcasts():
    """
    List of registered broadcasts as (cast table name, onto table name).

    """
    return list(_BROADCASTS.keys())


def _collect_injectables(names):
    """
    Find all the injectables specified in `names`.

    Parameters
    ----------
    names : list of str

    Returns
    -------
    injectables : dict
        Keys are the names, values are wrappers if the injectable
        is a table. If it's a plain injectable the value itself is given
        or the injectable function is evaluated.

    """
    names = set(names)
    dicts = toolz.keyfilter(
        lambda x: x in names, toolz.merge(_INJECTABLES, _TABLES))

    if set(dicts.keys()) != names:
        raise KeyError(
            'not all injectables found. '
            'missing: {}'.format(names - set(dicts.keys())))

    for name, thing in dicts.items():
        if isinstance(thing, _InjectableFuncWrapper):
            dicts[name] = thing()
        elif isinstance(thing, TableSourceWrapper):
            dicts[name] = thing.convert()

    return dicts


def add_table(table_name, table):
    """
    Register a table with the simulation.

    Parameters
    ----------
    table_name : str
        Should be globally unique to this table.
    table : pandas.DataFrame or function
        If a function it should return a DataFrame. Function argument
        names will be matched to known tables, which will be injected
        when this function is called.

    Returns
    -------
    wrapped : `DataFrameWrapper` or `TableFuncWrapper`

    """
    if isinstance(table, pd.DataFrame):
        table = DataFrameWrapper(table_name, table)
    elif isinstance(table, Callable):
        table = TableFuncWrapper(table_name, table)
    else:
        raise TypeError('table must be DataFrame or function.')

    _TABLES[table_name] = table

    return table


def table(table_name):
    """
    Decorator version of `add_table` used for decorating functions
    that return DataFrames.

    Decorated function argument names will be matched to known tables,
    which will be injected when this function is called.

    """
    def decorator(func):
        add_table(table_name, func)
        return func
    return decorator


def add_table_source(table_name, func):
    """
    Add a DataFrame source function to the simulation. This function is
    evaluated only once, after which the returned DataFrame replaces
    `func` as the injected table.

    Parameters
    ----------
    table_name : str
    func : callable
        Function argument names will be matched to known injectables,
        which will be injected when this function is called.

    Returns
    -------
    wrapped : `TableSourceWrapper`

    """
    wrapped = TableSourceWrapper(table_name, func)
    _TABLES[table_name] = wrapped
    return wrapped


def table_source(table_name):
    """
    Decorator version of `add_table_source`. Use it to decorate a function
    that returns a DataFrame. The function will be evaluated only once
    and the DataFrame will replace it.

    """
    def decorator(func):
        add_table_source(table_name, func)
        return func
    return decorator


def get_table(table_name):
    """
    Get a registered table.

    Parameters
    ----------
    table_name : str

    Returns
    -------
    table : `DataFrameWrapper`, `TableFuncWrapper`, or `TableSourceWrapper`

    """
    if table_name in _TABLES:
        return _TABLES[table_name]
    else:
        raise KeyError('table not found: {}'.format(table_name))


def add_column(table_name, column_name, column):
    """
    Add a new column to a table from a Series or callable.

    Parameters
    ----------
    table_name : str
        Table with which the column will be associated.
    column_name : str
        Name for the column.
    column : pandas.Series or callable
        If a callable it should return a Series. Any Series should have an
        index matching the table to which it is being added.

    """
    if isinstance(column, pd.Series):
        column = _SeriesWrapper(table_name, column_name, column)
    elif isinstance(column, Callable):
        column = \
            _ColumnFuncWrapper(table_name, column_name, column)
    else:
        raise TypeError('Only Series or callable allowed for column.')

    _COLUMNS[(table_name, column_name)] = column


def column(table_name, column_name):
    """
    Decorator version of `add_column` used for decorating functions
    that return a Series with an index matching the named table.

    The argument names of the function should match known tables, which
    will be injected.

    """
    def decorator(func):
        add_column(table_name, column_name, func)
        return func
    return decorator


def _list_columns_for_table(table_name):
    """
    Return a list of all the extra columns registered for a given table.

    Parameters
    ----------
    table_name : str

    Returns
    -------
    columns : list of str

    """
    return [cname for tname, cname in _COLUMNS.keys() if tname == table_name]


def _columns_for_table(table_name):
    """
    Return all of the columns registered for a given table.

    Parameters
    ----------
    table_name : str

    Returns
    -------
    columns : dict of column wrappers
        Keys will be column names.

    """
    return {cname: col
            for (tname, cname), col in _COLUMNS.items()
            if tname == table_name}


def add_injectable(name, value, autocall=True):
    """
    Add a value that will be injected into other functions that
    are part of the simulation.

    Parameters
    ----------
    name : str
    value
        If a callable and `autocall` is True then the function will be
        evaluated using dependency injection and the return value will
        be passed to any functions using this injectable. In all other
        cases `value` will be passed through untouched.
    autocall : bool, optional
        Set to True to have injectable functions automatically called
        (with dependency injection) and the result injected instead of
        the function itself.

    """
    if isinstance(value, Callable) and autocall:
        value = _InjectableFuncWrapper(name, value)
    _INJECTABLES[name] = value


def injectable(name, autocall=True):
    """
    Decorator version of `add_injectable`.

    """
    def decorator(func):
        add_injectable(name, func, autocall=autocall)
        return func
    return decorator


def get_injectable(name):
    """
    Get an injectable by name. *Does not* evaluate wrapped functions.

    Parameters
    ----------
    name : str

    Returns
    -------
    injectable
        Original value or _InjectableFuncWrapper if autocall was True.

    """
    if name in _INJECTABLES:
        return _INJECTABLES[name]
    else:
        raise KeyError('injectable not found: {}'.format(name))


def add_model(model_name, func):
    """
    Add a model function to the simulation.

    Model argument names are used for injecting known tables of the same name.
    The argument name "year" may be used to have the current simulation
    year injected.

    Parameters
    ----------
    model_name : str
    func : callable

    """
    if isinstance(func, Callable):
        _MODELS[model_name] = _ModelFuncWrapper(model_name, func)
    else:
        raise TypeError('func must be a callable')


def model(model_name):
    """
    Decorator version of `add_model`, used to decorate a function that
    will require injection of tables and that can be run by the
    `run` function.

    """
    def decorator(func):
        add_model(model_name, func)
        return func
    return decorator


def get_model(model_name):
    """
    Get a wrapped model by name.

    Parameters
    ----------

    """
    if model_name in _MODELS:
        return _MODELS[model_name]
    else:
        raise KeyError('no model named {}'.format(model_name))


def run(models, years=None):
    """
    Run models in series, optionally repeatedly over some years.
    The current year is set as a global injectable.

    Parameters
    ----------
    models : list of str
        List of models to run identified by their name.

    """
    years = years or [None]

    for year in years:
        add_injectable('year', year)
        if year:
            print('Running year {}'.format(year))
        for model_name in models:
            print('Running model {}'.format(model_name))
            model = get_model(model_name)
            t1 = time.time()
            model()
            logger.info("Time to execute model = %.3fs" % (time.time()-t1))


_Broadcast = namedtuple(
    '_Broadcast',
    ['cast', 'onto', 'cast_on', 'onto_on', 'cast_index', 'onto_index'])


def broadcast(cast, onto, cast_on=None, onto_on=None,
              cast_index=False, onto_index=False):
    """
    Register a rule for merging two tables by broadcasting one onto
    the other.

    Parameters
    ----------
    cast, onto : str
        Names of registered tables.
    cast_on, onto_on : str, optional
        Column names used for merge, equivalent of ``left_on``/``right_on``
        parameters of pandas.merge.
    cast_index, onto_index : bool, optional
        Whether to use table indexes for merge. Equivalent of
        ``left_index``/``right_index`` parameters of pandas.merge.

    """
    _BROADCASTS[(cast, onto)] = \
        _Broadcast(cast, onto, cast_on, onto_on, cast_index, onto_index)


def _get_broadcasts(tables):
    """
    Get the broadcasts associated with a set of tables.

    Parameters
    ----------
    tables : sequence of str
        Table names for which broadcasts have been registered.

    Returns
    -------
    casts : dict of `_Broadcast`
        Keys are tuples of strings like (cast_name, onto_name).

    """
    tables = set(tables)
    casts = toolz.keyfilter(
        lambda x: x[0] in tables and x[1] in tables, _BROADCASTS)
    if tables - set(toolz.concat(casts.keys())):
        raise ValueError('Not enough links to merge all tables.')
    return casts


# utilities for merge_tables
def _all_reachable_tables(t):
    for k, v in t.items():
        for tname in _all_reachable_tables(v):
            yield tname
        yield k


def _is_leaf_node(merge_node):
    return not any(merge for merge in merge_node.values())


def _next_merge(merge_node):
    if all(_is_leaf_node(merge) for merge in merge_node.values()):
        return merge_node
    else:
        for merge in merge_node.values():
            if merge:
                return _next_merge(merge)


def merge_tables(target, tables, columns=None):
    """
    Merge a number of tables onto a target table. Tables must have
    registered merge rules via the `broadcast` function.

    Parameters
    ----------
    target : str
        Name of the table onto which tables will be merged.
    tables : list of `DataFrameWrapper` or `TableFuncWrapper`
        All of the tables to merge. Should include the target table.
    columns : list of str, optional
        If given, columns will be mapped to `tables` and only those columns
        will be requested from each table. The final merged table will have
        only these columns. By default all columns are used from every
        table.

    Returns
    -------
    merged : pandas.DataFrame

    """
    merges = {t.name: {} for t in tables}
    tables = {t.name: t for t in tables}
    casts = _get_broadcasts(tables.keys())

    # relate all the tables by registered broadcasts
    for table, onto in casts:
        merges[onto][table] = merges[table]
    merges = {target: merges[target]}

    # verify that all the tables can be merged to the target
    all_tables = set(_all_reachable_tables(merges))

    if all_tables != set(tables.keys()):
        raise RuntimeError(
            ('Not all tables can be merged to target "{}". Unlinked tables: {}'
             ).format(target, list(set(tables.keys()) - all_tables)))

    # add any columns necessary for indexing into columns
    if columns:
        columns = list(columns)
        for c in casts.values():
            if c.onto_on:
                columns.append(c.onto_on)
            if c.cast_on:
                columns.append(c.cast_on)

    # get column map for which columns go with which table
    colmap = column_map(tables.values(), columns)

    # get frames
    frames = {name: t.to_frame(columns=colmap[name])
              for name, t in tables.items()}

    while merges[target]:
        nm = _next_merge(merges)
        onto = nm.keys()[0]
        onto_table = frames[onto]
        for cast in nm[onto].keys():
            cast_table = frames[cast]
            bc = casts[(cast, onto)]
            onto_table = pd.merge(
                onto_table, cast_table,
                left_on=bc.onto_on, right_on=bc.cast_on,
                left_index=bc.onto_index, right_index=bc.cast_index)
        frames[onto] = onto_table
        nm[onto] = {}

    return frames[target]


def partial_update(update, outdf_name, outfname):
    if not len(update):
        return
    s = get_table(outdf_name).get_column(outfname)
    s.loc[update.index] = update
    add_column(outdf_name, outfname, s)
