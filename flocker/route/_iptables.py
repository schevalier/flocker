# Copyright Hybrid Logic Ltd.  See LICENSE file for details.
# -*- test-case-name: flocker.route.test_create -*-

"""
Manipulate network routing behavior on a node using ``iptables``.
"""

from __future__ import unicode_literals

import shlex
from subprocess import check_call, check_output

from zope.interface import implementer
from ipaddr import IPAddress
from characteristic import attributes
from eliot import Logger
from psutil import net_connections
from twisted.python.filepath import FilePath

from ._logging import CREATE_PROXY_TO, DELETE_PROXY, IPTABLES
from ._interfaces import INetwork
from ._model import Proxy

FLOCKER_COMMENT_MARKER = b"flocker "


@attributes(["comment", "destination_port", "to_destination"])
class RuleOptions(object):
    """
    :ivar bytes comment: The value of the ``comment`` *match* for this rule.

    :ivar int destination_port: The value of the ``destination-port`` option
        for the ``tcp`` *match* for this rule.

    :ivar IPv4Address to_destination: The value of the ``to-destination``
        option for the ``DNAT`` *target* for this rule.
    """


def iptables(logger, argv):
    """
    Run ``iptables`` with the given arguments.

    :param list argv: A standard ``argv``-style argument list.  The path to
        iptables is prepended to this list for execution.
    """
    with IPTABLES(logger=logger, argv=argv):
        check_call([b"iptables"] + argv)


def create_proxy_to(logger, ip, port, tag):
    # TODO accept a tag argument and append it to FLOCKER_COMMENT_MARKER for use as the comment below
    """
    :see: ``HostNetwork.create_proxy_to``
    """
    # log the comment too
    action = CREATE_PROXY_TO(
        logger=logger, target_ip=ip, target_port=port)

    with action:
        encoded_ip = unicode(ip).encode("ascii")
        encoded_port = unicode(port).encode("ascii")

        # The first goal is to configure "Destination NAT" (DNAT).  We're just
        # going to rewrite the destination address of traffic arriving on the
        # specified port so it looks like it is destined for the specified ip
        # instead of destined for "us".  This gets the packets delivered to the
        # right destination.
        iptables(logger, [
            # All NAT stuff happens in the netfilter NAT table.
            b"--table", b"nat",

            # Destination NAT has to happen "pre"-routing so that the normal
            # routing rules on the machine will use the re-written destination
            # address and get the packet to that new destination.  Accomplish
            # this by appending the rule to the PREROUTING chain.
            b"--append", b"PREROUTING",

            # Only re-route traffic with a destination port matching the one we
            # were told to manipulate.  It is also necessary to specify TCP (or
            # UDP) here since that is the layer of the network stack that
            # defines ports.
            b"--protocol", b"tcp", b"--destination-port", encoded_port,

            # And only re-route traffic directed at this host.  Traffic
            # originating on this host directed at some random other host that
            # happens to be on the same port should be left alone.
            b"--match", b"addrtype", b"--dst-type", b"LOCAL",

            # Tag it as a flocker-created rule so we can recognize it later.
            # TODO use it here
            b"--match", b"comment", b"--comment", FLOCKER_COMMENT_MARKER + tag,

            # If the filter matched, jump to the DNAT chain to handle doing the
            # actual packet mangling.  DNAT is a built-in chain that already
            # knows how to do this.  Pass an argument to the DNAT chain so it
            # knows how to mangle the packet - rewrite the destination IP of
            # the address to the target we were told to use.
            b"--jump", b"DNAT", b"--to-destination", encoded_ip,
        ])

        # Bonus round!  Having performed DNAT (changing the destination) during
        # prerouting we are now prepared to send the packet on somewhere else.
        # On its way out of this system it is also necessary to further
        # modify and then track that packet.  We want it to look like it
        # comes from us (the downstream client will be *very* confused if
        # the node we're passing the packet on to replies *directly* to them;
        # and by confused I mean it will be totally broken, of course) so we
        # also need to "masquerade" in the postrouting chain.  This changes
        # the source address (ip and port) of the packet to the address of
        # the external interface the packet is exiting upon. Doing SNAT here
        # would be a little bit more efficient because the kernel could avoid
        # looking up the external interface's address for every single packet.
        # But it requires this code to know that address and it requires that
        # if it ever changes the rule gets updated and it may require some
        # steps to do port allocation (not sure what they are yet).  So we'll
        # just masquerade for now.
        iptables(logger, [
            # All NAT stuff happens in the netfilter NAT table.
            b"--table", b"nat",

            # As described above, this transformation happens after routing
            # decisions have been made and the packet is on its way out of the
            # system.  Therefore, append the rule to the POSTROUTING chain.
            b"--append", b"POSTROUTING",

            # We'll stick to matching the same kinds of packets we matched in
            # the earlier stage.  We might want to change the factoring of this
            # code to avoid the duplication - particularly in case we want to
            # change the specifics of the filter.
            #
            # This omits the LOCAL addrtype check, though, because at this
            # point the packet is definitely leaving this host.
            b"--protocol", b"tcp", b"--destination-port", encoded_port,

            # Do the masquerading.
            b"--jump", b"MASQUERADE",
        ])

        # Secret level!!  Traffic that originates *on* the host bypasses the
        # PREROUTING chain.  Instead, it passes through the OUTPUT chain.  If
        # we want connections from localhost to the forwarded port to be
        # affected then we need a rule in the OUTPUT chain to do the same kind
        # of DNAT that we did in the PREROUTING chain.
        iptables(logger, [
            # All NAT stuff happens in the netfilter NAT table.
            b"--table", b"nat",

            # As mentioned, this rule is for the OUTPUT chain.
            b"--append", b"OUTPUT",

            # Matching the exact same kinds of packets as the PREROUTING rule
            # matches.
            b"--protocol", b"tcp",
            b"--destination-port", encoded_port,
            b"--match", b"addrtype", b"--dst-type", b"LOCAL",

            # Do the same DNAT as we did in the rule for the PREROUTING chain.
            b"--jump", b"DNAT", b"--to-destination", encoded_ip,
        ])

        # The network stack only considers forwarding traffic when certain
        # system configuration is in place.
        #
        # https://www.kernel.org/doc/Documentation/networking/ip-sysctl.txt
        # will explain the meaning of these in (very slightly) more detail.
        conf = FilePath(b"/proc/sys/net/ipv4/conf")
        descendant = conf.descendant([b"default", b"forwarding"])
        with descendant.open("wb") as forwarding:
            forwarding.write(b"1")

        # In order to have the OUTPUT chain DNAT rule affect routing decisions,
        # we also need to tell the system to make routing decisions about
        # traffic from or to localhost.
        for path in conf.children():
            with path.child(b"route_localnet").open("wb") as route_localnet:
                route_localnet.write(b"1")

        return Proxy(ip=ip, port=port, namespace=tag)


