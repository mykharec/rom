
from collections import defaultdict
from datetime import datetime, date, time as dtime
from decimal import Decimal as _Decimal
import json
import warnings

import redis
import six

from .columns import (Column, Integer, Boolean, Float, Decimal, DateTime,
    Date, Time, String, Text, Json, PrimaryKey, ManyToOne, OneToOne,
    ForeignModel, OneToMany, MODELS, MODELS_REFERENCED, _on_delete,
    SKIP_ON_DELETE)
from .exceptions import (ORMError, UniqueKeyViolation, InvalidOperation,
    QueryError, ColumnError, MissingColumn, InvalidColumnValue, RestrictError)
from .index import GeneralIndex, Pattern, Prefix, Suffix
from .query import Query
from .util import (ClassProperty, _connect, session,
    _prefix_score, _script_load, _encode_unique_constraint,
    FULL_TEXT, CASE_INSENSITIVE, SIMPLE)

VERSION = '0.32.1'

COLUMN_TYPES = [Column, Integer, Boolean, Float, Decimal, DateTime, Date,
Time, String, Text, Json, PrimaryKey, ManyToOne, ForeignModel, OneToMany]

NUMERIC_TYPES = six.integer_types + (float, _Decimal, datetime, date, dtime)

# silence pyflakes
InvalidOperation, MissingColumn, RestrictError
Pattern, Suffix, Prefix, FULL_TEXT

USE_LUA = True

class _ModelMetaclass(type):
    def __new__(cls, name, bases, dict):
        ns = dict.pop('_namespace', None)
        if ns and not isinstance(ns, six.string_types):
            raise ORMError("The _namespace attribute must be a string, not %s"%type(ns))
        dict['_namespace'] = ns or name
        if name in MODELS or dict['_namespace'] in MODELS:
            raise ORMError("Cannot have two models with the same name (%s) or namespace (%s)"%(name, dict['_namespace']))
        dict['_required'] = required = set()
        dict['_index'] = index = set()
        dict['_unique'] = unique = set()
        dict['_cunique'] = cunique = set()
        dict['_prefix'] = prefix = set()
        dict['_suffix'] = suffix = set()

        dict['_columns'] = columns = {}
        pkey = None

        # load all columns from any base classes to allow for validation
        odict = {}
        for ocls in reversed(bases):
            if hasattr(ocls, '_columns'):
                odict.update(ocls._columns)
        odict.update(dict)
        dict = odict

        if not any(isinstance(col, PrimaryKey) for col in dict.values()):
            if 'id' in dict:
                raise ColumnError("Cannot have non-primary key named 'id' when no explicit PrimaryKey() is defined")
            dict['id'] = PrimaryKey()

        composite_unique = []
        many_to_one = defaultdict(list)

        # validate all of our columns to ensure that they fulfill our
        # expectations
        for attr, col in dict.items():
            if isinstance(col, Column):
                columns[attr] = col
                if col._required:
                    required.add(attr)
                if col._index:
                    index.add(attr)
                if col._prefix:
                    if not USE_LUA:
                        raise ColumnError("Lua scripting must be enabled to support prefix indexes (%s.%s)"%(name, attr))
                    prefix.add(attr)
                if col._suffix:
                    if not USE_LUA:
                        raise ColumnError("Lua scripting must be enabled to support suffix indexes (%s.%s)"%(name, attr))
                    suffix.add(attr)
                if col._unique:
                    # We only allow one for performance when USE_LUA is False
                    if unique and not USE_LUA:
                        raise ColumnError(
                            "Only one unique column allowed, you have at least two: %s %s"%(
                            attr, unique)
                        )
                    unique.add(attr)

            if isinstance(col, PrimaryKey):
                if pkey:
                    raise ColumnError("Only one primary key column allowed, you have: %s %s"%(
                        pkey, attr)
                    )
                pkey = attr

            if isinstance(col, OneToMany) and not col._column and col._ftable in MODELS:
                # Check to make sure that the foreign ManyToOne/OneToMany table
                # doesn't have multiple references to this table to require an
                # explicit foreign column.
                refs = []
                for _a, _c in MODELS[col._ftable]._columns.items():
                    if isinstance(_c, (ManyToOne, OneToOne)) and _c._ftable == name:
                        refs.append(_a)
                if len(refs) > 1:
                    raise ColumnError("Missing required column argument to OneToMany definition on column %s"%(attr,))

            if isinstance(col, (ManyToOne, OneToOne)):
                many_to_one[col._ftable].append((attr, col))
                MODELS_REFERENCED.setdefault(col._ftable, []).append((dict['_namespace'], attr, col._on_delete))

            if attr == 'unique_together':
                if not USE_LUA:
                    raise ColumnError("Lua scripting must be enabled to support multi-column uniqueness constraints")
                composite_unique = col

        # verify reverse OneToMany attributes for these ManyToOne/OneToOne
        # attributes if created after referenced models
        for t, cols in many_to_one.items():
            if len(cols) == 1:
                continue
            if t not in MODELS:
                continue
            for _a, _c in MODELS[t]._columns.items():
                if isinstance(_c, OneToMany) and _c._ftable == name and not _c._column:
                    raise ColumnError("Foreign model OneToMany attribute %s.%s missing column argument"%(t, _a))

        # handle multi-column uniqueness constraints
        if composite_unique and isinstance(composite_unique[0], six.string_types):
            composite_unique = [composite_unique]

        seen = {}
        for comp in composite_unique:
            key = tuple(sorted(set(comp)))
            if len(key) == 1:
                raise ColumnError("Single-column unique constraint: %r should be defined via 'unique=True' on the %r column"%(
                    comp, key[0]))
            if key in seen:
                raise ColumnError("Multi-column unique constraint: %r not different than earlier constrant: %r"%(
                    comp, seen[key]))
            for col in key:
                if col not in columns:
                    raise ColumnError("Multi-column unique index %r references non-existant column %r"%(
                        comp, col))
            seen[key] = comp
            cunique.add(key)

        dict['_pkey'] = pkey
        dict['_gindex'] = GeneralIndex(dict['_namespace'])

        MODELS[dict['_namespace']] = MODELS[name] = model = type.__new__(cls, name, bases, dict)
        return model

