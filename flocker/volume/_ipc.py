# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Inter-process communication for the volume manager.

Specific volume managers ("nodes") may wish to push data to other
nodes. In the current iteration this is done over SSH using a blocking
API. In some future iteration this will be replaced with an actual
well-specified communication protocol between daemon processes using
Twisted's event loop (https://github.com/ClusterHQ/flocker/issues/154).
"""

from contextlib import contextmanager
from io import BytesIO

from characteristic import with_cmp

from zope.interface import Interface, implementer

from .service import DEFAULT_CONFIG_PATH


class IRemoteVolumeManager(Interface):
    """
    A remote volume manager with which one can communicate somehow.
    """
    def receive(volume):
        """
        Context manager that returns a file-like object to which a volume's
        contents can be written.

        :param Volume volume: The volume which will be pushed to the
            remote volume manager.

        :return: A file-like object that can be written to, which will
             update the volume on the remote volume manager.
        """

    def acquire(volume):
        """
        Tell the remote volume manager to acquire the given volume.

        :param Volume volume: The volume which will be acquired by the
            remote volume manager.

        :return: The UUID of the remote volume manager (as ``unicode``).
        """

    def write_hints(volume):
        """
        Ask the remote volume manager for hints about how to update the volume
        on the destination.

        :param Volume volume: The volume which will subsequentially be pushed to
            by the remote volume manager.

        :return: The hints, as ``bytes``.
        """


@implementer(IRemoteVolumeManager)
@with_cmp(["_destination", "_config_path"])
class RemoteVolumeManager(object):
    """
    ``INode``\-based communication with a remote volume manager.
    """

    def __init__(self, destination, config_path=DEFAULT_CONFIG_PATH):
        """
        :param Node destination: The node to push to.
        :param FilePath config_path: Path to configuration file for the
            remote ``flocker-volume``.
        """
        self._destination = destination
        self._config_path = config_path

    def receive(self, volume):
        return self._destination.run([b"flocker-volume",
                                      b"--config", self._config_path.path,
                                      b"receive",
                                      volume.uuid.encode(b"ascii"),
                                      volume.name.encode("ascii")])

    def acquire(self, volume):
        return self._destination.get_output(
            [b"flocker-volume",
             b"--config", self._config_path.path,
             b"acquire",
             volume.uuid.encode(b"ascii"),
             volume.name.encode("ascii")]).decode("ascii")

    # def write_hints(self, volume):
    #     return self._destination.get_output(
    #         [b"flocker-volume",
    #          b"--config", self._config_path.path,
    #          b"write-hints",
    #          volume.uuid.encode(b"ascii"),
    #          volume.name.encode("ascii")]).decode("ascii")


@implementer(IRemoteVolumeManager)
class LocalVolumeManager(object):
    """
    In-memory communication with a ``VolumeService`` instance, for testing.
    """

    def __init__(self, service):
        """
        :param VolumeService service: The service to communicate with.
        """
        self._service = service

    @contextmanager
    def receive(self, volume):
        input_file = BytesIO()
        yield input_file
        input_file.seek(0, 0)
        self._service.receive(volume.uuid, volume.name, input_file)

    def acquire(self, volume):
        self._service.acquire(volume.uuid, volume.name)
        return self._service.uuid

    # def write_hints(self, volume):
    #    return volume.get_filesystem().write_hints()
