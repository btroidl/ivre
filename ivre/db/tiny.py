#! /usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of IVRE.
# Copyright 2011 - 2019 Pierre LALET <pierre.lalet@cea.fr>
#
# IVRE is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IVRE is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
# License for more details.
#
# You should have received a copy of the GNU General Public License
# along with IVRE. If not, see <http://www.gnu.org/licenses/>.

"""This sub-module contains functions to interact with TinyDB
databases.

"""


from collections import defaultdict, Counter
from copy import deepcopy
from datetime import datetime, timedelta
from functools import cmp_to_key
import os
import re
import socket
import struct
from uuid import uuid1


from future.builtins import int as int_types
from future.utils import viewitems
from past.builtins import basestring
from tinydb import TinyDB as TDB, Query
from tinydb.database import Document


from ivre.db import DB, DBActive, DBNmap, DBPassive, DBView
from ivre import utils
from ivre.xmlnmap import ALIASES_TABLE_ELEMS, Nmap2DB


class TinyDB(DB):

    """A DB using TinyDB backend"""

    flt_empty = Query()
    no_limit = None

    def __init__(self, url):
        super(TinyDB, self).__init__()
        self.basepath = url.path

    @property
    def db(self):
        """The DB"""
        try:
            return self._db
        except AttributeError:
            self._db = TDB(os.path.join(self.basepath,
                                        "%s.json" % self.dbname))
            return self._db

    def invalidate_cache(self):
        try:
            self._db.close()
        except AttributeError:
            pass
        del self._db

    def init(self):
        self.db.purge_tables()

    def count(self, flt):
        return self.db.count(flt)

    def get(self, flt, fields=None, sort=None, limit=None, skip=None):
        result = self.db.search(flt)
        if fields is not None:

            _fields = {}
            for fld in fields:
                try:
                    flds, lastfld = fld.rsplit('.', 1)
                except ValueError:
                    _fields[fld] = True
                else:
                    cur = _fields
                    for subfld in flds.split('.'):
                        cur = cur.setdefault(subfld, {})
                    cur[lastfld] = True
            fields = _fields

            def _extractor(rec, wanted_fields, base=""):
                if isinstance(rec, Document):
                    res = Document({}, doc_id=rec.doc_id)
                else:
                    res = {}
                for fld, value in viewitems(wanted_fields):
                    if fld not in rec:
                        continue
                    if value is True:
                        res[fld] = rec[fld]
                        continue
                    if base:
                        fullfld = '%s.%s' % (base, fld)
                    else:
                        fullfld = fld
                    if fullfld in self.list_fields:
                        res[fld] = [_extractor(subrec, value, base=fullfld)
                                    for subrec in rec[fld]]
                    else:
                        res[fld] = _extractor(rec[fld], value, base=fullfld)
                return res

        if not sort:
            if skip is not None:
                result = result[skip:]
            if limit is not None:
                result = result[:limit]
            if fields is not None:
                return [_extractor(rec, fields) for rec in result]
            return result

        def _cmp(v1, v2):
            for (k, o) in sort:
                f1 = v1
                f2 = v2
                for sk in k.split('.'):
                    f1 = (f1 or {}).get(sk)
                    f2 = (f2 or {}).get(sk)
                if f1 == f2:
                    continue
                if f1 is None:
                    # None is lower than anything
                    return -o
                if f2 is None:
                    return o
                if f1 < f2:
                    return -o
                return o
            return 0
        result = sorted(result, key=cmp_to_key(_cmp))
        if skip is not None:
            result = result[skip:]
        if limit is not None:
            result = result[:limit]
        if fields is not None:
            return [_extractor(rec, fields) for rec in result]
        return result

    @staticmethod
    def _searchstring_re_inarray(query, value, neg=False):
        if isinstance(value, utils.REGEXP_T):
            res = query.test(
                lambda val: any(value.search(subval) for subval in val)
            )
        else:
            res = query.any([value])
        if neg:
            return ~res
        return res

    @staticmethod
    def _searchstring_re(query, value, neg=False):
        if isinstance(value, utils.REGEXP_T):
            res = query.search(value.pattern, flags=value.flags)
            if neg:
                return ~res
            return res
        if neg:
            return query != value
        return query == value

    @classmethod
    def _generate_field_values(cls, record, field, base="", countfield=None,
                               countval=None):
        try:
            cur, field = field.split('.', 1)
        except ValueError:
            if field not in record:
                return
            if base:
                fullfield = '%s.%s' % (base, field)
            else:
                fullfield = field
            if fullfield in cls.list_fields:
                for val in record[field]:
                    if countval is not None:
                        yield val, countval
                    elif countfield is not None:
                        yield val, record.get(countfield, 1)
                    else:
                        yield val
            elif countval is not None:
                yield record[field], countval
            elif countfield is not None:
                yield record[field], record.get(countfield, 1)
            else:
                yield record[field]
            return
        if cur not in record:
            return
        if countfield is not None:
            if countfield.startswith('%s.' % cur):
                countfield = countfield.split('.', 1)[1]
            else:
                countval = record.get(countfield, 1)
                countfield = None
        record = record[cur]
        if base:
            base = "%s.%s" % (base, cur)
        else:
            base = cur
        if base in cls.list_fields:
            for subrec in record:
                for val in cls._generate_field_values(subrec, field, base=base,
                                                      countfield=countfield,
                                                      countval=countval):
                    yield val
        else:
            for val in cls._generate_field_values(record, field, base=base,
                                                  countfield=countfield,
                                                  countval=countval):
                yield val

    def _search_field_exists(self, field, base="", baseq=None):
        if baseq is None:
            baseq = Query()
        if '.' not in field:
            return getattr(baseq, field).exists()
        field, nextfields = field.split('.', 1)
        if base:
            fullfield = "%s.%s" % (base, field)
        else:
            fullfield = field
        if fullfield in self.list_fields:
            return getattr(baseq, field).any(
                self._search_field_exists(nextfields, base=fullfield)
            )
        return self._search_field_exists(nextfields, base=fullfield,
                                         baseq=getattr(baseq, field))

    def distinct(self, field, flt=None, sort=None, limit=None, skip=None):
        if flt is None:
            flt = self.flt_empty
        flt &= self._search_field_exists(field)
        return list(set(
            val
            for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                 fields=[field])
            for val in self._generate_field_values(rec, field)
        ))

    def remove(self, rec):
        """Removes the record from the active column. `rec` must be the
        record as returned by `.get()` or the record id.

        """
        if isinstance(rec, dict):
            rec = rec['_id']
        self.db.remove(cond=Query()._id == rec)

    @staticmethod
    def to_binary(data):
        return utils.encode_b64(data).decode()

    @staticmethod
    def from_binary(data):
        return utils.decode_b64(data.encode())

    @staticmethod
    def ip2internal(addr):
        if isinstance(addr, int_types):
            return addr
        val1, val2 = struct.unpack(
            '!QQ', utils.ip2bin(addr)
        )
        return (val1 << 64) + val2

    @staticmethod
    def internal2ip(addr):
        return utils.bin2ip(struct.pack('!QQ', addr >> 64,
                                        addr & 0xffffffffffffffff))

    @staticmethod
    def flt2str(flt):
        return str(flt)

    @staticmethod
    def _flt_and(cond1, cond2):
        return cond1 & cond2

    @staticmethod
    def _flt_or(cond1, cond2):
        return cond1 | cond2

    @staticmethod
    def searchnonexistent():
        return Query()._id == 0

    @staticmethod
    def searchobjectid(oid, neg=False):
        """Filters records by their ObjectID.  `oid` can be a single or many
        (as a list or any iterable) object ID(s), specified as strings.

        """
        q = Query()
        if isinstance(oid, list):
            res = q._id.one_of(oid)
            if neg:
                return ~res
            return res
        if neg:
            return q._id != oid
        return q._id == oid

    @staticmethod
    def searchversion(version):
        """Filters documents based on their schema's version."""
        q = Query()
        if version is None:
            return q.schema_version.exists()
        return q.schema_version == version

    @classmethod
    def searchhost(cls, addr, neg=False):
        """Filters (if `neg` == True, filters out) one particular host
        (IP address).

        """
        q = Query()
        addr = cls.ip2internal(addr)
        if neg:
            return q.addr != addr
        return q.addr == addr

    @classmethod
    def searchhosts(cls, hosts, neg=False):
        res = Query().addr.one_of([cls.ip2internal(addr) for addr in hosts])
        if neg:
            return ~res
        return res

    @classmethod
    def searchrange(cls, start, stop, neg=False):
        start = cls.ip2internal(start)
        stop = cls.ip2internal(stop)
        q = Query()
        res = (q.addr >= start) & (q.addr <= stop)
        if neg:
            return ~res
        return res

    @staticmethod
    def searchval(key, val):
        return getattr(Query(), key) == val

    @staticmethod
    def searchcmp(key, val, cmpop):
        q = getattr(Query(), key)
        if cmpop == '<':
            return q < val
        if cmpop == '<=':
            return q <= val
        if cmpop == '>':
            return q > val
        if cmpop == '>=':
            return q >= val
        raise Exception('Unknown operator %r (for key %r and val %r)' % (
            cmpop,
            key,
            val,
        ))


