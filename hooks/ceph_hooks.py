#!/usr/bin/python

#
# Copyright 2012 Canonical Ltd.
#
# Authors:
#  James Page <james.page@ubuntu.com>
#

import glob
import os
import shutil
import sys
import tempfile
import socket

import ceph
from charmhelpers.core.hookenv import (
    log,
    ERROR,
    DEBUG,
    config,
    relation_ids,
    related_units,
    relation_get,
    Hooks,
    UnregisteredHookError,
    service_name,
    status_set,
)
from charmhelpers.core.host import (
    umount,
    mkdir,
    cmp_pkgrevno
)
from charmhelpers.fetch import (
    add_source,
    apt_install,
    apt_update,
    filter_installed_packages,
)
from charmhelpers.core.sysctl import create as create_sysctl

from utils import (
    get_host_ip,
    get_networks,
    assert_charm_supports_ipv6,
    render_template,
)

from charmhelpers.contrib.openstack.alternatives import install_alternative
from charmhelpers.contrib.network.ip import (
    get_ipv6_addr,
    format_ipv6_addr,
)

from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.contrib.hardening.harden import harden

hooks = Hooks()


def install_upstart_scripts():
    # Only install upstart configurations for older versions
    if cmp_pkgrevno('ceph', "0.55.1") < 0:
        for x in glob.glob('files/upstart/*.conf'):
            shutil.copy(x, '/etc/init/')


@hooks.hook('install.real')
@harden()
def install():
    add_source(config('source'), config('key'))
    apt_update(fatal=True)
    apt_install(packages=ceph.PACKAGES, fatal=True)
    install_upstart_scripts()


def az_info():
    az_info = os.environ.get('JUJU_AVAILABILITY_ZONE')
    log("AZ Info: " + az_info)
    return az_info


def emit_cephconf():
    mon_hosts = get_mon_hosts()
    log('Monitor hosts are ' + repr(mon_hosts))

    networks = get_networks('ceph-public-network')
    public_network = ', '.join(networks)

    networks = get_networks('ceph-cluster-network')
    cluster_network = ', '.join(networks)

    cephcontext = {
        'auth_supported': get_auth(),
        'mon_hosts': ' '.join(mon_hosts),
        'fsid': get_fsid(),
        'old_auth': cmp_pkgrevno('ceph', "0.51") < 0,
        'osd_journal_size': config('osd-journal-size'),
        'use_syslog': str(config('use-syslog')).lower(),
        'ceph_public_network': public_network,
        'ceph_cluster_network': cluster_network,
        'loglevel': config('loglevel'),
        'dio': str(config('use-direct-io')).lower(),
    }

    if config('prefer-ipv6'):
        dynamic_ipv6_address = get_ipv6_addr()[0]
        if not public_network:
            cephcontext['public_addr'] = dynamic_ipv6_address
        if not cluster_network:
            cephcontext['cluster_addr'] = dynamic_ipv6_address

    if az_info():
        cephcontext['crush_location'] = "root=default rack={} host={}" \
            .format(az_info(), socket.gethostname())

    # Install ceph.conf as an alternative to support
    # co-existence with other charms that write this file
    charm_ceph_conf = "/var/lib/charm/{}/ceph.conf".format(service_name())
    mkdir(os.path.dirname(charm_ceph_conf), owner=ceph.ceph_user(),
          group=ceph.ceph_user())
    with open(charm_ceph_conf, 'w') as cephconf:
        cephconf.write(render_template('ceph.conf', cephcontext))
    install_alternative('ceph.conf', '/etc/ceph/ceph.conf',
                        charm_ceph_conf, 90)

JOURNAL_ZAPPED = '/var/lib/ceph/journal_zapped'


def read_zapped_journals():
    if os.path.exists(JOURNAL_ZAPPED):
        with open(JOURNAL_ZAPPED) as zapfile:
            zapped = set(
                filter(None,
                       [l.strip() for l in zapfile.readlines()]))
            log("read zapped: {}".format(zapped), level=DEBUG)
            return zapped
    return set()


def write_zapped_journals(journal_devs):
    tmpfh, tmpfile = tempfile.mkstemp()
    with os.fdopen(tmpfh, 'wb') as zapfile:
        log("write zapped: {}".format(journal_devs),
            level=DEBUG)
        zapfile.write('\n'.join(sorted(list(journal_devs))))
    os.rename(tmpfile, JOURNAL_ZAPPED)


def check_overlap(journaldevs, datadevs):
    if not journaldevs.isdisjoint(datadevs):
        msg = ("Journal/data devices mustn't"
               " overlap; journal: {0}, data: {1}".format(journaldevs,
                                                          datadevs))
        log(msg, level=ERROR)
        raise ValueError(msg)


