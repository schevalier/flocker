# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""Interfaces that filesystem APIs need to expose."""

from __future__ import absolute_import

from zope.interface import Interface


class FilesystemAlreadyExists(Exception):
    """
    Raised when creating or renaming a filesystem, and the target already
    exists.
    """


class IFilesystemSnapshots(Interface):
    """Support creating and listing snapshots of a specific filesystem."""

    def create(name):
        """Create a snapshot of the filesystem.

        :param name: The name of the snapshot.
        :type name: :py:class:`flocker.volume.snapshots.SnapshotName`

        :return: Deferred that fires on snapshot creation, or errbacks if
            snapshotting failed. The Deferred should support cancellation
            if at all possible.
        """

    def list():
        """Return all the filesystem's snapshots.

        :return: Deferred that fires with a ``list`` of
            :py:class:`flocker.snapshots.SnapshotName`.
        """


class IFilesystem(Interface):
    """A filesystem that is part of a pool."""

    def get_path():
        """Retrieve the filesystem's local path.

        E.g. for a ZFS filesystem this would be the path where it is
        mounted.

        :return: The path as a ``FilePath``.
        """

    def reader(destination_hints=None):
        """
        Context manager that allows reading the contents of the filesystem.

        A blocking API, for now.

        The returned file-like object will be closed by this object.

        :param destination_hints: If ``None``, the output stream is not
            specific to any destination and so the full contents will e
            included. If the output of ``write_hints()`` on the receiving
            side is passed in (as ``bytes``) the reader may be able to
            return a customized stream for writing to that destination.

        :return: A file-like object from whom the filesystem's data can be
            read as ``bytes``.
        """

    def writer():
        """Context manager that allows writing new contents to the filesystem.

        This receiver is a blocking API, for now.

        The returned file-like object will be closed by this object.

        The higher-level volume API will ensure that whoever is writing
        the data is the owner of the volume. As such, whatever new data is
        being received will overwrite the filesystem's existing data.

        :param Volume volume: A volume that is being pushed to us.

        :return: A file-like object which when written to with output of
            :meth:`IFilesystem.reader` will populate the volume's
            filesystem.
        """

    def write_hints():
        """
        Return information that might be useful to someone writing to this
        filesystem.

        :return: ``bytes`` in a format readable by writers of the same
            kind of filesystem.
        """

    def __eq__(other):
        """True if and only if underlying OS filesystem is the same."""

    def __ne__(other):
        """True if and only if underlying OS filesystem is different."""

    def __hash__():
        """Equal objects should have the same hash."""


class IStoragePool(Interface):
    """Pool of on-disk storage where filesystems are stored."""

    def create(volume):
        """Create a new filesystem for the given volume.

        By default new filesystems will be automounted. In future
        iterations when remotely owned filesystems are added
        (https://github.com/ClusterHQ/flocker/issues/93) this interface
        will be expanded to allow specifying that the filesystem should
        not be mounted.

        :param volume: The volume whose filesystem should be created.
        :type volume: :class:`flocker.volume.service.Volume`

        :return: Deferred that fires on filesystem creation with a
            :class:`IFilesystem` provider, or errbacks if creation failed.
        """

    def get(volume):
        """Return a filesystem object for the given volume.

        This presumes the volume exists.

        :param Volume volume: The volume whose filesystem is being retrieved.
        :type volume: :class:`flocker.volume.service.Volume`

        :return: A :class:`IFilesystem` provider.
        """

    def change_owner(volume, new_volume):
        """
        Make necessary changes to a filesystem whose volume's owner UUID is
        being changed.

        :param Volume volume: The volume whose owner will be changed.
        :param Volume new_volume: The volume with the changed owner.

        :return: ``Deferred`` that fires with the new :class:`IFilesystem`.
        :raises FilesystemAlreadyExists: If the target filesystem already
            exists.
        """

    def enumerate():
        """Get a listing of all filesystems in this pool.

        :return: A ``Deferred`` that fires with a :class:`list` of
            :class:`IFilesystem` providers.
        """
