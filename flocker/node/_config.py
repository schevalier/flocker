# Copyright Hybrid Logic Ltd.  See LICENSE file for details.
# -*- test-case-name: flocker.node.test.test_config -*-

"""
APIs for parsing and validating configuration.
"""

from __future__ import unicode_literals, absolute_import

import os
import types

from twisted.python.filepath import FilePath

from yaml import safe_dump
from zope.interface import Interface, implementer

from ._model import (
    Application, AttachedVolume, Deployment, Link,
    DockerImage, Node, Port
)


class IApplicationConfiguration(Interface):
    """
    A class to detect and parse an application configuration  in a given
    format, mapping configuration to a ``dict`` of keys specifying application
    names mapped to values of ``Application`` instances.
    """
    def is_valid_format():
        """
        Detect if the supplied application configuration is in a format
        compatible with this configuration parser.

        Note that "valid format" does not necessarily translate to
        "valid configuration"; this method should only indicate whether the
        supplied configuration's outermost structure is such that it should
        be treated and parsed as a configuration of this parser's format.
        Further validation should be done during the parsing stage.

        :returns: A ``bool`` indicating ``True`` for a fig-style configuration
            or ``False`` if fig-style is not detected.
        """

    def applications():
        """
        Returns the ``Application`` instances parsed from the supplied
        configuration.

        This method should only be called after valdiating the format
        with a call to ``is_valid_format``.

        This method should only be called once, in that calling it
        multiple times will re-parse an already parsed config.

        :returns: A ``dict`` mapping application names to ``Application``
            instances.
        """


class ConfigurationError(Exception):
    """
    Some part of the supplied configuration was wrong.

    The exception message will include some details about what.
    """


def _check_type(value, types, description, application_name):
    """
    Checks ``value`` has type in ``types``.

    :param value: Value whose type is to be checked
    :param tuple types: Tuple of types value can be.
    :param str description: Description of expected type.
    :param application_name unicode: Name of application whose config
        contains ``value``.

    :raises ConfigurationError: If ``value`` is not of type in ``types``.
    """
    if not isinstance(value, types):
        raise ConfigurationError(
            "Application '{application_name}' has a config "
            "error. {description}; got type '{type}'.".format(
                application_name=application_name,
                description=description,
                type=type(value).__name__,
            ))


class ApplicationMarshaller(object):
    """
    Convert ``Application`` instances or their properties to a ``dict``
    representation that matches the format of Flocker's YAML application
    configuration language.
    """
    _application = None

    def __init__(self, application):
        """
        Initialise the marshaller for a single application.

        :param application: An ``Application`` instance.
        """
        self._application = application

    def convert(self):
        """
        Convert all the properties of an ``Application`` instance to a
        ``dict`` representing a single application entry in Flocker's
        application configuration YAML format.

        :returns: A ``dict`` containing the application's converted properties.
        """
        config = dict()
        image = self.convert_image()
        if image:
            config['image'] = image
        ports = self.convert_ports()
        if ports:
            config['ports'] = ports
        links = self.convert_links()
        if links:
            config['links'] = links
        environment = self.convert_environment()
        if environment:
            config['environment'] = environment
        volume = self.convert_volume()
        if volume:
            config['volume'] = volume
        return config

    def convert_image(self):
        """
        Return the ``Application`` image name and tag.
        :returns: ``unicode`` representing the image name and tag or ``None``
            if the image is unknown (``None`` or not a ``DockerImage``).
        """
        if isinstance(self._application.image, DockerImage):
            return ':'.join(
                [self._application.image.repository,
                 self._application.image.tag]
            )
        return None

    def convert_ports(self):
        """
        Parse an ``Application`` instance for its ports and return
        a ``list`` representing the Flocker-format YAML configuration
        for those ports.
        """
        ports = []
        for port in self._application.ports:
            ports.append(dict(
                internal=port.internal_port,
                external=port.external_port
            ))
        return sorted(ports)

    def convert_environment(self):
        """
        Parse an ``Application`` instance for its environment variables and
        return a ``dict`` representing the Flocker-format YAML configuration
        for those variables.
        """
        if self._application.environment:
            return dict(self._application.environment)
        return dict()

    def convert_links(self):
        """
        Parse an ``Application`` instance for its links and return
        a ``dict`` representing the Flocker-format YAML configuration
        for those links.
        """
        links = []
        for link in self._application.links:
            links.append(dict(
                local_port=link.local_port,
                remote_port=link.remote_port,
                alias=link.alias
            ))
        return sorted(links)

    def convert_volume(self):
        """
        Parse an ``Application`` instance for its volume and return
        a ``dict`` representing the Flocker-format YAML configuration
        for the volume, or ``None`` if no volume is set for the application.

        NOTE: We only support one volume per conainer for now, this
        logic will need refactoring in future if this changes.
        """
        if self._application.volume:
            return {u'mountpoint': self._application.volume.mountpoint.path}
        return None


