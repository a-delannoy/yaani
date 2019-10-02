#!/usr/bin/env python3
from __future__ import absolute_import

from functools import reduce
import logging
import argparse
import sys
import os
import yaml
try:
    import json
except ImportError:
    import simplejson as json

from lark import Lark, Transformer
import re

from jsonschema import validate
import pynetbox

# The name of the Environment variable where to find the path towards the
# configuration file
DEFAULT_ENV_CONFIG_FILE = "NETBOX_CONFIG_FILE"


class KeyPathResolver:
    def __init__(self):
        self._grammar = """
            expr: sub
                | default_key
                | key_path

            default_key: expr "|" "default_key" "(" key_path ")"

            sub: expr "|" "sub" "(" STRING  "," STRING \
                                    ["," NUMBER  ["," NUMBER ]] ")"

            key_path: namespace KEY_NAME ("." KEY_NAME)*
            namespace: ("<" NAMESPACES ">")?
            KEY_NAME: /\\w+/
            NAMESPACES: /[isb]/

            %import common.ESCAPED_STRING   -> STRING
            %import common.SIGNED_NUMBER    -> NUMBER
            %import common.WS
            %ignore WS
        """
        self._parser = Lark(
            self._grammar, start="expr", parser='lalr'
        )
        self._namespaces = {}

    def record_namespace(self, key, namespace):
        if key not in self._namespaces.keys():
            self._namespaces[key] = namespace
        else:
            pass

    def resolve(self, key_path):
        t = KeyPathTransformer()
        for k, ns in self._namespaces.items():
            t.record_namespace(k, ns)
        return t.transform(self._parser.parse(key_path))


class StackTransformer(Transformer):
    """Provides an AST visitor. It is used to implement the feature of
    importing from netbox sub-elements joined together by certain parameters.
    """
    def __init__(self, vars_definition,
                 api_connector,
                 import_namespace,
                 sub_import_namespace):
        """Constructor of the stack transformer.

        Args:
            vars_definition (dict): The dict containing the definition
                of the vars referenced in the stack string
            api_connector (pynetbox.api): The connector to the netbox api
            import_namespace (dict): Namespace containing the vars from netbox
                for the current host
            sub_import_namespace (dict): Namespace containing the vars declared
                in the sub-import section.
        """
        self._api_connector = api_connector
        self._vars_definition = vars_definition
        self._import_namespace = import_namespace
        self._sub_import_namespace = sub_import_namespace

    def _import_var(self, parent_namespace, loading_var):
        # Access var configuration
        try:
            var_configuration = self._vars_definition[loading_var]
        except KeyError:
            # The var is not declared, exit program.
            sys.exit(
                "Bad key %s in sub-import section. Variable not defined in "
                "sub_import.vars section." % loading_var
            )

        # Access netbox API endpoint of wanted object
        app = getattr(self._api_connector, var_configuration['application'])
        endpoint = getattr(app, var_configuration['type'])

        # Resolve the actual filter value
        # ex: "device_id": "id" --> "device_id": 123
        filter_args = {}
        r = KeyPathResolver()
        r.record_namespace("i", parent_namespace)
        for k, v in var_configuration['filter'].items():
            try:
                filter_args[k] = r.resolve(v)
            except KeyError:
                sys.exit(
                    "The key given as a filter value '%s' does not exist." % v
                )

        # fetch sub elements from netbox
        if "id" in list(filter_args.keys()):
            elements = [endpoint.get(filter_args.get("id"))]
        else:
            elements = endpoint.filter(**filter_args)

        # Set the name of the key that will be used for index
        index_key_name = var_configuration['index']

        ret = {}
        for e in elements:
            # Resolve the actual index value
            index_value = getattr(e, index_key_name)

            if index_value in list(ret.keys()):
                # The index key must lead to a unique value, avoid duplicate
                # e[index_key] must be unique
                sys.exit(
                    "The key '%s', specified as an index key, is resolved to "
                    "non unique values." % index_value
                )
            ret[index_value] = dict(e)
        return ret

    def stack(self, n):
        return n[0]

    def nested_path(self, n):
        sub_pointer = [self._sub_import_namespace]
        parent_ns = self._import_namespace

        for path in map(lambda x: str(x), n):
            l = []
            for v in sub_pointer:
                if parent_ns is None:
                    parent_ns = v
                v[path] = self._import_var(
                    parent_namespace=parent_ns,
                    loading_var=path
                )
                l += list(v[path].values())
            parent_ns = None
            sub_pointer = l

        return self._sub_import_namespace