class Model(six.with_metaclass(_ModelMetaclass, object)):
    '''
    This is the base class for all models. You subclass from this base Model
    in order to create a model with columns. As an example::

        class User(Model):
            email_address = String(required=True, unique=True)
            salt = String(default='')
            hash = String(default='')
            created_at = Float(default=time.time, index=True)

    Which can then be used like::

        user = User(email_addrss='user@domain.com')
        user.save() # session.commit() or session.flush() works too
        user = User.get_by(email_address='user@domain.com')
        user = User.get(5)
        users = User.get([2, 6, 1, 7])

    To perform arbitrary queries on entities involving the indices that you
    defined (by passing ``index=True`` on column creation), you access the
    ``.query`` class property on the model::

        query = User.query
        query = query.filter(created_at=(time.time()-86400, time.time()))
        users = query.execute()

    .. note:: You can perform single or chained queries against any/all columns
      that were defined with ``index=True``.

    **Composite/multi-column unique constraints**

    As of version 0.28.0 and later, rom supports the ability for you to have a
    unique constraint involving multiple columns. Individual columns can be
    defined unique by passing the 'unique=True' specifier during column
    definition as always.

    The attribute ``unique_together`` defines those groups of columns that when
    taken together must be unique for ``.save()`` to complete successfully.
    This will work almost exactly the same as Django's ``unique_together``, and
    is comparable to SQLAlchemy's ``UniqueConstraint()``.

    Usage::

        class UniquePosition(Model):
            x = Integer()
            y = Integer()

            unique_together = [
                ('x', 'y'),
            ]

    .. note:: If one or more of the column values on an entity that is part of a
        unique constrant is None in Python, the unique constraint won't apply.
        This is the typical behavior of nulls in unique constraints inside both
        MySQL and Postgres.
    '''
    def __init__(self, **kwargs):
        self._new = not kwargs.pop('_loading', False)
        model = self._namespace
        self._data = {}
        self._last = {}
        self._modified = False
        self._deleted = False
        self._init = False
        for attr in self._columns:
            cval = kwargs.get(attr, None)
            data = (model, attr, cval, not self._new)
            if self._new and attr == self._pkey and cval:
                raise InvalidColumnValue("Cannot pass primary key on object creation")
            setattr(self, attr, data)
            if cval != None:
                if not isinstance(cval, six.string_types):
                    cval = self._columns[attr]._to_redis(cval)
                self._last[attr] = cval
        self._init = True
        session.add(self)

    @ClassProperty
    def _connection(cls):
        return _connect(cls)

    def refresh(self, force=False):
        if self._deleted:
            return
        if self._modified and not force:
            raise InvalidOperation("Cannot refresh a modified entity without passing force=True to override modified data")
        if self._new:
            raise InvalidOperation("Cannot refresh a new entity")

        conn = _connect(self)
        data = conn.hgetall(self._pk)
        if six.PY3:
            data = dict((k.decode(), v.decode()) for k, v in data.items())
        self.__init__(_loading=True, **data)

    @property
    def _pk(self):
        return '%s:%s'%(self._namespace, getattr(self, self._pkey))

    @classmethod
    def _apply_changes(cls, old, new, full=False, delete=False):
        use_lua = USE_LUA
        conn = _connect(cls)
        pk = old.get(cls._pkey) or new.get(cls._pkey)
        if not pk:
            raise ColumnError("Missing primary key value")

        model = cls._namespace
        key = '%s:%s'%(model, pk)
        pipe = conn.pipeline(True)

        columns = cls._columns
        while 1:
            changes = 0
            keys = set()
            scores = {}
            data = {}
            unique = {}
            deleted = []
            udeleted = {}
            prefix = []
            suffix = []
            redis_data = {}

            # check for unique keys
            if len(cls._unique) > 1 and not use_lua:
                raise ColumnError(
                    "Only one unique column allowed, you have: %s"%(unique,))

            if cls._cunique and not use_lua:
                raise ColumnError(
                    "Cannot use multi-column unique constraint 'unique_together' with Lua disabled")

            if not use_lua:
                for col in cls._unique:
                    ouval = old.get(col)
                    nuval = new.get(col)
                    nuvale = columns[col]._to_redis(nuval) if nuval is not None else None

                    if six.PY2 and not isinstance(ouval, str):
                        ouval = columns[col]._to_redis(ouval)
                    if not (nuval and (ouval != nuvale or full)):
                        # no changes to unique columns
                        continue

                    ikey = "%s:%s:uidx"%(model, col)
                    pipe.watch(ikey)
                    ival = pipe.hget(ikey, nuvale)
                    ival = ival if isinstance(ival, str) or ival is None else ival.decode()
                    if not ival or ival == str(pk):
                        pipe.multi()
                    else:
                        pipe.unwatch()
                        raise UniqueKeyViolation("Value %r for %s is not distinct"%(nuval, ikey))

            # update individual columns
            for attr in cls._columns:
                ikey = None
                if attr in cls._unique:
                    ikey = "%s:%s:uidx"%(model, attr)

                ca = columns[attr]
                roval = old.get(attr)
                oval = ca._from_redis(roval) if roval is not None else None

                nval = new.get(attr)
                rnval = ca._to_redis(nval) if nval is not None else None
                if rnval is not None:
                    redis_data[attr] = rnval

                # Add/update standard index
                if ca._keygen and not delete and nval is not None and (ca._index or ca._prefix or ca._suffix):
                    generated = ca._keygen(nval)
                    if isinstance(generated, (list, tuple, set)):
                        if ca._index:
                            for k in generated:
                                keys.add('%s:%s'%(attr, k))
                        if ca._prefix:
                            for k in generated:
                                prefix.append([attr, k])
                        if ca._suffix:
                            for k in generated:
                                if six.PY2 and isinstance(k, str) and isinstance(ca, Text):
                                    try:
                                        suffix.append([attr, k.decode('utf-8')[::-1].encode('utf-8')])
                                    except UnicodeDecodeError:
                                        suffix.append([attr, k[::-1]])
                                else:
                                    suffix.append([attr, k[::-1]])
                    elif isinstance(generated, dict):
                        for k, v in generated.items():
                            if not k:
                                scores[attr] = v
                            else:
                                scores['%s:%s'%(attr, k)] = v
                        if ca._prefix:
                            if ca._keygen not in (SIMPLE, CASE_INSENSITIVE):
                                warnings.warn("Prefix indexes are currently not enabled for non-standard keygen functions", stacklevel=2)
                            else:
                                prefix.append([attr, nval if ca._keygen is SIMPLE else nval.lower()])
                        if ca._suffix:
                            if ca._keygen not in (SIMPLE, CASE_INSENSITIVE):
                                warnings.warn("Prefix indexes are currently not enabled for non-standard keygen functions", stacklevel=2)
                            else:
                                ex = (lambda x:x) if ca._keygen is SIMPLE else (lambda x:x.lower())
                                if six.PY2 and isinstance(nval, str) and isinstance(ca, Text):
                                    try:
                                        suffix.append([attr, ex(nval.decode('utf-8')[::-1]).encode('utf-8')])
                                    except UnicodeDecodeError:
                                        suffix.append([attr, ex(nval[::-1])])
                                else:
                                    suffix.append([attr, ex(nval[::-1])])
                    elif not generated:
                        pass
                    else:
                        raise ColumnError("Don't know how to turn %r into a sequence of keys"%(generated,))

                if nval == oval and not full:
                    continue

                changes += 1

                # Delete removed columns
                if nval is None and oval is not None:
                    if use_lua:
                        deleted.append(attr)
                        if ikey:
                            udeleted[attr] = roval
                    else:
                        pipe.hdel(key, attr)
                        if ikey:
                            pipe.hdel(ikey, roval)
                        # Index removal will occur by virtue of no index entry
                        # for this column.
                    continue

                # Add/update column value
                if nval is not None:
                    data[attr] = rnval

                # Add/update unique index
                if ikey:
                    if six.PY2 and not isinstance(roval, str):
                        roval = columns[attr]._to_redis(roval)
                    if use_lua:
                        if oval is not None and roval != rnval:
                            udeleted[attr] = oval
                        if rnval is not None:
                            unique[attr] = rnval
                    else:
                        if oval is not None:
                            pipe.hdel(ikey, roval)
                        pipe.hset(ikey, rnval, pk)

            # Add/update multi-column unique constraint
            for uniq in cls._cunique:
                attr = ':'.join(uniq)

                odata = [old.get(c) for c in uniq]
                ndata = [new.get(c) for c in uniq]
                ndata = [columns[c]._to_redis(nv) if nv is not None else None for c, nv in zip(uniq, ndata)]

                if odata != ndata and None not in odata:
                    udeleted[attr] = _encode_unique_constraint(odata)

                if None not in ndata:
                    unique[attr] = _encode_unique_constraint(ndata)

            id_only = str(pk)
            if use_lua:
                redis_writer_lua(conn, model, id_only, unique, udeleted,
                    deleted, data, list(keys), scores, prefix, suffix, delete)
                return changes, redis_data
            elif delete:
                changes += 1
                cls._gindex._unindex(conn, pipe, id_only)
                pipe.delete(key)
            else:
                if data:
                    pipe.hmset(key, data)
                cls._gindex.index(conn, id_only, keys, scores, prefix, suffix, pipe=pipe)

            try:
                pipe.execute()
            except redis.exceptions.WatchError:
                continue
            else:
                return changes, redis_data

    def to_dict(self):
        '''
        Returns a copy of all data assigned to columns in this entity. Useful
        for returning items to JSON-enabled APIs. If you want to copy an
        entity, you should look at the ``.copy()`` method.
        '''
        return dict(self._data)

    def save(self, full=False):
        '''
        Saves the current entity to Redis. Will only save changed data by
        default, but you can force a full save by passing ``full=True``.
        '''
        new = self.to_dict()
        ret, data = self._apply_changes(self._last, new, full or self._new)
        self._last = data
        self._new = False
        self._modified = False
        self._deleted = False
        return ret

    def delete(self, **kwargs):
        '''
        Deletes the entity immediately. Also performs any on_delete operations
        specified as part of column definitions.
        '''
        if kwargs.get('skip_on_delete_i_really_mean_it') is not SKIP_ON_DELETE:
            _on_delete(self)

        session.forget(self)
        self._apply_changes(self._last, {}, delete=True)
        self._modified = True
        self._deleted = True

    def copy(self):
        '''
        Creates a shallow copy of the given entity (any entities that can be
        retrieved from a OneToMany relationship will not be copied).
        '''
        x = self.to_dict()
        x.pop(self._pkey)
        return self.__class__(**x)

    @classmethod
    def get(cls, ids):
        '''
        Will fetch one or more entities of this type from the session or
        Redis.

        Used like::

            MyModel.get(5)
            MyModel.get([1, 6, 2, 4])

        Passing a list or a tuple will return multiple entities, in the same
        order that the ids were passed.
        '''
        conn = _connect(cls)
        # prepare the ids
        single = not isinstance(ids, (list, tuple, set, frozenset))
        if single:
            ids = [ids]
        pks = ['%s:%s'%(cls._namespace, id) for id in map(int, ids)]
        # get from the session, if possible
        out = list(map(session.get, pks))
        # if we couldn't get an instance from the session, load from Redis
        if None in out:
            pipe = conn.pipeline(True)
            idxs = []
            # Fetch missing data
            for i, data in enumerate(out):
                if data is None:
                    idxs.append(i)
                    pipe.hgetall(pks[i])
            # Update output list
            for i, data in zip(idxs, pipe.execute()):
                if data:
                    if six.PY3:
                        data = dict((k.decode(), v.decode()) for k, v in data.items())
                    out[i] = cls(_loading=True, **data)
            # Get rid of missing models
            out = [x for x in out if x]
        if single:
            return out[0] if out else None
        return out

    @classmethod
    def get_by(cls, **kwargs):
        '''
        This method offers a simple query method for fetching entities of this
        type via attribute numeric ranges (such columns must be ``indexed``),
        or via ``unique`` columns.

        Some examples::

            user = User.get_by(email_address='user@domain.com')
            # gets up to 25 users created in the last 24 hours
            users = User.get_by(
                created_at=(time.time()-86400, time.time()),
                _limit=(0, 25))

        Optional keyword-only arguments:

            * *_limit* - A 2-tuple of (offset, count) that can be used to
              paginate or otherwise limit results returned by a numeric range
              query
            * *_numeric* - An optional boolean defaulting to False that forces
              the use of a numeric index for ``.get_by(col=val)`` queries even
              when ``col`` has an existing unique index

        If you would like to make queries against multiple columns or with
        multiple criteria, look into the Model.query class property.

        .. note:: rom will attempt to use a unique index first, then a numeric
            index if there was no unique index. You can explicitly tell rom to
            only use the numeric index by using ``.get_by(..., _numeric=True)``.
        .. note:: Ranged queries with `get_by(col=(start, end))` will only work
            with columns that use a numeric index.
        '''
        conn = _connect(cls)
        model = cls._namespace
        # handle limits and query requirements
        _limit = kwargs.pop('_limit', ())
        if _limit and len(_limit) != 2:
            raise QueryError("Limit must include both 'offset' and 'count' parameters")
        elif _limit and not all(isinstance(x, six.integer_types) for x in _limit):
            raise QueryError("Limit arguments must both be integers")
        if len(kwargs) != 1:
            raise QueryError("We can only fetch object(s) by exactly one attribute, you provided %s"%(len(kwargs),))

        _numeric = bool(kwargs.pop('_numeric', None))

        for attr, value in kwargs.items():
            plain_attr = attr.partition(':')[0]
            if isinstance(value, tuple) and len(value) != 2:
                raise QueryError("Range queries must include exactly two endpoints")

            # handle unique index lookups
            if attr in cls._unique and (plain_attr not in cls._index or not _numeric):
                if isinstance(value, tuple):
                    raise QueryError("Cannot query a unique index with a range of values")
                single = not isinstance(value, list)
                if single:
                    value = [value]
                qvalues = list(map(cls._columns[attr]._to_redis, value))
                ids = [x for x in conn.hmget('%s:%s:uidx'%(model, attr), qvalues) if x]
                if not ids:
                    return None if single else []
                return cls.get(ids[0] if single else ids)

            if plain_attr not in cls._index:
                raise QueryError("Cannot query on a column without an index")

            if isinstance(value, NUMERIC_TYPES) and not isinstance(value, bool):
                value = (value, value)

            if isinstance(value, tuple):
                # this is a numeric range query, we'll just pull it directly
                args = list(value)
                for i, a in enumerate(args):
                    # Handle the ranges where None is -inf on the left and inf
                    # on the right when used in the context of a range tuple.
                    args[i] = ('-inf', 'inf')[i] if a is None else cls._columns[attr]._to_redis(a)
                if _limit:
                    args.extend(_limit)
                ids = conn.zrangebyscore('%s:%s:idx'%(model, attr), *args)
                if not ids:
                    return []
                return cls.get(ids)

            # defer other index lookups to the query object
            query = cls.query.filter(**{attr: value})
            if _limit:
                query = query.limit(*_limit)
            return query.all()

    @ClassProperty
    def query(cls):
        '''
        Returns a ``Query`` object that refers to this model to handle
        subsequent filtering.
        '''
        return Query(cls)