def applications_to_flocker_yaml(applications):
    """
    Converts a ``dict`` of ``Application`` instances to Flocker's
    application configuration YAML.

    :param applications: A ``dict`` mapping application names to
        ``Application`` instances.

    :returns: ``unicode`` representation of a complete Flocker
        application configuration YAML.
    """
    config = {'version': 1, 'applications': dict()}
    for application_name, application in applications.items():
        converter = ApplicationMarshaller(application)
        value = converter.convert()
        config['applications'][application_name] = value
    return safe_dump(config)


@implementer(IApplicationConfiguration)
class FigConfiguration(object):
    """
    Validate and parse a fig-style application configuration.
    """
    def __init__(self, application_configuration):
        """
        Initializes ``FigConfiguration`` attributes and validates config.

        :param dict application_configuration: The intermediate
            configuration representation to load into ``Application``
            instances.  See :ref:`Configuration` for details.

        Attributes initialized in this method are:

        self._application_configuration: the application_configuration
            parameter

        self._application_names: A ``list`` of keys in
            application_configuration representing all application
            names.

        self._applications: The ``dict`` of ``Application`` objects
            after parsing.

        self._application_links: ``dict`` acting as an internal map
            of links to create between applications, this serves as an
            intermediary when parsing applications, since an application
            name specified in a link may not have been parsed at the point
            the link is encountered.

        self._possible_identifiers: A ``dict`` of keys that may identify a
            dictionary of parsed YAML as being Fig-format.

        self._unsupported_keys: A ``dict`` of keys representing Fig config
            directives that are not yet supported by Flocker.

        self._allowed_keys: A ``dict`` representing all the keys that are
            supported and therefore allowed to appear in a single Fig service
            definition.
        """
        if not isinstance(application_configuration, dict):
            raise ConfigurationError(
                "Application configuration must be a dictionary, got {type}.".
                format(type=type(application_configuration).__name__)
            )
        self._application_configuration = application_configuration
        self._application_names = self._application_configuration.keys()
        self._applications = {}
        self._application_links = {}
        self._validated = False
        self._possible_identifiers = {'image', 'build'}
        self._unsupported_keys = {
            "working_dir", "entrypoint", "user", "hostname",
            "domainname", "mem_limit", "privileged", "dns", "net",
            "volumes_from", "expose", "command"
        }
        self._allowed_keys = {
            "image", "environment", "ports",
            "links", "volumes"
        }

    def applications(self):
        self._parse()
        return self._applications

    def is_valid_format(self):
        """
        Detect if the supplied application configuration is in fig-compatible
        format.

        A fig-style configuration is defined as:
        Overall application configuration is of type dictionary, containing
        one or more keys which each contain a further dictionary, which
        contain exactly one "image" key or "build" key.
        http://www.fig.sh/yml.html
        """
        valid = False
        for application_name, config in (
                self._application_configuration.items()):
            if isinstance(config, dict):
                required_keys = self._count_identifier_keys(config)
                if required_keys == 1:
                    valid = True
                elif required_keys > 1:
                    raise ConfigurationError(
                        ("Application '{app_name}' has a config error. "
                         "Must specify either 'build' or 'image'; found both.")
                        .format(app_name=application_name)
                    )
        return valid

    def _count_identifier_keys(self, config):
        """
        Counts how many of the keys that identify a single application
        as having a fig-format are found in the supplied application
        definition.

        :param dict config: A single application definition from
            the application_configuration dictionary.

        :returns: ``int`` representing the number of identifying keys found.
        """
        config_keys = set(config)
        return len(self._possible_identifiers & config_keys)

    def _validate_application_keys(self, application, config):
        """
        Checks that a single application definition contains no invalid
        or unsupported keys.

        :param bytes application: The name of the application this config
            is mapped to.

        :param dict config: A single application definition from
            the application_configuration dictionary.

        :raises ValueError: if any invalid or unsupported keys found.

        :returns: ``None``
        """
        _check_type(config, dict,
                    "Application configuration must be dictionary",
                    application)
        if self._count_identifier_keys(config) == 0:
            raise ValueError(
                ("Application configuration must contain either an "
                 "'image' or 'build' key.")
            )
        if 'build' in config:
            raise ValueError(
                "'build' is not supported yet; please specify 'image'."
            )
        present_keys = set(config)
        invalid_keys = present_keys - self._allowed_keys
        present_unsupported_keys = self._unsupported_keys & present_keys
        if present_unsupported_keys:
            raise ValueError(
                "Unsupported fig keys found: {keys}".format(
                    keys=', '.join(sorted(present_unsupported_keys))
                )
            )
        if invalid_keys:
            raise ValueError(
                "Unrecognised keys: {keys}".format(
                    keys=', '.join(sorted(invalid_keys))
                )
            )

    def _parse_app_environment(self, application, environment):
        """
        Validate and parse the environment portion of an application
        configuration.

        :param bytes application: The name of the application this config
            is mapped to.

        :param dict environment: A dictionary of environment variable
            names and values.

        :raises ConfigurationError: if the environment config does
            not validate.

        :returns: A ``frozenset`` of environment variable name/value
            pairs.
        """
        _check_type(environment, dict,
                    "'environment' must be a dictionary",
                    application)
        for var, val in environment.items():
            _check_type(
                val, (str, unicode,),
                ("'environment' value for '{var}' must be a string"
                 .format(var=var)),
                application
            )
        return frozenset(environment.items())

    def _parse_app_volumes(self, application, volumes):
        """
        Validate and parse the volumes portion of an application
        configuration.

        :param bytes application: The name of the application this config
            is mapped to.

        :param list volumes: A list of ``str`` values giving absolute
            paths where a volume should be mounted inside the application.

        :raises ConfigurationError: if the volumes config does not validate.

        :returns: A ``AttachedVolume`` instance.
        """
        _check_type(volumes, list,
                    "'volumes' must be a list",
                    application)
        for volume in volumes:
            if not isinstance(volume, (str, unicode,)):
                raise ConfigurationError(
                    ("Application '{application}' has a config "
                     "error. 'volumes' values must be string; got "
                     "type '{type}'.").format(
                         application=application,
                         type=type(volume).__name__)
                )
        if len(volumes) > 1:
            raise ConfigurationError(
                ("Application '{application}' has a config "
                 "error. Only one volume per application is "
                 "supported at this time.").format(
                     application=application)
            )
        volume = AttachedVolume(
            name=application,
            mountpoint=FilePath(volumes[0])
        )
        return volume

    def _parse_app_ports(self, application, ports):
        """
        Validate and parse the ports portion of an application
        configuration.

        :param bytes application: The name of the application this config
            is mapped to.

        :param list ports: A list of ``str`` values mapping ports that
            should be exposed by the application container to the host.

        :raises ConfigurationError: if the ports config does not validate.

        :returns: A ``list`` of ``Port`` instances.
        """
        return_ports = list()
        _check_type(ports, list,
                    "'ports' must be a list",
                    application)
        for port in ports:
            parsed_ports = port.split(':')
            if len(parsed_ports) != 2:
                raise ConfigurationError(
                    ("Application '{application}' has a config "
                     "error. 'ports' must be list of string "
                     "values in the form of "
                     "'host_port:container_port'.").format(
                         application=application)
                )
            try:
                parsed_ports = [int(p) for p in parsed_ports]
            except ValueError:
                raise ConfigurationError(
                    ("Application '{application}' has a config "
                     "error. 'ports' value '{ports}' could not "
                     "be parsed in to integer values.")
                    .format(
                        application=application,
                        ports=port)
                )
            return_ports.append(
                Port(
                    internal_port=parsed_ports[1],
                    external_port=parsed_ports[0]
                )
            )
        return return_ports

    def _parse_app_links(self, application, links):
        """
        Validate and parse the links portion of an application
        configuration and store the links in the internal links map.

        :param bytes application: The name of the application this config
            is mapped to.

        :param list links: A list of ``str`` values specifying the names
            of applications that this application should link to.

        :raises ConfigurationError: if the links config does not validate.

        :returns: ``None``
        """
        _check_type(links, list,
                    "'links' must be a list",
                    application)
        for link in links:
            if not isinstance(link, (str, unicode,)):
                raise ConfigurationError(
                    ("Application '{application}' has a config "
                     "error. 'links' must be a list of "
                     "application names with optional :alias.")
                    .format(application=application)
                )
            parsed_link = link.split(':')
            local_link = parsed_link[0]
            aliased_link = local_link
            if len(parsed_link) == 2:
                aliased_link = parsed_link[1]
            if local_link not in self._application_names:
                raise ConfigurationError(
                    ("Application '{application}' has a config "
                     "error. 'links' value '{link}' could not be "
                     "mapped to any application; application "
                     "'{link}' does not exist.").format(
                         application=application,
                         link=link)
                )
            self._application_links[application].append({
                'target_application': local_link,
                'alias': aliased_link,
            })

    def _link_applications(self):
        """
        Iterate through the internal links map and create a
        frozenset of ``Link`` instances in each application, mapping
        the link name and alias to the ports of the target linked
        application.

        :returns: ``None``
        """
        for application_name, link in self._application_links.items():
            app_links = []
            for link_definition in link:
                target_application_ports = self._applications[
                    link_definition['target_application']].ports
                for target_ports_object in target_application_ports:
                    local_port = target_ports_object.internal_port
                    remote_port = target_ports_object.external_port
                    app_links.append(
                        Link(local_port=local_port,
                             remote_port=remote_port,
                             alias=link_definition['alias'])
                    )
            self._applications[application_name].links = frozenset(
                app_links)

    def _parse(self):
        """
        Validate and parse a given application configuration from fig's
        configuration format.

        :raises ConfigurationError: if there are validation errors.

        :returns: A ``dict`` mapping application names to ``Application``
            instances.

        """
        for application_name, config in (
            self._application_configuration.items()
        ):
            try:
                self._validate_application_keys(application_name, config)
                _check_type(config['image'], (str, unicode,),
                            "'image' must be a string",
                            application_name)
                image_name = config['image']
                image = DockerImage.from_string(image_name)
                environment = None
                ports = []
                volume = None
                self._application_links[application_name] = []
                if 'environment' in config:
                    environment = self._parse_app_environment(
                        application_name,
                        config['environment']
                    )
                if 'volumes' in config:
                    volume = self._parse_app_volumes(
                        application_name,
                        config['volumes']
                    )
                if 'ports' in config:
                    ports = self._parse_app_ports(
                        application_name,
                        config['ports']
                    )
                if 'links' in config:
                    self._parse_app_links(
                        application_name,
                        config['links']
                    )
                self._applications[application_name] = Application(
                    name=application_name,
                    image=image,
                    volume=volume,
                    ports=frozenset(ports),
                    links=frozenset(),
                    environment=environment
                )
            except ValueError as e:
                raise ConfigurationError(
                    ("Application '{application_name}' has a config error. "
                     "{message}".format(application_name=application_name,
                                        message=e.message))
                )
        # Here we can now process application links; we cannot perform this
        # logic at the parsing stage above, because at the point we encounter
        # a link in application A to application B, there is no guarantee that
        # application B has been parsed yet.
        self._link_applications()