class TinyDBActive(TinyDB, DBActive):

    """An Active-specific DB using TinyDB backend

This will be used by TinyDBNmap & TinyDBView

    """

    def _get(self, *args, **kargs):
        for host in super(TinyDBActive, self).get(*args, **kargs):
            host = deepcopy(host)
            try:
                host['addr'] = self.internal2ip(host['addr'])
            except (KeyError, socket.error):
                pass
            for port in host.get('ports', []):
                try:
                    port['state_reason_ip'] = self.internal2ip(
                        port['state_reason_ip']
                    )
                except (KeyError, socket.error):
                    pass
            for trace in host.get('traces', []):
                for hop in trace.get('hops', []):
                    try:
                        hop['ipaddr'] = self.internal2ip(hop['ipaddr'])
                    except (KeyError, socket.error):
                        pass
            for fld in ['starttime', 'endtime']:
                try:
                    host[fld] = utils.all2datetime(host[fld])
                except KeyError:
                    pass
            yield host

    def get(self, *args, **kargs):
        return list(self._get(*args, **kargs))

    def store_host(self, host):
        host = deepcopy(host)
        try:
            host['scanid'] = [host['scanid'].decode()]
        except KeyError:
            pass
        try:
            host['addr'] = self.ip2internal(host['addr'])
        except (KeyError, ValueError):
            pass
        for port in host.get('ports', []):
            if 'state_reason_ip' in port:
                try:
                    port['state_reason_ip'] = self.ip2internal(
                        port['state_reason_ip']
                    )
                except ValueError:
                    pass
        for trace in host.get('traces', []):
            for hop in trace.get('hops', []):
                if 'ipaddr' in hop:
                    try:
                        hop['ipaddr'] = self.ip2internal(hop['ipaddr'])
                    except ValueError:
                        pass
        for fld in ['starttime', 'endtime']:
            if isinstance(host[fld], datetime):
                host[fld] = utils.datetime2timestamp(host[fld])
            elif isinstance(host[fld], basestring):
                host[fld] = utils.datetime2timestamp(
                    utils.all2datetime(host[fld])
                )
        if '_id' not in host:
            _id = host['_id'] = str(uuid1())
        self.db.insert(host)
        utils.LOGGER.debug("HOST STORED: %r in %r", _id, self.dbname)
        return _id

    @staticmethod
    def getscanids(host):
        return host.get("scanid", [])

    @classmethod
    def searchdomain(cls, name, neg=False):
        q = Query()
        res = q.hostnames.any(cls._searchstring_re_inarray(q.domains, name))
        if neg:
            return ~res
        return res

    @classmethod
    def searchhostname(cls, name, neg=False):
        q = Query()
        res = q.hostnames.any(cls._searchstring_re(q.name, name))
        if neg:
            return ~res
        return res

    @classmethod
    def searchcategory(cls, cat, neg=False):
        """
        Filters (if `neg` == True, filters out) one particular category
        (records may have zero, one or more categories).
        """
        return cls._searchstring_re_inarray(Query().categories, cat, neg=neg)

    @staticmethod
    def searchcountry(country, neg=False):
        """Filters (if `neg` == True, filters out) one particular
        country, or a list of countries.

        """
        q = Query()
        country = utils.country_unalias(country)
        if isinstance(country, list):
            res = q.infos.country_code.one_of(country)
            if neg:
                return ~res
            return res
        if neg:
            return q.infos.country_code != country
        return q.infos.country_code == country

    @classmethod
    def searchcity(cls, city, neg=False):
        """
        Filters (if `neg` == True, filters out) one particular city.
        """
        return cls._searchstring_re(Query().infos.city, city, neg=neg)

    @staticmethod
    def searchhaslocation(neg=False):
        res = Query().infos.coordinates.exists()
        if neg:
            return ~res
        return res

    @staticmethod
    def searchasnum(asnum, neg=False):
        """Filters (if `neg` == True, filters out) one or more
        particular AS number(s).

        """
        q = Query()
        if not isinstance(asnum, basestring) and hasattr(asnum, '__iter__'):
            res = q.infos.as_num.one_of([int(val) for val in asnum])
            if neg:
                return ~res
            return res
        asnum = int(asnum)
        if neg:
            return q.infos.as_num != asnum
        return q.infos.as_num == asnum

    @classmethod
    def searchasname(cls, asname, neg=False):
        """Filters (if `neg` == True, filters out) one or more
        particular AS.

        """
        return cls._searchstring_re(Query().infos.as_num, asname, neg=neg)

    @classmethod
    def searchsource(cls, src, neg=False):
        """Filters (if `neg` == True, filters out) one particular
        source.

        """
        return cls._searchstring_re(Query().source, src, neg=neg)

    @staticmethod
    def searchport(port, protocol='tcp', state='open', neg=False):
        """Filters (if `neg` == True, filters out) records with
        specified protocol/port at required state. Be aware that when
        a host has a lot of ports filtered or closed, it will not
        report all of them, but only a summary, and thus the filter
        might not work as expected. This filter will always work to
        find open ports.

        """
        q = Query()
        if port == "host":
            res = (q.port > 0) if neg else (q.port == -1)
        else:
            res = (q.port == port) & (q.protocol == protocol)
            if neg:
                return q.ports.any(res & (q.state_state != state)) | \
                    q.ports.all(~res)
            res &= q.state_state == state
        return q.ports.any(res)

    @staticmethod
    def searchportsother(ports, protocol='tcp', state='open'):
        """Filters records with at least one port other than those
        listed in `ports` with state `state`.

        """
        q = Query()
        return q.ports.any(q.protocol == protocol & q.state_state == state &
                           ~q.port.one_of(ports))

    @classmethod
    def searchports(cls, ports, protocol='tcp', state='open', neg=False):
        res = [cls.searchport(port=port, protocol=protocol, state=state)
               for port in ports]
        if neg:
            # pylint: disable=invalid-unary-operand-type
            return ~cls.flt_or(*res)
        return cls.flt_and(*res)

    @classmethod
    def searchcountopenports(cls, minn=None, maxn=None, neg=False):
        "Filters records with open port number between minn and maxn"
        assert minn is not None or maxn is not None
        res = []
        q = Query()
        if minn == maxn:
            if neg:
                return q.openports.count != minn
            return q.openports.count == minn
        if minn is not None:
            if neg:
                res.append(q.openports.count < minn)
            else:
                res.append(q.openports.count >= minn)
        if maxn is not None:
            if neg:
                res.append(q.openports.count > maxn)
            else:
                res.append(q.openports.count <= maxn)
        if neg:
            return cls.flt_or(*res)
        return cls.flt_and(*res)

    @staticmethod
    def searchopenport(neg=False):
        "Filters records with at least one open port."
        q = Query()
        res = q.ports.any(q.state_state == "open")
        if neg:
            return ~res
        return res

    @classmethod
    def searchservice(cls, srv, port=None, protocol=None):
        """Search an open port with a particular service."""
        q = Query()
        flt = cls._searchstring_re(q.service_name, srv)
        if port is not None:
            flt &= (q.port == port)
        if protocol is not None:
            flt &= (q.protocol == protocol)
        return q.ports.any(flt)

    @classmethod
    def searchproduct(cls, product, version=None, service=None, port=None,
                      protocol=None):
        """Search a port with a particular `product`. It is (much)
        better to provide the `service` name and/or `port` number
        since those fields are indexed.

        """
        q = Query()
        flt = cls._searchstring_re(q.service_product, product)
        if version is not None:
            flt &= cls._searchstring_re(q.service_version, version)
        if service is not None:
            flt &= cls._searchstring_re(q.service_name, service)
        if port is not None:
            flt &= (q.port == port)
        if protocol is not None:
            flt &= (q.protocol == protocol)
        return q.ports.any(flt)

    @classmethod
    def searchscript(cls, name=None, output=None, values=None, neg=False):
        """Search a particular content in the scripts results.

        """
        q = Query()
        res = []
        if name is not None:
            res.append(cls._searchstring_re(q.id, name))
        if output is not None:
            res.append(cls._searchstring_re(q.output, output))
        if values is not None:
            if not isinstance(name, basestring):
                raise TypeError(".searchscript() needs a `name` arg "
                                "when using a `values` arg")
            key = ALIASES_TABLE_ELEMS.get(name, name)
            if isinstance(values, dict):
                for field, value in viewitems(values):
                    if 'ports.scripts.%s' % key in cls.list_fields:
                        base = q
                        for subfld in field.split('.'):
                            base = getattr(base, subfld)
                        list_field = True
                    else:
                        base = getattr(q, key)
                        for subfld in field.split('.'):
                            base = getattr(base, subfld)
                        list_field = False
                    if isinstance(value, utils.REGEXP_T):
                        if 'ports.scripts.%s.%s' % (key,
                                                    field) in cls.list_fields:
                            base = base.test(
                                lambda val: any(value.search(subval)
                                                for subval in val)
                            )
                        else:
                            base = base.search(value.pattern,
                                               flags=value.flags)
                    elif 'ports.scripts.%s.%s' % (key,
                                                  field) in cls.list_fields:
                        base = base.any([value])
                    else:
                        base = (base == value)
                    if list_field:
                        res.append(getattr(q, key).any(base))
                    else:
                        res.append(base)
            elif 'ports.scripts.%s' % key in cls.list_fields:
                res.append(cls._searchstring_re_inarray(getattr(q, key),
                                                        values))
            else:
                res.append(cls._searchstring_re(getattr(q, key), values))
        if res:
            res = q.ports.any(q.scripts.any(cls.flt_and(*res)))
        else:
            res = q.ports.any(q.scripts.exists())
        if neg:
            # pylint: disable=invalid-unary-operand-type
            return ~res
        return res

    @classmethod
    def searchsvchostname(cls, hostname):
        q = Query()
        return q.ports.any(cls._searchstring_re(q.service_hostname, hostname))

    @staticmethod
    def searchwebmin():
        q = Query()
        return q.ports.any(
            (q.service_name == 'http') &
            (q.service_product == 'MiniServ') &
            (q.service_extrainfo != 'Webmin httpd')
        )

    @staticmethod
    def searchx11():
        q = Query()
        return q.ports.any(
            (q.service_name == 'X11') &
            (q.service_extrainfo != 'access denied')
        )

    def searchfile(self, fname=None, scripts=None):
        """Search shared files from a file name (either a string or a
        regexp), only from scripts using the "ls" NSE module.

        """
        q = Query()
        if fname is None:
            fname = q.filename.exists()
        else:
            fname = self._searchstring_re(q.filename, fname)
        if scripts is None:
            return q.ports.any(q.scripts.any(q.ls.volumes.any(
                q.files.any(fname)
            )))
        if isinstance(scripts, basestring):
            scripts = [scripts]
        if len(scripts) == 1:
            return q.ports.any(q.scripts.any(
                (q.id == scripts[0]) &
                q.ls.volumes.any(q.files.any(fname))
            ))
        return q.ports.any(q.scripts.any(
            q.id.one_of(scripts) &
            q.ls.volumes.any(q.files.any(fname))
        ))

    @classmethod
    def searchhttptitle(cls, title):
        q = Query()
        base = cls._searchstring_re(q.output, title)
        return q.ports.any(q.scripts.any(
            q.id.one_of(['http-title', 'html-title']) &
            base
        ))

    @staticmethod
    def searchos(txt):
        if isinstance(txt, utils.REGEXP_T):
            def _match(base):
                return base.search(txt.pattern, flags=txt.flags)
        else:
            def _match(base):
                return base == txt
        q = Query()
        return q.os.osclass.any(
            _match(q.vendor) |
            _match(q.osfamily) |
            _match(q.osclass)
        )

    @staticmethod
    def searchvsftpdbackdoor():
        q = Query()
        return q.ports.any((q.protocol == "tcp") &
                           (q.state_state == "open") &
                           (q.service_product == 'vsftpd') &
                           (q.service_version == '2.3.4'))

    @staticmethod
    def searchvulnintersil():
        # See MSF modules/auxiliary/admin/http/intersil_pass_reset.rb
        q = Query()
        return q.ports.any(
            (q.protocol == 'tcp') &
            (q.state_state == 'open') &
            (q.service_product == 'Boa HTTPd') &
            (q.service_version.search('^0\\.9(3([^0-9]|$)|'
                                      '4\\.([0-9]|0[0-9]|'
                                      '1[0-1])([^0-9]|$))'))
        )

    @staticmethod
    def searchdevicetype(devtype):
        q = Query()
        if isinstance(devtype, utils.REGEXP_T):
            res = (q.service_devicetype.search(devtype.pattern,
                                               flags=devtype.flags))
        elif isinstance(devtype, list):
            res = q.service_devicetype.one_of(devtype)
        else:
            res = q.service_devicetype == devtype
        return q.ports.any(res)

    def searchnetdev(self):
        return self.searchdevicetype([
            'bridge',
            'broadband router',
            'firewall',
            'hub',
            'load balancer',
            'proxy server',
            'router',
            'switch',
            'WAP',
        ])

    def searchphonedev(self):
        return self.searchdevicetype([
            'PBX',
            'phone',
            'telecom-misc',
            'VoIP adapter',
            'VoIP phone',
        ])

    @staticmethod
    def searchldapanon():
        q = Query()
        return q.ports.any(q.service_extrainfo == 'Anonymous bind OK')

    @classmethod
    def searchvuln(cls, vulnid=None, status=None):
        q = Query()
        res = []
        if status is not None:
            res.append(cls._searchstring_re(q.vulns.status, status))
        if vulnid is not None:
            res.append(cls._searchstring_re(q.vulns.id, vulnid))
        if res:
            res = cls.flt_and(*res)
        else:
            res = q.vulns.id.exists()
        return q.ports.any(q.scripts.any(res))

    @staticmethod
    def searchtimeago(delta, neg=False):
        if not isinstance(delta, timedelta):
            delta = timedelta(seconds=delta)
        tstamp = utils.datetime2timestamp(datetime.now() - delta)
        q = Query().endtime
        if neg:
            return q < tstamp
        return q >= tstamp

    @staticmethod
    def searchtimerange(start, stop, neg=False):
        if isinstance(start, datetime):
            start = utils.datetime2timestamp(start)
        if isinstance(stop, datetime):
            stop = utils.datetime2timestamp(stop)
        q = Query()
        if neg:
            return (q.endtime < start) | (q.starttime > stop)
        return (q.endtime >= start) & (q.starttime <= stop)

    @classmethod
    def searchhop(cls, hop, ttl=None, neg=False):
        try:
            hop = cls.ip2internal(hop)
        except ValueError:
            pass
        q = Query()
        res = [q.ipaddr == hop]
        if ttl is not None:
            res.append(q.ttl == ttl)
        res = q.traces.any(q.hops.any(cls.flt_and(*res)))
        if neg:
            return ~res
        return res

    @classmethod
    def searchhopdomain(cls, hop, neg=False):
        q = Query()
        res = q.traces.any(q.hops.any(cls._searchstring_re_inarray(
            q.domains,
            hop,
        )))
        if neg:
            return ~res
        return res

    @classmethod
    def searchhopname(cls, hop, neg=False):
        q = Query()
        res = q.traces.any(q.hops.any(cls._searchstring_re(q.host, hop)))
        if neg:
            return ~res
        return res

    @classmethod
    def searchcpe(cls, cpe_type=None, vendor=None, product=None, version=None):
        """Look for a CPE by type (a, o or h), vendor, product or version (the
        part after the column following the product). No argument will just
        check for cpe existence.

        """
        q = Query()
        fields = [
            ("type", cpe_type),
            ("vendor", vendor),
            ("product", product),
            ("version", version),
        ]
        flt = [cls._searchstring_re(getattr(q, field), value)
               for field, value in fields
               if value is not None]
        if not flt:
            return q.cpes.exists()
        return q.cpes.any(cls.flt_and(*flt))

    def topvalues(self, field, flt=None, topnbr=10, sort=None,
                  limit=None, skip=None, least=False, aggrflt=None,
                  specialproj=None, specialflt=None):
        """
        This method makes use of the aggregation framework to produce
        top values for a given field or pseudo-field. Pseudo-fields are:
          - category / asnum / country / net[:mask]
          - port
          - port:open / :closed / :filtered / :<servicename>
          - portlist:open / :closed / :filtered
          - countports:open / :closed / :filtered
          - service / service:<portnbr>
          - product / product:<portnbr>
          - cpe / cpe.<part> / cpe:<cpe_spec> / cpe.<part>:<cpe_spec>
          - devicetype / devicetype:<portnbr>
          - script:<scriptid> / script:<port>:<scriptid>
            / script:host:<scriptid>
          - cert.* / smb.* / sshkey.* / ike.*
          - httphdr / httphdr.{name,value} / httphdr:<name>
          - modbus.* / s7.* / enip.*
          - mongo.dbs.*
          - vulns.*
          - screenwords
          - file.* / file.*:scriptid
          - hop
        """
        q = Query()
        if flt is None:
            flt = self.flt_empty

        def _outputproc(val):
            return val

        def _extractor(flt, field):
            for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                 fields=[field]):
                for val in self._generate_field_values(rec, field):
                    yield val

        def _newflt(field):
            return self._search_field_exists(field)

        if field == "category":
            field = "categories"
        elif field == "country":
            field = "infos.country_code"

            def _extractor(flt, field):  # noqa: F811
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=[field, "infos.country_name"]):
                    rec = rec["infos"]
                    yield (rec["country_code"], rec.get("country_name", "?"))
        elif field == "city":

            def _newflt(field):  # noqa: F811
                return (self._search_field_exists("infos.country_code") &
                        self._search_field_exists("infos.city"))

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["infos.country_code",
                                             "infos.city"]):
                    rec = rec["infos"]
                    yield (rec["country_code"], rec["city"])
        elif field == "asnum":
            field = "infos.as_num"
        elif field == "as":
            field = "infos.as_num"

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=[field, "infos.as_name"]):
                    rec = rec["infos"]
                    yield (rec["as_num"], rec.get("as_name", "?"))
        elif field == "net" or field.startswith("net:"):
            maskval = int(field.split(':', 1)[1]) if ':' in field else 24
            mask = utils.int2mask(maskval)
            field = "addr"

            def _newflt(field):
                return self.searchipv4()

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=[field]):
                    yield "%s/%s" % (
                        utils.int2ip(utils.ip2int(rec['addr']) & mask),
                        maskval,
                    )
        elif field == "port" or field.startswith("port:"):
            def _newflt(field):
                return q.ports.any(q.state_state.exists())
            if field == "port":
                matchfld = "ports.state_state"

                def _match(port):
                    return "state_state" in port
            else:
                info = field.split(':', 1)[1]
                if info in ['open', 'filtered', 'closed']:
                    matchfld = "ports.state_state"

                    def _match(port):
                        return port.get('state_state') == info
                else:
                    matchfld = "ports.service_name"

                    def _match(port):
                        return port.get('service_name') == info

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["ports.port", "ports.protocol",
                                             matchfld]):
                    for port in rec['ports']:
                        if _match(port):
                            yield (port.get('protocol', '?'), port['port'])
        elif field.startswith('portlist:'):
            fields = ["ports.port", "ports.protocol", "ports.state_state"]
            info = field.split(':', 1)[1]

            def _newflt(field):
                return q.ports.any(q.state_state.exists())

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=fields):
                    yield tuple(sorted((port.get('protocol', '?'),
                                        port['port'])
                                       for port in rec['ports']
                                       if port.get('state_state') == info))

            def _outputproc(val):  # noqa: F811
                return list(val)
        elif field.startswith('countports:'):
            state = field.split(':', 1)[1]

            def _newflt(field):
                return q.ports.any(q.state_state.exists())

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=['ports.state_state']):
                    yield sum(1 for port in rec['ports']
                              if port.get('state_state') == state)
        elif field == "service":
            def _newflt(field):
                return self.searchopenport()

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["ports.state_state",
                                             "ports.service_name"]):
                    for port in rec['ports']:
                        if port.get('state_state') == "open":
                            yield port.get('service_name')
        elif field.startswith("service:"):
            portnum = int(field[8:])

            def _newflt(field):
                return self.searchport(portnum)

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["ports.port", "ports.state_state",
                                             "ports.service_name"]):
                    for port in rec['ports']:
                        if port.get('port') == portnum and \
                           port.get('state_state') == "open":
                            yield port.get('service_name')
        elif field == "product":
            def _newflt(field):
                return self.searchopenport()

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["ports.state_state",
                                             "ports.service_name",
                                             "ports.service_product"]):
                    for port in rec['ports']:
                        if port.get('state_state') == "open":
                            yield (port.get('service_name'),
                                   port.get('service_product'))
        elif field.startswith("product:"):
            service = field[8:]
            if service.isdigit():
                portnum = int(service)

                def _newflt(field):
                    return self.searchport(portnum)

                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip,
                                         fields=["ports.port",
                                                 "ports.state_state",
                                                 "ports.service_name",
                                                 "ports.service_product"]):
                        for port in rec['ports']:
                            if port.get('port') == portnum and \
                               port.get('state_state') == "open":
                                yield (port.get('service_name'),
                                       port.get('service_product'))
            else:

                def _newflt(field):
                    return self.searchservice(service)

                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip,
                                         fields=["ports.state_state",
                                                 "ports.service_name",
                                                 "ports.service_product"]):
                        for port in rec['ports']:
                            if port.get('state_state') == "open" and \
                               port.get('service_name') == service:
                                yield (port.get('service_name'),
                                       port.get('service_product'))
        elif field == "version":
            def _newflt(field):
                return self.searchopenport()

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["ports.state_state",
                                             "ports.service_name",
                                             "ports.service_product",
                                             "ports.service_version"]):
                    for port in rec['ports']:
                        if port.get('state_state') == "open":
                            yield (port.get('service_name'),
                                   port.get('service_product'),
                                   port.get('service_version'))
        elif field.startswith("version:"):
            service = field[8:]
            if service.isdigit():
                portnum = int(service)

                def _newflt(field):
                    return self.searchport(portnum)

                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip,
                                         fields=["ports.port",
                                                 "ports.state_state",
                                                 "ports.service_name",
                                                 "ports.service_product",
                                                 "ports.service_version"]):
                        for port in rec['ports']:
                            if port.get('port') == portnum and \
                               port.get('state_state') == "open":
                                yield (port.get('service_name'),
                                       port.get('service_product'),
                                       port.get('service_version'))
            elif ':' in service:
                service, product = service.split(':', 1)

                def _newflt(field):
                    return self.searchproduct(product, service=service)

                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip,
                                         fields=["ports.state_state",
                                                 "ports.service_name",
                                                 "ports.service_product",
                                                 "ports.service_version"]):
                        for port in rec['ports']:
                            if port.get('state_state') == "open" and \
                               port.get('service_name') == service and \
                               port.get('service_product') == product:
                                yield (port.get('service_name'),
                                       port.get('service_product'),
                                       port.get('service_version'))
            else:

                def _newflt(field):
                    return self.searchservice(service)

                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip,
                                         fields=["ports.state_state",
                                                 "ports.service_name",
                                                 "ports.service_product",
                                                 "ports.service_version"]):
                        for port in rec['ports']:
                            if port.get('state_state') == "open" and \
                               port.get('service_name') == service:
                                yield (port.get('service_name'),
                                       port.get('service_product'),
                                       port.get('service_version'))
        elif field.startswith("cpe"):
            try:
                field, cpeflt = field.split(":", 1)
                cpeflt = cpeflt.split(':', 3)
            except ValueError:
                cpeflt = []
            try:
                field = field.split(".", 1)[1]
            except IndexError:
                field = "version"
            fields = ["type", "vendor", "product", "version"]
            if field not in fields:
                try:
                    field = fields[int(field) - 1]
                except (IndexError, ValueError):
                    field = "version"
            cpeflt = zip(fields, (utils.str2regexp(value) for value in cpeflt))

            def _newflt(field):
                return self.searchcpe(**dict(
                    ("cpe_type" if key == "type" else key, value)
                    for key, value in cpeflt
                ))

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["cpes"]):
                    for cpe in rec['cpes']:
                        good = True
                        for key, value in cpeflt:
                            if isinstance(value, utils.REGEXP_T):
                                if not value.search(cpe.get(key, "")):
                                    good = False
                                    break
                            elif cpe.get(key) != value:
                                good = False
                                break
                        if good:
                            res = []
                            for fld in fields:
                                res.append(cpe.get(fld))
                                if fld == field:
                                    break
                            yield tuple(res)
        elif field == 'devicetype':
            field = "ports.service_devicetype"
        elif field.startswith('devicetype:'):
            portnum = int(field.split(':', 1)[1])

            def _newflt(field):
                return self.searchport(portnum)

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=["ports.port", "ports.state_state",
                                             "ports.service_devicetype"]):
                    for port in rec['ports']:
                        if port.get('port') == portnum and \
                           port.get('state_state') == "open":
                            yield port.get('service_devicetype')
        elif field.startswith('smb.'):

            def _newflt(field):
                return self.searchscript(name='smb-os-discovery')
            if field == 'smb.dnsdomain':
                field = 'ports.scripts.smb-os-discovery.domain_dns'
            elif field == 'smb.forest':
                field = 'ports.scripts.smb-os-discovery.forest_dns'
            else:
                field = 'ports.scripts.smb-os-discovery.' + field[4:]
        elif field == "script":
            field = "ports.scripts.id"
        elif field.startswith('script:'):
            scriptid = field.split(':', 1)[1]
            if ':' in scriptid:
                portnum, scriptid = scriptid.split(':', 1)
                portnum = int(portnum)

                def _newflt(field):
                    return (self.searchscript(name=scriptid) &
                            self.searchport(portnum))

                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip,
                                         fields=["ports.port",
                                                 "ports.scripts.id",
                                                 "ports.scripts.output"]):
                        for port in rec['ports']:
                            if port.get('port') != portnum:
                                continue
                            for script in port.get('scripts', []):
                                if script['id'] == scriptid:
                                    yield script['output']
            else:

                def _newflt(field):
                    return self.searchscript(name=scriptid)

                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip,
                                         fields=["ports.scripts.id",
                                                 "ports.scripts.output"]):
                        for port in rec['ports']:
                            for script in port.get('scripts', []):
                                if script['id'] == scriptid:
                                    yield script['output']
        elif field == 'domains':
            field = 'hostnames.domains'
        elif field.startswith('domains:'):
            level = int(field[8:]) - 1
            field = 'hostnames.domains'

            def _extractor(flt, field):
                for rec in self._get(flt, sort=sort, limit=limit,
                                     skip=skip,
                                     fields=["hostnames.domains"]):
                    for host in rec['hostnames']:
                        for dom in host.get('domains', []):
                            if dom.count('.') == level:
                                yield dom
        elif field.startswith('cert.'):
            subfld = field[5:]
            field = 'ports.scripts.ssl-cert.' + subfld

            if subfld in ['issuer', 'subject']:
                def _extractor(flt, field):
                    for rec in self._get(flt, sort=sort, limit=limit,
                                         skip=skip, fields=[field]):
                        for val in self._generate_field_values(rec, field):
                            yield tuple(sorted(viewitems(val)))

                def _outputproc(val):
                    return dict(val)
        elif field == 'useragent' or field.startswith('useragent:'):
            if field == 'useragent':

                def _newflt(field):
                    return self.searchuseragent()
            else:
                subfield = utils.str2regexp(field[10:])

                def _newflt(field):
                    return self.searchuseragent(useragent=subfield)

                def _extractor(flt, field):
                    for rec in self._get(
                            flt, sort=sort, limit=limit, skip=skip,
                            fields=["ports.scripts.http-user-agent"],
                    ):
                        for port in rec['ports']:
                            for script in port.get('scripts', []):
                                for ua in script.get('http-user-agent', []):
                                    if isinstance(subfield, utils.REGEXP_T):
                                        if subfield.search(ua):
                                            yield ua
                                    else:
                                        if ua == subfield:
                                            yield ua
            field = "ports.scripts.http-user-agent"
        elif field == 'ja3-client' or (
                field.startswith('ja3-client') and field[10] in ':.'
        ):
            if ':' in field:
                field, value = field.split(':', 1)
                subkey, value = self._ja3keyvalue(utils.str2regexp(value))
                if isinstance(value, utils.REGEXP_T):

                    def _match(ja3cli):
                        return value.search(ja3cli.get(subkey, "")) is not None
                else:

                    def _match(ja3cli):
                        return value == ja3cli.get(subkey, "")

            else:
                value = None
                subkey = None

                def _match(ja3cli):
                    return True
            if '.' in field:
                field, subfield = field.split('.', 1)
            else:
                subfield = 'md5'

            def _newflt(field):
                return self.searchja3client(value_or_hash=value)

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=["ports.scripts.ssl-ja3-client"]
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            for ja3cli in script.get('ssl-ja3-client', []):
                                if isinstance(value, utils.REGEXP_T):
                                    if not value.search(
                                        ja3cli.get(subkey, "")
                                    ):
                                        continue
                                elif value is not None:
                                    if value != ja3cli.get(subkey):
                                        continue
                                yield ja3cli.get(subfield)
        elif field == 'ja3-server' or (
                field.startswith('ja3-server') and field[10] in ':.'
        ):
            if ':' in field:
                field, values = field.split(':', 1)
                if ':' in values:
                    value1, value2 = values.split(':', 1)
                    if value1:
                        subkey1, value1 = self._ja3keyvalue(
                            utils.str2regexp(value1)
                        )
                    else:
                        subkey1, value1 = None, None
                    if value2:
                        subkey2, value2 = self._ja3keyvalue(
                            utils.str2regexp(value2)
                        )
                    else:
                        subkey2, value2 = None, None
                else:
                    subkey1, value1 = self._ja3keyvalue(
                        utils.str2regexp(values)
                    )
                    subkey2, value2 = None, None
            else:
                subkey1, value1 = None, None
                subkey2, value2 = None, None
            if '.' in field:
                field, subfield = field.split('.', 1)
            else:
                subfield = 'md5'

            def _newflt(field):
                return self.searchja3server(
                    value_or_hash=value1,
                    client_value_or_hash=value2,
                )

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=["ports.scripts.ssl-ja3-server"]
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            for ja3srv in script.get('ssl-ja3-server', []):
                                ja3cli = ja3srv.get('client', {})
                                if isinstance(value1, utils.REGEXP_T):
                                    if not value1.search(
                                        ja3srv.get(subkey1, "")
                                    ):
                                        continue
                                elif value1 is not None:
                                    if value1 != ja3srv.get(subkey1):
                                        continue
                                if isinstance(value2, utils.REGEXP_T):
                                    if not value2.search(
                                        ja3cli.get(subkey2, "")
                                    ):
                                        continue
                                elif value2 is not None:
                                    if value2 != ja3cli.get(subkey2):
                                        continue
                                yield (ja3srv.get(subfield),
                                       ja3cli.get(subfield))
        elif field == 'sshkey.bits':

            def _newflt(field):
                return self.searchsshkey()

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=["ports.scripts.ssh-hostkey"]
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            for hostk in script.get('ssh-hostkey', []):
                                yield (hostk.get('type'), hostk.get('bits'))
        elif field.startswith('sshkey.'):

            def _newflt(field):
                return self.searchsshkey()
            field = 'ports.scripts.ssh-hostkey.' + field[7:]
        elif field == 'ike.vendor_ids':

            def _newflt(field):
                return self.searchscript(name="ike-info")

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=["ports.scripts.ike-info.vendor_ids"]
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            for vid in script.get(
                                'ike-info', {}
                            ).get('vendor_ids', []):
                                yield (vid.get('value'), vid.get('name'))
        elif field == 'ike.transforms':

            def _newflt(field):
                return self.searchscript(name="ike-info")

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=["ports.scripts.ike-info.transforms"],
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            for xfrm in script.get(
                                'ike-info', {}
                            ).get('transforms', []):
                                yield (
                                    xfrm.get("Authentication"),
                                    xfrm.get("Encryption"),
                                    xfrm.get("GroupDesc"),
                                    xfrm.get("Hash"),
                                    xfrm.get("LifeDuration"),
                                    xfrm.get("LifeType"),
                                )
        elif field == 'ike.notification':
            field = "ports.scripts.ike-info.notification_type"
        elif field.startswith('ike.'):
            field = "ports.scripts.ike-info." + field[4:]
        elif field == 'httphdr':

            def _newflt(field):
                return self.searchscript(name="http-headers")

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=["ports.scripts.http-headers"],
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            for hdr in script.get('http-headers', []):
                                yield (hdr.get("name"),
                                       hdr.get("value"))
        elif field.startswith('httphdr.'):
            field = "ports.scripts.http-headers.%s" % field[8:]
        elif field.startswith('httphdr:'):
            subfield = field[8:].lower()

            def _newflt(field):
                return self.searchscript(name="http-headers",
                                         values={"name": subfield})

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=["ports.scripts.http-headers"],
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            for hdr in script.get('http-headers', []):
                                if hdr.get("name", "").lower() == subfield:
                                    yield hdr.get("value")
        elif field.startswith('modbus.'):
            field = 'ports.scripts.modbus-discover.' + field[7:]
        elif field.startswith('s7.'):
            field = 'ports.scripts.s7-info.' + field[3:]
        elif field.startswith('enip.'):
            subfield = field[5:]
            subfield = {
                "vendor": "Vendor",
                "product": "Product Name",
                "serial": "Serial Number",
                "devtype": "Device Type",
                "prodcode": "Product Code",
                "rev": "Revision",
                "ip": "Device IP",
            }.get(subfield, subfield)
            field = 'ports.scripts.enip-info.' + subfield
        elif field.startswith('mongo.dbs.'):
            field = 'ports.scripts.mongodb-databases.' + field[10:]
        elif field.startswith('vulns.'):
            subfield = field[6:]
            if subfield == "id":
                field = 'ports.scripts.vulns.id'
            else:
                field = "ports.scripts.vulns." + subfield

                def _extractor(flt, field):
                    for rec in self._get(
                            flt, sort=sort, limit=limit, skip=skip,
                            fields=[field, 'ports.scripts.vulns.id'],
                    ):
                        for port in rec['ports']:
                            for script in port.get('scripts', []):
                                for vuln in script.get('vulns', []):
                                    yield (vuln.get('id'), vuln.get(subfield))
        elif field == 'file' or (field.startswith('file') and
                                 field[4] in '.:'):
            if field.startswith('file:'):
                scripts = field[5:]
                if '.' in scripts:
                    scripts, fieldname = scripts.split('.', 1)
                else:
                    fieldname = 'filename'
                scripts = scripts.split(',')
            else:
                fieldname = field[5:] or 'filename'
                scripts = None

            def _newflt(field):
                return self.searchfile(scripts=scripts)

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=['ports.scripts.id', 'ports.scripts.ls'],
                ):
                    for port in rec['ports']:
                        for script in port.get('scripts', []):
                            if scripts is not None and \
                               script.get('id') not in scripts:
                                continue
                            for vol in script.get('ls', {}).get('volumes', []):
                                for fil in vol.get('files', []):
                                    yield fil.get(fieldname)
        elif field == 'screenwords':
            field = 'ports.screenwords'
        elif field == 'hop':
            field = 'traces.hops.ipaddr'
        elif field.startswith('hop') and field[3] in ':>':
            ttl = int(field[4:])
            if field[3] == ':':

                def _match(hop):
                    return hop.get('ttl', 0) == ttl
            else:

                def _match(hop):
                    return hop.get('ttl', 0) > ttl

            field = 'traces.hops.ipaddr'

            def _extractor(flt, field):
                for rec in self._get(
                        flt, sort=sort, limit=limit, skip=skip,
                        fields=['traces.hops.ipaddr', 'traces.hops.ttl'],
                ):
                    for trace in rec['traces']:
                        for hop in trace.get('hops', []):
                            if _match(hop):
                                yield hop['ipaddr']
        return [
            {'_id': _outputproc(val), 'count': count}
            for val, count in
            Counter(_extractor(flt & _newflt(field),
                               field)).most_common(topnbr)
        ]

    def _features_port_list(self, flt, yieldall, use_service, use_product,
                            use_version):
        flt &= self._search_field_exists('ports.port')
        fields = ["ports.port"]
        if use_service:
            fields.append('ports.service_name')
            if use_product:
                fields.append('ports.service_product')
                if use_version:
                    fields.append('ports.service_version')

                    def _extract(port):
                        return (port.get("port"), port.get("service_name"),
                                port.get("service_product"),
                                port.get("service_version"))
                else:

                    def _extract(port):
                        return (port.get("port"), port.get("service_name"),
                                port.get("service_product"))
            else:

                def _extract(port):
                    return (port.get("port"), port.get("service_name"))
        else:

            def _extract(port):
                return (port.get("port"),)

        res = set()
        for rec in self._get(flt, fields=fields):
            for port in rec.get('ports', []):
                if port.get('port') == -1:
                    continue
                res.add(_extract(port))

        if yieldall:
            return res

        return sorted(res,
                      key=lambda val: [utils.key_sort_none(v) for v in val])

    def getlocations(self, flt):
        res = defaultdict(int)
        for rec in self.get(flt):
            c = rec.get('infos', {}).get('coordinates', {})
            if not c:
                continue
            c = tuple(c)
            res[c] += 1
        for rec, count in viewitems(res):
            yield {'_id': rec, 'count': count}

    def get_ips_ports(self, flt, limit=None, skip=None):
        res = self.get(flt, limit=limit, skip=skip)
        count = sum(len(host.get('ports', [])) for host in res)
        return (({'addr': host['addr'],
                  'ports': [{'state_state': port['state_state'],
                             'port': port['port']}
                            for port in host.get('ports', [])
                            if 'state_state' in port]}
                 for host in res if host.get('ports')),
                count)

    def get_ips(self, flt, limit=None, skip=None):
        res = self.get(flt, limit=limit, skip=skip)
        return (({'addr': host['addr']} for host in res),
                len(res))

    def get_open_port_count(self, flt, limit=None, skip=None):
        res = self.get(flt, limit=limit, skip=skip)
        return (({'addr': host['addr'],
                  'starttime': host.get('starttime'),
                  'openports': {'count': host['openports']['count']}}
                 for host in res
                 if host.get('openports', {}).get('count') is not None),
                len(res))


