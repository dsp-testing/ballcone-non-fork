__author__ = 'Dmitry Ustalov'

import logging
from contextlib import contextmanager
from datetime import datetime, date
from ipaddress import ip_address, IPv4Address, IPv6Address
from typing import Generator, NamedTuple, Optional, List, Sequence, Union, Any, NewType, Tuple, Deque, Set, cast

import duckdb
import pypika.enums
from pypika import Query, Column, Field, Parameter, Table, Order, functions as fn, analytics as an
from pypika.queries import QueryBuilder

smallint = NewType('smallint', int)

TYPES = {
    datetime: 'TIMESTAMP',
    str: 'VARCHAR',
    smallint: 'SMALLINT',
    int: 'INTEGER',
    float: 'DOUBLE',
    IPv4Address: 'VARCHAR',
    IPv6Address: 'VARCHAR',
    bool: 'BOOLEAN'
}


def is_empty(obj: Any) -> bool:
    if hasattr(obj, '__len__'):
        return not len(obj)

    return obj is None


def optional_types(annotation: Any) -> Tuple[Set[Any], bool]:
    if hasattr(annotation, '__args__'):
        types = set(annotation.__args__)
        null = type(None) in types

        if null:
            types.remove(type(None))

        return types, null
    else:
        return {annotation}, False


def python_type_to_sql(annotation: Any) -> str:
    types, null = optional_types(annotation)
    first_type = next(iter(types))

    if null:
        return TYPES[first_type]
    else:
        return TYPES[first_type] + ' NOT NULL'


def sql_value_to_python(name: str, annotation: Any, value: Any) -> Any:
    args, null = optional_types(annotation)
    first_type = next(iter(args))

    if first_type == datetime:
        return value

    if first_type == smallint:
        return int(value)

    if first_type in (IPv4Address, IPv6Address):
        return ip_address(value)

    return None if is_empty(value) and null else first_type(value)


class Entry(NamedTuple):
    datetime: datetime
    host: str
    path: str
    status: smallint
    length: int
    generation_time: float
    referer: Optional[str]
    # IP address and derivatives
    ip: Union[IPv4Address, IPv6Address]
    country_iso_code: Optional[str]
    # derivatives from User-Agent
    platform_name: Optional[str]
    platform_version: Optional[str]
    browser_name: Optional[str]
    browser_version: Optional[str]
    is_robot: Optional[bool]

    @staticmethod
    def from_values(entry: Sequence[Any]) -> 'Entry':
        return Entry(*(sql_value_to_python(name, annotation, value)
                       for (name, annotation), value in zip(Entry.__annotations__.items(), entry)))

    @staticmethod
    def as_value(value: Any, annotation: Any = None) -> Any:
        if isinstance(value, (IPv4Address, IPv6Address)):
            return str(value)

        if annotation:
            _, null = optional_types(annotation)

            return None if is_empty(value) and null else value
        else:
            return value

    def as_values(self) -> Sequence[Any]:
        return tuple(self.as_value(getattr(self, name), annotation)
                     for name, annotation in self.__annotations__.items())


class Count(NamedTuple):
    date: date
    group: Optional[str]
    count: Any  # mypy prints an error if this is an int


class CountResult(NamedTuple):
    table: str
    field: Optional[str]
    distinct: bool
    ascending: Optional[bool]
    group: Optional[str]
    elements: List[Count]


class Average(NamedTuple):
    date: date
    avg: float
    sum: float
    count: Any  # mypy prints an error if this is an int


class AverageResult(NamedTuple):
    table: str
    field: str
    elements: List[Average]


