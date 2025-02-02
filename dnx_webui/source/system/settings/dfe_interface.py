#!/usr/bin/python3

from __future__ import annotations

import dnx_iptools.interface_ops as interface

from source.web_typing import *
from source.web_validate import *

from dnx_gentools.def_constants import HOME_DIR
from dnx_gentools.def_enums import CFG, DATA, INTF
from dnx_gentools.file_operations import load_data, load_configuration, config, ConfigurationManager, json_to_yaml

from dnx_control.control.ctl_action import system_action

from dnx_iptools.cprotocol_tools import itoip, default_route

from source.web_interfaces import StandardWebPage

__all__ = ('WebPage', 'get_interfaces')

_IP_DISABLED = True

class WebPage(StandardWebPage):
    '''
    available methods: load, update
    '''
    @staticmethod
    def load(_: Form) -> dict[str, Any]:
        system_settings: ConfigChain = load_configuration('system', cfg_type='global')

        wan_ident: str = system_settings['interfaces->builtin->wan->ident']
        wan_state: int = system_settings['interfaces->builtin->wan->state']
        default_mac:    str = system_settings['interfaces->builtin->wan->default_mac']
        configured_mac: str = system_settings['interfaces->builtin->wan->default_mac']

        try:
            ip_addr = itoip(interface.get_ipaddress(interface=wan_ident))
        except OverflowError:
            ip_addr = 'NOT SET'

        try:
            netmask = itoip(interface.get_netmask(interface=wan_ident))
        except OverflowError:
            netmask = 'NOT SET'

        return {
            'mac': {
                'default': default_mac,
                'current': configured_mac if configured_mac else default_mac
            },
            'ip': {
                'state': wan_state,
                'ip_address': ip_addr,
                'netmask': netmask,
                'default_gateway': itoip(default_route())
            },
            'interfaces': get_interfaces()
        }

    @staticmethod
    def update(form: Form) -> tuple[int, str]:
        if ('wan_state_update' in form):

            wan_state = form.get('wan_state_update', DATA.MISSING)
            if (wan_state is DATA.MISSING):
                return 1, INVALID_FORM

            try:
                wan_state = INTF(convert_int(wan_state))
            except (ValidationError, KeyError):
                return 2, INVALID_FORM

            else:
                set_wan_interface(wan_state)

        elif ('wan_ip_update' in form):
            wan_ip_settings = config(**{
                'ip': form.get('wan_ip', DATA.MISSING),
                'cidr': form.get('wan_cidr', DATA.MISSING),
                'dfg': form.get('wan_dfg', DATA.MISSING)
            })

            if (DATA.MISSING in wan_ip_settings.values()):
                return 3, INVALID_FORM

            try:
                ip_address(wan_ip_settings.ip)
                cidr(wan_ip_settings.cidr)
                ip_address(wan_ip_settings.dfg)
            except ValidationError as ve:
                return 4, ve.message

            set_wan_ip(wan_ip_settings)

        elif (_IP_DISABLED):
            return 98, 'wan interface configuration currently disabled for system rework.'

        elif ('wan_mac_update' in form):

            mac_addr = form.get('ud_wan_mac', DATA.MISSING)
            if (mac_addr is DATA.MISSING):
                return 5, INVALID_FORM

            try:
                mac_address(mac_addr)
            except ValidationError as ve:
                return 6, ve.message
            else:
                set_wan_mac(CFG.ADD, mac_address=mac_address)

        elif ('wan_mac_restore' in form):
            set_wan_mac(CFG.DEL)

        else:
            return 99, INVALID_FORM

        return NO_STANDARD_ERROR

# ==============
# CONFIGURATION
# ==============
# TODO: TEST THIS
def set_wan_interface(intf_type: INTF = INTF.DHCP):
    '''Change wan interface state between static or dhcp.

    1. Configure interface type
    2. Create netplan config from template
    3. Move file to /etc/netplan

    This does not configure an ip address of the interface when setting to static. see: set_wan_ip()
    '''
    # changing dhcp status of wan interface in config file.
    with ConfigurationManager('system') as dnx:
        dnx_settings: ConfigChain = dnx.load_configuration()

        wan_ident: str = dnx_settings['interfaces->builtin->wan->ident']

        # template used to generate yaml file with user configured fields
        intf_template: dict = load_data('interfaces.cfg', filepath='dnx_profile/interfaces')

        # for static dhcp4 and dhcp_overrides keys are removed and creating an empty list for the addresses.
        # NOTE: the ip configuration will unlock after the switch and can then be updated
        if (intf_type is INTF.STATIC):
            wan_intf: dict = intf_template['network']['ethernets'][wan_ident]

            wan_intf.pop('dhcp4')
            wan_intf.pop('dhcp4-overrides')

            # initializing static, but not configuring an ip address
            wan_intf['addresses'] = '[]'

        _configure_netplan(intf_template)

        # TODO: writing state change after file has been replaced because any errors prior to this will prevent
        #  configuration from taking effect.
        #  the trade off is that the process could replace the file, but not set the wan state (configuration mismatch)
        dnx_settings['interfaces->builtin->wan->state'] = intf_type
        dnx.write_configuration(dnx_settings.expanded_user_data)

        system_action(module='webui', command='netplan apply', args='')