class TinyDBNmap(TinyDBActive, DBNmap):

    """An Nmap-specific DB using TinyDB backend"""

    content_handler = Nmap2DB
    dbname = "nmap"
    dbname_scans = "nmap_scans"

    def __init__(self, url):
        super(TinyDBNmap, self).__init__(url)
        self.output_function = None

    @property
    def db_scans(self):
        """The DB for scan files"""
        try:
            return self._db_scans
        except AttributeError:
            self._db_scans = TDB(os.path.join(self.basepath,
                                              "%s.json" % self.dbname_scans))
            return self._db_scans

    def init(self):
        super(TinyDBNmap, self).init()
        self.db_scans.purge_tables()

    def remove(self, rec):
        """Removes the record from the active column. `rec` must be the
        record as returned by `.get()` or the record id.

        """
        q = Query()
        if isinstance(rec, dict):
            scanids = rec.get("scanid", [])
        else:
            try:
                scanids = self.get(q._id == rec)[0].get("scanid", [])
            except IndexError:
                scanids = []
        super(TinyDBNmap, self).remove(rec)
        for scanid in scanids:
            if not self.db.get(q.scanid.any([scanid])):
                self.db_scans.remove(cond=Query()._id == scanid)

    def store_or_merge_host(self, host):
        self.store_host(host)

    def getscan(self, scanid):
        try:
            scanid = scanid.decode()
        except AttributeError:
            pass
        return self.db_scans.get(Query()._id == scanid)

    def is_scan_present(self, scanid):
        return self.getscan(scanid) is not None

    def store_scan_doc(self, scan):
        scan = deepcopy(scan)
        _id = scan['_id'] = scan['_id'].decode()
        if self.db_scans.get(Query()._id == _id) is not None:
            raise ValueError("Duplicate entry for id %r" % _id)
        self.db_scans.insert(scan)
        utils.LOGGER.debug("SCAN STORED: %r in %r", _id, self.dbname_scans)
        return _id


