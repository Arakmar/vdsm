from collections import deque
import threading
import time

from functional import dummy
from functional.networkTests import IP_ADDRESS, IP_CIDR
from vdsm.netlink import monitor
from vdsm.sysctl import is_disabled_ipv6
from vdsm.utils import monotonic_time

from testValidation import ValidateRunningAsRoot
from testlib import VdsmTestCase as TestCaseBase


class NetlinkEventMonitorTests(TestCaseBase):

    TIMEOUT = 1

    @ValidateRunningAsRoot
    def test_iterate_after_events(self):
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            dummy_name = dummy.create()
            dummy.remove(dummy_name)
            for event in mon:
                if event.get('name') == dummy_name:
                    break

    @ValidateRunningAsRoot
    def test_iterate_while_events(self):
        """Tests if monitor is able to catch event while iterating. Before the
        iteration we start _set_and_remove_device, which is delayed for .2
        seconds. Then iteration starts and wait for new dummy.
        """
        dummy_name = dummy.create()

        def _set_and_remove_device():
            time.sleep(.2)
            dummy.setLinkUp(dummy_name)
            dummy.remove(dummy_name)
        add_device_thread = threading.Thread(target=_set_and_remove_device)

        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            add_device_thread.start()
            for event in mon:
                if event.get('name') == dummy_name:
                    break
            add_device_thread.join()

    @ValidateRunningAsRoot
    def test_stopped(self):
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            dummy_name = dummy.create()
            dummy.remove(dummy_name)

        found = any(event.get('name') == dummy_name for event in mon)
        self.assertTrue(found, 'Expected event was not caught.')

    @ValidateRunningAsRoot
    def test_event_groups(self):
        with monitor.Monitor(timeout=self.TIMEOUT,
                             groups=('ipv4-ifaddr',)) as mon_a:
            with monitor.Monitor(timeout=self.TIMEOUT,
                                 groups=('link', 'ipv4-route')) as mon_l_r:
                dummy_name = dummy.create()
                dummy.setIP(dummy_name, IP_ADDRESS, IP_CIDR)
                dummy.setLinkUp(dummy_name)
                dummy.remove(dummy_name)

        for event in mon_a:
            self.assertIn('_addr', event['event'], "Caught event '%s' is not "
                          "related to address." % event['event'])

        for event in mon_l_r:
            link_or_route = ('_link' in event['event'] or
                             '_route' in event['event'])
            self.assertTrue(link_or_route, "Caught event '%s' is not related "
                            "to link or route." % event['event'])

    @ValidateRunningAsRoot
    def test_iteration(self):
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            iterator = iter(mon)

            # Generate events to avoid blocking
            dummy_name = dummy.create()
            iterator.next()

            dummy.remove(dummy_name)
            iterator.next()

        with self.assertRaises(StopIteration):
            while True:
                iterator.next()

    @ValidateRunningAsRoot
    def test_events_keys(self):
        def _expected_events(nic, address, cidr):
            events_add = [
                {'event': 'new_link', 'name': nic},
                {'event': 'new_addr', 'address': address + '/' + cidr},
                {'event': 'new_link', 'name': nic}]
            events_del = [
                {'address': address + '/' + cidr, 'event': 'del_addr'},
                {'destination': address, 'event': 'del_route'},
                {'event': 'del_link', 'name': nic}]
            events_ipv6 = [
                {'event': 'new_addr', 'family': 'inet6'},
                {'event': 'del_addr', 'family': 'inet6'}]
            if is_disabled_ipv6():
                return deque(events_add + events_del)
            else:
                return deque(events_add + events_ipv6 + events_del)

        with monitor.Monitor(timeout=self.TIMEOUT,
                             silent_timeout=True) as mon:
            dummy_name = dummy.create()
            dummy.setIP(dummy_name, IP_ADDRESS, IP_CIDR)
            dummy.setLinkUp(dummy_name)
            dummy.remove(dummy_name)

            expected_events = _expected_events(dummy_name, IP_ADDRESS, IP_CIDR)

            expected = expected_events.popleft()
            for event in mon:
                if _is_subdict(expected, event):
                    expected = expected_events.popleft()
                    if len(expected_events) == 0:
                        break

        self.assertEqual(0, len(expected_events), '%d expected events have not'
                         ' been caught (in the right order)'
                         % (1 + len(expected_events)))

    def test_timeout(self):
        with self.assertRaises(monitor.MonitorError):
            try:
                with monitor.Monitor(timeout=.01) as mon:
                    for event in mon:
                        pass
            except monitor.MonitorError as e:
                self.assertEquals(e[0], monitor.E_TIMEOUT)
                raise

        self.assertTrue(mon.is_stopped())

    def test_timeout_silent(self):
        with monitor.Monitor(timeout=.01, silent_timeout=True) as mon:
            for event in mon:
                pass

        self.assertTrue(mon.is_stopped())

    @ValidateRunningAsRoot
    def test_timeout_not_triggered(self):
        time_start = monotonic_time()
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            dummy_name = dummy.create()
            dummy.remove(dummy_name)

            for event in mon:
                break

        self.assertTrue((monotonic_time() - time_start) <= self.TIMEOUT)
        self.assertTrue(mon.is_stopped())


def _is_subdict(subset, superset):
    return all(item in superset.items() for item in subset.items())