@implementer(IApplicationConfiguration)
class FlockerConfiguration(object):
    """
    Validate and parse native Flocker-formatted configurations.
    """
    def __init__(self, application_configuration, lenient=False):
        """
        :param bool lenient: If ``True`` don't complain about certain
            deficiencies in the output of ``flocker-reportstate``, In
            particular https://github.com/ClusterHQ/flocker/issues/289 means
            the mountpoint is unknown.

        :param dict application_configuration: The native parsed YAML
            configuration to load into ``Application`` instances.
            See :ref:`Configuration` for details.
        """
        if not isinstance(application_configuration, dict):
            raise ConfigurationError(
                "Application configuration must be a dictionary, got {type}.".
                format(type=type(application_configuration).__name__)
            )
        self._application_configuration = application_configuration
        self._lenient = lenient
        self._allowed_keys = {
            "image", "environment", "ports",
            "links", "volume"
        }
        self._applications = {}

    def applications(self):
        self._parse()
        return self._applications

    def is_valid_format(self):
        """
        Detect if the supplied application configuration is a Flocker
        compatible format.

        A Flocker configuration is a dictionary containing, at a minimum,
        a version key containing an integer version number  and an applications
        key containing a mapping of application names to definitions.
        """
        valid = False
        flocker_keys = set(['version', 'applications'])
        present_keys = set(self._application_configuration)
        if flocker_keys.issubset(present_keys):
            valid = True
            for application_name, application in (
                self._application_configuration['applications'].items()
            ):
                if not isinstance(application, dict):
                    valid = False
                else:
                    if 'image' not in application:
                        valid = False
        return valid

    def _validate_configuration_keys(self):
        """
        Validates a Flocker configuration contains all required keys and
        no unrecognised keys.

        :raises ConfigurationError: if any invalid or unsupported keys found.

        :returns: ``None``
        """
        if u'applications' not in self._application_configuration:
            raise ConfigurationError("Application configuration has an error. "
                                     "Missing 'applications' key.")
        if u'version' not in self._application_configuration:
            raise ConfigurationError("Application configuration has an error. "
                                     "Missing 'version' key.")
        if self._application_configuration[u'version'] != 1:
            raise ConfigurationError(
                "Application configuration has an error. "
                "Incorrect version specified."
            )

    def _validate_application_keys(self, application, config):
        """
        Checks that a single application definition contains no invalid
        or unsupported keys.

        :param bytes application: The name of the application this config
            is mapped to.

        :param dict config: A single application definition from
            the application_configuration dictionary.

        :raises ValueError: if any invalid or unsupported keys found.

        :returns: ``None``
        """
        _check_type(config, dict,
                    "Application configuration must be dictionary",
                    application)
        present_keys = set(config)
        invalid_keys = present_keys - self._allowed_keys
        if invalid_keys:
            raise ConfigurationError(
                ("Application '{application_name}' has a config error. "
                 "Unrecognised keys: {keys}.").format(
                    application_name=application,
                    keys=', '.join(sorted(invalid_keys))
                )
            )
        if 'image' not in config:
            raise ConfigurationError(
                ("Application '{application_name}' has a config error. "
                 "Missing 'image' key.").format(
                    application_name=application)
            )

    def _parse_environment_config(self, application_name, config):
        """
        Validate and return an application config's environment variables.

        :param unicode application_name: The name of the application.

        :param dict config: The config of a single ``Application`` instance,
            as extracted from the ``applications`` ``dict`` in
            ``_applications_from_configuration``.

        :raises ConfigurationError: if the ``environment`` element of
            ``config`` is not a ``dict`` or ``dict``-like value.

        :returns: ``None`` if there is no ``environment`` element in the
            config, or the ``frozenset`` of environment variables if there is,
            in the form of a ``frozenset`` of ``tuple`` \s mapped to
            (key, value)

        """
        environment = config.pop('environment', None)
        if environment:
            _check_type(value=environment, types=(dict,),
                        description="'environment' must be a dictionary of "
                                    "key/value pairs",
                        application_name=application_name)
            for key, value in environment.iteritems():
                # We should normailzie strings to either bytes or unicode here
                # https://github.com/ClusterHQ/flocker/issues/636
                _check_type(value=key, types=types.StringTypes,
                            description="Environment variable name "
                                        "must be a string",
                            application_name=application_name)
                _check_type(value=value, types=types.StringTypes,
                            description="Environment variable '{key}' "
                                        "must be a string".format(key=key),
                            application_name=application_name)
            environment = frozenset(environment.items())
        return environment

    def _parse_link_configuration(self, application_name, config):
        """
        Validate and retrun an application config's links.

        :param unicode application_name: The name of the application

        :param dict config: The ``links`` configuration stanza of this
            application.

        :returns: A ``frozenset`` of ``Link``s specfied for this application.
        """
        links = []
        _check_type(value=config, types=(list,),
                    description="'links' must be a list of dictionaries",
                    application_name=application_name)
        try:
            for link in config:
                _check_type(value=link, types=(dict,),
                            description="Link must be a dictionary",
                            application_name=application_name)

                try:
                    local_port = link.pop('local_port')
                    _check_type(value=local_port, types=(int,),
                                description="Link's local port must be an int",
                                application_name=application_name)
                except KeyError:
                    raise ValueError("Missing local port.")

                try:
                    remote_port = link.pop('remote_port')
                    _check_type(value=remote_port, types=(int,),
                                description="Link's remote port "
                                            "must be an int",
                                application_name=application_name)
                except KeyError:
                    raise ValueError("Missing remote port.")

                try:
                    # We should normailzie strings to either bytes or unicode
                    # here. https://github.com/ClusterHQ/flocker/issues/636
                    alias = link.pop('alias')
                    _check_type(value=alias, types=types.StringTypes,
                                description="Link alias must be a string",
                                application_name=application_name)
                except KeyError:
                    raise ValueError("Missing alias.")

                if link:
                    raise ValueError(
                        "Unrecognised keys: {keys}.".format(
                            keys=', '.join(sorted(link))))
                links.append(Link(local_port=local_port,
                                  remote_port=remote_port,
                                  alias=alias))
        except ValueError as e:
            raise ConfigurationError(
                ("Application '{application_name}' has a config error. "
                 "Invalid links specification. {message}").format(
                     application_name=application_name, message=e.message))

        return frozenset(links)

    def _parse(self):
        """
        Validate and parse a given application configuration from flocker's
        configuration format.

        :raises ConfigurationError: if there are validation errors.
        """
        self._validate_configuration_keys()
        for application_name, config in (
                self._application_configuration['applications'].items()):
            self._validate_application_keys(application_name, config)
            image_name = config['image']
            try:
                image = DockerImage.from_string(image_name)
            except ValueError as e:
                raise ConfigurationError(
                    ("Application '{application_name}' has a config error. "
                     "Invalid Docker image name. {message}").format(
                        application_name=application_name, message=e.message)
                )

            ports = []
            try:
                for port in config.pop('ports', []):
                    try:
                        internal_port = port.pop('internal')
                    except KeyError:
                        raise ValueError("Missing internal port.")
                    try:
                        external_port = port.pop('external')
                    except KeyError:
                        raise ValueError("Missing external port.")

                    if port:
                        raise ValueError(
                            "Unrecognised keys: {keys}.".format(
                                keys=', '.join(sorted(port.keys()))))
                    ports.append(Port(internal_port=internal_port,
                                      external_port=external_port))
            except ValueError as e:
                raise ConfigurationError(
                    ("Application '{application_name}' has a config error. "
                     "Invalid ports specification. {message}").format(
                        application_name=application_name, message=e.message)
                )

            links = self._parse_link_configuration(
                application_name, config.pop('links', []))

            volume = None
            if "volume" in config:
                try:
                    configured_volume = config.pop('volume')
                    try:
                        mountpoint = configured_volume['mountpoint']
                    except TypeError:
                        raise ValueError(
                            "Unexpected value: " + str(configured_volume)
                        )
                    except KeyError:
                        raise ValueError("Missing mountpoint.")

                    if not (self._lenient and mountpoint is None):
                        if not isinstance(mountpoint, str):
                            raise ValueError(
                                "Mountpoint {path} contains non-ASCII "
                                "(unsupported).".format(
                                    path=mountpoint
                                )
                            )
                        if not os.path.isabs(mountpoint):
                            raise ValueError(
                                "Mountpoint {path} is not an absolute path."
                                .format(
                                    path=mountpoint
                                )
                            )
                        configured_volume.pop('mountpoint')
                        if configured_volume:
                            raise ValueError(
                                "Unrecognised keys: {keys}.".format(
                                    keys=', '.join(sorted(
                                        configured_volume.keys()))
                                ))
                        mountpoint = FilePath(mountpoint)

                    volume = AttachedVolume(
                        name=application_name,
                        mountpoint=mountpoint
                        )
                except ValueError as e:
                    raise ConfigurationError(
                        ("Application '{application_name}' has a config "
                         "error. Invalid volume specification. {message}")
                        .format(
                            application_name=application_name,
                            message=e.message
                        )
                    )

            environment = self._parse_environment_config(
                application_name, config)

            self._applications[application_name] = Application(
                name=application_name,
                image=image,
                volume=volume,
                ports=frozenset(ports),
                links=links,
                environment=environment)


