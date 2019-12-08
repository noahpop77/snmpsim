#
# This file is part of snmpsim software.
#
# Copyright (c) 2010-2019, Ilya Etingof <etingof@gmail.com>
# License: http://snmplabs.com/snmpsim/license.html
#
# SNMP Simulator MIB to data file converter
#
import bisect
import getopt
import os
import socket
import struct
import sys
import time
import traceback

try:
    import pcap

except ImportError:
    pcap = None

from pyasn1 import debug as pyasn1_debug
from pyasn1.codec.ber import decoder
from pyasn1.error import PyAsn1Error
from pyasn1.type import univ
from pysnmp import debug as pysnmp_debug
from pysnmp.carrier.asynsock.dgram import udp
from pysnmp.error import PySnmpError
from pysnmp.proto import api
from pysnmp.proto.rfc1902 import Bits
from pysnmp.proto.rfc1902 import OctetString
from pysnmp.proto.rfc1902 import Unsigned32
from pysnmp.proto.rfc1902 import Integer32
from pysnmp.proto import rfc1905
from pysnmp.smi import builder
from pysnmp.smi import compiler
from pysnmp.smi import rfc1902
from pysnmp.smi import view

from snmpsim import confdir
from snmpsim import error
from snmpsim import log
from snmpsim.record import dump
from snmpsim.record import mvc
from snmpsim.record import sap
from snmpsim.record import snmprec
from snmpsim.record import walk

PROGRAM_NAME = 'snmpsim-record-traffic'

RECORD_TYPES = {
    dump.DumpRecord.ext: dump.DumpRecord(),
    mvc.MvcRecord.ext: mvc.MvcRecord(),
    sap.SapRecord.ext: sap.SapRecord(),
    walk.WalkRecord.ext: walk.WalkRecord(),
    snmprec.SnmprecRecord.ext: snmprec.SnmprecRecord(),
    snmprec.CompressedSnmprecRecord.ext: snmprec.CompressedSnmprecRecord()
}

HELP_MESSAGE = """\
Usage: %s [--help]
    [--version]
    [--debug=<%s>]
    [--debug-asn1=<%s>]
    [--quiet]
    [--logging-method=<%s[:args]>]
    [--log-level=<%s>]
    [--mib-source=<url>]
    [--start-object=<MIB-NAME::[symbol-name]|OID>]
    [--stop-object=<MIB-NAME::[symbol-name]|OID>]
    [--destination-record-type=<%s>]
    [--output-dir=<directory>]
    [--transport-id-offset=<number>]
    [--capture-file=<filename.pcap>]
    [--listen-interface=<device>]
    [--promiscuous-mode]
    [--packet-filter=<ruleset>]
    [--variation-modules-dir=<dir>]
    [--variation-module=<module>]
    [--variation-module-options=<args>]\
""" % (sys.argv[0],
       '|'.join([x for x in getattr(pysnmp_debug, 'FLAG_MAP',
                                    getattr(pysnmp_debug, 'flagMap', ()))
                 if x != 'mibview']),
       '|'.join([x for x in getattr(pyasn1_debug, 'FLAG_MAP',
                                    getattr(pyasn1_debug, 'flagMap', ()))]),
       '|'.join(log.METHODS_MAP),
       '|'.join(log.LEVELS_MAP),
       '|'.join(RECORD_TYPES))


class SnmprecRecord(snmprec.SnmprecRecord):

    def formatValue(self, oid, value, **context):
        textOid, textTag, textValue = snmprec.SnmprecRecord.formatValue(
            self, oid, value)

        if context['variationModule']:
            plainOid, plainTag, plainValue = snmprec.SnmprecRecord.formatValue(
                self, oid, value, nohex=True)

            if plainTag != textTag:
                context['hextag'], context['hexvalue'] = textTag, textValue

            else:
                textTag, textValue = plainTag, plainValue

            handler = context['variationModule']['record']

            textOid, textTag, textValue = handler(
                textOid, textTag, textValue, **context)

        elif 'stopFlag' in context and context['stopFlag']:
            raise error.NoDataNotification()

        return textOid, textTag, textValue