_redis_writer_lua = _script_load('''
local namespace = ARGV[1]
local id = ARGV[2]
local is_delete = cjson.decode(ARGV[11])

-- check and update unique column constraints
for i, write in ipairs({false, true}) do
    for col, value in pairs(cjson.decode(ARGV[3])) do
        local key = string.format('%s:%s:uidx', namespace, col)
        if write then
            redis.call('HSET', key, value, id)
        else
            local known = redis.call('HGET', key, value)
            if known ~= id and known ~= false then
                return col
            end
        end
    end
end

-- remove deleted unique constraints
for col, value in pairs(cjson.decode(ARGV[4])) do
    local key = string.format('%s:%s:uidx', namespace, col)
    local known = redis.call('HGET', key, value)
    if known == id then
        redis.call('HDEL', key, value)
    end
end

-- remove deleted columns
local deleted = cjson.decode(ARGV[5])
if #deleted > 0 then
    redis.call('HDEL', string.format('%s:%s', namespace, id), unpack(deleted))
end

-- update changed/added columns
local data = cjson.decode(ARGV[6])
if #data > 0 then
    redis.call('HMSET', string.format('%s:%s', namespace, id), unpack(data))
end

-- remove old index data, update util.clean_index_lua when changed
local idata = redis.call('HGET', namespace .. '::', id)
if idata then
    idata = cjson.decode(idata)
    if #idata == 2 then
        idata[3] = {}
        idata[4] = {}
    end
    for i, key in ipairs(idata[1]) do
        redis.call('SREM', string.format('%s:%s:idx', namespace, key), id)
    end
    for i, key in ipairs(idata[2]) do
        redis.call('ZREM', string.format('%s:%s:idx', namespace, key), id)
    end
    for i, data in ipairs(idata[3]) do
        local key = string.format('%s:%s:pre', namespace, data[1])
        local mem = string.format('%s\0%s', data[2], id)
        redis.call('ZREM', key, mem)
    end
    for i, data in ipairs(idata[4]) do
        local key = string.format('%s:%s:suf', namespace, data[1])
        local mem = string.format('%s\0%s', data[2], id)
        redis.call('ZREM', key, mem)
    end
end

if is_delete then
    redis.call('DEL', string.format('%s:%s', namespace, id))
    redis.call('HDEL', namespace .. '::', id)
end

-- add new key index data
local nkeys = cjson.decode(ARGV[7])
for i, key in ipairs(nkeys) do
    redis.call('SADD', string.format('%s:%s:idx', namespace, key), id)
end

-- add new scored index data
local nscored = {}
for key, score in pairs(cjson.decode(ARGV[8])) do
    redis.call('ZADD', string.format('%s:%s:idx', namespace, key), score, id)
    nscored[#nscored + 1] = key
end

-- add new prefix data
local nprefix = {}
for i, data in ipairs(cjson.decode(ARGV[9])) do
    local key = string.format('%s:%s:pre', namespace, data[1])
    local mem = string.format("%s\0%s", data[2], id)
    redis.call('ZADD', key, data[3], mem)
    nprefix[#nprefix + 1] = {data[1], data[2]}
end

-- add new suffix data
local nsuffix = {}
for i, data in ipairs(cjson.decode(ARGV[10])) do
    local key = string.format('%s:%s:suf', namespace, data[1])
    local mem = string.format("%s\0%s", data[2], id)
    redis.call('ZADD', key, data[3], mem)
    nsuffix[#nsuffix + 1] = {data[1], data[2]}
end

if not is_delete then
    -- update known index data
    local encoded = cjson.encode({nkeys, nscored, nprefix, nsuffix})
    redis.call('HSET', namespace .. '::', id, encoded)
end
return #nkeys + #nscored + #nprefix + #nsuffix
''')

def _fix_bytes(d):
    if six.PY2:
        raise TypeError
    if isinstance(d, bytes):
        return d.decode('latin-1')
    raise TypeError

def redis_writer_lua(conn, namespace, id, unique, udelete, delete, data, keys,
                     scored, prefix, suffix, is_delete):
    ldata = []
    for pair in data.items():
        ldata.extend(pair)

    for item in prefix:
        item.append(_prefix_score(item[-1]))
    for item in suffix:
        item.append(_prefix_score(item[-1]))

    result = _redis_writer_lua(conn, [], [namespace, id] + [json.dumps(x, default=_fix_bytes)
        for x in [unique, udelete, delete, ldata, keys, scored, prefix, suffix, is_delete]])
    if isinstance(result, six.binary_type):
        result = result.decode()
        raise UniqueKeyViolation("Value %r for %s:%s:uidx not distinct"%(unique[result], namespace, result))
