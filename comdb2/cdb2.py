from ._cdb2api import ffi, lib
from datetime import datetime, timedelta
import pytz
import six

__all__ = ['Error', 'Handle', 'DatetimeUs', 'ERROR_CODE', 'TYPE']

# Pull all comdb2 error codes from cdb2api.h into our namespace
ERROR_CODE = {k[len('CDB2ERR_'):]: v
              for k, v in ffi.typeof('enum cdb2_errors').relements.items()
              if k.startswith('CDB2ERR_')}

# Pull comdb2 column types from cdb2api.h into our namespace
TYPE = {k[len('CDB2_'):]: v
        for k, v in ffi.typeof('enum cdb2_coltype').relements.items()
        if k.startswith('CDB2_')}


class DatetimeUs(datetime):
    '''DatetimeUs parameters to Cursor.execute will give microsecond precision.

    This differs from datetime.datetime parameters, which only give millisecond
    precision.  The behavior is otherwise identical to datetime.datetime, with
    the exception of a single extra classmethod, fromdatetime, for constructing
    a DatetimeUs from a datetime.datetime.
    '''
    @classmethod
    def fromdatetime(cls, dt):
        return DatetimeUs(dt.year, dt.month, dt.day,
                          dt.hour, dt.minute, dt.second, dt.microsecond,
                          dt.tzinfo)

    def __add__(self, other):
        ret = super(DatetimeUs, self).__add__(other)
        if isinstance(ret, datetime):
            return DatetimeUs.fromdatetime(ret)
        return ret  # must be a timedelta

    def __sub__(self, other):
        ret = super(DatetimeUs, self).__sub__(other)
        if isinstance(ret, datetime):
            return DatetimeUs.fromdatetime(ret)
        return ret  # must be a timedelta

    def __radd__(self, other):
        return self + other

    @classmethod
    def now(cls, tz=None):
        ret = super(DatetimeUs, cls).now(tz)
        return DatetimeUs.fromdatetime(ret)

    @classmethod
    def fromtimestamp(cls, timestamp, tz=None):
        ret = super(DatetimeUs, cls).fromtimestamp(timestamp, tz)
        return DatetimeUs.fromdatetime(ret)

    def astimezone(self, tz):
        ret = super(DatetimeUs, self).astimezone(tz)
        return DatetimeUs.fromdatetime(ret)


class Error(RuntimeError):
    def __init__(self, error_code, error_message):
        self.error_code = error_code
        self.error_message = error_message
        super(Error, self).__init__(error_code, error_message)


if six.PY2:
    _ffi_string = ffi.string
else:
    def _ffi_string(cdata):
        return ffi.string(cdata).decode('utf-8')


def _construct_datetime(cls, tm, microseconds, tzname):
    timezone = pytz.timezone(_ffi_string(tzname))
    timestamp = cls(year=tm.tm_year + 1900,
                    month=tm.tm_mon + 1,
                    day=tm.tm_mday,
                    hour=tm.tm_hour,
                    minute=tm.tm_min,
                    second=tm.tm_sec,
                    microsecond=microseconds)
    return timezone.localize(timestamp, is_dst=tm.tm_isdst)


def _datetime(ptr):
    return _construct_datetime(datetime, ptr.tm, ptr.msec * 1000, ptr.tzname)


def _datetimeus(ptr):
    return _construct_datetime(DatetimeUs, ptr.tm, ptr.usec, ptr.tzname)


def _errstr(hndl):
    errstr = _ffi_string(lib.cdb2_errstr(hndl))
    if not isinstance(errstr, str):
        errstr = errstr.decode('utf-8')  # bytes to str for Python 3
    return errstr


def _check_rc(rc, hndl):
    if rc != 0:
        errstr = _errstr(hndl)
        raise Error(rc, errstr)