def delete_proxy(logger, proxy):
    """
    :see: ``HostNetwork.delete_proxy``
    """
    ip = unicode(proxy.ip).encode("ascii")
    port = unicode(proxy.port).encode("ascii")
    namespace = proxy.namespace.encode("utf-8")
    
    commands = [
        [b"--table", b"nat",
         b"--delete", b"PREROUTING",
         b"--protocol", b"tcp", b"--destination-port", port,
         b"--match", b"addrtype", b"--dst-type", b"LOCAL",
         # TODO use proxy.namespace and FLOCKER_COMMENT_MARKER to construct correct comment
         b"--match", b"comment", b"--comment", FLOCKER_COMMENT_MARKER + namespace,
         b"--jump", b"DNAT", b"--to-destination", ip],
        [b"--table", b"nat",
         b"--delete", b"POSTROUTING",
         b"--protocol", b"tcp", b"--destination-port", port,
         b"--jump", b"MASQUERADE"],
        [b"--table", b"nat",
         b"--delete", b"OUTPUT",
         b"--protocol", b"tcp", b"--destination-port", port,
         b"--match", b"addrtype", b"--dst-type", b"LOCAL",
         b"--jump", b"DNAT", b"--to-destination", ip],
    ]

    with DELETE_PROXY(logger, target_ip=proxy.ip, target_port=proxy.port):
        for argv in commands:
            iptables(logger, argv)


def enumerate_proxies():
    """
    Inspect the system's iptables configuration to determine what proxies
    currently exist.

    :see: :py:meth:`INetwork.enumerate_proxies` for parameter documentation.
    """
    proxies = []
    for rule in get_flocker_rules():
        comment = rule.comment
        namespace = comment[len(FLOCKER_COMMENT_MARKER):]
        proxies.append(
             Proxy(ip=rule.to_destination, port=rule.destination_port,
                   namespace=namespace.decode('utf-8')))

    return proxies


