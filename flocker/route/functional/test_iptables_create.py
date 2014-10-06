# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Tests for :py:mod:`flocker.route._iptables`.
# TODO rename this module to "test_iptables" - after the contents changes
# have been approved in review
"""

from __future__ import print_function

from time import sleep
from errno import ECONNREFUSED
from os import getuid, getpid
from socket import error, socket
from unittest import skipUnless
from subprocess import check_call

from ipaddr import IPAddress, IPNetwork
from eliot.testing import LoggedAction, validateLogging, assertHasAction

from twisted.trial.unittest import TestCase
from twisted.python.procutils import which

from ...testtools import if_root
from .. import make_host_network
from .._logging import CREATE_PROXY_TO, DELETE_PROXY, IPTABLES
from .networktests import make_proxying_tests

try:
    from .iptables import create_network_namespace, get_iptables_rules
    NOMENCLATURE_INSTALLED = True
except ImportError:
    NOMENCLATURE_INSTALLED = False


def connect_nonblocking(ip, port):
    """
    Attempt a TCP connection to the given address without blocking.
    """
    client = socket()
    client.setblocking(False)
    client.connect_ex((ip.exploded, port))
    return client


def create_user_rule():
    """
    Create an iptables rule which simulates an existing (or otherwise
    configured beyond flocker's control) rule on the system and needs to be
    ignored by :py:func:`enumerate_proxies`.
    """
    check_call([
        b"iptables",
        # Stick it in the PREROUTING chain based on our knowledge that the
        # implementation inspects this chain to enumerate proxies.
        b"--table", b"nat", b"--append", b"PREROUTING",

        b"--protocol", b"tcp", b"--dport", b"12345",
        b"--match", b"addrtype", b"--dst-type", b"LOCAL",

        b"--jump", b"DNAT", b"--to-destination", b"10.7.8.9",
        ])


def is_environment_configured():
    """
    Determine whether it is possible to exercise the proxy setup functionality
    in the current execution environment.

    :return: :obj:`True` if the proxy setup functionality could work given the
        underlying system and the privileges of this process, :obj:`False`
        otherwise.
    """
    return getuid() == 0


def some_iptables_logged(parent_action_type):
    """
    Create a validator which assert that some ``IPTABLES`` actions got logged.

    They should be logged as children of a ``parent_action_type`` action (but
    this function will not verify that).  No other assertions are made about
    the particulars of the message because that would be difficult (by virtue
    of requiring we duplicate the exact iptables commands from the
    implementation here, in the tests, which is tedious and produces fragile
    tests).
    """
    def validate(case, logger):
        assertHasAction(case, logger, parent_action_type, succeeded=True)
        # Remember what the docstring said?  Ideally this would inspect the
        # children of the action returned by assertHasAction but the interfaces
        # don't seem to line up.
        iptables = LoggedAction.ofType(logger.messages, IPTABLES)
        case.assertNotEqual(iptables, [])
    return validate


_environment_skip = skipUnless(
    is_environment_configured(),
    "Cannot test port forwarding without suitable test environment.")

_dependency_skip = skipUnless(
    NOMENCLATURE_INSTALLED,
    "Cannot test port forwarding without nomenclature installed.")

_iptables_skip = skipUnless(
    which(b"iptables-save"),
    "Cannot set up isolated environment without iptables-save.")


class GetIPTablesTests(TestCase):
    """
    Tests for the iptables rule preserving helper.
    """
    @_dependency_skip
    @_environment_skip
    def test_get_iptables_rules(self):
        """
        :py:code:`get_iptables_rules()` returns the same list of
        bytes as long as no rules have changed.
        """
        first = get_iptables_rules()
        # The most likely reason the result might change is that
        # `iptables-save` includes timestamps with one-second resolution in its
        # output.
        sleep(1.0)
        second = get_iptables_rules()
        self.assertEqual(first, second)


class IPTablesProxyTests(make_proxying_tests(make_host_network)):
    """
    Apply the generic ``INetwork`` test suite to the implementation which
    manipulates the actual system configuration.
    """
    @_dependency_skip
    @_environment_skip
    def setUp(self):
        """
        Arrange for the tests to not corrupt the system network configuration.
        """
        self.namespace = create_network_namespace()
        self.addCleanup(self.namespace.restore)
        super(IPTablesProxyTests, self).setUp()

    def test_proxied_ports_used_different_namespace(self):
        """
        :py:meth:`INetwork.enumerate_used_ports` does not limit used ports
        to those in the network's namespace, because used ports are global.
        """
        # Some random, not-very-likely-to-be-bound port number.  It's not a
        # disaster if this does accidentally collide with a port in use on
        # the host when the test suite runs but the test only demonstrates
        # what it's meant to demonstrate when it doesn't collide.

        # TODO This is not in `make_proxying_tests` because the memory
        # implementation doesn't know which ports have been used by other
        # networks. This should be resolved perhaps only by a change in the
        # docstring of this class.
        port_number = 18173
        network = make_host_network()
        network.create_proxy_to(IPAddress("10.0.0.3"), port_number)
        another_network = make_host_network(namespace="another_namespace")
        self.assertIn(port_number, another_network.enumerate_used_ports())


class CreateTests(TestCase):
    """
    Tests for the creation of new external routing rules.
    """
    @_dependency_skip
    @_environment_skip
    def setUp(self):
        """
        Select some addresses between which to proxy and set up a server to act
        as the target of the proxying.
        """
        self.namespace = create_network_namespace()
        self.addCleanup(self.namespace.restore)

        self.network = make_host_network()

        # https://github.com/ClusterHQ/flocker/issues/135
        # Don't hardcode addresses in the created namespace
        self.server_ip = self.namespace.ADDRESSES[0]
        self.proxy_ip = self.namespace.ADDRESSES[1]

        # This is the target of the proxy which will be created.
        self.server = socket()
        self.server.bind((self.server_ip.exploded, 0))
        self.server.listen(1)

        # This is used to accept connections over the local network stack.
        # They should be nearly instantaneous.  If they are not then something
        # is *probably* wrong (and hopefully it isn't just an instance of the
        # machine being so loaded the local network stack can't complete a TCP
        # handshake in under one second...).
        self.server.settimeout(1)
        self.port = self.server.getsockname()[1]

    def test_setup(self):
        """
        A connection attempt to the server created in ``setUp`` is successful.
        """
        client = connect_nonblocking(self.server_ip, self.port)
        accepted, client_address = self.server.accept()
        self.assertEqual(client.getsockname(), client_address)

    @validateLogging(some_iptables_logged(CREATE_PROXY_TO))
    def test_connection(self, logger):
        """
        A connection attempt is forwarded to the specified destination address.
        """
        self.patch(self.network, "logger", logger)

        self.network.create_proxy_to(self.server_ip, self.port)

        client = connect_nonblocking(self.proxy_ip, self.port)
        accepted, client_address = self.server.accept()
        self.assertEqual(client.getsockname(), client_address)

    def test_client_to_server(self):
        """
        A proxied connection will deliver bytes from the client side to the
        server side.
        """
        self.network.create_proxy_to(self.server_ip, self.port)

        client = connect_nonblocking(self.proxy_ip, self.port)
        accepted, client_address = self.server.accept()

        client.send(b"x")
        self.assertEqual(b"x", accepted.recv(1))

    def test_server_to_client(self):
        """
        A proxied connection will deliver bytes from the server side to the
        client side.
        """
        self.network.create_proxy_to(self.server_ip, self.port)

        client = connect_nonblocking(self.proxy_ip, self.port)
        accepted, client_address = self.server.accept()

        accepted.send(b"x")
        self.assertEqual(b"x", client.recv(1))

    def test_remote_connections_unaffected(self):
        """
        A connection attempt to an IP not assigned to this host on the proxied
        port is not proxied.
        """
        network = IPNetwork("172.16.0.0/12")
        gateway = network[1]
        address = network[2]

        # The strategy taken by this test is to create a new, clean network
        # stack and then treat it like a foreign host.  A connection to that
        # foreign host should not be proxied.  This is possible because Linux
        # supports the creation of an arbitrary number of instances of its
        # network stack, all isolated from each other.
        #
        # To learn more, here are some links:
        #
        # http://man7.org/linux/man-pages/man8/ip-netns.8.html
        # http://blog.scottlowe.org/2013/09/04/introducing-linux-network-namespaces/
        #
        # Note also that Linux network namespaces are how Docker creates
        # isolated network environments.

        # Create a remote "host" that the test can reliably fail a connection
        # attempt to.
        pid = getpid()
        veth0 = b"veth_" + hex(pid)
        veth1 = b"veth1"
        network_namespace = b"%s.%s" % (self.id(), getpid())

        def run(cmd):
            check_call(cmd.split())

        # Destroy whatever system resources we go on to allocate in this test.
        # We set this up first so even if one of the operations encounters an
        # error after a resource has been allocated we'll still clean it up.
        # It's not an error to try to delete things that don't exist
        # (conveniently).
        self.addCleanup(run, b"ip netns delete " + network_namespace)
        self.addCleanup(run, b"ip link delete " + veth0)

        ops = [
            # Create a new network namespace where we can assign a non-local
            # address to use as the target of a connection attempt.
            b"ip netns add %(netns)s",

            # Create a virtual ethernet pair so there is a network link between
            # the host and the new network namespace.
            b"ip link add %(veth0)s type veth peer name %(veth1)s",

            # Assign an address to the virtual ethernet interface that will
            # remain on the host.  This will be our "gateway" into the network
            # namespace.
            b"ip address add %(gateway)s dev %(veth0)s",

            # Bring it up.
            b"ip link set dev %(veth0)s up",

            # Put the other virtual ethernet interface into the network
            # namespace.  Now it will only affect networking behavior for code
            # running in that network namespace, not for code running directly
            # on the host network (like the code in this test and whatever
            # iptables rules we created).
            b"ip link set %(veth1)s netns %(netns)s",

            # Assign to that virtual ethernet interface an address on the same
            # (private, unused) network as the address we gave to the gateway
            # interface.
            b"ip netns exec %(netns)s ip address add %(address)s "
            b"dev %(veth1)s",

            # And bring it up.
            b"ip netns exec %(netns)s ip link set dev %(veth1)s up",

            # Add a route into the network namespace via the virtual interface
            # for traffic bound for addresses on that network.
            b"ip route add %(network)s dev %(veth0)s scope link",

            # And add a reciprocal route so traffic generated inside the
            # network namespace (like TCP RST packets) can get back to us.
            b"ip netns exec %(netns)s ip route add default dev %(veth1)s",
        ]

        params = dict(
            netns=network_namespace, veth0=veth0, veth1=veth1,
            address=address, gateway=gateway, network=network,
            )
        for op in ops:
            run(op % params)

        # Create the proxy which we expect not to be invoked.
        self.network.create_proxy_to(self.server_ip, self.port)

        client = socket()
        client.settimeout(1)

        # Try to connect to an address hosted inside that network namespace.
        # It should fail.  It should not be proxied to the server created in
        # setUp.
        exception = self.assertRaises(
            error, client.connect, (str(address), self.port))
        self.assertEqual(ECONNREFUSED, exception.errno)


class EnumerateTests(TestCase):
    """
    Tests for the enumerate of Flocker-managed external routing rules.
    """
    @_dependency_skip
    @_environment_skip
    def setUp(self):
        self.addCleanup(create_network_namespace().restore)
        self.network = make_host_network()

    def test_different_namespaces_not_enumerated(self):
        """
        If there are rules in NAT table which aren't related to the given
        namespace then
        :py:func:`enumerate_proxies` does not include information about them in
        its return function.
        """
        another_network = make_host_network(u"another_namespace")
        self.network.create_proxy_to(IPAddress("10.1.2.3"), 1234)
        self.assertEqual([], another_network.enumerate_proxies())

    def test_proxies_enumerated(self):
        """
        :py:func:`enumerate_proxies` includes information about rules in the
        NAT table which are related to the given namespace.
        """
        another_network = make_host_network(u"\N{HEAVY BLACK HEART}")
        proxy = another_network.create_proxy_to(IPAddress("10.1.2.3"), 1234)
        self.assertEqual([proxy], another_network.enumerate_proxies())

    # TODO move this somewhere which also tests the memory implementation.
    def test_proxy_has_namespace(self):
        """
        Proxies which :py:func:`enumerate_proxies` returns have the correct
        namespaces.
        """
        namespace = u"my_namespace"
        another_network = make_host_network(namespace)
        another_network.create_proxy_to(IPAddress("10.1.2.3"), 1234)
        proxy = another_network.enumerate_proxies()[0]
        self.assertEqual(proxy.namespace, namespace)

    def test_unrelated_iptables_rules(self):
        """
        If there are rules in NAT table which aren't related to flocker then
        :py:func:`enumerate_proxies` does not include information about them in
        its return value.
        """
        create_user_rule()
        proxy = self.network.create_proxy_to(IPAddress("10.1.2.3"), 1234)
        self.assertEqual([proxy], self.network.enumerate_proxies())


class DeleteTests(TestCase):
    """
    Tests for the deletion of Flocker-managed external routing rules.
    """
    @_dependency_skip
    @_environment_skip
    def setUp(self):
        self.addCleanup(create_network_namespace().restore)
        self.network = make_host_network()

    @validateLogging(some_iptables_logged(DELETE_PROXY))
    def test_created_rules_deleted(self, logger):
        """
        After a route created using :py:func:`flocker.route.create_proxy_to` is
        deleted using :py:meth:`delete_proxy` the iptables rules which were
        added by the former are removed.
        """
        original_rules = get_iptables_rules()

        proxy = self.network.create_proxy_to(IPAddress("10.1.2.3"), 12345)

        # Only interested in logging behavior of delete_proxy here.
        self.patch(self.network, "logger", logger)
        self.network.delete_proxy(proxy)

        # Capture the new rules
        new_rules = get_iptables_rules()

        # And compare them against the rules when we started.
        self.assertEqual(
            original_rules,
            new_rules)

    def test_only_specified_proxy_deleted(self):
        """
        Only the rules associated with the proxy specified by the object passed
        to :py:func:`delete_proxy` are deleted.
        """
        self.network.create_proxy_to(IPAddress("10.1.2.3"), 12345)

        # Capture the rules that exist now for comparison later.
        expected = get_iptables_rules()

        delete = self.network.create_proxy_to(IPAddress("10.1.2.4"), 23456)
        self.network.delete_proxy(delete)

        # Capture the new rules
        actual = get_iptables_rules()

        # They should match because only the second proxy should have been torn
        # down.
        self.assertEqual(
            expected,
            actual)


class UsedPortsTests(TestCase):
    """
    Tests for enumeration of used ports.
    """
    @if_root
    @_iptables_skip
    def setUp(self):
        pass

    def _listening_test(self, interface):
        """
        Verify that a socket listening on the given interface has its port
        number included in the result of ``HostNetwork.enumerate_used_ports``.

        :param str interface: A native string giving the address of the
            interface to which the listening socket will be bound.

        :raise: If the port number is not indicated as used, a failure
            exception is raised.
        """
        network = make_host_network()
        listener = socket()
        self.addCleanup(listener.close)

        listener.bind((interface, 0))
        listener.listen(3)

        self.assertIn(
            listener.getsockname()[1], network.enumerate_used_ports())

    def test_listening_ports(self):
        """
        If a socket is bound to a port and listening the port number is
        included in the result of ``HostNetwork.enumerate_used_ports``.
        """
        self._listening_test('')

    def test_localhost_listening_ports(self):
        """
        If a socket is bound to a port on localhost only the port number is
        included in the result of ``HostNetwork.enumerate_used_ports``.
        """
        self._listening_test('127.0.0.1')

    def test_client_ports(self):
        """
        If a socket is bound to a port and connected to a server then the
        client port is included in ``HostNetwork.enumerate_used_ports``\ s
        return value.
        """
        network = make_host_network()
        listener = socket()
        self.addCleanup(listener.close)

        listener.listen(3)

        client = socket()
        self.addCleanup(client.close)

        client.setblocking(False)
        try:
            client.connect_ex(listener.getsockname())
        except error:
            pass

        self.assertIn(
            client.getsockname()[1], network.enumerate_used_ports())