def deployment_from_configuration(deployment_configuration, all_applications):
    """
    Validate and parse a given deployment configuration.

    :param dict deployment_configuration: The intermediate configuration
        representation to load into ``Node`` instances.  See
        :ref:`Configuration` for details.

    :param set all_applications: All applications which should be running
        on all nodes.

    :raises ConfigurationError: if there are validation errors.

    :returns: A ``set`` of ``Node`` instances.
    """
    if 'nodes' not in deployment_configuration:
        raise ConfigurationError("Deployment configuration has an error. "
                                 "Missing 'nodes' key.")

    if u'version' not in deployment_configuration:
        raise ConfigurationError("Deployment configuration has an error. "
                                 "Missing 'version' key.")

    if deployment_configuration[u'version'] != 1:
        raise ConfigurationError("Deployment configuration has an error. "
                                 "Incorrect version specified.")

    nodes = []
    for hostname, application_names in (
            deployment_configuration['nodes'].items()):
        if not isinstance(application_names, list):
            raise ConfigurationError(
                "Node {node_name} has a config error. "
                "Wrong value type: {value_type}. "
                "Should be list.".format(
                    node_name=hostname,
                    value_type=application_names.__class__.__name__)
            )
        node_applications = []
        for name in application_names:
            application = all_applications.get(name)
            if application is None:
                raise ConfigurationError(
                    "Node {hostname} has a config error. "
                    "Unrecognised application name: "
                    "{application_name}.".format(
                        hostname=hostname, application_name=name)
                )
            node_applications.append(application)
        node = Node(hostname=hostname,
                    applications=frozenset(node_applications))
        nodes.append(node)
    return set(nodes)