class KeyPathTransformer(Transformer):
    """The key path transformer is an AST visitor used to resolve expressions
    in the configuration file.
    """
    DEFAULT_NS = 'i'

    def __init__(self):
        """The constructor of the transformer

        Args:
            build_ns (dict): Namespace containing variables declared at
                hostvars loading phase
            import_ns (dict): Namespace containing variables returned by
                netbox
            sub_import_ns (dict): Namespace containing variables declared
                at sub import phase
        """
        self._namespaces = {}

    def record_namespace(self, key, namespace):
        if key not in self._namespaces.keys():
            self._namespaces[key] = namespace
        else:
            pass

    def expr(self, n):
        return n[0]

    def key_path(self, n):
        ns_selector = str(n[0])
        # Select the proper namespace to browse through
        try:
            selected_ns = self._namespaces[ns_selector]
        except KeyError:
            sys.exist("Unknown namespace")

        # Remove the namespace indication
        keys_list = n[1:]
        for key in keys_list:
            if selected_ns is None:
                break
            elif key in list(selected_ns.keys()):
                selected_ns = selected_ns[key]
            elif key == 'ALL' and key is keys_list[-1]:
                break
            else:
                sys.exit(
                    "Error: The key solving failed "
                    "in key_path '%s'" % (keys_list)
                )

        return selected_ns

    def sub(self, n):
        if n[0] is None:
            return None
        data_str = str(n[0])
        pattern = str(n[1]).strip("\"").strip("\'")
        repl = str(n[2]).strip("\"").strip("\'")
        if len(n) > 3:
            return re.sub(
                pattern, repl, data_str,
                *list(map(lambda x: int(x), n[3:]))
            )
        return re.sub(pattern, repl, data_str)

    def default_key(self, n):
        if n[0] is None:
            return n[1]
        else:
            return n[0]

    def namespace(self, n):
        if len(n):
            return str(n[0])
        return self.DEFAULT_NS