def _cdb2_client_datetime_common(val, ptr):
    struct_time = val.timetuple()
    ptr.tm.tm_sec = struct_time.tm_sec
    ptr.tm.tm_min = struct_time.tm_min
    ptr.tm.tm_hour = struct_time.tm_hour
    ptr.tm.tm_mday = struct_time.tm_mday
    ptr.tm.tm_mon = struct_time.tm_mon - 1
    ptr.tm.tm_year = struct_time.tm_year - 1900
    ptr.tm.tm_wday = struct_time.tm_wday
    ptr.tm.tm_yday = struct_time.tm_yday - 1
    ptr.tm.tm_isdst = struct_time.tm_isdst
    if val.tzname() is not None:
        tzname = val.tzname()
        if not isinstance(tzname, bytes):
            tzname = tzname.encode('utf-8')
        ptr.tzname = tzname


def _cdb2_client_datetime_t(val):
    ptr = ffi.new("cdb2_client_datetime_t *")
    val += timedelta(microseconds=500)  # For rounding to nearest millisecond
    _cdb2_client_datetime_common(val, ptr)
    ptr.msec = val.microsecond // 1000  # For rounding to nearest millisecond
    return ptr


def _cdb2_client_datetimeus_t(val):
    ptr = ffi.new("cdb2_client_datetimeus_t *")
    _cdb2_client_datetime_common(val, ptr)
    ptr.usec = val.microsecond
    return ptr


def _bind_args(val):
    if val is None:
        return lib.CDB2_INTEGER, ffi.NULL, 0
    elif isinstance(val, six.integer_types):
        try:
            return lib.CDB2_INTEGER, ffi.new("int64_t *", val), 8
        except OverflowError as e:
            raise Error(lib.CDB2ERR_CONV_FAIL,
                        "Can't bind value %s: %s" % (val, e))
    elif isinstance(val, float):
        return lib.CDB2_REAL, ffi.new("double *", val), 8
    elif isinstance(val, bytes):
        return lib.CDB2_BLOB, val, len(val)
    elif isinstance(val, six.text_type):
        val = val.encode('utf-8')
        return lib.CDB2_CSTRING, val, len(val)
    elif isinstance(val, DatetimeUs):
        return (lib.CDB2_DATETIMEUS, _cdb2_client_datetimeus_t(val),
                ffi.sizeof("cdb2_client_datetimeus_t"))
    elif isinstance(val, datetime):
        return (lib.CDB2_DATETIME, _cdb2_client_datetime_t(val),
                ffi.sizeof("cdb2_client_datetime_t"))
    raise Error(lib.CDB2ERR_NOTSUPPORTED,
                "Can't map type %s to a comdb2 type" % val.__class__.__name__)


