# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Unit tests for :py:mod:`flocker.route._memory`.
"""

from ipaddr import IPAddress

from twisted.trial.unittest import SynchronousTestCase

from .. import make_memory_network, Proxy


class MemoryProxyTests(SynchronousTestCase):
    """
    Tests for distinctive behaviors of the ``INetwork`` provider created by
    ``make_memory_network``.
    """
    def test_custom_used_ports(self):
        """
        Additional used ports can be specified by passing them to
        ``make_memory_network``.
        """
        extra = 20001
        ports = frozenset({50, 100, 15000})
        network = make_memory_network(used_ports=ports)
        network.create_proxy_to(IPAddress("10.0.0.1"), extra)
        expected = frozenset(ports | {extra})
        self.assertEqual(expected, network.enumerate_used_ports())

    def test_proxy_has_namespace(self):
        """
        Proxies which :py:func:`enumerate_proxies` returns have the namespace
        passed to ``make_memory_network``.
        """
        namespace = u"my_namespace"
        another_network = make_memory_network(namespace=namespace)
        another_network.create_proxy_to(IPAddress("10.1.2.3"), 1234)
        proxy = another_network.enumerate_proxies()[0]
        self.assertEqual(proxy.namespace, namespace)

    def test_default_namespace(self):
        """
        If no namespace is passed to ``make_memory_network``,
        ``enumerate_proxies`` returns proxies with a default namespace.
        """
        ip = IPAddress("10.2.3.4")
        port = 4321
        namespace = "default"
        network = make_memory_network()
        network.create_proxy_to(ip, 4321)
        expected = Proxy(ip=ip, port=port, namespace=namespace)
        self.assertEqual([expected], network.enumerate_proxies())