class InventoryBuilder:
    """Inventory Builder is the object that builds and return the inventory.

    Attributes:
        config_api (dict): The configuration of the api section
        config_data (dict): The configuration parsed from the configuration
                            file
        config_file (str): The path of thge configuration file
        host (str): The hostname if specified, None else
        imports (list): The list of import statements in the configuration file
        key_path_parser (Lark): The parser used to parse expressions in the
            configuration file
        list_mode (bool): The value of --list option
        nb (pynetbox.api): The netbox api connector
        stack_parser (Lark): The parser used to parse the stack string

    """
    def __init__(self, script_args, script_config):
        # Script args
        self._config_file = script_args.config_file
        self._host = script_args.host
        self._list_mode = script_args.list

        # Configuration file
        self._config_data = script_config['netbox']
        self._config_api = self._config_data['api']
        self._import_section = self._config_data.get('import', None)

        # Create the api connector
        self._nb = pynetbox.api(**self._config_api)

        # Expression resolutions objects

        stack_grammar = """
            stack: nested_path

            nested_path: VAR ("." VAR)*
            VAR: /\\w+/
        """

        self._stack_parser = Lark(
            stack_grammar, start="stack",
        )

    def _init_inventory(self):
        return {'_meta': {'hostvars': {}}}

    def build_inventory(self):
        """Build and return the inventory dict.

        Returns:
            dict: The inventory
        """
        # Check if both mode are deactivated
        if not self._list_mode and not self._host:
            return {}

        inventory = self._init_inventory()

        if self._list_mode:

            if self._import_section:
                # Check whether the import section exists.
                iterator = self._import_section
            else:
                # Set the default behaviour args
                iterator = {
                    "dcim": {
                        "devices": {}
                    }
                }

            # For each application, iterate over all inner object types
            for app_name, app_import in list(self._import_section.items()):
                for type_key, import_statement in app_import.items():
                    self._execute_import(
                        application=app_name,
                        import_type=type_key,
                        import_options=import_statement,
                        inventory=inventory
                    )

            return inventory
        else:
            # The host mode is on:
            #   If a host name is specified, return the inventory filled with
            #   only its information.
            #   A host is considered to be a device. Check if devices import
            #   options are set.
            device_import_options = (
                self._import_section
                .get('dcim', {})
                .get('devices', {})
            )

            self._execute_import(
                application='dcim',
                import_type='devices',
                import_options=device_import_options,
                inventory=inventory
            )

            return inventory['_meta']['hostvars'].get(self._host, {})

    def _execute_import(self, application, import_type,
                        import_options, inventory):
        """Fetch requested entities in Netbox.

        Args:
            application (str): The name of the netbox application.
                example: dcim, virtualization, etc.
            import_type (str): The type of objects to fetch from Netbox.
            import_options (str): The complementary arguments to refine search.
            inventory (dict): The inventory in which the information must be
                added.
        """
        # Access vars in config
        filters = import_options.get("filters", None)
        group_by = import_options.get('group_by', None)
        group_prefix = import_options.get('group_prefix', None)
        host_vars_section = import_options.get('host_vars', None)
        sub_import = import_options.get('sub_import', None)

        # Fetch the list of entities from Netbox
        netbox_hosts_list = self._get_elements_list(
            application,
            import_type,
            filters=filters,
            specific_host=self._host
        )

        # If the netbox hosts list fetching was successful, add the elements to
        # the inventory.
        for host in netbox_hosts_list:
            # Compute an id for the given host data
            element_index = self._get_identifier(dict(host), import_type)
            # Add the element to the propper group(s)
            self._add_element_to_inventory(
                element_index=element_index,
                host_dict=dict(host),
                inventory=inventory,
                obj_type=import_type,
                group_by=group_by,
                group_prefix=group_prefix,
                host_vars=host_vars_section,
                sub_import=sub_import
            )

    def _load_element_vars(self, element_name, inventory,
                           host_vars,
                           build_ns,
                           import_ns,
                           sub_import_ns):
        """Enrich build namespace with hostvars configuration.
        """
        # If there is no required host var to load, do nothing.
        if host_vars:
            # Iterate over every key value pairs in host_vars required in
            # config file
            for d in host_vars:
                for key, value in d.items():
                    try:
                        build_ns[key] = self._resolve_expression(
                            key_path=value,
                            build_ns=build_ns,
                            import_ns=import_ns,
                            sub_import_ns=sub_import_ns
                        )
                    except KeyError:
                        sys.exit("Error: Key '%s' not found" % value)

            # Add the loaded variables in the inventory under the proper
            # section (name of the host)
            inventory['_meta']['hostvars'].update(
                {element_name: dict(build_ns)}
            )

    def _execute_sub_import(self, sub_import, import_namespace,
                            sub_import_namespace):
        """Enrich sub import namespace with configured sub elements
        from netbox.
        """
        # Extract stack string
        stack_string = sub_import['stack']

        # Extract vars definition
        vars_definition = {}
        for i in sub_import['vars']:
            vars_definition.update(i)

        t = StackTransformer(
            api_connector=self._nb,
            vars_definition=vars_definition,
            import_namespace=import_namespace,
            sub_import_namespace=sub_import_namespace
        )
        return t.transform(self._stack_parser.parse(stack_string))

    def _add_element_to_inventory(self, element_index, host_dict, inventory,
                                  obj_type, group_by=None, group_prefix=None,
                                  host_vars=None, sub_import=None):
        # Declare namespaces
        build_namespace = {}
        import_namespace = dict(host_dict)
        sub_import_namespace = {}

        # Handle sub imports
        for imports in sub_import:
            self._execute_sub_import(
                imports,
                import_namespace,
                sub_import_namespace
            )

        # Load the host vars in the inventory
        self._load_element_vars(
            element_index,
            inventory,
            host_vars,
            build_namespace,
            import_namespace,
            sub_import_namespace
        )
        # Add the host to its main type group (devices, racks, etc.)
        # and to the group 'all'
        self._add_element_to_group(
            element_name=element_index, group_name=obj_type,
            inventory=inventory
        )
        self._add_element_to_group(
            element_name=element_index, group_name='all', inventory=inventory
        )

        # If the group_by option is specified, insert the element in the
        # propper groups.
        if group_by:
            # Iterate over every groups
            for group in group_by:
                # The 'tags' field is a list, a second iteration must be
                # performed at a deeper level
                if group == 'tags':
                    # Iterate over every tag
                    for tag in host_dict.get(group):
                        # Add the optional prefix
                        if group_prefix is None:
                            group_name = tag
                        else:
                            group_name = group_prefix + tag
                        # Insert the element in the propper group
                        self._add_element_to_group(
                            element_name=element_index,
                            group_name=group_name,
                            inventory=inventory
                        )
                else:
                    # Check that the specified group points towards an
                    # actual value
                    group_name = self._resolve_expression(
                        key_path=group,
                        build_ns=build_namespace,
                        import_ns=import_namespace,
                        sub_import_ns=sub_import_namespace
                    )
                    if group_name is not None:
                        # Add the optional prefix
                        if group_prefix:
                            group_name = group_prefix + group_name
                        # Insert the element in the propper group
                        self._add_element_to_group(
                            element_name=element_index,
                            group_name=group_name,
                            inventory=inventory
                        )

    def _get_elements_list(self, application, object_type,
                           filters=None, specific_host=None):
        """Retrieves a list of element from netbox API.

        Returns:
            A list of all elements from netbox API.

        Args:
            application (str): The name of the netbox application
            object_type (str): The type of object to import
            filters (dict, optional): The filters to pass on to pynetbox calls
            specific_host (str, optional): The name of a specific host which
                host vars must be returned alone.
        """
        app_obj = getattr(self._nb, application)
        endpoint = getattr(app_obj, object_type)

        # specific host handling
        if specific_host is not None:
            result = endpoint.filter(name=specific_host)
        elif filters is not None:
            if "id" in list(filters.keys()):
                result = [endpoint.get(filters.get("id"))]
            else:
                result = endpoint.filter(**filters)
        else:
            result = endpoint.all()

        return result

    def _add_element_to_group(self, element_name, group_name, inventory):
        # FIXME: Add comments
        self._initialize_group(group_name=group_name, inventory=inventory)
        if element_name not in inventory.get(group_name).get('hosts'):
            inventory[group_name]['hosts'].append(element_name)

    def _get_identifier(self, host, obj_type):
        """Return an identifier for the given host.

        Args:
            host (str): The name of the host
            obj_type (str): The type of the object

        Returns:
            str: The computed id
        """
        # Get the 'name' field value of the specified host
        r = host.get('name')
        # If the 'name' field is empty, compute the id :
        #   <object type>_<id in netbox db>
        if r is None or r == "":
            r = "%s_%s" % (obj_type, host.get('id'))
        return r

    def _initialize_group(self, group_name, inventory):
        """
        Args:
            group_name (str): The group to be initialized
            inventory (dict): The inventory including the group

        Returns:
            The updated inventory
        """
        # Initialize the group in the inventory
        inventory.setdefault(group_name, {})
        # Initialize the host field of the group
        inventory[group_name].setdefault('hosts', [])
        return inventory

    def _resolve_expression(self, key_path,
                            build_ns, import_ns, sub_import_ns):
        """Resolve the given key path to a value.

        Args:
            key_path (str): The path toward the key
            data (dict): The actual host data dict that holds
                the target value.

        Returns:
            The target value
        """
        # FIXME: Correct comments
        r = KeyPathResolver()
        r.record_namespace("b", build_ns)
        r.record_namespace("i", import_ns)
        r.record_namespace("s", sub_import_ns)
        return r.resolve(key_path)

        # t = KeyPathTransformer(
        #     build_ns=build_ns,
        #     import_ns=import_ns,
        #     sub_import_ns=sub_import_ns
        # )
        # return t.transform(self._key_path_parser.parse(key_path))


