# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Tests for ``flocker.node._deploy``.
"""

from uuid import uuid4

from zope.interface.verify import verifyObject
from zope.interface import implementer

from twisted.internet.defer import fail, FirstError, succeed, Deferred
from twisted.trial.unittest import SynchronousTestCase
from twisted.python.filepath import FilePath

from .. import (
    Deployer, Application, DockerImage, Deployment, Node, Port, Link,
    NodeState)
from .._deploy import (
    IStateChange, Sequentially, InParallel, StartApplication, StopApplication,
    CreateVolume, WaitForVolume, HandoffVolume, SetProxies, PushVolume,
    _link_environment, _to_volume_name)
from .._model import AttachedVolume
from .._docker import (
    FakeDockerClient, AlreadyExists, Unit, PortMap, Environment,
    DockerClient)
from ...route import Proxy, make_memory_network
from ...route._iptables import HostNetwork
from ...volume.service import Volume, VolumeName
from ...volume.testtools import create_volume_service
from ...volume._ipc import RemoteVolumeManager, standard_node


class DeployerAttributesTests(SynchronousTestCase):
    """
    Tests for attributes and initialiser arguments of `Deployer`.
    """
    def test_docker_client_default(self):
        """
        ``Deployer.docker_client`` is a ``DockerClient`` by default.
        """
        self.assertIsInstance(
            Deployer(None).docker_client,
            DockerClient
        )

    def test_docker_override(self):
        """
        ``Deployer.docker_client`` can be overridden in the constructor.
        """
        dummy_docker_client = object()
        self.assertIs(
            dummy_docker_client,
            Deployer(create_volume_service(self),
                     docker_client=dummy_docker_client).docker_client
        )

    def test_network_default(self):
        """
        ``Deployer._network`` is a ``HostNetwork`` by default.
        """
        self.assertIsInstance(Deployer(None).network, HostNetwork)

    def test_network_override(self):
        """
        ``Deployer._network`` can be overridden in the constructor.
        """
        dummy_network = object()
        self.assertIs(
            dummy_network,
            Deployer(create_volume_service(self),
                     network=dummy_network).network
        )


def make_istatechange_tests(klass, kwargs1, kwargs2):
    """
    Create tests to verify a class provides ``IStateChange``.

    :param klass: Class that implements ``IStateChange``.
    :param kwargs1: Keyword arguments to ``klass``.
    :param kwargs2: Keyword arguments to ``klass`` that create different
        change than ``kwargs1``.

    :return: ``SynchronousTestCase`` subclass named
        ``<klassname>IStateChangeTests``.
    """
    class Tests(SynchronousTestCase):
        def test_interface(self):
            """
            The class implements ``IStateChange``.
            """
            self.assertTrue(verifyObject(IStateChange, klass(**kwargs1)))

        def test_equality(self):
            """
            Instances with the same arguments are equal.
            """
            self.assertTrue(klass(**kwargs1) == klass(**kwargs1))
            self.assertFalse(klass(**kwargs1) == klass(**kwargs2))

        def test_notequality(self):
            """
            Instance with different arguments are not equal.
            """
            self.assertTrue(klass(**kwargs1) != klass(**kwargs2))
            self.assertFalse(klass(**kwargs1) != klass(**kwargs1))
    Tests.__name__ = klass.__name__ + "IStateChangeTests"
    return Tests


SequentiallyIStateChangeTests = make_istatechange_tests(
    Sequentially, dict(changes=[1]), dict(changes=[2]))
InParallelIStateChangeTests = make_istatechange_tests(
    InParallel, dict(changes=[1]), dict(changes=[2]))
StartApplicationIStateChangeTests = make_istatechange_tests(
    StartApplication,
    dict(application=1, hostname="node1.example.com"),
    dict(application=2, hostname="node2.example.com"))
StopApplicationIStageChangeTests = make_istatechange_tests(
    StopApplication, dict(application=1), dict(application=2))
SetProxiesIStateChangeTests = make_istatechange_tests(
    SetProxies, dict(ports=[1]), dict(ports=[2]))
WaitForVolumeIStateChangeTests = make_istatechange_tests(
    WaitForVolume, dict(volume=1), dict(volume=2))
CreateVolumeIStateChangeTests = make_istatechange_tests(
    CreateVolume, dict(volume=1), dict(volume=2))
HandoffVolumeIStateChangeTests = make_istatechange_tests(
    HandoffVolume, dict(volume=1, hostname=b"123"),
    dict(volume=2, hostname=b"123"))
PushVolumeIStateChangeTests = make_istatechange_tests(
    PushVolume, dict(volume=1, hostname=b"123"),
    dict(volume=2, hostname=b"123"))


NOT_CALLED = object()


@implementer(IStateChange)
class FakeChange(object):
    """
    A change that returns the given result and records the deployer.

    :ivar deployer: The deployer passed to ``run()``, or ``NOT_CALLED``
        before that.
    """
    def __init__(self, result):
        """
        :param Deferred result: The result to return from ``run()``.
        """
        self.result = result
        self.deployer = NOT_CALLED

    def run(self, deployer):
        self.deployer = deployer
        return self.result

    def was_run_called(self):
        """
        Return whether or not run() has been called yet.

        :return: ``True`` if ``run()`` was called, otherwise ``False``.
        """
        return self.deployer != NOT_CALLED

    def __eq__(self, other):
        return False

    def __ne__(self, other):
        return True


class SequentiallyTests(SynchronousTestCase):
    """
    Tests for ``Sequentially``.
    """
    def test_subchanges_get_deployer(self):
        """
        ``Sequentially.run`` runs sub-changes with the given deployer.
        """
        subchanges = [FakeChange(succeed(None)), FakeChange(succeed(None))]
        change = Sequentially(changes=subchanges)
        deployer = object()
        change.run(deployer)
        self.assertEqual([c.deployer for c in subchanges],
                         [deployer, deployer])

    def test_result(self):
        """
        The result of ``Sequentially.run`` fires when all changes are done.
        """
        not_done1, not_done2 = Deferred(), Deferred()
        subchanges = [FakeChange(not_done1), FakeChange(not_done2)]
        change = Sequentially(changes=subchanges)
        deployer = object()
        result = change.run(deployer)
        self.assertNoResult(result)
        not_done1.callback(None)
        self.assertNoResult(result)
        not_done2.callback(None)
        self.successResultOf(result)

    def test_in_order(self):
        """
        ``Sequentially.run`` runs sub-changes in order.
        """
        # We have two changes; the first one will not finish until we fire
        # not_done, the second one will finish as soon as its run() is
        # called.
        not_done = Deferred()
        subchanges = [FakeChange(not_done), FakeChange(succeed(None))]
        change = Sequentially(changes=subchanges)
        deployer = object()
        # Run the sequential change. We expect the first FakeChange's
        # run() to be called, but we expect second one *not* to be called
        # yet, since first one has finished.
        change.run(deployer)
        called = [subchanges[0].was_run_called(),
                  subchanges[1].was_run_called()]
        not_done.callback(None)
        called.append(subchanges[1].was_run_called())
        self.assertEqual(called, [True, False, True])

    def test_failure_stops_later_change(self):
        """
        ``Sequentially.run`` fails with the first failed change, rather than
        continuing to run later changes.
        """
        not_done = Deferred()
        subchanges = [FakeChange(not_done), FakeChange(succeed(None))]
        change = Sequentially(changes=subchanges)
        deployer = object()
        result = change.run(deployer)
        called = [subchanges[1].was_run_called()]
        exception = RuntimeError()
        not_done.errback(exception)
        called.extend([subchanges[1].was_run_called(),
                       self.failureResultOf(result).value])
        self.assertEqual(called, [False, False, exception])


class InParallelTests(SynchronousTestCase):
    """
    Tests for ``InParallel``.
    """
    def test_subchanges_get_deployer(self):
        """
        ``InParallel.run`` runs sub-changes with the given deployer.
        """
        subchanges = [FakeChange(succeed(None)), FakeChange(succeed(None))]
        change = InParallel(changes=subchanges)
        deployer = object()
        change.run(deployer)
        self.assertEqual([c.deployer for c in subchanges],
                         [deployer, deployer])

    def test_result(self):
        """
        The result of ``InParallel.run`` fires when all changes are done.
        """
        not_done1, not_done2 = Deferred(), Deferred()
        subchanges = [FakeChange(not_done1), FakeChange(not_done2)]
        change = InParallel(changes=subchanges)
        deployer = object()
        result = change.run(deployer)
        self.assertNoResult(result)
        not_done1.callback(None)
        self.assertNoResult(result)
        not_done2.callback(None)
        self.successResultOf(result)

    def test_in_parallel(self):
        """
        ``InParallel.run`` runs sub-changes in parallel.
        """
        # The first change will not finish immediately when run(), but we
        # expect the second one to be run() nonetheless.
        subchanges = [FakeChange(Deferred()), FakeChange(succeed(None))]
        change = InParallel(changes=subchanges)
        deployer = object()
        change.run(deployer)
        called = [subchanges[0].was_run_called(),
                  subchanges[1].was_run_called()]
        self.assertEqual(called, [True, True])

    def test_failure_result(self):
        """
        ``InParallel.run`` returns the first failure.
        """
        subchanges = [FakeChange(fail(RuntimeError()))]
        change = InParallel(changes=subchanges)
        result = change.run(object())
        failure = self.failureResultOf(result, FirstError)
        self.assertEqual(failure.value.subFailure.type, RuntimeError)
        self.flushLoggedErrors(RuntimeError)

    def test_failure_all_logged(self):
        """
        Errors in the async operations performed by ``InParallel.run`` are all
        logged.
        """
        subchanges = [
            FakeChange(fail(ZeroDivisionError('e1'))),
            FakeChange(fail(ZeroDivisionError('e2'))),
            FakeChange(fail(ZeroDivisionError('e3'))),
        ]
        change = InParallel(changes=subchanges)
        result = change.run(deployer=object())
        self.failureResultOf(result, FirstError)

        self.assertEqual(
            len(subchanges),
            len(self.flushLoggedErrors(ZeroDivisionError))
        )


class StartApplicationTests(SynchronousTestCase):
    """
    Tests for ``StartApplication``.
    """
    def test_start(self):
        """
        ``StartApplication`` accepts an application object and when ``run()``
        is called returns a ``Deferred`` which fires when the docker container
        has been added and started.
        """
        fake_docker = FakeDockerClient()
        api = Deployer(create_volume_service(self), docker_client=fake_docker)
        docker_image = DockerImage(repository=u'clusterhq/flocker',
                                   tag=u'release-14.0')
        ports = frozenset([Port(internal_port=80, external_port=8080)])
        application = Application(
            name=u'site-example.com',
            image=docker_image,
            ports=ports,
            links=frozenset(),
        )
        start_result = StartApplication(application=application,
                                        hostname="node1.example.com").run(api)
        exists_result = fake_docker.exists(unit_name=application.name)

        port_maps = [PortMap(internal_port=80, external_port=8080)]
        self.assertEqual(
            (None, True, docker_image.full_name, port_maps),
            (self.successResultOf(start_result),
             self.successResultOf(exists_result),
             fake_docker._units[application.name].container_image,
             fake_docker._units[application.name].ports)
        )

    def test_already_exists(self):
        """
        ``StartApplication.run`` returns a `Deferred` which errbacks with
        an ``AlreadyExists`` error if there is already a unit with the supplied
        application name.
        """
        api = Deployer(create_volume_service(self),
                       docker_client=FakeDockerClient())
        application = Application(
            name=b'site-example.com',
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0'),
            links=frozenset(),
        )

        result1 = StartApplication(application=application,
                                   hostname="node1.example.com").run(api)
        self.successResultOf(result1)

        result2 = StartApplication(application=application,
                                   hostname="node1.example.com").run(api)
        self.failureResultOf(result2, AlreadyExists)

    def test_volume_exposed_on_start(self):
        """
        ``StartApplication.run()`` exposes an application's volume before
        it is started.
        """
        volume_service = create_volume_service(self)
        fake_docker = FakeDockerClient()
        deployer = Deployer(volume_service, fake_docker)
        docker_image = DockerImage.from_string(u"busybox")
        application = Application(
            name=u'site-example.com',
            image=docker_image,
            volume=AttachedVolume(name=u'site-example.com',
                                  mountpoint=FilePath(b"/var")),
            links=frozenset(),
        )

        # This would be better to test with a verified fake:
        # https://github.com/ClusterHQ/flocker/issues/234
        exposed = []

        def expose_to_docker(volume, mount_path):
            # We check for existence of unit so we can ensure exposure
            # happens *before* the unit is started:
            exposed.append((volume, mount_path, self.successResultOf(
                fake_docker.exists(u"site-example.com"))))
            return succeed(None)
        self.patch(Volume, "expose_to_docker", expose_to_docker)

        StartApplication(application=application,
                         hostname="node1.example.com").run(deployer)
        self.assertEqual(
            exposed,
            [(volume_service.get(_to_volume_name(u"site-example.com")),
              FilePath(b"/var"), False)])

    def test_environment_supplied_to_docker(self):
        """
        ``StartApplication.run()`` passes the environment dictionary of the
        application to ``DockerClient.add`` as an ``Environment`` instance.
        """
        volume_service = create_volume_service(self)
        fake_docker = FakeDockerClient()
        deployer = Deployer(volume_service, fake_docker)

        application_name = u'site-example.com'
        variables = frozenset({u'foo': u"bar", u"baz": u"qux"}.iteritems())
        application = Application(
            name=application_name,
            image=DockerImage(repository=u'clusterhq/postgresql',
                              tag=u'9.3.5'),
            environment=variables.copy(),
            links=frozenset(),
        )

        StartApplication(application=application,
                         hostname="node1.example.com").run(deployer)

        expected_environment = Environment(variables=variables.copy())

        self.assertEqual(
            expected_environment,
            fake_docker._units[application_name].environment
        )

    def test_environment_not_supplied(self):
        """
        ``StartApplication.run()`` only passes an ``Environment`` instance
        if the application defines an environment.
        """
        volume_service = create_volume_service(self)
        fake_docker = FakeDockerClient()
        deployer = Deployer(volume_service, fake_docker)

        application_name = u'site-example.com'
        application = Application(
            name=application_name,
            image=DockerImage(repository=u'clusterhq/postgresql',
                              tag=u'9.3.5'),
            environment=None,
            links=frozenset(),
        )

        StartApplication(application=application,
                         hostname="node1.example.com").run(deployer)

        self.assertEqual(
            None,
            fake_docker._units[application_name].environment
        )

    def test_links(self):
        """
        ``StartApplication.run()`` passes environment variables to connect to
        the remote application to ``DockerClient.add``.
        """
        volume_service = create_volume_service(self)
        fake_docker = FakeDockerClient()
        deployer = Deployer(volume_service, fake_docker)

        application_name = u'site-example.com'
        application = Application(
            name=application_name,
            image=DockerImage(repository=u'clusterhq/postgresql',
                              tag=u'9.3.5'),
            links=frozenset([Link(alias="alias", local_port=80,
                                  remote_port=8080)]))

        StartApplication(application=application,
                         hostname="node1.example.com").run(deployer)

        variables = frozenset({
            'ALIAS_PORT_80_TCP': 'tcp://node1.example.com:8080',
            'ALIAS_PORT_80_TCP_ADDR': 'node1.example.com',
            'ALIAS_PORT_80_TCP_PORT': '8080',
            'ALIAS_PORT_80_TCP_PROTO': 'tcp',
        }.iteritems())
        expected_environment = Environment(variables=variables.copy())

        self.assertEqual(
            expected_environment,
            fake_docker._units[application_name].environment
        )


class LinkEnviromentTests(SynchronousTestCase):
    """
    Tests for ``_link_environment``.
    """

    def test_link_environment(self):
        """
        ``_link_environment(link)`` returns a dictonary
        with keys used by docker to represent links. Specifically
        ``<alias>_PORT_<local_port>_<protocol>`` and the broken out variants
        ``_ADDR``, ``_PORT`` and ``_PROTO``.
        """

        environment = _link_environment(
            protocol="udp",
            alias="dash-alias",
            local_port=80,
            hostname=u"the-host",
            remote_port=8080)
        self.assertEqual(
            environment,
            {
                u'DASH_ALIAS_PORT_80_UDP': u'udp://the-host:8080',
                u'DASH_ALIAS_PORT_80_UDP_PROTO': u'udp',
                u'DASH_ALIAS_PORT_80_UDP_ADDR': u'the-host',
                u'DASH_ALIAS_PORT_80_UDP_PORT': u'8080',
            })


class StopApplicationTests(SynchronousTestCase):
    """
    Tests for ``StopApplication``.
    """
    def test_stop(self):
        """
        ``StopApplication`` accepts an application object and when ``run()``
        is called returns a ``Deferred`` which fires when the container
        has been removed.
        """
        fake_docker = FakeDockerClient()
        api = Deployer(create_volume_service(self), docker_client=fake_docker)
        application = Application(
            name=b'site-example.com',
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0'),
            links=frozenset(),
        )

        StartApplication(application=application,
                         hostname="node1.example.com").run(api)
        existed = fake_docker.exists(application.name)
        stop_result = StopApplication(application=application).run(api)
        exists_result = fake_docker.exists(unit_name=application.name)

        self.assertEqual(
            (None, True, False),
            (self.successResultOf(stop_result),
             self.successResultOf(existed),
             self.successResultOf(exists_result))
        )

    def test_does_not_exist(self):
        """
        ``StopApplication.run()`` does not errback if the application does
        not exist.
        """
        api = Deployer(create_volume_service(self),
                       docker_client=FakeDockerClient())
        application = Application(
            name=b'site-example.com',
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0'),
            links=frozenset(),
        )
        result = StopApplication(application=application).run(api)
        result = self.successResultOf(result)

        self.assertIs(None, result)

    def test_volume_unexposed(self):
        """
        ``StopApplication.run()`` removes an application's volume from
        Docker after it is stopped.
        """
        volume_service = create_volume_service(self)
        fake_docker = FakeDockerClient()
        deployer = Deployer(volume_service, fake_docker)
        docker_image = DockerImage.from_string(u"busybox")
        application = Application(
            name=u'site-example.com',
            image=docker_image,
            volume=AttachedVolume(name=u'site-example.com',
                                  mountpoint=FilePath(b"/var")),
            links=frozenset(),
        )

        # This would be better to test with a verified fake:
        # https://github.com/ClusterHQ/flocker/issues/234
        self.patch(Volume, "expose_to_docker", lambda *args: succeed(None))
        removed = []

        def remove_from_docker(volume):
            # We check for existence of unit so we can ensure exposure
            # happens *after* the unit is stopped:
            removed.append((volume, self.successResultOf(
                fake_docker.exists(u"site-example.com"))))
            return succeed(None)
        self.patch(Volume, "remove_from_docker", remove_from_docker)

        self.successResultOf(StartApplication(application=application,
                                              hostname="node1.example.com",
                                              ).run(deployer))
        self.successResultOf(StopApplication(application=application).run(
            deployer))
        self.assertEqual(
            removed,
            [(volume_service.get(_to_volume_name(u"site-example.com")),
              False)])


# This models an application that has a volume.
APPLICATION_WITH_VOLUME_NAME = b"psql-clusterhq"
APPLICATION_WITH_VOLUME_MOUNTPOINT = b"/var/lib/postgresql"
APPLICATION_WITH_VOLUME = Application(
    name=APPLICATION_WITH_VOLUME_NAME,
    image=DockerImage(repository=u'clusterhq/postgresql',
                      tag=u'9.1'),
    volume=AttachedVolume(
        # XXX For now we require volume names match application names,
        # see https://github.com/ClusterHQ/flocker/issues/49
        name=APPLICATION_WITH_VOLUME_NAME,
        mountpoint=APPLICATION_WITH_VOLUME_MOUNTPOINT,
    ),
    links=frozenset(),
)

# XXX Until https://github.com/ClusterHQ/flocker/issues/289 is fixed the
# current state passed to calculate_necessary_state_changes won't know
# mountpoint. Until https://github.com/ClusterHQ/flocker/issues/207 is
# fixed the image will be unknown.
DISCOVERED_APPLICATION_WITH_VOLUME = Application(
    name=APPLICATION_WITH_VOLUME_NAME,
    image=DockerImage.from_string('unknown'),
    volume=AttachedVolume(
        # XXX For now we require volume names match application names,
        # see https://github.com/ClusterHQ/flocker/issues/49
        name=APPLICATION_WITH_VOLUME_NAME,
        mountpoint=None,
    ),
    links=frozenset(),
)


class DeployerDiscoverNodeConfigurationTests(SynchronousTestCase):
    """
    Tests for ``Deployer.discover_node_configuration``.
    """
    def setUp(self):
        self.volume_service = create_volume_service(self)
        self.network = make_memory_network()

    def test_discover_none(self):
        """
        ``Deployer.discover_node_configuration`` returns an empty
        ``NodeState`` if there are no Docker containers on the host.
        """
        fake_docker = FakeDockerClient(units={})
        api = Deployer(
            self.volume_service,
            docker_client=fake_docker,
            network=self.network
        )
        d = api.discover_node_configuration()

        self.assertEqual(NodeState(running=[], not_running=[]),
                         self.successResultOf(d))

    def test_discover_one(self):
        """
        ``Deployer.discover_node_configuration`` returns ``NodeState`` with a
        a list of running ``Application``\ s; one for each active container.
        """
        expected_application_name = u'site-example.com'
        unit = Unit(name=expected_application_name,
                    container_name=expected_application_name,
                    activation_state=u'active')
        fake_docker = FakeDockerClient(units={expected_application_name: unit})
        application = Application(name=unit.name)
        api = Deployer(
            self.volume_service,
            docker_client=fake_docker,
            network=self.network
        )
        d = api.discover_node_configuration()

        self.assertEqual(NodeState(running=[application], not_running=[]),
                         self.successResultOf(d))

    def test_discover_multiple(self):
        """
        ``Deployer.discover_node_configuration`` returns a ``NodeState`` with
        a running ``Application`` for every active container on the host.
        """
        unit1 = Unit(name=u'site-example.com',
                     container_name=u'site-example.com',
                     activation_state=u'active')
        unit2 = Unit(name=u'site-example.net',
                     container_name=u'site-example.net',
                     activation_state=u'active')
        units = {unit1.name: unit1, unit2.name: unit2}

        fake_docker = FakeDockerClient(units=units)
        applications = [Application(name=unit.name) for unit in units.values()]
        api = Deployer(
            self.volume_service,
            docker_client=fake_docker,
            network=self.network
        )
        d = api.discover_node_configuration()

        self.assertEqual(sorted(applications),
                         sorted(self.successResultOf(d).running))

    def test_discover_locally_owned_volume(self):
        """
        Locally owned volumes are added to ``Application`` with same name as
        an ``AttachedVolume``.
        """
        unit1 = Unit(name=u'site-example.com',
                     container_name=u'site-example.com',
                     activation_state=u'active')
        unit2 = Unit(name=u'site-example.net',
                     container_name=u'site-example.net',
                     activation_state=u'active')
        units = {unit1.name: unit1, unit2.name: unit2}

        self.successResultOf(self.volume_service.create(
            _to_volume_name(u"site-example.com")))
        self.successResultOf(self.volume_service.create(
            _to_volume_name(u"site-example.net")))

        # Eventually when https://github.com/ClusterHQ/flocker/issues/289
        # is fixed the mountpoint should actually be specified.
        fake_docker = FakeDockerClient(units=units)
        applications = [Application(name=unit.name,
                                    volume=AttachedVolume(name=unit.name,
                                                          mountpoint=None))
                        for unit in units.values()]
        api = Deployer(
            self.volume_service,
            docker_client=fake_docker,
            network=self.network
        )
        d = api.discover_node_configuration()

        self.assertEqual(sorted(applications),
                         sorted(self.successResultOf(d).running))

    def test_discover_remotely_owned_volumes_ignored(self):
        """
        Remotely owned volumes are not added to the discovered ``Application``
        instances even if they have the same name.
        """
        unit = Unit(name=u'site-example.com',
                    container_name=u'site-example.com',
                    activation_state=u'active')
        units = {unit.name: unit}

        volume = Volume(uuid=unicode(uuid4()),
                        name=_to_volume_name(u"site-example.com"),
                        service=self.volume_service)
        self.successResultOf(volume.service.pool.create(volume))

        fake_docker = FakeDockerClient(units=units)
        applications = [Application(name=unit.name)]
        api = Deployer(
            self.volume_service,
            docker_client=fake_docker,
            network=self.network
        )
        d = api.discover_node_configuration()
        self.assertEqual(sorted(applications),
                         sorted(self.successResultOf(d).running))

    def test_not_running_units(self):
        """
        Units that are not active are considered to be not running by
        ``discover_node_configuration()``.
        """
        unit1 = Unit(name=u'site-example3.net',
                     container_name=u'site-example3.net',
                     activation_state=u'inactive')
        unit2 = Unit(name=u'site-example4.net',
                     container_name=u'site-example4.net',
                     activation_state=u'madeup')
        units = {unit1.name: unit1, unit2.name: unit2}

        fake_docker = FakeDockerClient(units=units)
        applications = [Application(name=unit.name) for unit in units.values()]
        applications.sort()
        api = Deployer(
            self.volume_service,
            docker_client=fake_docker,
            network=self.network
        )
        d = api.discover_node_configuration()
        result = self.successResultOf(d)
        result.not_running.sort()

        self.assertEqual(NodeState(running=[], not_running=applications),
                         result)

    def test_discover_used_ports(self):
        """
        Any ports in use, as reported by the deployer's ``INetwork`` provider,
        are reported in the ``used_ports`` attribute of the ``NodeState``
        returned by ``discover_node_configuration``.
        """
        used_ports = frozenset([1, 3, 5, 1000])
        api = Deployer(
            create_volume_service(self),
            docker_client=FakeDockerClient(),
            network=make_memory_network(used_ports=used_ports)
        )

        discovering = api.discover_node_configuration()
        state = self.successResultOf(discovering)

        self.assertEqual(
            NodeState(running=[], not_running=[], used_ports=used_ports),
            state
        )


# A deployment with no information:
EMPTY = Deployment(nodes=frozenset())


class DeployerCalculateNecessaryStateChangesTests(SynchronousTestCase):
    """
    Tests for ``Deployer.calculate_necessary_state_changes``.
    """
    def test_no_state_changes(self):
        """
        ``Deployer.calculate_necessary_state_changes`` returns a ``Deferred``
        which fires with a :class:`IStateChange` instance indicating that no
        changes are necessary when there are no applications running or
        desired, and no proxies exist or are desired.
        """
        fake_docker = FakeDockerClient(units={})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        desired = Deployment(nodes=frozenset())
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'node.example.com')
        expected = Sequentially(changes=[])
        self.assertEqual(expected, self.successResultOf(d))

    def test_proxy_needs_creating(self):
        """
        ``Deployer.calculate_necessary_state_changes`` returns a
        ``IStateChange``, specifically a ``SetProxies`` with a list of
        ``Proxy`` objects. One for each port exposed by ``Application``\ s
        hosted on a remote nodes.
        """
        fake_docker = FakeDockerClient(units={})
        network = make_memory_network()
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=network)
        expected_destination_port = 1001
        expected_destination_host = u'node1.example.com'
        port = Port(internal_port=3306,
                    external_port=expected_destination_port)
        application = Application(
            name=b'mysql-hybridcluster',
            image=DockerImage(repository=u'clusterhq/mysql',
                              tag=u'release-14.0'),
            ports=frozenset([port]),
        )

        nodes = frozenset([
            Node(
                hostname=expected_destination_host,
                applications=frozenset([application])
            )
        ])

        desired = Deployment(nodes=nodes)
        d = api.calculate_necessary_state_changes(
            desired_state=desired, current_cluster_state=EMPTY,
            hostname=u'node2.example.com')
        proxy = Proxy(ip=expected_destination_host,
                      port=expected_destination_port,
                      namespace=network.namespace)
        expected = Sequentially(changes=[SetProxies(ports=frozenset([proxy]))])
        self.assertEqual(expected, self.successResultOf(d))

    def test_proxy_empty(self):
        """
        ``Deployer.calculate_necessary_state_changes`` returns a
        ``SetProxies`` instance containing an empty `proxies`
        list if there are no remote applications that need proxies.
        """
        network = make_memory_network()
        network.create_proxy_to(ip=u'192.0.2.100', port=3306)

        api = Deployer(create_volume_service(self),
                       docker_client=FakeDockerClient(),
                       network=network)
        desired = Deployment(nodes=frozenset())
        d = api.calculate_necessary_state_changes(
            desired_state=desired, current_cluster_state=EMPTY,
            hostname=u'node2.example.com')
        expected = Sequentially(changes=[SetProxies(ports=frozenset())])
        self.assertEqual(expected, self.successResultOf(d))

    def test_application_needs_stopping(self):
        """
        ``Deployer.calculate_necessary_state_changes`` specifies that an
        application must be stopped when it is running but not desired.
        """
        unit = Unit(name=u'site-example.com',
                    container_name=u'site-example.com',
                    activation_state=u'active')

        fake_docker = FakeDockerClient(units={unit.name: unit})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        desired = Deployment(nodes=frozenset())
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'node.example.com')
        to_stop = StopApplication(application=Application(name=unit.name))
        expected = Sequentially(changes=[InParallel(changes=[to_stop])])
        self.assertEqual(expected, self.successResultOf(d))

    def test_application_needs_starting(self):
        """
        ``Deployer.calculate_necessary_state_changes`` specifies that an
        application must be started when it is desired on the given node but
        not running.
        """
        fake_docker = FakeDockerClient(units={})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        application = Application(
            name=b'mysql-hybridcluster',
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0')
        )

        nodes = frozenset([
            Node(
                hostname=u'node.example.com',
                applications=frozenset([application])
            )
        ])

        desired = Deployment(nodes=nodes)
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'node.example.com')
        expected = Sequentially(changes=[InParallel(
            changes=[StartApplication(application=application,
                                      hostname="node.example.com")])])
        self.assertEqual(expected, self.successResultOf(d))

    def test_only_this_node(self):
        """
        ``Deployer.calculate_necessary_state_changes`` does not specify that an
        application must be started if the desired changes apply to a different
        node.
        """
        fake_docker = FakeDockerClient(units={})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        application = Application(
            name=b'mysql-hybridcluster',
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0')
        )

        nodes = frozenset([
            Node(
                hostname=u'node1.example.net',
                applications=frozenset([application])
            )
        ])

        desired = Deployment(nodes=nodes)
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'node.example.com')
        expected = Sequentially(changes=[])
        self.assertEqual(expected, self.successResultOf(d))

    def test_no_change_needed(self):
        """
        ``Deployer.calculate_necessary_state_changes`` does not specify that an
        application must be started or stopped if the desired configuration
        is the same as the current configuration.
        """
        unit = Unit(name=u'mysql-hybridcluster',
                    container_name=u'mysql-hybridcluster',
                    activation_state=u'active')

        fake_docker = FakeDockerClient(units={unit.name: unit})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())

        application = Application(
            name=u'mysql-hybridcluster',
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0'),
            ports=frozenset(),
        )

        nodes = frozenset([
            Node(
                hostname=u'node.example.com',
                applications=frozenset([application])
            )
        ])

        desired = Deployment(nodes=nodes)
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'node.example.com')
        expected = Sequentially(changes=[])
        self.assertEqual(expected, self.successResultOf(d))

    def test_node_not_described(self):
        """
        ``Deployer.calculate_necessary_state_changes`` specifies that all
        applications on a node must be stopped if the desired configuration
        does not include that node.
        """
        unit = Unit(name=u'mysql-hybridcluster',
                    container_name='mysql-hybridcluster',
                    activation_state=u'active')

        fake_docker = FakeDockerClient(units={unit.name: unit})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        desired = Deployment(nodes=frozenset())
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'node.example.com')
        to_stop = StopApplication(application=Application(name=unit.name))
        expected = Sequentially(changes=[InParallel(changes=[to_stop])])
        self.assertEqual(expected, self.successResultOf(d))

    def test_volume_created(self):
        """
        ``Deployer.calculate_necessary_state_changes`` specifies that a new
        volume must be created if the desired configuration specifies that an
        application which was previously running nowhere is going to be running
        on *this* node and that application requires a volume.
        """
        hostname = u"node1.example.com"

        # The application is not running here - therefore there is no container
        # for it.
        docker = FakeDockerClient(units={})

        # The discovered current configuration of the cluster also reflects
        # this.
        current = Deployment(nodes=frozenset({
            Node(hostname=hostname, applications=frozenset()),
        }))

        api = Deployer(
            create_volume_service(self), docker_client=docker,
            network=make_memory_network()
        )

        node = Node(
            hostname=hostname,
            applications=frozenset({APPLICATION_WITH_VOLUME}),
        )

        # This completely expresses the configuration for a cluster of one node
        # with one application which requires a volume.  It's the state we
        # should get to with the changes calculated below.
        desired = Deployment(nodes=frozenset({node}))

        calculating = api.calculate_necessary_state_changes(
            desired_state=desired,
            current_cluster_state=current,
            hostname=hostname,
        )

        changes = self.successResultOf(calculating)

        volume = AttachedVolume(
            name=APPLICATION_WITH_VOLUME_NAME,
            mountpoint=APPLICATION_WITH_VOLUME_MOUNTPOINT
        )
        expected = Sequentially(changes=[
            InParallel(changes=[CreateVolume(volume=volume)]),
            InParallel(changes=[StartApplication(
                application=APPLICATION_WITH_VOLUME,
                hostname="node1.example.com")])])
        self.assertEqual(expected, changes)

    def test_volume_wait(self):
        """
        ``Deployer.calculate_necessary_state_changes`` specifies that the
        volume for an application which was previously running on another node
        must be waited for, in anticipation of that node handing it off to us.
        """
        # The application is not running here - therefore there is no container
        # for it.
        docker = FakeDockerClient(units={})

        node = Node(
            hostname=u"node1.example.com",
            applications=frozenset(),
        )
        another_node = Node(
            hostname=u"node2.example.com",
            applications=frozenset({DISCOVERED_APPLICATION_WITH_VOLUME}),
        )

        # The discovered current configuration of the cluster reveals the
        # application is running somewhere else.
        current = Deployment(nodes=frozenset([node, another_node]))

        api = Deployer(
            create_volume_service(self), docker_client=docker,
            network=make_memory_network()
        )

        desired = Deployment(nodes=frozenset({
            Node(hostname=node.hostname,
                 applications=frozenset({APPLICATION_WITH_VOLUME})),
            Node(hostname=another_node.hostname,
                 applications=frozenset()),
        }))

        calculating = api.calculate_necessary_state_changes(
            desired_state=desired,
            current_cluster_state=current,
            hostname=node.hostname,
        )

        changes = self.successResultOf(calculating)
        volume = AttachedVolume(
            name=APPLICATION_WITH_VOLUME_NAME,
            mountpoint=APPLICATION_WITH_VOLUME_MOUNTPOINT,
        )
        expected = Sequentially(changes=[
            InParallel(changes=[WaitForVolume(volume=volume)]),
            InParallel(changes=[StartApplication(
                application=APPLICATION_WITH_VOLUME,
                hostname="node1.example.com")])])
        self.assertEqual(expected, changes)

    def test_volume_handoff(self):
        """
        ``Deployer.calculate_necessary_state_changes`` specifies that the
        volume for an application which was previously running on this node but
        is now running on another node must be handed off.
        """
        # The application is running here.
        unit = Unit(
            name=APPLICATION_WITH_VOLUME_NAME,
            container_name=APPLICATION_WITH_VOLUME_NAME,
            activation_state=u'active'
        )
        docker = FakeDockerClient(units={unit.name: unit})

        node = Node(
            hostname=u"node1.example.com",
            applications=frozenset({DISCOVERED_APPLICATION_WITH_VOLUME}),
        )
        another_node = Node(
            hostname=u"node2.example.com",
            applications=frozenset(),
        )

        # The discovered current configuration of the cluster reveals the
        # application is running here.
        current = Deployment(nodes=frozenset([node, another_node]))

        api = Deployer(
            create_volume_service(self), docker_client=docker,
            network=make_memory_network()
        )

        desired = Deployment(nodes=frozenset({
            Node(hostname=node.hostname,
                 applications=frozenset()),
            Node(hostname=another_node.hostname,
                 applications=frozenset({APPLICATION_WITH_VOLUME})),
        }))

        calculating = api.calculate_necessary_state_changes(
            desired_state=desired,
            current_cluster_state=current,
            hostname=node.hostname,
        )

        changes = self.successResultOf(calculating)

        volume = AttachedVolume(
            name=APPLICATION_WITH_VOLUME_NAME,
            mountpoint=APPLICATION_WITH_VOLUME_MOUNTPOINT,
        )

        expected = Sequentially(changes=[
            InParallel(changes=[PushVolume(
                volume=volume, hostname=another_node.hostname)]),
            InParallel(changes=[StopApplication(
                application=Application(name=APPLICATION_WITH_VOLUME_NAME),)]),
            InParallel(changes=[HandoffVolume(
                volume=volume, hostname=another_node.hostname)]),
        ])
        self.assertEqual(expected, changes)

    def test_no_volume_changes(self):
        """
        ``Deployer.calculate_necessary_state_changes`` specifies no work for
        the volume if an application which was previously running on this
        node continues to run on this node.
        """
        # The application is running here.
        unit = Unit(
            name=APPLICATION_WITH_VOLUME_NAME,
            container_name=APPLICATION_WITH_VOLUME_NAME,
            activation_state=u'active'
        )
        docker = FakeDockerClient(units={unit.name: unit})

        current_node = Node(
            hostname=u"node1.example.com",
            applications=frozenset({DISCOVERED_APPLICATION_WITH_VOLUME}),
        )
        desired_node = Node(
            hostname=u"node1.example.com",
            applications=frozenset({APPLICATION_WITH_VOLUME}),
        )
        another_node = Node(
            hostname=u"node2.example.com",
            applications=frozenset(),
        )

        # The discovered current configuration of the cluster reveals the
        # application is running here.
        current = Deployment(nodes=frozenset([current_node, another_node]))
        desired = Deployment(nodes=frozenset([desired_node, another_node]))

        api = Deployer(
            create_volume_service(self), docker_client=docker,
            network=make_memory_network()
        )

        calculating = api.calculate_necessary_state_changes(
            desired_state=desired,
            current_cluster_state=current,
            hostname=current_node.hostname,
        )

        changes = self.successResultOf(calculating)

        expected = Sequentially(changes=[])
        self.assertEqual(expected, changes)

    def test_local_not_running_applications_restarted(self):
        """
        Applications that are not running but are supposed to be on the local
        node are added to the list of applications to restart.
        """
        unit = Unit(name=u'mysql-hybridcluster',
                    container_name=u'mysql-hybridcluster',
                    activation_state=u'inactive')

        fake_docker = FakeDockerClient(units={unit.name: unit})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        application = Application(
            name=b'mysql-hybridcluster',
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0')
        )
        nodes = frozenset([
            Node(
                hostname=u'n.example.com',
                applications=frozenset([application])
            )
        ])
        desired = Deployment(nodes=nodes)
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'n.example.com')

        expected = Sequentially(changes=[InParallel(changes=[
            Sequentially(changes=[StopApplication(application=application),
                                  StartApplication(application=application,
                                                   hostname="n.example.com")]),
        ])])
        self.assertEqual(expected, self.successResultOf(d))

    def test_not_local_not_running_applications_stopped(self):
        """
        Applications that are not running and are supposed to be on the local
        node are added to the list of applications to stop.
        """
        unit = Unit(name=u'mysql-hybridcluster',
                    container_name=u'mysql-hybridcluster',
                    activation_state=u'inactive')

        fake_docker = FakeDockerClient(units={unit.name: unit})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())

        desired = Deployment(nodes=frozenset())
        d = api.calculate_necessary_state_changes(desired_state=desired,
                                                  current_cluster_state=EMPTY,
                                                  hostname=u'node.example.com')
        to_stop = Application(name=unit.name)
        expected = Sequentially(changes=[InParallel(changes=[
            StopApplication(application=to_stop)])])
        self.assertEqual(expected, self.successResultOf(d))

    def test_handoff_precedes_wait(self):
        """
        Volume handoffs happen before volume waits, to prevent deadlocks
        between two nodes that are swapping volumes.
        """
        # The application is running here.
        unit = Unit(
            name=APPLICATION_WITH_VOLUME_NAME,
            container_name=APPLICATION_WITH_VOLUME_NAME,
            activation_state=u'active'
        )
        docker = FakeDockerClient(units={unit.name: unit})

        another_application = Application(
            name=u"another",
            image=DockerImage(repository=u'clusterhq/postgresql',
                              tag=u'9.1'),
            volume=AttachedVolume(
                # XXX For now we require volume names match application names,
                # see https://github.com/ClusterHQ/flocker/issues/49
                name=u"another",
                mountpoint=FilePath(b"/blah"),
            ),
            links=frozenset(),
        )
        # XXX don't know image or volume because of
        # https://github.com/ClusterHQ/flocker/issues/289
        # https://github.com/ClusterHQ/flocker/issues/207
        discovered_another_application = Application(
            name=u"another",
            image=DockerImage.from_string(u'unknown'),
            volume=AttachedVolume(
                # XXX For now we require volume names match application names,
                # see https://github.com/ClusterHQ/flocker/issues/49
                name=u"another",
                mountpoint=None,
            )
        )

        node = Node(
            hostname=u"node1.example.com",
            applications=frozenset({DISCOVERED_APPLICATION_WITH_VOLUME}),
        )
        another_node = Node(
            hostname=u"node2.example.com",
            applications=frozenset({discovered_another_application}),
        )

        # The discovered current configuration of the cluster reveals the
        # application is running here, and another application is running
        # at the other node.
        current = Deployment(nodes=frozenset([node, another_node]))

        api = Deployer(
            create_volume_service(self), docker_client=docker,
            network=make_memory_network()
        )

        # We're swapping the location of applications:
        desired = Deployment(nodes=frozenset({
            Node(hostname=node.hostname,
                 applications=frozenset({another_application})),
            Node(hostname=another_node.hostname,
                 applications=frozenset({APPLICATION_WITH_VOLUME})),
        }))

        calculating = api.calculate_necessary_state_changes(
            desired_state=desired,
            current_cluster_state=current,
            hostname=node.hostname,
        )

        changes = self.successResultOf(calculating)

        volume = AttachedVolume(
            name=APPLICATION_WITH_VOLUME_NAME,
            mountpoint=APPLICATION_WITH_VOLUME_MOUNTPOINT,
        )
        volume2 = AttachedVolume(
            name=u"another",
            mountpoint=FilePath(b"/blah"),
        )
        expected = Sequentially(changes=[
            InParallel(changes=[PushVolume(
                volume=volume, hostname=another_node.hostname)]),
            InParallel(changes=[StopApplication(
                application=Application(name=APPLICATION_WITH_VOLUME_NAME),)]),
            InParallel(changes=[HandoffVolume(
                volume=volume, hostname=another_node.hostname)]),
            InParallel(changes=[WaitForVolume(volume=volume2)]),
            InParallel(changes=[
                StartApplication(application=another_application,
                                 hostname="node1.example.com")]),
        ])
        self.assertEqual(expected, changes)


class SetProxiesTests(SynchronousTestCase):
    """
    Tests for ``SetProxies``.
    """
    def test_proxies_added(self):
        """
        Proxies which are required are added.
        """
        fake_network = make_memory_network()
        api = Deployer(
            create_volume_service(self), docker_client=FakeDockerClient(),
            network=fake_network)

        expected_proxy = Proxy(ip=u'192.0.2.100', port=3306,
                               namespace="my_namespace")
        d = SetProxies(ports=[expected_proxy]).run(api)
        self.successResultOf(d)
        self.assertEqual(
            [expected_proxy],
            fake_network.enumerate_proxies()
        )

    def test_proxies_removed(self):
        """
        Proxies which are no longer required on the node are removed.
        """
        fake_network = make_memory_network()
        fake_network.create_proxy_to(ip=u'192.0.2.100', port=3306)
        api = Deployer(
            create_volume_service(self), docker_client=FakeDockerClient(),
            network=fake_network)

        d = SetProxies(ports=[]).run(api)
        self.successResultOf(d)
        self.assertEqual(
            [],
            fake_network.enumerate_proxies()
        )

    def test_desired_proxies_remain(self):
        """
        Proxies which exist on the node and which are still required are not
        removed.
        """
        fake_network = make_memory_network()

        # A proxy which will be removed
        fake_network.create_proxy_to(ip=u'192.0.2.100', port=3306)
        # And some proxies which are still required
        required_proxy1 = fake_network.create_proxy_to(ip=u'192.0.2.101',
                                                       port=3306)
        required_proxy2 = fake_network.create_proxy_to(ip=u'192.0.2.101',
                                                       port=8080)

        api = Deployer(
            create_volume_service(self), docker_client=FakeDockerClient(),
            network=fake_network)

        d = SetProxies(ports=[required_proxy1, required_proxy2]).run(api)

        self.successResultOf(d)
        self.assertEqual(
            set([required_proxy1, required_proxy2]),
            set(fake_network.enumerate_proxies())
        )

    def test_delete_proxy_errors_as_errbacks(self):
        """
        Exceptions raised in `delete_proxy` operations are reported as
        failures in the returned deferred.
        """
        fake_network = make_memory_network()
        fake_network.create_proxy_to(ip=u'192.0.2.100', port=3306)
        fake_network.delete_proxy = lambda proxy: 1/0

        api = Deployer(
            create_volume_service(self), docker_client=FakeDockerClient(),
            network=fake_network)

        d = SetProxies(ports=[]).run(api)
        exception = self.failureResultOf(d, FirstError)
        self.assertIsInstance(
            exception.value.subFailure.value,
            ZeroDivisionError
        )
        self.flushLoggedErrors(ZeroDivisionError)

    def test_create_proxy_errors_as_errbacks(self):
        """
        Exceptions raised in `create_proxy_to` operations are reported as
        failures in the returned deferred.
        """
        fake_network = make_memory_network()
        fake_network.create_proxy_to = lambda ip, port: 1/0

        api = Deployer(
            create_volume_service(self), docker_client=FakeDockerClient(),
            network=fake_network)

        proxy = Proxy(ip=u'192.0.2.100', port=3306, namespace="my_namespace")
        d = SetProxies(ports=[proxy]).run(api)
        exception = self.failureResultOf(d, FirstError)
        self.assertIsInstance(
            exception.value.subFailure.value,
            ZeroDivisionError
        )
        self.flushLoggedErrors(ZeroDivisionError)

    def test_create_proxy_errors_all_logged(self):
        """
        Exceptions raised in `create_proxy_to` operations are all logged.
        """
        fake_network = make_memory_network()
        fake_network.create_proxy_to = lambda ip, port: 1/0

        api = Deployer(
            create_volume_service(self), docker_client=FakeDockerClient(),
            network=fake_network)

        namespace = "my_namespace"
        d = SetProxies(
            ports=[Proxy(ip=u'192.0.2.100', port=3306, namespace=namespace),
                   Proxy(ip=u'192.0.2.101', port=3306, namespace=namespace),
                   Proxy(ip=u'192.0.2.102', port=3306, namespace=namespace)]
        ).run(api)

        self.failureResultOf(d, FirstError)

        failures = self.flushLoggedErrors(ZeroDivisionError)
        self.assertEqual(3, len(failures))


class DeployerChangeNodeStateTests(SynchronousTestCase):
    """
    Tests for ``Deployer.change_node_state``.

    XXX: Some of these tests are exercising code which has now been
    refactored into ``IStateChange`` objects. As such they can be
    refactored to not be based on side-effects. See
    https://github.com/ClusterHQ/flocker/issues/321
    """
    def test_applications_stopped(self):
        """
        Existing applications which are not in the desired configuration are
        stopped.
        """
        unit = Unit(name=u'mysql-hybridcluster',
                    container_name=u'mysql-hybridcluster',
                    activation_state=u'active')
        fake_docker = FakeDockerClient(units={unit.name: unit})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        desired = Deployment(nodes=frozenset())

        d = api.change_node_state(desired_state=desired,
                                  current_cluster_state=EMPTY,
                                  hostname=u'node.example.com')
        d.addCallback(lambda _: api.discover_node_configuration())

        self.assertEqual(NodeState(running=[], not_running=[]),
                         self.successResultOf(d))

    def test_applications_started(self):
        """
        Applications which are in the desired configuration are started.
        """
        fake_docker = FakeDockerClient(units={})
        api = Deployer(create_volume_service(self), docker_client=fake_docker,
                       network=make_memory_network())
        expected_application_name = u'mysql-hybridcluster'
        application = Application(
            name=expected_application_name,
            image=DockerImage(repository=u'clusterhq/flocker',
                              tag=u'release-14.0'),
            links=frozenset(),
        )

        nodes = frozenset([
            Node(
                hostname=u'node.example.com',
                applications=frozenset([application])
            )
        ])

        desired = Deployment(nodes=nodes)
        d = api.change_node_state(desired_state=desired,
                                  current_cluster_state=EMPTY,
                                  hostname=u'node.example.com')
        d.addCallback(lambda _: api.discover_node_configuration())

        expected_application = Application(name=expected_application_name)
        self.assertEqual(
            NodeState(running=[expected_application], not_running=[]),
            self.successResultOf(d))

    def test_result(self):
        """
        The result of calling ``change_node_state()`` is the result of calling
        ``run()`` on the result of ``calculate_necessary_state_changes``.
        """
        deferred = Deferred()
        api = Deployer(create_volume_service(self),
                       docker_client=FakeDockerClient(),
                       network=make_memory_network())
        self.patch(api, "calculate_necessary_state_changes",
                   lambda *args, **kwargs: succeed(FakeChange(deferred)))
        result = api.change_node_state(desired_state=EMPTY,
                                       current_cluster_state=EMPTY,
                                       hostname=u'node.example.com')
        deferred.callback(123)
        self.assertEqual(self.successResultOf(result), 123)

    def test_deployer(self):
        """
        The result of ``calculate_necessary_state_changes`` is called with the
        deployer.
        """
        change = FakeChange(succeed(None))
        api = Deployer(create_volume_service(self),
                       docker_client=FakeDockerClient(),
                       network=make_memory_network())
        self.patch(api, "calculate_necessary_state_changes",
                   lambda *args, **kwargs: succeed(change))
        api.change_node_state(desired_state=EMPTY,
                              current_cluster_state=EMPTY,
                              hostname=u'node.example.com')
        self.assertIs(change.deployer, api)

    def test_arguments(self):
        """
        The passed in arguments are passed on in turn to
        ``calculate_necessary_state_changes``.
        """
        desired = object()
        state = object()
        host = object()
        api = Deployer(create_volume_service(self),
                       docker_client=FakeDockerClient(),
                       network=make_memory_network())
        arguments = []

        def calculate(desired_state, current_cluster_state, hostname):
            arguments.extend([desired_state, current_cluster_state, hostname])
            return succeed(FakeChange(succeed(None)))
        api.calculate_necessary_state_changes = calculate
        api.change_node_state(desired, state, host)
        self.assertEqual(arguments, [desired, state, host])


class CreateVolumeTests(SynchronousTestCase):
    """
    Tests for ``CreateVolume``.
    """
    def test_creates(self):
        """
        ``CreateVolume.run()`` creates the named volume.
        """
        volume_service = create_volume_service(self)
        deployer = Deployer(volume_service,
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        create = CreateVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var")))
        create.run(deployer)
        self.assertIn(
            volume_service.get(_to_volume_name(u"myvol")),
            list(self.successResultOf(volume_service.enumerate())))

    def test_return(self):
        """
        ``CreateVolume.run()`` returns a ``Deferred`` that fires with the
        created volume.
        """
        deployer = Deployer(create_volume_service(self),
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        create = CreateVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var")))
        result = self.successResultOf(create.run(deployer))
        self.assertEqual(result, deployer.volume_service.get(
            _to_volume_name(u"myvol")))


class WaitForVolumeTests(SynchronousTestCase):
    """
    Tests for ``WaitForVolume``.
    """
    def test_waits(self):
        """
        ``WaitForVolume.run()`` waits for the named volume.
        """
        volume_service = create_volume_service(self)
        result = []

        def wait(name):
            result.append(name)
        self.patch(volume_service, "wait_for_volume", wait)
        deployer = Deployer(volume_service,
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        wait = WaitForVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var")))
        wait.run(deployer)
        self.assertEqual(result,
                         [VolumeName(namespace=u"default", id=u"myvol")])

    def test_return(self):
        """
        ``WaitVolume.run()`` returns a ``Deferred`` that fires when the
        named volume is available.
        """
        result = Deferred()
        volume_service = create_volume_service(self)
        self.patch(volume_service, "wait_for_volume", lambda name: result)
        deployer = Deployer(volume_service,
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        wait = WaitForVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var")))
        wait_result = wait.run(deployer)
        self.assertIs(wait_result, result)


class HandoffVolumeTests(SynchronousTestCase):
    """
    Tests for ``HandoffVolume``.
    """
    def test_handoff(self):
        """
        ``HandoffVolume.run()`` hands off the named volume to the given
        destination nodex.
        """
        volume_service = create_volume_service(self)
        hostname = b"dest.example.com"

        result = []

        def _handoff(volume, destination):
            result.extend([volume, destination])
        self.patch(volume_service, "handoff", _handoff)
        deployer = Deployer(volume_service,
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        handoff = HandoffVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var/blah")),
            hostname=hostname)
        handoff.run(deployer)
        self.assertEqual(
            result,
            [volume_service.get(_to_volume_name(u"myvol")),
             RemoteVolumeManager(standard_node(hostname))])

    def test_return(self):
        """
        ``HandoffVolume.run()`` returns the result of
        ``VolumeService.handoff``.
        """
        result = Deferred()
        volume_service = create_volume_service(self)
        self.patch(volume_service, "handoff",
                   lambda volume, destination: result)
        deployer = Deployer(volume_service,
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        handoff = HandoffVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var")),
            hostname=b"dest.example.com")
        handoff_result = handoff.run(deployer)
        self.assertIs(handoff_result, result)


class PushVolumeTests(SynchronousTestCase):
    """
    Tests for ``PushVolume``.
    """
    def test_push(self):
        """
        ``PushVolume.run()`` pushes the named volume to the given destination
        node.
        """
        volume_service = create_volume_service(self)
        hostname = b"dest.example.com"

        result = []

        def _push(volume, destination):
            result.extend([volume, destination])
        self.patch(volume_service, "push", _push)
        deployer = Deployer(volume_service,
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        push = PushVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var/blah")),
            hostname=hostname)
        push.run(deployer)
        self.assertEqual(
            result,
            [volume_service.get(_to_volume_name(u"myvol")),
             RemoteVolumeManager(standard_node(hostname))])

    def test_return(self):
        """
        ``PushVolume.run()`` returns the result of
        ``VolumeService.push``.
        """
        result = Deferred()
        volume_service = create_volume_service(self)
        self.patch(volume_service, "push",
                   lambda volume, destination: result)
        deployer = Deployer(volume_service,
                            docker_client=FakeDockerClient(),
                            network=make_memory_network())
        push = PushVolume(
            volume=AttachedVolume(name=u"myvol",
                                  mountpoint=FilePath(u"/var")),
            hostname=b"dest.example.com")
        push_result = push.run(deployer)
        self.assertIs(push_result, result)
