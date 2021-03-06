# Copyright (c) 2017 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import collections
import eventlet
import six
import time

from kuryr.lib._i18n import _
from neutronclient.common import exceptions as n_exc
from oslo_config import cfg as oslo_cfg
from oslo_log import log as logging

from kuryr_kubernetes import clients
from kuryr_kubernetes.controller.drivers import base
from kuryr_kubernetes import exceptions

LOG = logging.getLogger(__name__)

# Moved out from neutron_default group
vif_pool_driver_opts = [
    oslo_cfg.IntOpt('ports_pool_max',
        help=_("Set a maximun amount of ports per pool. 0 to disable"),
        default=0),
    oslo_cfg.IntOpt('ports_pool_min',
        help=_("Set a target minimum size of the pool of ports"),
        default=5),
    oslo_cfg.IntOpt('ports_pool_batch',
        help=_("Number of ports to be created in a bulk request"),
        default=10),
    oslo_cfg.IntOpt('ports_pool_update_frequency',
        help=_("Minimun interval (in seconds) between pool updates"),
        default=20),
]

oslo_cfg.CONF.register_opts(vif_pool_driver_opts, "vif_pool")


class NoopVIFPool(base.VIFPoolDriver):
    """No pool VIFs for Kubernetes Pods"""

    def set_vif_driver(self, driver):
        self._drv_vif = driver

    def request_vif(self, pod, project_id, subnets, security_groups):
        return self._drv_vif.request_vif(pod, project_id, subnets,
                                         security_groups)

    def release_vif(self, pod, vif, *argv):
        self._drv_vif.release_vif(pod, vif)

    def activate_vif(self, pod, vif):
        self._drv_vif.activate_vif(pod, vif)


@six.add_metaclass(abc.ABCMeta)
class BaseVIFPool(base.VIFPoolDriver):
    """Skeletal pool driver.

    In order to handle the pools of ports, a few dicts are used:
    _available_ports_pool is a dictionary with the ready to use Neutron ports
    information. The keys are the 'pool_key' and the values the 'port_id's.
    _existing_vifs is a dictionary containing the port vif objects. The keys
    are the 'port_id' and the values are the vif objects.
    _recyclable_ports is a dictionary with the Neutron ports to be
    recycled. The keys are the 'port_id' and their values are the 'pool_key'.
    _last_update is a dictionary with the timestamp of the last population
    action for each pool. The keys are the pool_keys and the values are the
    timestamps.

    The following driver configuration options exist:
    - ports_pool_max: it specifies how many ports can be kept at each pool.
    If the pool already reached the specified size, the ports to be recycled
    are deleted instead. If set to 0, the limit is disabled and ports are
    always recycled.
    - ports_pool_min: minimum desired number of ready to use ports at populated
    pools. Should be smaller than ports_pool_max (if enabled).
    - ports_pool_batch: target number of ports to be created in bulk requests
    when populating pools.
    - ports_pool_update_frequency: interval in seconds between ports pool
    updates, both for populating pools as well as for recycling ports.
    """
    _available_ports_pools = collections.defaultdict(collections.deque)
    _existing_vifs = collections.defaultdict(collections.defaultdict)
    _recyclable_ports = collections.defaultdict(collections.defaultdict)
    _last_update = collections.defaultdict(collections.defaultdict)

    def __init__(self):
        # Note(ltomasbo) Execute the port recycling periodic actions in a
        # background thread
        eventlet.spawn(self._return_ports_to_pool)

    def set_vif_driver(self, driver):
        self._drv_vif = driver

    def activate_vif(self, pod, vif):
        self._drv_vif.activate_vif(pod, vif)

    def _get_pool_size(self, pool_key=None):
        return len(self._available_ports_pools.get(pool_key, []))

    def request_vif(self, pod, project_id, subnets, security_groups):
        try:
            host_addr = pod['status']['hostIP']
        except KeyError:
            LOG.warning("Pod has not been scheduled yet.")
            raise
        pool_key = (host_addr, project_id, tuple(security_groups))

        try:
            return self._get_port_from_pool(pool_key, pod, subnets)
        except exceptions.ResourceNotReady as ex:
            LOG.warning("Ports pool does not have available ports!")
            eventlet.spawn(self._populate_pool, pool_key, pod, subnets)
            raise ex

    def _populate_pool(self, pool_key, pod, subnets):
        # REVISIT(ltomasbo): Drop the subnets parameter and get the information
        # from the pool_key, which will be required when multi-network is
        # supported
        now = time.time()
        if (now - oslo_cfg.CONF.vif_pool.ports_pool_update_frequency <
                self._last_update.get(pool_key, 0)):
            LOG.info("Not enough time since the last pool update")
            return
        self._last_update[pool_key] = now

        pool_size = self._get_pool_size(pool_key)
        if pool_size < oslo_cfg.CONF.vif_pool.ports_pool_min:
            num_ports = max(oslo_cfg.CONF.vif_pool.ports_pool_batch,
                oslo_cfg.CONF.vif_pool.ports_pool_min - pool_size)
            vifs = self._drv_vif.request_vifs(pod=pod,
                project_id=pool_key[1],
                subnets=subnets,
                security_groups=list(pool_key[2]),
                num_ports=num_ports)
            for vif in vifs:
                self._existing_vifs[vif.id] = vif
                self._available_ports_pools.setdefault(pool_key,
                                                       []).append(vif.id)

    def release_vif(self, pod, vif, project_id, security_groups):
        host_addr = pod['status']['hostIP']
        pool_key = (host_addr, project_id, tuple(security_groups))

        if not self._existing_vifs.get(vif.id):
            self._existing_vifs[vif.id] = vif
        self._recyclable_ports[vif.id] = pool_key