@hooks.hook('config-changed')
@harden()
def config_changed():
    # Pre-flight checks
    if config('osd-format') not in ceph.DISK_FORMATS:
        log('Invalid OSD disk format configuration specified', level=ERROR)
        sys.exit(1)

    if config('prefer-ipv6'):
        assert_charm_supports_ipv6()

    sysctl_dict = config('sysctl')
    if sysctl_dict:
        create_sysctl(sysctl_dict, '/etc/sysctl.d/50-ceph-osd-charm.conf')

    e_mountpoint = config('ephemeral-unmount')
    if (e_mountpoint and ceph.filesystem_mounted(e_mountpoint)):
        umount(e_mountpoint)
    prepare_disks_and_activate()


def prepare_disks_and_activate():
    osd_journal = get_journal_devices()
    check_overlap(osd_journal, set(get_devices()))
    log("got journal devs: {}".format(osd_journal), level=DEBUG)
    already_zapped = read_zapped_journals()
    non_zapped = osd_journal - already_zapped
    for journ in non_zapped:
        ceph.maybe_zap_journal(journ)
    write_zapped_journals(osd_journal)

    if ceph.is_bootstrapped():
        log('ceph bootstrapped, rescanning disks')
        emit_cephconf()
        for dev in get_devices():
            ceph.osdize(dev, config('osd-format'),
                        osd_journal, config('osd-reformat'),
                        config('ignore-device-errors'),
                        config('osd-encrypt'))
        ceph.start_osds(get_devices())


def get_mon_hosts():
    hosts = []
    for relid in relation_ids('mon'):
        for unit in related_units(relid):
            addr = relation_get('ceph-public-address', unit, relid) or \
                get_host_ip(relation_get('private-address', unit, relid))

            if addr:
                hosts.append('{}:6789'.format(format_ipv6_addr(addr) or addr))

    hosts.sort()
    return hosts


def get_fsid():
    return get_conf('fsid')


def get_auth():
    return get_conf('auth')


def get_conf(name):
    for relid in relation_ids('mon'):
        for unit in related_units(relid):
            conf = relation_get(name,
                                unit, relid)
            if conf:
                return conf
    return None


def reformat_osd():
    if config('osd-reformat'):
        return True
    else:
        return False


def get_devices():
    if config('osd-devices'):
        return [
            os.path.realpath(path)
            for path in config('osd-devices').split(' ')]
    else:
        return []


def get_journal_devices():
    osd_journal = config('osd-journal')
    if not osd_journal:
        return set()
    osd_journal = [l.strip() for l in config('osd-journal').split(' ')]
    osd_journal = set(filter(os.path.exists, osd_journal))
    return osd_journal


@hooks.hook('mon-relation-changed',
            'mon-relation-departed')
def mon_relation():
    bootstrap_key = relation_get('osd_bootstrap_key')
    if get_fsid() and get_auth() and bootstrap_key:
        log('mon has provided conf- scanning disks')
        emit_cephconf()
        ceph.import_osd_bootstrap_key(bootstrap_key)
        prepare_disks_and_activate()
    else:
        log('mon cluster has not yet provided conf')


@hooks.hook('upgrade-charm')
@harden()
def upgrade_charm():
    if get_fsid() and get_auth():
        emit_cephconf()
    install_upstart_scripts()
    apt_install(packages=filter_installed_packages(ceph.PACKAGES),
                fatal=True)


@hooks.hook('nrpe-external-master-relation-joined',
            'nrpe-external-master-relation-changed')
def update_nrpe_config():
    # python-dbus is used by check_upstart_job
    apt_install('python-dbus')
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe_setup.add_check(
        shortname='ceph-osd',
        description='process check {%s}' % current_unit,
        check_cmd=('/bin/cat /var/lib/ceph/osd/ceph-*/whoami |'
                   'xargs -I@ status ceph-osd id=@ && exit 0 || exit 2')
    )
    nrpe_setup.write()


def assess_status():
    '''Assess status of current unit'''
    # Check for mon relation
    if len(relation_ids('mon')) < 1:
        status_set('blocked', 'Missing relation: monitor')
        return

    # Check for monitors with presented addresses
    # Check for bootstrap key presentation
    monitors = get_mon_hosts()
    if len(monitors) < 1 or not get_conf('osd_bootstrap_key'):
        status_set('waiting', 'Incomplete relation: monitor')
        return

    # Check for OSD device creation parity i.e. at least some devices
    # must have been presented and used for this charm to be operational
    running_osds = ceph.get_running_osds()
    if not running_osds:
        status_set('blocked',
                   'No block devices detected using current configuration')
    else:
        status_set('active',
                   'Unit is ready ({} OSD)'.format(len(running_osds)))


@hooks.hook('update-status')
@harden()
def update_status():
    log('Updating status.')


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status()