def parse_cli_args(script_args):
    """Declare and configure script argument parser

    Args:
            script_args (list): The list of script arguments

    Returns:
            obj: The parsed arguments in an object. See argparse documention
                 (https://docs.python.org/3.7/library/argparse.html)
                 for more information.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config-file',
        default=os.getenv(DEFAULT_ENV_CONFIG_FILE, "netbox.yml"),
        help="""Path for script's configuration file. If None is specified,
                default value is %s environment variable or netbox.yml in the
                current dir.""" % DEFAULT_ENV_CONFIG_FILE
    )
    parser.add_argument(
        '--list', action='store_true', default=False,
        help="""Print the entire inventory with hostvars respecting
                the Ansible dynamic inventory syntax."""
    )
    parser.add_argument(
        '--host', action='store', default=None,
        help="""Print specific host vars as Ansible dynamic
                inventory syntax."""
    )

    # Parse script arguments and return the result
    return parser.parse_args(script_args)


def validate_configuration(configuration):
    """Validate the configuration structure. If no error is found, nothing
    happens.

    Args:
            configuration (dict): The parsed configuration
    """
    sub_import_def = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "required": ["stack", "vars"],
            "properties": {
                "stack": {
                    "type": "string"
                },
                "vars": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "maxProperties": 1,
                        "minProperties": 1,
                        "patternProperties": {
                            "\\w+": {
                                "type": "object",
                                "properties": {
                                    "application": {
                                        "type": "string"
                                    },
                                    "type": {
                                        "type": "string"
                                    },
                                    "index": {
                                        "type": "string"
                                    },
                                    "filter": {
                                        "type": "object",
                                        "patternProperties": {
                                            "\\w+": {
                                                "type": "string"
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    config_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "http://example.com/product.schema.json",
        "title": "Configuration file",
        "description": "The configuration file of the dynamic inventory",
        "type": "object",
        "properties": {
            "netbox": {
                "type": "object",
                "description": "The base key of the configuration file",
                "properties": {
                    "api": {
                        "type": "object",
                        "description": (
                            "The section holding information used "
                            "to connect to netbox api"
                        ),
                        "additionalProperties": False,
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "The url of netbox api"
                            },
                            "token": {
                                "type": "string",
                                "description": "The netbox token to use"
                            },
                            "private_key": {
                                "type": "string",
                                "description": "The private key"
                            },
                            "private_key_file": {
                                "type": "string",
                                "description": "The private key file"
                            },
                            "ssl_verify": {
                                "type": "boolean",
                                "description": (
                                    "Specify SSL verification behavior"
                                )
                            }
                        },
                        "required": ["url"],
                        "allOf": [
                            {
                                "not": {
                                    "type": "object",
                                    "required": [
                                        "private_key",
                                        "private_key_file"
                                    ]
                                }
                            }
                        ]
                    },
                    "import": {
                        "type": "object",
                        "description": "The netbox application",
                        "minProperties": 1,
                        "additionalProperties": False,
                        "patternProperties": {
                            "\\w+": {
                                "type": "object",
                                "description": "The import section",
                                "minProperties": 1,
                                "additionalProperties": False,
                                "patternProperties": {
                                    "\\w+": {
                                        "type": "object",
                                        "minProperties": 1,
                                        "additionalProperties": False,
                                        "properties": {
                                            "group_by": {
                                                "type": "array",
                                                "minItems": 1,
                                                "items": {
                                                    "type": "string"
                                                }
                                            },
                                            "sub_import": sub_import_def,
                                            "group_prefix": {
                                                "type": "string",
                                            },
                                            "filters": {
                                                "type": "object",
                                                "minProperties": 1
                                            },
                                            "host_vars": {
                                                "type": "array",
                                                "minItems": 1,
                                                "items": {
                                                    "type": "object",
                                                    "minProperties": 1,
                                                    "maxProperties": 1
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
                "required": ["api"]
            }
        },
        "required": ["netbox"]
    }

    return validate(instance=configuration, schema=config_schema)


def load_config_file(config_file_path):
    """ Load the configuration file and returns its parsed content.

    Args:
            config_file_path (str): The path towards the configuration file
    """
    try:
        with open(config_file_path, 'r') as file:
            parsed_config = yaml.safe_load(file)
    except IOError as io_error:
        # Handle file level exception
        sys.exit("Error: Cannot open configuration file.\n%s" % io_error)
    except yaml.YAMLError as yaml_error:
        # Handle Yaml level exceptions
        sys.exit("Error: Unable to parse configuration file: %s" %
                 yaml_error)

    # If syntax of configuration file is valid, nothing happens
    # Beware, syntax can be valid while semantic is not
    validate_configuration(parsed_config)

    return parsed_config


def dump_json_inventory(inventory):
    """Dumps the given inventory in json

    Args:
            inventory (dict): The inventory
    """
    print(json.dumps(inventory))


def main():
    # Parse cli args
    args = parse_cli_args(sys.argv[1:])
    # Parse the configuration file
    configuration = load_config_file(args.config_file)

    # Build the inventory
    builder = InventoryBuilder(args, configuration)
    # Print the JSON formatted inventory dict
    dump_json_inventory(builder.build_inventory())

if __name__ == '__main__':
    main()
