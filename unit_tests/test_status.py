import mock
import test_utils

from mock import patch

with patch('charmhelpers.contrib.hardening.harden.harden') as mock_dec:
    mock_dec.side_effect = (lambda *dargs, **dkwargs: lambda f:
                            lambda *args, **kwargs: f(*args, **kwargs))
    import ceph_hooks as hooks

TO_PATCH = [
    'status_set',
    'config',
    'ceph',
    'relation_ids',
    'relation_get',
    'related_units',
    'get_conf',
]

CEPH_MONS = [
    'ceph/0',
    'ceph/1',
    'ceph/2',
]


class ServiceStatusTestCase(test_utils.CharmTestCase):

    def setUp(self):
        super(ServiceStatusTestCase, self).setUp(hooks, TO_PATCH)
        self.config.side_effect = self.test_config.get

    def test_assess_status_no_monitor_relation(self):
        self.relation_ids.return_value = []
        hooks.assess_status()
        self.status_set.assert_called_with('blocked', mock.ANY)

    def test_assess_status_monitor_relation_incomplete(self):
        self.relation_ids.return_value = ['mon:1']
        self.related_units.return_value = CEPH_MONS
        self.get_conf.return_value = None
        hooks.assess_status()
        self.status_set.assert_called_with('waiting', mock.ANY)

    def test_assess_status_monitor_complete_no_disks(self):
        self.relation_ids.return_value = ['mon:1']
        self.related_units.return_value = CEPH_MONS
        self.get_conf.return_value = 'monitor-bootstrap-key'
        self.ceph.get_running_osds.return_value = []
        hooks.assess_status()
        self.status_set.assert_called_with('blocked', mock.ANY)

    def test_assess_status_monitor_complete_disks(self):
        self.relation_ids.return_value = ['mon:1']
        self.related_units.return_value = CEPH_MONS
        self.get_conf.return_value = 'monitor-bootstrap-key'
        self.ceph.get_running_osds.return_value = ['12345',
                                                   '67890']
        hooks.assess_status()
        self.status_set.assert_called_with('active', mock.ANY)