class DAO:
    def __init__(self, db: duckdb.DuckDBPyConnection) -> None:
        self.db = db
        self.placeholders = [Parameter('?') for _ in Entry._fields]

    def size(self) -> int:
        return cast(int, self.run('SELECT COALESCE(total_blocks * block_size, 0) FROM pragma_database_size()')[0][0])

    def tables(self) -> Sequence[str]:
        master = Table('sqlite_master')
        query = Query.from_('sqlite_master').select(master.name). \
            where(master.type == 'table').distinct(). \
            orderby(master.name)

        sql = str(query)

        logging.debug(sql)

        return [table for table, *_ in self.run(query)]

    def table_exists(self, table: str) -> bool:
        master = Table('sqlite_master')

        query = Query.from_(master).select(master.name). \
            where((master.type == 'table') & (master.name == table))

        sql = str(query)

        logging.debug(sql)

        return len(self.run(query)) > 0

    def create_table(self, table: str) -> int:
        target = Table(table)

        columns = [Column(name, python_type_to_sql(annotation)) for name, annotation in Entry.__annotations__.items()]

        query = Query.create_table(target).columns(*columns)
        sql = str(query)

        logging.debug(sql)

        with self.transaction() as cursor:
            return cast(int, cursor.execute(sql))

    def drop_table(self, table: str) -> int:
        sql = str(Query.drop_table(table))

        logging.debug(sql)

        with self.transaction() as cursor:
            return cast(int, cursor.execute(sql))

    def insert_into(self, table: str, entry: Entry, cursor: Optional[duckdb.DuckDBPyConnection] = None) -> None:
        target = Table(table)

        query = Query.into(target).insert(*self.placeholders)
        sql, values = str(query), entry.as_values()

        logging.debug(sql + ' -- ' + str(values))

        if cursor:
            cursor.execute(sql, values)
        else:
            with self.transaction() as cursor:
                cursor.execute(sql, values)

    def batch_insert_into(self, table: str, entries: Sequence[Entry]) -> int:
        if not entries:
            return 0

        with self.transaction() as cursor:
            count = 0

            for entry in entries:
                self.insert_into(table, entry, cursor=cursor)
                count += 1

            return count

    def batch_insert_into_from_deque(self, table: str, entries: Deque[Entry]) -> int:
        if not entries:
            return 0

        with self.transaction() as cursor:
            count = 0

            while entries:
                entry = entries.popleft()
                self.insert_into(table, entry, cursor=cursor)
                count += 1

            return count

    def select(self, table: str, start: Optional[date] = None, stop: Optional[date] = None,
               limit: Optional[int] = None) -> List[Entry]:
        target = Table(table)

        query = Query.from_(target).select('*').orderby(target.datetime).limit(limit)

        query = self.apply_dates(query, target, start, stop)

        rows = cast(List[Union[List[Any], Entry]], self.run(query))

        for i, current in enumerate(rows):
            rows[i] = Entry.from_values(cast(List[Any], current))

        return cast(List[Entry], rows)

    def select_average(self, table: str, field: str, start: Optional[date] = None,
                       stop: Optional[date] = None) -> AverageResult:
        target = Table(table)
        target_field = Field(field, table=target)
        date = fn.Cast(target.datetime, pypika.enums.SqlTypes.DATE, alias='date')

        query = Query.from_(target).select(date,
                                           fn.Avg(target_field, alias='average'),
                                           fn.Sum(target_field, alias='sum'),
                                           fn.Count(target_field, alias='count')). \
            groupby(date).orderby(target.date)

        query = self.apply_dates(query, target, start, stop)

        result = AverageResult(table=table, field=field, elements=[])

        for current in self.run(query):
            result.elements.append(Average(
                date=current[0],
                avg=float(current[1]),
                sum=float(current[2]) if current[3] else 0.,
                count=int(current[3])
            ))

        return result

    def select_count(self, table: str, field: Optional[str] = None, start: Optional[date] = None,
                     stop: Optional[date] = None) -> CountResult:
        target = Table(table)
        date = fn.Cast(target.datetime, pypika.enums.SqlTypes.DATE, alias='date')

        count_field = fn.Count(Field(field, table=target) if field else date, alias='count')

        if field:
            count_field = count_field.distinct()

        query = Query.from_(target).select(date, count_field).groupby(date).orderby(date)

        query = self.apply_dates(query, target, start, stop)

        result = CountResult(table=table, field=field, distinct=field is not None, group=None, ascending=None,
                             elements=[])

        for current in self.run(query):
            result.elements.append(Count(
                date=current[0],
                group=None,
                count=int(current[1])
            ))

        return result

    def select_count_group(self, table: str, field: Optional[str], group: str, distinct: bool = False,
                           start: Optional[date] = None, stop: Optional[date] = None,
                           ascending: bool = True, limit: Optional[int] = None) -> CountResult:
        target = Table(table)
        date = fn.Cast(target.datetime, pypika.enums.SqlTypes.DATE, alias='date')

        count_field = fn.Count(Field(field, table=target) if field else date, alias='count')
        order = Order.asc if ascending else Order.desc

        if distinct:
            count_field = count_field.distinct()

        group_field = Field(group, table=target)

        query = Query.from_(target).select(date, group_field.as_('group'), count_field). \
            groupby(date, group_field).orderby(date). \
            orderby(count_field, order=order).orderby(group_field)

        query = self.apply_dates(query, target, start, stop)

        if limit is not None:
            window = Query.from_(query).select(query.date, query.group, query.count,
                                               an.RowNumber(alias='row_number').over(query.date).
                                               orderby(query.count, order=order).orderby(query.group))

            query = Query.from_(window).select(window.date, window.group, window.count). \
                where(window.row_number <= limit).orderby(window.date). \
                orderby(window.count, order=order).orderby(window.group)

        result = CountResult(table=table, field=field, distinct=distinct, group=group, ascending=ascending,
                             elements=[])

        for current in self.run(query):
            result.elements.append(Count(
                date=current[0],
                group=current[1],
                count=int(current[2])
            ))

        return result

    def run(self, query: Union[QueryBuilder, str]) -> List[List[Any]]:
        sql = str(query) if isinstance(query, QueryBuilder) else query

        logging.debug(sql)

        with self.cursor() as cursor:
            cursor.execute(sql)

            return cast(List[List[Any]], cursor.fetchall())

    @staticmethod
    def apply_dates(query: QueryBuilder, target: Table,
                    start: Optional[date] = None, stop: Optional[date] = None) -> QueryBuilder:
        date = fn.Cast(target.datetime, pypika.enums.SqlTypes.DATE, alias='date')

        if start and stop:
            if start == stop:
                return query.where(date == Entry.as_value(start))
            else:
                return query.where(date[Entry.as_value(start):Entry.as_value(stop)])
        elif start:
            return query.where(date >= Entry.as_value(start))
        elif stop:
            return query.where(date <= Entry.as_value(stop))

        return query

    @contextmanager
    def cursor(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        cursor = self.db.cursor()

        try:
            cursor.begin()
            yield cursor
        except RuntimeError as e:
            raise e
        finally:
            cursor.close()

    @contextmanager
    def transaction(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        with self.cursor() as cursor:
            yield cursor
            cursor.commit()