def model_from_configuration(applications, deployment_configuration):
    """
    Validate and coerce the supplied application configuration and
    deployment configuration dictionaries into a ``Deployment`` instance.

    :param dict applications: Map of application names to ``Application``
        instances.

    :param dict deployment_configuration: Map of node names to application
        names.

    :raises ConfigurationError: if there are validation errors.

    :returns: A ``Deployment`` object.
    """
    nodes = deployment_from_configuration(
        deployment_configuration, applications)
    return Deployment(nodes=frozenset(nodes))


def current_from_configuration(current_configuration):
    """
    Validate and coerce the supplied current cluster configuration into a
    ``Deployment`` instance.

    The passed in configuration is the aggregated output of
    ``marshal_configuration`` as combined by ``flocker-deploy``.

    :param dict current_configuration: Map of node names to list of
        application maps.

    :raises ConfigurationError: if there are validation errors.

    :returns: A ``Deployment`` object.
    """
    nodes = []
    for hostname, applications in current_configuration.items():
        configuration = FlockerConfiguration(applications, lenient=True)
        node_applications = configuration.applications()
        nodes.append(Node(hostname=hostname,
                          applications=frozenset(node_applications.values())))
    return Deployment(nodes=frozenset(nodes))


def marshal_configuration(state):
    """
    Generate representation of a node's applications using only simple Python
    types.

    A bunch of information is missing, but this is sufficient for the
    initial requirement of determining what to do about volumes when
    applying configuration changes.
    https://github.com/ClusterHQ/flocker/issues/289

    :param NodeState state: The configuration state to marshal.

    :return: An object representing the node configuration in a structure
        roughly compatible with the configuration file format.  Only "simple"
        (easily serialized) Python types will be used: ``dict``, ``list``,
        ``int``, ``unicode``, etc.
    """
    result = {}
    for application in state.running + state.not_running:
        converter = ApplicationMarshaller(application)

        # XXX image unknown, see
        # https://github.com/ClusterHQ/flocker/issues/207
        # When 207 is complete, use ``converter.convert_image``
        result[application.name] = {"image": "unknown"}

        result[application.name]["ports"] = converter.convert_ports()

        if application.links:
            result[application.name]["links"] = converter.convert_links()

        if application.volume:
            # Until multiple volumes are supported, assume volume name
            # matches application name, see:
            # https://github.com/ClusterHQ/flocker/issues/49
            # When 49 is complete, use ``converter.convert_volume``
            result[application.name]["volume"] = {
                "mountpoint": None,
            }
    return {
        "version": 1,
        "applications": result,
        "used_ports": sorted(state.used_ports),
    }