def get_interfaces() -> dict:
    '''loading installed system interfaces, then returning dict with "built-in" and "extended" separated.
    '''
    configured_intfs: dict = load_configuration('system', cfg_type='global').get_dict('interfaces')

    # this will filter out any interface slot that does not have an associated interface
    builtin_intfs: dict[str, str] = {
        intf['ident']: intf for intf in configured_intfs['builtin'].values() if intf['ident']
    }
    extended_intfs: dict[str, str] = {
        intf['ident']: intf for intf in configured_intfs['extended'].values() if intf['ident']
    }

    # intf values -> [ ["general info"], ["transmit"], ["receive"] ]
    system_interfaces = {'builtin': [], 'extended': [], 'unassociated': []}

    with open('/proc/net/dev', 'r') as netdev:
        detected_intfs = netdev.readlines()

    # skipping identifier column names
    for intf in detected_intfs[2:]:

        data = intf.split()
        name = data[0][:-1]  # removing the ":"

        intf_cfg: Optional[dict[str, str]]

        if intf_cfg := builtin_intfs.get(name, None):

            system_interfaces['builtin'].append([
                [name, intf_cfg['zone'], intf_cfg.get('subnet', 'n/a')], [data[1], data[2]], [data[9], data[10]]
            ])

        elif intf_cfg := extended_intfs.get(name, None):

            system_interfaces['extended'].append([
                [name, intf_cfg['zone'], intf_cfg['subnet']], [data[1], data[2]], [data[9], data[10]]
            ])

        # functional else with loopback filter
        elif (name != 'lo'):
            system_interfaces['unassociated'].append([[name, 'none', 'none'], [data[1], data[2]], [data[9], data[10]]])

    return system_interfaces

# TODO: fix this later
# def set_wan_mac(action: CFG, mac_address: Optional[str] = None):
#     with ConfigurationManager('system') as dnx:
#         dnx_settings = dnx.load_configuration()
#
#         wan_settings = dnx_settings['interfaces']['builtin']['wan']
#
#         new_mac = mac_address if action is CFG.ADD else wan_settings['default_mac']
#
#         wan_int = wan_settings['ident']
#         # iterating over the necessary command args, then sending over local socket
#         # for control service to issue the commands
#         args = [f'{wan_int} down', f'{wan_int} hw ether {new_mac}', f'{wan_int} up']
#         for arg in args:
#
#             system_action(module='webui', command='ifconfig', args=arg)
#
#         wan_settings['configured_mac'] = mac_address
#
#         dnx.write_configuration(dnx_settings)

def set_wan_ip(wan_settings: config) -> None:
    '''
    Modify configured WAN interface IP address.

    1. Loads configured DNS servers
    2. Loads wan interface identity
    3. Create netplan config from template
    4. Move file to /etc/netplan
    '''
    dnx_settings: ConfigChain = load_configuration('system', cfg_type='global')

    wan_ident: str = dnx_settings['interfaces->builtin->wan->ident']

    intf_template: dict = load_data('interfaces.cfg', filepath='dnx_profile/interfaces')

    # removing dhcp4 and dhcp_overrides keys, then adding ip address value
    wan_intf: dict = intf_template['network']['ethernets'][wan_ident]

    wan_intf.pop('dhcp4')
    wan_intf.pop('dhcp4-overrides')

    wan_intf['addresses'] = f'[{wan_settings.ip}/{wan_settings.cidr}]'
    wan_intf['gateway4']  = f'{wan_settings.dfg}'

    _configure_netplan(intf_template)

    system_action(module='webui', command='netplan apply')

def _configure_netplan(intf_config: dict) -> None:
    '''writes modified template to file and moves it to /etc/netplan using os.replace.

        note: this does NOT run "netplan apply"
    '''
    # grabbing configured dns servers
    dns_server_settings: ConfigChain = load_configuration('dns_server', cfg_type='global')

    dns1: str = dns_server_settings['resolvers->primary->ip_address']
    dns2: str = dns_server_settings['resolvers->secondary->ip_address']

    # creating YAML string and applying loaded server values
    converted_config = json_to_yaml(intf_config)
    converted_config = converted_config.replace('_PRIMARY__SECONDARY_', f'{dns1},{dns2}')

    # writing YAML to interface folder to be used as swap
    with open(f'{HOME_DIR}/dnx_profile/interfaces/01-dnx-interfaces.yaml', 'w') as dnx_intfs:
        dnx_intfs.write(converted_config)

    # sending replace command to system control service
    cmd_args = [f'{HOME_DIR}/dnx_profile/interfaces/01-dnx-interfaces.yaml', '/etc/netplan/01-dnx-interfaces.yaml']
    system_action(module='webui', command='os.replace', args=cmd_args)
