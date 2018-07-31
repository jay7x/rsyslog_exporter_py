#!/usr/bin/env python
"""
Export rsyslog counters as prometheus metrics (impstats via omprog)

Copyright (c) 2018, Yury Bushmelev <jay4mail@gmail.com>
All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

__version__ = '1.0'

import os
import re
import sys
import time
import json
import select
import argparse
import collections
from prometheus_client import start_http_server, Summary
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

PARSE_TIME = Summary('rsyslog_exporter_parsing_seconds', 'Time spent on parsing input')
COLLECT_TIME = Summary('rsyslog_exporter_collecting_seconds', 'Time spent on collecting metrics')


def dbg(msg):
    """ Print [debug] message to stderr """
    sys.stderr.write("%s\n" % msg)
    sys.stderr.flush()


class RsyslogStats(object):
    """ Class to parse and collect rsyslog stats """
    metric_prefix = 'rsyslog'

    def __init__(self):
        self._current = collections.defaultdict(dict)
        self._exported = collections.defaultdict(dict)
        self.is_up = False
        self._is_exported = True
        self.parser_failures = 0
        self.stats_count = 0
        self.export_time = 0
        self.labels = {}

    def parser_failure(self):
        self.parser_failures += 1
        return self.parser_failures

    def is_exported(self):
        return self._is_exported

    def export(self):
        self._exported = self._current
        self._current = collections.defaultdict(dict)
        self._is_exported = True
        self.export_time = time.time()

    def counters(self):
        return self._exported

    def add(self, metric_name, name, value):
        self._current[metric_name][name] = value

    def dump(self, kind='c', prefix=''):
        if kind == 'c':
            metrics = self._current
        else:
            metrics = self._exported

        dbg("%s====" % (prefix))
        for k, v in metrics.items():
            for kk, vv in v.items():
                dbg("%s%s{label=\"%s\"}: %s" % (prefix, k, kk, vv))
        dbg("%s...." % (prefix))

    def _fix_metric_name(self, metric):
        m = re.sub('[^_a-zA-Z0-9]', '_', metric.lower())
        m = re.sub('_+', '_', m)
        m = m.strip('_')
        return m

    @PARSE_TIME.time()
    def parse(self, statline):
        if not self.is_up:
            self.is_up = True

        try:
            stats = json.loads(statline)
        except ValueError:
            return self.parser_failure()

        if 'name' not in stats:
            return self.parser_failure()

        if 'origin' not in stats:
            # Workaround for https://github.com/rsyslog/rsyslog/issues/1508
            # 'omkafka' module stats output contains no 'origin' field
            if stats['name'] == 'omkafka':
                stats['origin'] = 'omkafka'
            else:
                return self.parser_failure()

        origin = stats['origin']
        name = stats['name']
        metric_basename = self.metric_prefix + '_' + self._fix_metric_name(origin)

        if name == 'global':
            if not self._is_exported:
                self.export()

            # Special case for first line ("name":"global").
            # There are dynamic stats fields reported in <name>.<field> format
            for k, v in stats['values'].items():
                n, c = k.split('.')
                metric_name = metric_basename + '_' + self._fix_metric_name(c)
                self.add(metric_name, n, v)

        else:
            for k, v in stats.items():
                metric_name = metric_basename + '_' + self._fix_metric_name(k)
                if k not in ['origin', 'name']:
                    if k != 'values':
                        self.add(metric_name, name, v)
                    else:
                        if origin == 'dynstats.bucket':
                            metric_name = self.metric_prefix + '_dynstats_' + self._fix_metric_name(name)
                        for kk, vv in v.items():
                            self.add(metric_name, kk, vv)

        if self._is_exported:
            self.stats_count = 0
            self._is_exported = False

        self.stats_count += 1


class RsyslogCollector(object):
    """ Custom prometheus collector class """
    def __init__(self, stats):
        self._stats = stats

    @COLLECT_TIME.time()
    def collect(self):
        custom_label_names = self._stats.labels.keys()
        custom_label_values = self._stats.labels.values()

        m = GaugeMetricFamily(
            'rsyslog_exporter_version',
            'Version of rsyslog_exporter running',
            labels=['version'] + custom_label_names)
        m.add_metric([__version__] + custom_label_values, 1.0)
        yield m

        m = GaugeMetricFamily(
            'rsyslog_exporter_up',
            'Is rsyslog_exporter up and connected?',
            labels=custom_label_names)
        m.add_metric(custom_label_values, float(self._stats.is_up is True))
        yield m

        m = GaugeMetricFamily(
            'rsyslog_exporter_last_stats_processed',
            'Amount of rsyslog stats processed last time',
            labels=custom_label_names)
        m.add_metric(custom_label_values, self._stats.stats_count)
        yield m

        m = CounterMetricFamily(
            'rsyslog_exporter_parser_failures',
            'Amount of rsyslog stats parsing failures',
            labels=custom_label_names)
        m.add_metric(custom_label_values, self._stats.parser_failures)
        yield m

        m = GaugeMetricFamily(
            'rsyslog_exporter_last_export_timestamp',
            'Last metrics export timestamp',
            labels=custom_label_names)
        m.add_metric(custom_label_values, self._stats.export_time)
        yield m

        if not self._stats.is_up:
            return

        label_names = ['name'] + custom_label_names

        for metric_name, v in self._stats.counters().items():
            if metric_name == 'rsyslog_core_queue_size':
                m = GaugeMetricFamily(metric_name, '', labels=label_names)
            else:
                m = CounterMetricFamily(metric_name, '', labels=label_names)

            for name, value in v.items():
                m.add_metric([name] + custom_label_values, value)
            yield m


def parse_args():

    """ Parse cmdline args """
    parser = argparse.ArgumentParser(
        description='Export rsyslog stats to prometheus'
    )
    parser.add_argument(
        '-v', '--version',
        action='version',
        version='%(prog)s ' + __version__,
    )
    parser.add_argument(
        '-p', '--port',
        help='Port to serve metrics request on',
        type=int,
        default=int(os.environ.get('RSYSLOG_EXPORTER_PORT', 9292)),
        dest='port',
    )
    parser.add_argument(
        '-e', '--export-after',
        help='Export current stats if nothing is received during specified interval in seconds',
        type=float,
        default=float(os.environ.get('RSYSLOG_EXPORTER_EXPORT_AFTER', 5.0)),
        dest='export_after',
    )
    parser.add_argument(
        '-d', '--down-after',
        help='Mark exporter as down if nothing is received during specified interval in seconds',
        type=float,
        default=float(os.environ.get('RSYSLOG_EXPORTER_DOWN_AFTER', 180.0)),
        dest='down_after',
    )
    parser.add_argument(
        '-L', '--label',
        help='Add custom label to every rsyslog metric. Use multiple times to add multiple labels',
        action='append',
        default=os.environ.get('RSYSLOG_EXPORTER_LABELS', '').split(','),
        dest='labels',
    )
    return parser.parse_args()


def parse_labels(key_values):
    labels = {}
    for kv in key_values:
        try:
            k, v = kv.split('=')
        except ValueError:
            continue
        else:
            labels[k] = v
    return labels


def main():
    """ Main procedure """
    try:
        args = parse_args()

        if args.down_after <= args.export_after:
            sys.stderr.write("Down timeout must be greater than export timeout!\n")
            return 1

        stats = RsyslogStats()
        stats.labels = parse_labels(args.labels)

        # Make stdin unbuffered
        stdin_unbuf = os.fdopen(sys.stdin.fileno(), 'rb', 0)
        sys.stdin = stdin_unbuf

        # Start http server thread to expose metrics
        start_http_server(args.port)
        REGISTRY.register(RsyslogCollector(stats))

        sleep_seconds = args.down_after
        silent_seconds = 0
        keep_running = True
        while keep_running:

            sleep_start = time.time()
            if sys.stdin not in select.select([sys.stdin], [], [], sleep_seconds)[0]:
                sleep_end = time.time()
                slept_seconds = abs(sleep_end - sleep_start)
                silent_seconds += slept_seconds

                if not stats.is_exported() and silent_seconds >= args.export_after:
                    stats.export()

                if stats.is_up and silent_seconds >= args.down_after:
                    stats.is_up = False

                if not stats.is_up:
                    sleep_seconds = args.down_after
                else:
                    if stats.is_exported():
                        sleep_seconds = args.down_after - slept_seconds
                    else:
                        sleep_seconds = args.export_after - slept_seconds

            else:
                silent_seconds = 0
                sleep_seconds = args.export_after
                while keep_running and sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                    line = sys.stdin.readline()
                    if line:
                        json_start_idx = line.find('{')
                        json_end_idx = line.rfind('}')
                        stats.parse(line[json_start_idx:json_end_idx + 1])
                    else:
                        # Exit when EOF received on stdin
                        keep_running = False

    except KeyboardInterrupt:
        sys.stderr.write("Interrupted!\n")
        return 0


if __name__ == '__main__':
    sys.exit(main())
