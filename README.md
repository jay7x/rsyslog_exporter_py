# rsyslog prometheus exporter

This software is acting like a proxy. It reads rsyslog stats on stdin, convert them to the prometheus mertics and expose them via HTTP. Rsyslog stats are expected in 'json' format.

## How to setup

### 1. Put `rsyslog_exporter.py` into /usr/local/bin directory
```
$ sudo install -m 0755 -o root -g root -D -v rsyslog_exporter.py /usr/local/bin/
'rsyslog_exporter.py' -> '/usr/local/bin/rsyslog_exporter.py'
```
### 2. Store following snippet into /etc/rsyslog.d/stats.conf

```
module(load="omprog")

module(load="impstats"
  interval="60"
  resetCounters="off"
  format="json"
  ruleset="stats"
)

template(name="stats_exporter_tmpl" type="string" string="%msg%\n")

ruleset(name="stats"
) {
  action(type="omprog" name="stats_exporter"
    binary="/usr/bin/python -u /usr/local/bin/rsyslog_exporter.py -p 9292 -e 5 -d 120"
    signalOnClose="on"
    template="stats_exporter_tmpl"
  )
}
```
Please note `rsyslog_exporter.py` command line parameters:
* `-e` timeout should be small enough to export metrics faster but big enough to prevent export of unfinished data block. 5s looks fine (and it's default value).
* `-d` timeout should be 2-3 times of impstat's `interval` value. 120s-180s is ok for config above.

### 3. Check rsyslog configuration systax by running `rsyslogd -N 1`
### 4. Restart rsyslog if no errors found (`systemctl restart rsyslog` e.g.)
### 5. Go to http://localhost:9292/ to see metrics