class NeutronVIFPool(BaseVIFPool):
    """Manages VIFs for Bare Metal Kubernetes Pods."""

    def _get_port_from_pool(self, pool_key, pod, subnets):
        try:
            port_id = self._available_ports_pools[pool_key].pop()
        except IndexError:
            raise exceptions.ResourceNotReady(pod)
        neutron = clients.get_neutron_client()
        neutron.update_port(port_id,
            {
                "port": {
                    'name': pod['metadata']['name'],
                    'device_id': pod['metadata']['uid']
                }
            })
        # check if the pool needs to be populated
        if (self._get_pool_size(pool_key) <
                oslo_cfg.CONF.vif_pool.ports_pool_min):
            eventlet.spawn(self._populate_pool, pool_key, pod, subnets)
        return self._existing_vifs[port_id]

    def _return_ports_to_pool(self):
        """Recycle ports to be reused by future pods.

        For each port in the recyclable_ports dict it reaplies
        security group and changes the port name to available_port.
        Upon successful port update, the port_id is included in the dict
        with the available_ports.

        If a maximun number of port per pool is set, the port will be
        deleted if the maximun has been already reached.
        """
        neutron = clients.get_neutron_client()
        while True:
            for port_id, pool_key in self._recyclable_ports.copy().items():
                if (not oslo_cfg.CONF.vif_pool.ports_pool_max or
                    self._get_pool_size(pool_key) <
                        oslo_cfg.CONF.vif_pool.ports_pool_max):
                    try:
                        neutron.update_port(port_id,
                            {
                                "port": {
                                    'name': 'available-port',
                                    'device_id': '',
                                    'security_groups': list(pool_key[2])
                                }
                            })
                    except n_exc.NeutronClientException:
                        LOG.warning("Error preparing port %s to be reused, put"
                                    " back on the cleanable pool.", port_id)
                        continue
                    self._available_ports_pools.setdefault(
                        pool_key, []).append(port_id)
                else:
                    try:
                        del self._existing_vifs[port_id]
                        neutron.delete_port(port_id)
                    except n_exc.PortNotFoundClient:
                        LOG.debug('Unable to release port %s as it no longer '
                                  'exists.', port_id)
                    except KeyError:
                        LOG.debug('Port %s is not in the ports list.', port_id)
                del self._recyclable_ports[port_id]
            eventlet.sleep(oslo_cfg.CONF.vif_pool.ports_pool_update_frequency)


class NestedVIFPool(BaseVIFPool):
    """Manages VIFs for nested Kubernetes Pods.

    In order to handle the pools of ports for nested Pods, an extra dict is
    used:
    _known_trunk_ids is a dictionary that keeps the trunk port ids associated
    to each pool_key to skip calls to neutron to get the trunk information.
    """
    _known_trunk_ids = collections.defaultdict(str)

    def _get_port_from_pool(self, pool_key, pod, subnets):
        try:
            port_id = self._available_ports_pools[pool_key].pop()
        except IndexError:
            raise exceptions.ResourceNotReady(pod)
        neutron = clients.get_neutron_client()
        neutron.update_port(port_id,
            {
                "port": {
                    'name': pod['metadata']['name'],
                }
            })
        # check if the pool needs to be populated
        if (self._get_pool_size(pool_key) <
                oslo_cfg.CONF.vif_pool.ports_pool_min):
            eventlet.spawn(self._populate_pool, pool_key, pod, subnets)
        return self._existing_vifs[port_id]

    def _return_ports_to_pool(self):
        """Recycle ports to be reused by future pods.

        For each port in the recyclable_ports dict it reaplies
        security group and changes the port name to available_port.
        Upon successful port update, the port_id is included in the dict
        with the available_ports.

        If a maximun number of ports per pool is set, the port will be
        deleted if the maximun has been already reached.
        """
        neutron = clients.get_neutron_client()
        while True:
            for port_id, pool_key in self._recyclable_ports.copy().items():
                if (not oslo_cfg.CONF.vif_pool.ports_pool_max or
                    self._get_pool_size(pool_key) <
                        oslo_cfg.CONF.vif_pool.ports_pool_max):
                    try:
                        neutron.update_port(port_id,
                            {
                                "port": {
                                    'name': 'available-port',
                                    'security_groups': list(pool_key[2])
                                }
                            })
                    except n_exc.NeutronClientException:
                        LOG.warning("Error preparing port %s to be reused, put"
                                    " back on the cleanable pool.", port_id)
                        continue
                    self._available_ports_pools.setdefault(
                        pool_key, []).append(port_id)
                else:
                    trunk_id = self._known_trunk_ids.get(pool_key, None)
                    if not trunk_id:
                        p_port = self._drv_vif._get_parent_port_by_host_ip(
                            neutron, pool_key[0])
                        trunk_id = self._drv_vif._get_trunk_id(p_port)
                        self._known_trunk_ids[pool_key] = trunk_id
                    try:
                        self._drv_vif._remove_subport(neutron, trunk_id,
                                                      port_id)
                        self._drv_vif._release_vlan_id(
                            self._existing_vifs[port_id].vlan_id)
                        del self._existing_vifs[port_id]
                        neutron.delete_port(port_id)
                    except n_exc.PortNotFoundClient:
                        LOG.debug('Unable to release port %s as it no longer '
                                  'exists.', port_id)
                    except KeyError:
                        LOG.debug('Port %s is not in the ports list.', port_id)
                    except n_exc.NeutronClientException:
                        LOG.warning('Error removing the subport %s', port_id)
                        continue
                del self._recyclable_ports[port_id]
            eventlet.sleep(oslo_cfg.CONF.vif_pool.ports_pool_update_frequency)