def get_flocker_rules():
    """
    Look up all of the iptables rules created/managed by flocker.

    :return: An iterator of :py:class:`Options` instances, one for each rule
        found.
    """
    # Life is horrible.
    # https://stackoverflow.com/questions/109553/how-can-i-programmatically-manage-iptables-rules-on-the-fly
    # At least we know all the rules we need to inspect are in the NAT table.
    output = check_output([b"iptables-save", b"--table", b"nat"])

    # Find the beginning of the NAT table
    header = b"*nat\n"
    begin = output.find(header) + len(header)

    # Find the end of the NAT table
    footer = b"COMMIT\n"
    end = output.find(footer, begin)

    # Slice it out.
    nat = output[begin:end]

    for line in nat.splitlines():
        if line.startswith(b":"):
            # Skip these lines describing a chain or the table overall.
            continue

        options = parse_iptables_options(shlex.split(line))

        # TODO do a startswith("flocker ") instead to get rules for all namespaces
        if options.comment is not None and options.comment.startswith(FLOCKER_COMMENT_MARKER):
            yield options


def parse_iptables_options(argv):
    """
    Parse a single line of iptables-save(8) output from the NAT table section.

    :param argv: A :py:class:`list` of :py:class:`bytes` instances like an
        iptables argv (not including ``b"iptables"`` as ``argv[0]``).

    :return: A :py:class:`RuleOptions` instance holding the values taken from
        ``argv``.
    """
    # "Parsing" things like this:
    #
    # -A PREROUTING -p tcp -m tcp --dport 4567 -m addrtype --dst-type LOCAL
    #     -m comment --comment flocker -j DNAT --to-destination 10.1.2.3
    #
    # -A OUTPUT -p tcp -m tcp --dport 4567 -m addrtype --dst-type LOCAL -j DNAT
    #     --to-destination 10.1.2.3
    #
    # -A POSTROUTING -p tcp -m tcp --dport 4567 -j MASQUERADE
    #
    # To avoid having to know about every single possible current and future
    # iptables option, don't try to parse the whole line.  Just look for things
    # we expect and recognize.
    comment = None
    destination_port = None
    to_destination = None

    try:
        destination_port_index = argv.index(b"--dport")
        destination_port = int(argv[destination_port_index + 1])

        to_destination_index = argv.index(b"--to-destination")
        to_destination = IPAddress(argv[to_destination_index + 1])

        # Find the comment last so that the other two attributes always have a
        # value if the comment has a value.
        comment_index = argv.index(b"--comment")
        comment = argv[comment_index + 1]
    except (IndexError, ValueError):
        pass

    return RuleOptions(
        comment=comment,
        destination_port=destination_port,
        to_destination=to_destination)


@attributes(["namespace"])
@implementer(INetwork)
class HostNetwork(object):
    """
    An ``INetwork`` implementation based on ``iptables``.

    :TODO: document namespace
    """
    logger = Logger()

    def create_proxy_to(self, ip, port):
        """
        Configure iptables to proxy TCP traffic on the given port.

        :see: :meth:`INetwork.create_proxy_to` for parameter documentation.
        """
        return create_proxy_to(self.logger, ip, port, self.namespace)

    def delete_proxy(self, proxy):
        """
        Remove the iptables configuration which makes the given proxy work.

        :see: :meth:`INetwork.delete_proxy` for parameter documentation.
        """
        # (not-)TODO nothing needed here, namespace info is on the proxy object already
        return delete_proxy(self.logger, proxy)

    # TODO Turn into a real method that filters the proxies by
    # namespace and returns only those matching self.namespace
    #enumerate_proxies = staticmethod(enumerate_proxies)
    def enumerate_proxies(self):
        all_proxies = enumerate_proxies()
        proxies_in_this_namespace = list(proxy for proxy in all_proxies if self.namespace == proxy.namespace)
        return proxies_in_this_namespace


    def enumerate_used_ports(self):
        """
        Find all ports that are in use on this node by normal TCP servers or by
        proxies managed by this object.

        :see: :meth:`INetwork.enumerate_used_ports` for parameter
            documentation.
        """
        listening = set(
            conn.laddr[1]
            for conn
            in net_connections(kind='tcp')
        )
        proxied = set(
            proxy.port
            # TODO use the global enumerate_proxies instead so that we don't filter based on our namespace.  used ports are global so limiting to one namespace doesn't make sense.
            for proxy in self.enumerate_proxies()
        )
        # net_connections won't tell us about ports bound by sockets that
        # haven't entered the TCP state graph yet.
        return frozenset(listening | proxied)


def make_host_network(namespace="default"): # TODO add namespace parameter
    """
    Create a new ``INetwork`` provider which will interact with the underlying
    system's network configuration.

    :TODO document namespace
    :TODO change tests and don't have a default namespaceenumerate_proxies
    """
    return HostNetwork(namespace=namespace) # TODO pass namespace through