class TinyDBView(TinyDBActive, DBView):

    """A View-specific DB using TinyDB backend"""

    dbname = "view"

    def store_or_merge_host(self, host):
        if not self.merge_host(host):
            self.store_host(host)


def op_update(count, firstseen, lastseen):
    """A TinyDB operation to update a document with count, firstseen and
lastseen values.

    """
    def transform(doc):
        doc["count"] = doc.get("count", 0) + count
        if firstseen is not None:
            doc["firstseen"] = min(doc.get('firstseen', firstseen), firstseen)
        if lastseen is not None:
            doc["lastseen"] = max(doc.get('lastseen', lastseen), lastseen)
    return transform


class TinyDBPassive(TinyDB, DBPassive):

    """A Passive-specific DB using TinyDB backend"""

    dbname = "passive"

    @classmethod
    def rec2internal(cls, rec):
        """Given a record as presented to the user, fixes it before it can be
inserted in the database.

        """
        rec = deepcopy(rec)
        try:
            rec['addr'] = cls.ip2internal(rec['addr'])
        except (KeyError, ValueError):
            pass
        for fld in ['firstseen', 'lastseen']:
            if fld not in rec:
                continue
            if isinstance(rec[fld], datetime):
                rec[fld] = utils.datetime2timestamp(rec[fld])
            elif isinstance(rec[fld], basestring):
                rec[fld] = utils.datetime2timestamp(
                    utils.all2datetime(rec[fld])
                )
            if '_id' in rec:
                del rec['_id']
        return rec

    @classmethod
    def internal2rec(cls, rec):
        """Given a record as stored in the database, fixes it before it can be
returned to backend-agnostic functions.

        """
        rec = deepcopy(rec)
        try:
            rec['addr'] = cls.internal2ip(rec['addr'])
        except (KeyError, ValueError):
            pass
        for fld in ['firstseen', 'lastseen']:
            try:
                rec[fld] = utils.all2datetime(rec[fld])
            except KeyError:
                pass
        if rec.get('recontype') == 'SSL_SERVER' and \
           rec.get('source') == 'cert':
            rec['value'] = cls.from_binary(rec['value'])
        if isinstance(rec, Document):
            rec['_id'] = rec.doc_id
        return rec

    def _get(self, *args, **kargs):
        for rec in super(TinyDBPassive, self).get(*args, **kargs):
            yield self.internal2rec(rec)

    def get(self, *args, **kargs):
        """Queries the passive column with the provided filter "spec", and
returns a list of results.

        """
        return list(self._get(*args, **kargs))

    def get_one(self, *args, **kargs):
        """Same function as get, except the first record matching "spec" (or
None) is returned.

        """
        try:
            return self.get(*args, **kargs)[0]
        except IndexError:
            return None

    def insert(self, spec, getinfos=None):
        """Inserts the record "spec" into the passive column."""
        if getinfos is not None:
            spec.update(getinfos(spec))
        spec = self.rec2internal(spec)
        self.db.insert(spec)

    def insert_or_update(self, timestamp, spec, getinfos=None, lastseen=None):
        if spec is None:
            return
        q = Query()
        orig = deepcopy(spec)
        spec = self.rec2internal(spec)
        try:
            del spec['infos']
        except KeyError:
            pass
        count = spec.pop("count", 1)
        spec_cond = self.flt_and(*(getattr(q, key) == value
                                   for key, value in viewitems(spec)))
        if isinstance(timestamp, datetime):
            timestamp = utils.datetime2timestamp(timestamp)
        elif isinstance(timestamp, basestring):
            timestamp = utils.datetime2timestamp(utils.all2datetime(timestamp))
        if isinstance(lastseen, datetime):
            lastseen = utils.datetime2timestamp(lastseen)
        elif isinstance(lastseen, basestring):
            lastseen = utils.datetime2timestamp(
                utils.all2datetime(lastseen)
            )
        current = self.get_one(spec_cond, fields=[])
        if current is not None:
            self.db.update(op_update(count, timestamp, lastseen or timestamp),
                           doc_ids=[current.doc_id])
        else:
            doc = dict(spec, count=count, firstseen=timestamp,
                       lastseen=lastseen or timestamp)
            if getinfos is not None:
                orig.update(getinfos(orig))
                try:
                    doc['infos'] = orig['infos']
                except KeyError:
                    pass
                # upsert() won't handle operations
            self.db.upsert(doc, spec_cond)

    def remove(self, spec_or_id):
        if isinstance(spec_or_id, int_types):
            self.db.remove(doc_ids=[spec_or_id])
        else:
            self.db.remove(cond=spec_or_id)

    def topvalues(self, field, flt=None, distinct=True, topnbr=10, sort=None,
                  limit=None, skip=None, least=False, aggrflt=None,
                  specialproj=None, specialflt=None):
        """This method makes use of the aggregation framework to
        produce top values for a given field.

        If `distinct` is True (default), the top values are computed
        by distinct events. If it is False, they are computed based on
        the "count" field.

        """
        if flt is None:
            flt = self.flt_empty
        if distinct:
            countfield = None
            fields = [field]
        else:
            countfield = "count"
            fields = [field, "count"]

        def _outputproc(val):
            return val

        def _extractor(flt, field):
            for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                 fields=fields):
                for val in self._generate_field_values(rec, field,
                                                       countfield=countfield):
                    yield val

        def _newflt(field):
            return self._search_field_exists(field)

        if field == "net" or field.startswith("net:"):
            maskval = int(field.split(':', 1)[1]) if ':' in field else 24
            mask = utils.int2mask(maskval)
            field = "addr"

            def _newflt(field):  # noqa: F811
                return self.searchipv4()

            def _extractor(flt, field):  # noqa: F811
                for rec in self._get(flt, sort=sort, limit=limit, skip=skip,
                                     fields=fields):
                    val = "%s/%s" % (
                        utils.int2ip(utils.ip2int(rec['addr']) & mask),
                        maskval,
                    )
                    if distinct:
                        yield val
                    else:
                        yield (val, rec.get('count', 1))

        if distinct:
            return [
                {'_id': _outputproc(val), 'count': count}
                for val, count in
                Counter(_extractor(flt & _newflt(field),
                                   field)).most_common(topnbr)
            ]
        res = Counter()
        for val, count in _extractor(flt & _newflt(field), field):
            res[val] += count
        return [
            {'_id': _outputproc(val), 'count': count}
            for val, count in res.most_common(topnbr)
        ]

    def _features_port_list(self, flt, yieldall, use_service, use_product,
                            use_version):
        flt &= self._search_field_exists('port')
        fields = ['port']
        if use_service:
            fields.append('infos.service_name')
            if use_product:
                fields.append('infos.service_product')
                if use_version:
                    fields.append('infos.service_version')

                    def _extract(rec):
                        infos = rec.get('infos', {})
                        return (rec.get("port"), infos.get("service_name"),
                                infos.get("service_product"),
                                infos.get("service_version"))
                else:

                    def _extract(rec):
                        infos = rec.get('infos', {})
                        return (rec.get("port"), infos.get("service_name"),
                                infos.get("service_product"))
            else:

                def _extract(rec):
                    return (rec.get("port"),
                            rec.get('infos', {}).get("service_name"))
        else:

            def _extract(rec):
                return (rec.get("port"),)

        res = set()
        for rec in self._get(flt, fields=fields):
            res.add(_extract(rec))

        if yieldall:
            return res

        return sorted(res,
                      key=lambda val: [utils.key_sort_none(v) for v in val])

    @classmethod
    def searchrecontype(cls, rectype):
        return cls._searchstring_re(Query().recontype, rectype)

    @classmethod
    def searchsensor(cls, sensor, neg=False):
        return cls._searchstring_re(Query().sensor, sensor, neg=neg)

    @staticmethod
    def searchport(port, protocol='tcp', state='open', neg=False):
        """Filters (if `neg` == True, filters out) records on the specified
        protocol/port.

        """
        if protocol != 'tcp':
            raise ValueError("Protocols other than TCP are not supported "
                             "in passive")
        if state != 'open':
            raise ValueError("Only open ports can be found in passive")
        if neg:
            return Query().port != port
        return Query().port == port

    @classmethod
    def searchservice(cls, srv, port=None, protocol=None):
        """Search a port with a particular service."""
        q = Query()
        flt = cls._searchstring_re(q.infos.service_name, srv)
        if port is not None:
            flt &= q.port == port
        if protocol is not None and protocol != 'tcp':
            raise ValueError("Protocols other than TCP are not supported "
                             "in passive")
        return flt

    @classmethod
    def searchproduct(cls, product, version=None, service=None, port=None,
                      protocol=None):
        """Search a port with a particular `product`. It is (much)
        better to provide the `service` name and/or `port` number
        since those fields are indexed.

        """
        q = Query()
        flt = cls._searchstring_re(q.infos.service_product, product)
        if version is not None:
            flt &= cls._searchstring_re(q.infos.service_version, version)
        if service is not None:
            flt &= cls._searchstring_re(q.infos.service_name, service)
        if port is not None:
            flt &= q.port == port
        if protocol is not None:
            if protocol != 'tcp':
                raise ValueError("Protocols other than TCP are not supported "
                                 "in passive")
        return flt

    @classmethod
    def searchsvchostname(cls, hostname):
        return cls._searchstring_re(Query().infos.service_hostname, hostname)

    @classmethod
    def searchmac(cls, mac=None, neg=False):
        q = Query()
        res = (q.recontype == 'MAC_ADDRESS')
        if mac is not None:
            res &= cls._searchstring_re(q.value, mac, neg=neg)
        elif neg:
            return q.recontype != 'MAC_ADDRESS'
        return res

    @classmethod
    def searchuseragent(cls, useragent=None, neg=False):
        if neg:
            raise ValueError("searchuseragent([...], neg=True) is not "
                             "supported in passive DB.")
        q = Query()
        res = ((q.recontype == 'HTTP_CLIENT_HEADER') &
               (q.source == 'USER-AGENT'))
        if useragent is None:
            return res
        return res & cls._searchstring_re(q.value, useragent)

    @classmethod
    def searchdns(cls, name=None, reverse=False, dnstype=None,
                  subdomains=False):
        q = Query()
        res = (q.recontype == 'DNS_ANSWER')
        if name is not None:
            if subdomains:
                inarray = True
                if reverse:
                    req = q.infos.domaintarget
                else:
                    req = q.infos.domain
            else:
                inarray = False
                if reverse:
                    req = q.targetval
                else:
                    req = q.value
            if isinstance(name, list):
                if inarray:
                    res &= req.any(name)
                else:
                    res &= req.one_of(name)
            elif inarray:
                res &= cls._searchstring_re_inarray(req, name)
            else:
                res &= cls._searchstring_re(req, name)
        if dnstype is not None:
            res &= q.source.search('^%s-' % dnstype.upper())
        return res

    @staticmethod
    def searchcert(keytype=None):
        q = Query()
        req = ((q.recontype == 'SSL_SERVER') & (q.source == 'cert'))
        if keytype is None:
            return req
        return req & (q.infos.pubkeyalgo == (keytype + 'Encryption'))

    @classmethod
    def _searchja3(cls, query, value_or_hash):
        if value_or_hash is None:
            return None
        key, value = cls._ja3keyvalue(value_or_hash)
        return cls._searchstring_re(
            query.value if key == 'md5' else getattr(query.infos, key),
            value,
        )

    @classmethod
    def searchja3client(cls, value_or_hash=None):
        q = Query()
        base = ((q.recontype == 'SSL_CLIENT') &
                (q.source == 'ja3'))
        res = cls._searchja3(q, value_or_hash)
        if res is None:
            return base
        return base & res

    @classmethod
    def searchja3server(cls, value_or_hash=None, client_value_or_hash=None):
        q = Query()
        base = (q.recontype == 'SSL_SERVER')
        res = cls._searchja3(q, value_or_hash)
        if res is not None:
            base &= res
        if client_value_or_hash is None:
            return base & q.source.search('^ja3-')
        key, value = cls._ja3keyvalue(client_value_or_hash)
        if key == 'md5':
            return base & (q.source == ('ja3-%s' % value))
        return (base & q.source.search('^ja3-') &
                cls._searchstring_re(getattr(q.infos.client, key),
                                     client_value_or_hash))

    @staticmethod
    def searchsshkey(keytype=None):
        q = Query()
        req = ((q.recontype == 'SSH_SERVER_HOSTKEY') & (q.source == 'SSHv2'))
        if keytype is None:
            return req
        return req & (q.infos.algo == 'ssh-' + keytype)

    @classmethod
    def searchcertsubject(cls, expr, issuer=None):
        q = Query()
        req = ((q.recontype == 'SSL_SERVER') & (q.source == 'cert') &
               cls._searchstring_re(q.infos.subject_text, expr))
        if issuer is None:
            return req
        return req & cls._searchstring_re(q.infos.issuer_text, expr)

    @classmethod
    def searchcertissuer(cls, expr):
        q = Query()
        return ((q.recontype == 'SSL_SERVER') & (q.source == 'cert') &
                cls._searchstring_re(q.infos.issuer_text, expr))

    @staticmethod
    def searchbasicauth():
        q = Query()
        return (
            q.recontype.one_of(['HTTP_CLIENT_HEADER',
                                'HTTP_CLIENT_HEADER_SERVER']) &
            q.source.one_of(['AUTHORIZATION',
                             'PROXY-AUTHORIZATION']) &
            q.value.search('^Basic', flags=re.I)
        )

    @staticmethod
    def searchhttpauth():
        q = Query()
        return (
            q.recontype.one_of(['HTTP_CLIENT_HEADER',
                                'HTTP_CLIENT_HEADER_SERVER']) &
            q.source.one_of(['AUTHORIZATION',
                             'PROXY-AUTHORIZATION'])
        )

    @staticmethod
    def searchftpauth():
        return Query().recontype.one_of(['FTP_CLIENT', 'FTP_SERVER'])

    @staticmethod
    def searchpopauth():
        return Query().recontype.one_of(['POP_CLIENT', 'POP_SERVER'])

    @classmethod
    def searchtcpsrvbanner(cls, banner):
        q = Query()
        return ((q.recontype == 'TCP_SERVER_BANNER') &
                cls._searchstring_re(q.value, banner))

    @staticmethod
    def searchtimeago(delta, neg=False, new=True):
        if not isinstance(delta, timedelta):
            delta = timedelta(seconds=delta)
        tstamp = utils.datetime2timestamp(datetime.now() - delta)
        req = getattr(Query(), 'firstseen' if new else 'lastseen')
        if neg:
            return req < tstamp
        return req >= tstamp

    @staticmethod
    def searchnewer(timestamp, neg=False, new=True):
        if isinstance(timestamp, datetime):
            timestamp = utils.datetime2timestamp(timestamp)
        elif isinstance(timestamp, basestring):
            timestamp = utils.datetime2timestamp(
                utils.all2datetime(timestamp)
            )
        req = getattr(Query(), 'firstseen' if new else 'lastseen')
        if neg:
            return req <= timestamp
        return req > timestamp