def main():
    verboseFlag = True
    mibSources = []
    defaultMibSources = ['http://mibs.snmplabs.com/asn1/@mib@']
    startOID = univ.ObjectIdentifier('1.3.6')
    stopOID = None
    promiscuousMode = False
    dstRecordType = 'snmprec'
    outputDir = '.'
    transportIdOffset = 0
    loggingMethod = ['stderr']
    loggingLevel = None
    variationModuleOptions = ""
    variationModuleName = variationModule = None
    listenInterface = captureFile = None
    packetFilter = 'udp and src port 161'

    endpoints = {}
    contexts = {}

    stats = {
        'UDP packets': 0,
        'IP packets': 0,
        'bad packets': 0,
        'empty packets': 0,
        'unknown L2 protocol': 0,
        'SNMP errors': 0,
        'SNMP exceptions': 0,
        'agents seen': 0,
        'contexts seen': 0,
        'snapshots taken': 0,
        'Response PDUs seen': 0,
        'OIDs seen': 0
    }

    try:
        opts, params = getopt.getopt(
            sys.argv[1:], 'hv',
            ['help', 'version', 'debug=', 'debug-snmp=', 'debug-asn1=',
             'quiet', 'logging-method=', 'log-level=', 'start-oid=', 'stop-oid=',
             'start-object=', 'stop-object=', 'mib-source=',
             'destination-record-type=',
             'output-dir=', 'transport-id-offset=',
             'capture-file=', 'listen-interface=', 'promiscuous-mode',
             'packet-filter=',
             'variation-modules-dir=', 'variation-module=',
             'variation-module-options='])

    except Exception as exc:
        sys.stderr.write(
            'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
        return 1

    if params:
        sys.stderr.write(
            'ERROR: extra arguments supplied %s\r\n'
            '%s\r\n' % (params, HELP_MESSAGE))
        return 1

    for opt in opts:
        if opt[0] == '-h' or opt[0] == '--help':
            sys.stderr.write("""\
Synopsis:
  Snoops network traffic for SNMP responses, builds SNMP Simulator
  data files.
  Can read capture files or listen live network interface.

Documentation:
  http://snmplabs.com/snmpsim/
%s
""" % HELP_MESSAGE)
            return 1

        if opt[0] == '-v' or opt[0] == '--version':
            import snmpsim
            import pysmi
            import pysnmp
            import pyasn1

            sys.stderr.write("""\
SNMP Simulator version %s, written by Ilya Etingof <etingof@gmail.com>
Using foundation libraries: pysmi %s, pysnmp %s, pyasn1 %s.
Python interpreter: %s
Software documentation and support at http://snmplabs.com/snmpsim
%s
""" % (snmpsim.__version__,
       getattr(pysmi, '__version__', 'unknown'),
       getattr(pysnmp, '__version__', 'unknown'),
       getattr(pyasn1, '__version__', 'unknown'),
       sys.version, HELP_MESSAGE))
            return 1

        elif opt[0] in ('--debug', '--debug-snmp'):
            pysnmp_debug.setLogger(
                pysnmp_debug.Debug(
                    *opt[1].split(','),
                    **dict(loggerName='%s.pysnmp' % PROGRAM_NAME)))

        elif opt[0] == '--debug-asn1':
            pyasn1_debug.setLogger(
                pyasn1_debug.Debug(
                    *opt[1].split(','),
                    **dict(loggerName='%s.pyasn1' % PROGRAM_NAME)))

        elif opt[0] == '--logging-method':
            loggingMethod = opt[1].split(':')

        elif opt[0] == '--log-level':
            loggingLevel = opt[1]

        if opt[0] == '--quiet':
            verboseFlag = False

        # obsolete begin
        elif opt[0] == '--start-oid':
            startOID = univ.ObjectIdentifier(opt[1])

        elif opt[0] == '--stop-oid':
            stopOID = univ.ObjectIdentifier(opt[1])
        # obsolete end

        if opt[0] == '--mib-source':
            mibSources.append(opt[1])

        if opt[0] == '--start-object':
            startOID = rfc1902.ObjectIdentity(*opt[1].split('::', 1))

        if opt[0] == '--stop-object':
            stopOID = rfc1902.ObjectIdentity(
                *opt[1].split('::', 1), **dict(last=True))

        if opt[0] == '--destination-record-type':
            if opt[1] not in RECORD_TYPES:
                sys.stderr.write(
                    'ERROR: unknown record type <%s> (known types are %s)\r\n%s'
                    '\r\n' % (opt[1], ', '.join(RECORD_TYPES),
                              HELP_MESSAGE))
                return 1

            dstRecordType = opt[1]

        elif opt[0] == '--output-dir':
            outputDir = opt[1]

        elif opt[0] == '--transport-id-offset':
            try:
                transportIdOffset = max(0, int(opt[1]))

            except Exception as exc:
                sys.stderr.write(
                    'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
                return 1

        elif opt[0] == '--listen-interface':
            listenInterface = opt[1]

        elif opt[0] == '--promiscuous-mode':
            promiscuousMode = True

        elif opt[0] == '--capture-file':
            captureFile = opt[1]

        elif opt[0] == '--packet-filter':
            packetFilter = opt[1]

        elif opt[0] == '--variation-modules-dir':
            confdir.variation.insert(0, opt[1])

        elif opt[0] == '--variation-module':
            variationModuleName = opt[1]

        elif opt[0] == '--variation-module-options':
            variationModuleOptions = opt[1]

    if params:
        sys.stderr.write(
            'ERROR: extra arguments supplied %s\r\n'
            '%s\r\n' % (params, HELP_MESSAGE))
        return 1

    if not pcap:
        sys.stderr.write(
            'ERROR: pylibpcap package is missing!\r\nGet it by running `pip install '
            'https://downloads.sourceforge.net/project/pylibpcap/pylibpcap/0.6.4/pylibpcap-0.6.4.tar.gz`'
            '\r\n%s\r\n' % HELP_MESSAGE)
        return 1

    try:
        log.setLogger(PROGRAM_NAME, *loggingMethod, force=True)

        if loggingLevel:
            log.setLevel(loggingLevel)

    except error.SnmpsimError as exc:
        sys.stderr.write('%s\r\n%s\r\n' % (exc, HELP_MESSAGE))
        sys.exit(1)

    if (isinstance(startOID, rfc1902.ObjectIdentity) or
            isinstance(stopOID, rfc1902.ObjectIdentity)):
        mibBuilder = builder.MibBuilder()

        mibViewController = view.MibViewController(mibBuilder)

        compiler.addMibCompiler(
            mibBuilder, sources=mibSources or defaultMibSources)

        try:
            if isinstance(startOID, rfc1902.ObjectIdentity):
                startOID.resolveWithMib(mibViewController)

            if isinstance(stopOID, rfc1902.ObjectIdentity):
                stopOID.resolveWithMib(mibViewController)

        except PySnmpError as exc:
            sys.stderr.write('ERROR: %s\r\n' % exc)
            return 1

    # Load variation module

    if variationModuleName:

        for variationModulesDir in confdir.variation:
            log.info('Scanning "%s" directory for variation '
                     'modules...' % variationModulesDir)

            if not os.path.exists(variationModulesDir):
                log.info('Directory "%s" does not exist' % variationModulesDir)
                continue

            mod = os.path.join(variationModulesDir, variationModuleName + '.py')
            if not os.path.exists(mod):
                log.info('Variation module "%s" not found' % mod)
                continue

            ctx = {'path': mod, 'moduleContext': {}}

            try:
                if sys.version_info[0] > 2:
                    exec(compile(open(mod).read(), mod, 'exec'), ctx)

                else:
                    execfile(mod, ctx)

            except Exception as exc:
                log.error('Variation module "%s" execution '
                          'failure: %s' % (mod, exc))
                return 1

            else:
                variationModule = ctx
                log.info('Variation module "%s" loaded' % variationModuleName)
                break

        else:
            log.error('variation module "%s" not found' % variationModuleName)
            return 1

    # Variation module initialization

    if variationModule:
        log.info('Initializing variation module...')

        for handler in ('init', 'record', 'shutdown'):
            if handler not in variationModule:
                log.error('missing "%s" handler at variation module '
                          '"%s"' % (handler, variationModuleName))
                return 1

        handler = variationModule['init']

        try:
            handler(options=variationModuleOptions, mode='recording',
                    startOID=startOID, stopOID=stopOID)

        except Exception as exc:
            log.error('Variation module "%s" initialization '
                      'FAILED: %s' % (variationModuleName, exc))

        else:
            log.info('Variation module "%s" '
                     'initialization OK' % variationModuleName)

    pcapObj = pcap.pcapObject()

    if listenInterface:
        if verboseFlag:
            log.info(
                'Listening on interface %s in %spromiscuous '
                'mode' % (listenInterface, promiscuousMode is False and 'non-' or ''))

        try:
            pcapObj.open_live(listenInterface, 65536, promiscuousMode, 1000)

        except Exception as exc:
            log.error(
                'Error opening interface %s for snooping: '
                '%s' % (listenInterface, exc))
            return 1

    elif captureFile:
        if verboseFlag:
            log.info('Opening capture file %s' % captureFile)

        try:
            pcapObj.open_offline(captureFile)

        except Exception as exc:
            log.error('Error opening capture file %s for reading: '
                      '%s' % (captureFile, exc))
            return 1

    else:
        sys.stderr.write(
            'ERROR: no capture file or live interface specified\r\n%s'
            '\r\n' % HELP_MESSAGE)
        return 1

    if packetFilter:
        if verboseFlag:
            log.info('Applying packet filter \"%s\"' % packetFilter)

        pcapObj.setfilter(packetFilter, 0, 0)

    if verboseFlag:
        log.info('Processing records from %s till '
                 '%s' % (startOID or 'the beginning', stopOID or 'the end'))

    def parsePacket(raw):
        pkt = {}

        # http://www.tcpdump.org/linktypes.html
        llHeaders = {
            0: 4,
            1: 14,
            108: 4,
            228: 0
        }

        if pcapObj.datalink() in llHeaders:
            raw = raw[llHeaders[pcapObj.datalink()]:]

        else:
            stats['unknown L2 protocol'] += 1

        pkt['version'] = (ord(raw[0]) & 0xf0) >> 4
        pkt['header_len'] = ord(raw[0]) & 0x0f
        pkt['tos'] = ord(raw[1])
        pkt['total_len'] = socket.ntohs(
            struct.unpack('H', raw[2:4])[0])
        pkt['id'] = socket.ntohs(
            struct.unpack('H', raw[4:6])[0])
        pkt['flags'] = (ord(raw[6]) & 0xe0) >> 5
        pkt['fragment_offset'] = socket.ntohs(
            struct.unpack('H', raw[6:8])[0] & 0x1f)
        pkt['ttl'] = ord(raw[8])
        pkt['protocol'] = ord(raw[9])
        pkt['checksum'] = socket.ntohs(
            struct.unpack('H', raw[10:12])[0])
        pkt['source_address'] = pcap.ntoa(
            struct.unpack('i', raw[12:16])[0])
        pkt['destination_address'] = pcap.ntoa(
            struct.unpack('i', raw[16:20])[0])

        if pkt['header_len'] > 5:
            pkt['options'] = raw[20:4 * (pkt['header_len'] - 5)]

        else:
            pkt['options'] = None

        raw = raw[4 * pkt['header_len']:]

        if pkt['protocol'] == 17:
            pkt['source_port'] = socket.ntohs(
                struct.unpack('H', raw[0:2])[0])
            pkt['destination_port'] = socket.ntohs(
                struct.unpack('H', raw[2:4])[0])
            raw = raw[8:]
            stats['UDP packets'] += 1

        pkt['data'] = raw
        stats['IP packets'] += 1

        return pkt

    def handleSnmpMessage(d, t, private={}):
        msgVer = api.decodeMessageVersion(d['data'])

        if msgVer in api.protoModules:
            pMod = api.protoModules[msgVer]

        else:
            stats['bad packets'] += 1
            return

        try:
            rspMsg, wholeMsg = decoder.decode(
                d['data'], asn1Spec=pMod.Message())

        except PyAsn1Error:
            stats['bad packets'] += 1
            return

        if rspMsg['data'].getName() == 'response':
            rspPDU = pMod.apiMessage.getPDU(rspMsg)
            errorStatus = pMod.apiPDU.getErrorStatus(rspPDU)

            if errorStatus:
                stats['SNMP errors'] += 1

            else:
                endpoint = d['source_address'], d['source_port']

                if endpoint not in endpoints:
                    endpoints[endpoint] = udp.domainName + (
                        transportIdOffset + len(endpoints),)
                    stats['agents seen'] += 1

                context = '%s/%s' % (
                    pMod.ObjectIdentifier(endpoints[endpoint]),
                    pMod.apiMessage.getCommunity(rspMsg))

                if context not in contexts:
                    contexts[context] = {}
                    stats['contexts seen'] += 1

                context = '%s/%s' % (
                    pMod.ObjectIdentifier(endpoints[endpoint]),
                    pMod.apiMessage.getCommunity(rspMsg))

                stats['Response PDUs seen'] += 1

                if 'basetime' not in private:
                    private['basetime'] = t

                for oid, value in pMod.apiPDU.getVarBinds(rspPDU):
                    if oid < startOID:
                        continue

                    if stopOID and oid >= stopOID:
                        continue

                    if oid in contexts[context]:
                        if value != contexts[context][oid]:
                            stats['snapshots taken'] += 1

                    else:
                        contexts[context][oid] = [], []

                    contexts[context][oid][0].append(t - private['basetime'])
                    contexts[context][oid][1].append(value)

                    stats['OIDs seen'] += 1

    def handlePacket(pktlen, data, timestamp):
        if not data:
            stats['empty packets'] += 1
            return

        handleSnmpMessage(parsePacket(data), timestamp)

    try:
        if listenInterface:
            log.info(
                'Listening on interface "%s", kill me when you '
                'are done.' % listenInterface)

            while True:
                pcapObj.dispatch(1, handlePacket)

        elif captureFile:
            log.info('Processing capture file "%s"....' % captureFile)

            args = pcapObj.next()

            while args:
                handlePacket(*args)
                args = pcapObj.next()

    except (TypeError, KeyboardInterrupt):
        log.info('Shutting down process...')

    finally:
        dataFileHandler = SnmprecRecord()

        for context in contexts:
            ext = os.path.extsep + RECORD_TYPES[dstRecordType].ext

            filename = os.path.join(outputDir, context + ext)

            if verboseFlag:
                log.info(
                    'Creating simulation context %s at '
                    '%s' % (context, filename))

            try:
                os.mkdir(os.path.dirname(filename))

            except OSError:
                pass

            try:
                outputFile = RECORD_TYPES[dstRecordType].open(filename, 'wb')

            except IOError as exc:
                log.error('writing %s: %s' % (filename, exc))
                return 1

            count = total = iteration = 0
            timeOffset = 0
            reqTime = time.time()

            oids = sorted(contexts[context])
            oids.append(oids[-1])  # duplicate last OID to trigger stopFlag

            while True:
                for oid in oids:

                    timeline, values = contexts[context][oid]

                    value = values[
                        min(len(values) - 1,
                            bisect.bisect_left(timeline, timeOffset))
                    ]

                    if value.tagSet in (rfc1905.NoSuchObject.tagSet,
                                        rfc1905.NoSuchInstance.tagSet,
                                        rfc1905.EndOfMibView.tagSet):
                        stats['SNMP exceptions'] += 1
                        continue

                    # remove value enumeration

                    if value.tagSet == Integer32.tagSet:
                        value = Integer32(value)

                    if value.tagSet == Unsigned32.tagSet:
                        value = Unsigned32(value)

                    if value.tagSet == Bits.tagSet:
                        value = OctetString(value)

                    # Build .snmprec record

                    ctx = {
                        'origOid': oid,
                        'origValue': value,
                        'count': count,
                        'total': total,
                        'iteration': iteration,
                        'reqTime': reqTime,
                        'startOID': startOID,
                        'stopOID': stopOID,
                        'stopFlag': oids.index(oid) == len(oids) - 1,
                        'variationModule': variationModule
                    }

                    try:
                        line = dataFileHandler.format(oid, value, **ctx)

                    except error.MoreDataNotification as exc:
                        count = 0
                        iteration += 1

                        moreDataNotification = exc
                        if 'period' in moreDataNotification:
                            timeOffset += moreDataNotification['period']
                            log.info(
                                '%s OIDs dumped, advancing time window to '
                                '%.2f sec(s)...' % (total, timeOffset))
                        break

                    except error.NoDataNotification:
                        pass

                    except error.SnmpsimError as exc:
                        log.error(exc)
                        continue

                    else:
                        outputFile.write(line)

                        count += 1
                        total += 1

                else:
                    break

            outputFile.flush()
            outputFile.close()

        if variationModule:
            log.info('Shutting down variation module '
                     '"%s"...' % variationModuleName)

            handler = variationModule['shutdown']

            try:
                handler(options=variationModuleOptions, mode='recording')

            except Exception as exc:
                log.error('Variation module "%s" shutdown FAILED: '
                          '%s' % (variationModuleName, exc))

            else:
                log.info('Variation module "%s" shutdown OK' % variationModuleName)

        log.info("""\
    PCap statistics:
        packets snooped: %s
        packets dropped: %s
        packets dropped: by interface %s\
    """ % pcapObj.stats())

        log.info("""\
    SNMP statistics:
        %s\
    """ % '    '.join(['%s: %s\r\n' % kv for kv in stats.items()]))

    return 0


if __name__ == '__main__':
    try:
        rc = main()

    except KeyboardInterrupt:
        sys.stderr.write('shutting down process...')
        rc = 0

    except Exception as exc:
        sys.stderr.write('process terminated: %s' % exc)

        for line in traceback.format_exception(*sys.exc_info()):
            sys.stderr.write(line.replace('\n', ';'))
        rc = 1

    sys.exit(rc)