class Handle(object):
    def __init__(self, database_name, tier="default", flags=0, tz='UTC'):
        self._more_rows_available = False
        self._hndl_p = None
        self._hndl = None

        if not isinstance(database_name, bytes):
            database_name = database_name.encode('utf-8')  # Python 3

        if not isinstance(tier, bytes):
            tier = tier.encode('utf-8')  # Python 3

        self._hndl_p = ffi.new("struct cdb2_hndl **")
        rc = lib.cdb2_open(self._hndl_p, database_name, tier, 0)
        if rc != lib.CDB2_OK:
            errstr = _errstr(self._hndl_p[0])
            lib.cdb2_close(self._hndl_p[0])
            raise Error(rc, errstr)

        self._hndl = self._hndl_p[0]
        self._column_range = []

        if tz is not None:
            # XXX This is technically SQL injectable, but
            # a) SET statements don't go through a normal SQL parser anyway,
            # b) DRQS 86887068 leaves us no choice.
            self.execute("set timezone %s" % tz)

    def __del__(self):
        if self._hndl is not None:
            self.close()

    def close(self):
        self._more_rows_available = False
        lib.cdb2_close(self._hndl)
        self._hndl = None
        self._hndl_p = None

        def closed_error(func_name):
            raise Error(lib.CDB2ERR_NOTCONNECTED,
                        "%s() called on closed connection" % func_name)

        for func in ("close", "execute",
                     "get_effects", "column_names", "column_types"):
            setattr(self, func, lambda *a, **k: closed_error(func))

    def execute(self, sql, parameters=None):
        self._column_range = []
        self._consume_all_rows()

        if not isinstance(sql, bytes):
            sql = sql.encode('utf-8')  # Python 3

        try:
            if parameters is not None:
                params_cdata = self._bind_params(parameters)  # NOQA
                # XXX params_cdata owns memory that has been bound to cdb2api,
                # so must not be garbage collected before cdb2_run_statement
            rc = lib.cdb2_run_statement(self._hndl, sql)
        finally:
            lib.cdb2_clearbindings(self._hndl)

        _check_rc(rc, self._hndl)
        self._more_rows_available = True
        self._next_record()
        self._column_range = range(lib.cdb2_numcolumns(self._hndl))
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if not self._more_rows_available:
            raise StopIteration()

        try:
            data = [self._column_value(i) for i in self._column_range]
            self._next_record()
        except UnicodeDecodeError as e:
            # Allow _consume_all_rows to raise.  Its error code is relevant,
            # since it may indicate that the connection is no longer usable.
            self._consume_all_rows()
            # If it didn't, raise our own error for the failed UTF-8 decode.
            raise Error(lib.CDB2ERR_CONV_FAIL, str(e))

        return data

    next = __next__

    def get_effects(self):
        effects = ffi.new("cdb2_effects_tp *")
        self._more_rows_available = False
        # XXX cdb2_get_effects consumes any remaining rows implicitly
        rc = lib.cdb2_get_effects(self._hndl, effects)
        _check_rc(rc, self._hndl)
        return (effects.num_affected,
                effects.num_selected,
                effects.num_updated,
                effects.num_deleted,
                effects.num_inserted)

    def column_names(self):
        return [_ffi_string(lib.cdb2_column_name(self._hndl, i))
                for i in self._column_range]

    def column_types(self):
        return [lib.cdb2_column_type(self._hndl, i)
                for i in self._column_range]

    def _consume_all_rows(self):
        while self._more_rows_available:
            self._next_record()

    def _bind_params(self, parameters):
        params_cdata = []
        for key, val in parameters.items():
            if not isinstance(key, bytes):
                key = key.encode('utf-8')  # str to bytes for Python 3

            typecode, ptr, size = _bind_args(val)

            params_cdata.append((key, ptr))
            rc = lib.cdb2_bind_param(self._hndl, key, typecode, ptr, size)
            _check_rc(rc, self._hndl)

        return params_cdata

    def _next_record(self):
        try:
            rc = lib.cdb2_next_record(self._hndl)
        except:
            self._more_rows_available = False
            raise
        else:
            if rc != 0:
                self._more_rows_available = False
                if rc != lib.CDB2_OK_DONE:
                    _check_rc(rc, self._hndl)

    def _column_value(self, i):
        val = lib.cdb2_column_value(self._hndl, i)
        typecode = lib.cdb2_column_type(self._hndl, i)

        if val == ffi.NULL:
            return None
        if typecode == lib.CDB2_INTEGER:
            return lib.integer_value(val)
        if typecode == lib.CDB2_REAL:
            return lib.real_value(val)
        if typecode == lib.CDB2_BLOB:
            size = lib.cdb2_column_size(self._hndl, i)
            return bytes(ffi.buffer(val, size))
        if typecode == lib.CDB2_CSTRING:
            size = lib.cdb2_column_size(self._hndl, i)
            return six.text_type(ffi.buffer(val, size - 1), "utf-8")
        if typecode == lib.CDB2_DATETIMEUS:
            return _datetimeus(lib.datetimeus_value(val))
        if typecode == lib.CDB2_DATETIME:
            return _datetime(lib.datetime_value(val))
        raise Error(lib.CDB2ERR_NOTSUPPORTED,
                    "Can't handle type %d returned by database!" % typecode)
