#    Copyright 2013 Mirantis, Inc.
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

import os
import re

from devops.helpers.helpers import wait
from proboscis.asserts import assert_equal
from proboscis.asserts import assert_false
from proboscis.asserts import assert_true
from proboscis import SkipTest
from proboscis import test

from fuelweb_test.helpers import checkers
from devops.helpers.helpers import tcp_ping
from fuelweb_test.helpers.decorators import log_snapshot_after_test
from fuelweb_test.helpers.eb_tables import Ebtables
from fuelweb_test.helpers import os_actions
from fuelweb_test.settings import CLASSIC_PROVISIONING
from fuelweb_test.settings import DEPLOYMENT_MODE
from fuelweb_test.settings import NODE_VOLUME_SIZE
from fuelweb_test.settings import OPENSTACK_RELEASE
from fuelweb_test.settings import OPENSTACK_RELEASE_UBUNTU
from fuelweb_test.tests.base_test_case import SetupEnvironment
from fuelweb_test.tests.base_test_case import TestBasic
from fuelweb_test import logger
from fuelweb_test.tests.test_ha_one_controller_base\
    import HAOneControllerFlatBase


@test(groups=["thread_2"])
class OneNodeDeploy(TestBasic):
    """OneNodeDeploy."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_release],
          groups=["deploy_one_node", 'master'])
    @log_snapshot_after_test
    def deploy_one_node(self):
        """Deploy cluster with controller node only

        Scenario:
            1. Create cluster
            2. Add 1 node with controller role
            3. Deploy the cluster
            4. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs

        Duration 20m

        """
        self.env.revert_snapshot("ready")
        self.fuel_web.client.get_root()
        self.env.bootstrap_nodes(
            self.env.d_env.nodes().slaves[:1])

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE
        )
        logger.info('cluster is %s' % str(cluster_id))
        self.fuel_web.update_nodes(
            cluster_id,
            {'slave-01': ['controller']}
        )
        self.fuel_web.deploy_cluster_wait(cluster_id)
        os_conn = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id))
        self.fuel_web.assert_cluster_ready(
            os_conn, smiles_count=4, networks_count=1, timeout=300)
        self.fuel_web.run_single_ostf_test(
            cluster_id=cluster_id, test_sets=['sanity'],
            test_name=('fuel_health.tests.sanity.test_sanity_identity'
                       '.SanityIdentityTest.test_list_users'))


@test(groups=["thread_2"])
class HAOneControllerFlat(HAOneControllerFlatBase):
    """HAOneControllerFlat."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["smoke", "deploy_ha_one_controller_flat",
                  "ha_one_controller_nova_flat", "smoke_nova"])
    @log_snapshot_after_test
    def deploy_ha_one_controller_flat(self):
        """Deploy cluster in HA mode (one controller) with flat nova-network

        Scenario:
            1. Create cluster in HA mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Deploy the cluster
            5. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs
            6. Verify networks
            7. Verify network configuration on controller
            8. Run OSTF

        Duration 30m
        Snapshot: deploy_ha_one_controller_flat
        """
        super(self.__class__, self).deploy_ha_one_controller_flat_base()

    @test(enabled=False, depends_on=[deploy_ha_one_controller_flat],
          groups=["ha_one_controller_flat_create_instance"])
    @log_snapshot_after_test
    def ha_one_controller_flat_create_instance(self):
        """Create instance with file injection

         Scenario:
            1. Revert "ha one controller flat" environment
            2. Create instance with file injection
            3. Assert instance was created
            4. Assert file is on instance

        Duration 20m

        """
        self.env.revert_snapshot("deploy_ha_one_controller_flat")
        data = {
            'tenant': 'novaSimpleFlat',
            'user': 'novaSimpleFlat',
            'password': 'novaSimpleFlat'
        }
        cluster_id = self.fuel_web.get_last_created_cluster()
        os = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id),
            data['user'], data['password'], data['tenant'])

        _ip = self.fuel_web.get_nailgun_node_by_name("slave-01")['ip']
        remote = self.env.d_env.get_ssh_to_remote(_ip)
        remote.execute("echo 'Hello World' > /root/test.txt")
        server_files = {"/root/test.txt": 'Hello World'}
        instance = os.create_server_for_migration(file=server_files)
        floating_ip = os.assign_floating_ip(instance)
        wait(lambda: tcp_ping(floating_ip.ip, 22), timeout=120)
        res = os.execute_through_host(
            remote,
            floating_ip.ip, "sudo cat /root/test.txt")
        assert_true(res == 'Hello World', 'file content is {0}'.format(res))

    @test(depends_on=[deploy_ha_one_controller_flat],
          groups=["ha_one_controller_flat_node_deletion"])
    @log_snapshot_after_test
    def ha_one_controller_flat_node_deletion(self):
        """Remove compute from cluster in ha mode with flat nova-network

         Scenario:
            1. Revert "deploy_ha_one_controller_flat" environment
            2. Remove compute node
            3. Deploy changes
            4. Verify node returns to unallocated pull

        Duration 8m

        """
        self.env.revert_snapshot("deploy_ha_one_controller_flat")

        cluster_id = self.fuel_web.get_last_created_cluster()
        nailgun_nodes = self.fuel_web.update_nodes(
            cluster_id, {'slave-02': ['compute']}, False, True)
        task = self.fuel_web.deploy_cluster(cluster_id)
        self.fuel_web.assert_task_success(task)
        nodes = filter(lambda x: x["pending_deletion"] is True, nailgun_nodes)
        assert_true(
            len(nodes) == 1, "Verify 1 node has pending deletion status"
        )
        wait(
            lambda: self.fuel_web.is_node_discovered(nodes[0]),
            timeout=10 * 60
        )

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["ha_one_controller_flat_blocked_vlan"])
    @log_snapshot_after_test
    def ha_one_controller_flat_blocked_vlan(self):
        """Verify network verification with blocked VLANs

        Scenario:
            1. Create cluster in Ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Deploy the cluster
            5. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs
            6. Block first VLAN
            7. Run Verify network and assert it fails
            8. Restore first VLAN

        Duration 20m

        """
        self.env.revert_snapshot("ready_with_3_slaves")

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute']
            }
        )
        self.fuel_web.deploy_cluster_wait(cluster_id)
        os_conn = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id))
        self.fuel_web.assert_cluster_ready(
            os_conn, smiles_count=6, networks_count=1, timeout=300)

        ebtables = self.env.get_ebtables(
            cluster_id, self.env.d_env.nodes().slaves[:2])
        ebtables.restore_vlans()
        try:
            ebtables.block_first_vlan()
            self.fuel_web.verify_network(cluster_id, success=False)
        finally:
            ebtables.restore_first_vlan()

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["ha_one_controller_flat_add_compute"])
    @log_snapshot_after_test
    def ha_one_controller_flat_add_compute(self):
        """Add compute node to cluster in ha mode

        Scenario:
            1. Create cluster in HA mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Deploy the cluster
            5. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs
            6. Add 1 node with role compute
            7. Deploy changes
            8. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs
            9. Verify services list on compute nodes
            10. Run OSTF

        Duration 40m
        Snapshot: ha_one_controller_flat_add_compute
        """
        self.env.revert_snapshot("ready_with_3_slaves")

        data = {
            'tenant': 'flatAddCompute',
            'user': 'flatAddCompute',
            'password': 'flatAddCompute'

        }

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE,
            settings=data
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute']
            }
        )
        self.fuel_web.deploy_cluster_wait(cluster_id)
        os_conn = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id),
            data['user'], data['password'], data['tenant'])
        self.fuel_web.assert_cluster_ready(
            os_conn, smiles_count=6, networks_count=1, timeout=300)

        self.fuel_web.update_nodes(
            cluster_id, {'slave-03': ['compute']}, True, False)
        self.fuel_web.deploy_cluster_wait(cluster_id)

        assert_equal(
            3, len(self.fuel_web.client.list_cluster_nodes(cluster_id)))

        self.fuel_web.assert_cluster_ready(
            os_conn, smiles_count=8, networks_count=1, timeout=300)

        self.fuel_web.run_ostf(
            cluster_id=cluster_id)

        self.env.make_snapshot("ha_one_controller_flat_add_compute")


@test(groups=["thread_2"])
class HAOneControllerVlan(TestBasic):
    """HAOneControllerVlan."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["deploy_ha_one_controller_vlan",
                  "ha_one_controller_nova_vlan"])
    @log_snapshot_after_test
    def deploy_ha_one_controller_vlan(self):
        """Deploy cluster in ha mode with nova-network VLAN Manager

        Scenario:
            1. Create cluster in Ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Set up cluster to use Network VLAN manager with 8 networks
            5. Deploy the cluster
            6. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs
            7. Run network verification
            8. Run OSTF

        Duration 30m
        Snapshot: deploy_ha_one_controller_vlan
        """
        self.env.revert_snapshot("ready_with_3_slaves")

        data = {
            'tenant': 'novaSimpleVlan',
            'user': 'novaSimpleVlan',
            'password': 'novaSimpleVlan'
        }

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE,
            settings=data
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute']
            }
        )

        self.fuel_web.update_vlan_network_fixed(
            cluster_id, amount=8, network_size=32)
        self.fuel_web.deploy_cluster_wait(cluster_id)

        os_conn = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id),
            data['user'], data['password'], data['tenant'])

        self.fuel_web.assert_cluster_ready(
            os_conn, smiles_count=6, networks_count=8, timeout=300)

        self.fuel_web.verify_network(cluster_id)

        self.fuel_web.run_ostf(
            cluster_id=cluster_id)

        self.env.make_snapshot("deploy_ha_one_controller_vlan", is_make=True)

    @test(depends_on=[deploy_ha_one_controller_vlan],
          groups=["deploy_base_os_node"])
    @log_snapshot_after_test
    def deploy_base_os_node(self):
        """Add base-os node to cluster in HA mode with one controller

        Scenario:
            1. Revert snapshot "deploy_ha_one_controller_vlan"
            2. Add 1 node with base-os role
            3. Deploy the cluster
            4. Run network verification
            5. Run OSTF
            6. Ssh to the base-os node and check
                /etc/astute.yaml link source.
            7. Make snapshot.

        Snapshot: deploy_base_os_node

        """
        self.env.revert_snapshot("deploy_ha_one_controller_vlan")

        cluster_id = self.fuel_web.get_last_created_cluster()

        self.fuel_web.update_nodes(
            cluster_id, {'slave-03': ['base-os']}, True, False)
        self.fuel_web.deploy_cluster_wait(cluster_id)

        assert_equal(
            3, len(self.fuel_web.client.list_cluster_nodes(cluster_id)))

        self.fuel_web.verify_network(cluster_id)

        self.fuel_web.run_ostf(cluster_id=cluster_id)

        _ip = self.fuel_web.get_nailgun_node_by_name("slave-03")['ip']
        remote = self.env.d_env.get_ssh_to_remote(_ip)

        result = remote.execute('readlink /etc/astute.yaml')['stdout']

        assert_true("base-os" in result[0],
                    "Role mismatch. Node slave-03 is not base-os")

        self.env.make_snapshot("deploy_base_os_node")


@test(groups=["thread_2", "multirole"])
class MultiroleControllerCinder(TestBasic):
    """MultiroleControllerCinder."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["deploy_multirole_controller_cinder"])
    @log_snapshot_after_test
    def deploy_multirole_controller_cinder(self):
        """Deploy cluster in HA mode with multi-role controller and cinder

        Scenario:
            1. Create cluster in HA mode with 1 controller
            2. Add 1 node with controller and cinder roles
            3. Add 1 node with compute role
            4. Deploy the cluster
            5. Run network verification
            6. Run OSTF

        Duration 30m
        Snapshot: deploy_multirole_controller_cinder

        """
        self.env.revert_snapshot("ready_with_3_slaves")

        data = {
            'tenant': 'multirolecinder',
            'user': 'multirolecinder',
            'password': 'multirolecinder'
        }

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE,
            settings=data
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller', 'cinder'],
                'slave-02': ['compute']
            }
        )
        self.fuel_web.deploy_cluster_wait(cluster_id)

        self.fuel_web.verify_network(cluster_id)

        self.fuel_web.run_ostf(cluster_id=cluster_id)

        self.env.make_snapshot("deploy_multirole_controller_cinder")


@test(groups=["thread_2", "multirole"])
class MultiroleComputeCinder(TestBasic):
    """MultiroleComputeCinder."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["deploy_multirole_compute_cinder"])
    @log_snapshot_after_test
    def deploy_multirole_compute_cinder(self):
        """Deploy cluster in HA mode with multi-role compute and cinder

        Scenario:
            1. Create cluster in Ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 2 node with compute and cinder roles
            4. Deploy the cluster
            5. Run network verification
            6. Run OSTF

        Duration 30m
        Snapshot: deploy_multirole_compute_cinder

        """
        self.env.revert_snapshot("ready_with_3_slaves")

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute', 'cinder'],
                'slave-03': ['compute', 'cinder']
            }
        )
        self.fuel_web.deploy_cluster_wait(cluster_id)

        self.fuel_web.verify_network(cluster_id)

        self.fuel_web.run_ostf(cluster_id=cluster_id)

        self.env.make_snapshot("deploy_multirole_compute_cinder")


@test(groups=["thread_2"])
class FloatingIPs(TestBasic):
    """FloatingIPs."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["deploy_floating_ips"])
    @log_snapshot_after_test
    def deploy_floating_ips(self):
        """Deploy cluster with non-default 3 floating IPs ranges

        Scenario:
            1. Create cluster in Ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute and cinder roles
            4. Update floating IP ranges. Use 3 ranges
            5. Deploy the cluster
            6. Verify available floating IP list
            7. Run OSTF

        Duration 30m
        Snapshot: deploy_floating_ips

        """
        self.env.revert_snapshot("ready_with_3_slaves")

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE,
            settings={
                'tenant': 'floatingip',
                'user': 'floatingip',
                'password': 'floatingip'
            }
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute']
            }
        )

        networking_parameters = {
            "floating_ranges": self.fuel_web.get_floating_ranges()[0]}

        self.fuel_web.client.update_network(
            cluster_id,
            networking_parameters=networking_parameters
        )

        self.fuel_web.deploy_cluster_wait(cluster_id)

        # assert ips
        expected_ips = self.fuel_web.get_floating_ranges()[1]
        self.fuel_web.assert_cluster_floating_list('slave-02', expected_ips)

        self.fuel_web.run_ostf(
            cluster_id=cluster_id)

        self.env.make_snapshot("deploy_floating_ips")


@test(groups=["ha_one_controller"])
class HAOneControllerCinder(TestBasic):
    """HAOneControllerCinder."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["deploy_ha_one_controller_cinder",
                  "ha_one_controller_nova_cinder"])
    @log_snapshot_after_test
    def deploy_ha_one_controller_cinder(self):
        """Deploy cluster in HA mode with cinder

        Scenario:
            1. Create cluster in Ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Add 1 node with cinder role
            5. Deploy the cluster
            6. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs
            7. Run OSTF

        Duration 30m
        Snapshot: deploy_ha_one_controller_cinder
        """
        self.env.revert_snapshot("ready_with_3_slaves")

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute'],
                'slave-03': ['cinder']
            }
        )
        self.fuel_web.deploy_cluster_wait(cluster_id)

        os_conn = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id))

        self.fuel_web.assert_cluster_ready(
            os_conn, smiles_count=6, networks_count=1, timeout=300)

        self.fuel_web.check_fixed_network_cidr(
            cluster_id, os_conn)
        self.fuel_web.verify_network(cluster_id)
        self.env.verify_network_configuration("slave-01")

        self.fuel_web.run_ostf(cluster_id=cluster_id)

        self.env.make_snapshot("deploy_ha_one_controller_cinder")


@test(groups=["thread_1"])
class NodeMultipleInterfaces(TestBasic):
    """NodeMultipleInterfaces."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["deploy_node_multiple_interfaces"])
    @log_snapshot_after_test
    def deploy_node_multiple_interfaces(self):
        """Deploy cluster with networks allocated on different interfaces

        Scenario:
            1. Create cluster in Ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Add 1 node with cinder role
            5. Split networks on existing physical interfaces
            6. Deploy the cluster
            7. Verify network configuration on each deployed node
            8. Run network verification

        Duration 25m
        Snapshot: deploy_node_multiple_interfaces

        """
        self.env.revert_snapshot("ready_with_3_slaves")

        interfaces_dict = {
            'eth1': ['public'],
            'eth2': ['storage'],
            'eth3': ['fixed'],
            'eth4': ['management'],
        }

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute'],
                'slave-03': ['cinder']
            }
        )
        nailgun_nodes = self.fuel_web.client.list_cluster_nodes(cluster_id)
        for node in nailgun_nodes:
            self.fuel_web.update_node_networks(node['id'], interfaces_dict)

        self.fuel_web.deploy_cluster_wait(cluster_id)
        for node in ['slave-01', 'slave-02', 'slave-03']:
            self.env.verify_network_configuration(node)

        self.fuel_web.verify_network(cluster_id)

        self.env.make_snapshot("deploy_node_multiple_interfaces", is_make=True)


@test(groups=["thread_1"])
class NodeDiskSizes(TestBasic):
    """NodeDiskSizes."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["check_nodes_notifications"])
    @log_snapshot_after_test
    def check_nodes_notifications(self):
        """Verify nailgun notifications for discovered nodes

        Scenario:
            1. Revert snapshot "ready_with_3_slaves"
            2. Verify hard drive sizes for discovered nodes in /api/nodes
            3. Verify hard drive sizes for discovered nodes in notifications

        Duration 5m

        """
        self.env.revert_snapshot("ready_with_3_slaves")

        # assert /api/nodes
        disk_size = NODE_VOLUME_SIZE * 1024 ** 3
        nailgun_nodes = self.fuel_web.client.list_nodes()
        for node in nailgun_nodes:
            for disk in node['meta']['disks']:
                assert_equal(disk['size'], disk_size, 'Disk size')

        hdd_size = "{} TB HDD".format(float(disk_size * 3 / (10 ** 9)) / 1000)
        notifications = self.fuel_web.client.get_notifications()
        for node in nailgun_nodes:
            # assert /api/notifications
            for notification in notifications:
                discover = notification['topic'] == 'discover'
                current_node = notification['node_id'] == node['id']
                if current_node and discover and \
                   "discovered" in notification['message']:
                    assert_true(hdd_size in notification['message'])

            # assert disks
            disks = self.fuel_web.client.get_node_disks(node['id'])
            for disk in disks:
                assert_equal(disk['size'],
                             NODE_VOLUME_SIZE * 1024 - 500, 'Disk size')

    @test(depends_on=[NodeMultipleInterfaces.deploy_node_multiple_interfaces],
          groups=["check_nodes_disks"])
    @log_snapshot_after_test
    def check_nodes_disks(self):
        """Verify hard drive sizes for deployed nodes

        Scenario:
            1. Revert snapshot "deploy_node_multiple_interfaces"
            2. Verify hard drive sizes for deployed nodes

        Duration 15m
        """

        self.env.revert_snapshot("deploy_node_multiple_interfaces")

        nodes_dict = {
            'slave-01': ['controller'],
            'slave-02': ['compute'],
            'slave-03': ['cinder']
        }

        # assert node disks after deployment
        for node_name in nodes_dict:
            str_block_devices = self.fuel_web.get_cluster_block_devices(
                node_name)

            logger.debug("Block device:\n{}".format(str_block_devices))

            expected_regexp = re.compile(
                "vda\s+\d+:\d+\s+0\s+{}G\s+0\s+disk".format(NODE_VOLUME_SIZE))
            assert_true(
                expected_regexp.search(str_block_devices),
                "Unable to find vda block device for {}G in: {}".format(
                    NODE_VOLUME_SIZE, str_block_devices
                ))

            expected_regexp = re.compile(
                "vdb\s+\d+:\d+\s+0\s+{}G\s+0\s+disk".format(NODE_VOLUME_SIZE))
            assert_true(
                expected_regexp.search(str_block_devices),
                "Unable to find vdb block device for {}G in: {}".format(
                    NODE_VOLUME_SIZE, str_block_devices
                ))

            expected_regexp = re.compile(
                "vdc\s+\d+:\d+\s+0\s+{}G\s+0\s+disk".format(NODE_VOLUME_SIZE))
            assert_true(
                expected_regexp.search(str_block_devices),
                "Unable to find vdc block device for {}G in: {}".format(
                    NODE_VOLUME_SIZE, str_block_devices
                ))


@test(groups=["thread_1"])
class MultinicBootstrap(TestBasic):
    """MultinicBootstrap."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_release],
          groups=["multinic_bootstrap_booting"])
    @log_snapshot_after_test
    def multinic_bootstrap_booting(self):
        """Verify slaves booting with blocked mac address

        Scenario:
            1. Revert snapshot "ready"
            2. Block traffic for first slave node (by mac)
            3. Restore mac addresses and boot first slave
            4. Verify slave mac addresses is equal to unblocked

        Duration 2m

        """
        self.env.revert_snapshot("ready")

        slave = self.env.d_env.nodes().slaves[0]
        mac_addresses = [interface.mac_address for interface in
                         slave.interfaces.filter(network__name='internal')]
        try:
            for mac in mac_addresses:
                Ebtables.block_mac(mac)
            for mac in mac_addresses:
                Ebtables.restore_mac(mac)
                slave.destroy(verbose=False)
                self.env.d_env.nodes().admins[0].revert("ready")
                nailgun_slave = self.env.bootstrap_nodes([slave])[0]
                assert_equal(mac.upper(), nailgun_slave['mac'].upper())
                Ebtables.block_mac(mac)
        finally:
            for mac in mac_addresses:
                Ebtables.restore_mac(mac)


@test(groups=["thread_2", "test"])
class DeleteEnvironment(TestBasic):
    """DeleteEnvironment."""  # TODO documentation

    @test(depends_on=[HAOneControllerFlat.deploy_ha_one_controller_flat],
          groups=["delete_environment"])
    @log_snapshot_after_test
    def delete_environment(self):
        """Delete existing environment
        and verify nodes returns to unallocated state

        Scenario:
            1. Revert "deploy_ha_one_controller" environment
            2. Delete environment
            3. Verify node returns to unallocated pull

        Duration 15m

        """
        self.env.revert_snapshot("deploy_ha_one_controller_flat")

        cluster_id = self.fuel_web.get_last_created_cluster()
        self.fuel_web.client.delete_cluster(cluster_id)
        nailgun_nodes = self.fuel_web.client.list_nodes()
        nodes = filter(lambda x: x["pending_deletion"] is True, nailgun_nodes)
        assert_true(
            len(nodes) == 2, "Verify 2 node has pending deletion status"
        )
        wait(
            lambda:
            self.fuel_web.is_node_discovered(nodes[0]) and
            self.fuel_web.is_node_discovered(nodes[1]),
            timeout=10 * 60,
            interval=15
        )


@test(groups=["thread_1"])
class UntaggedNetworksNegative(TestBasic):
    """UntaggedNetworksNegative."""  # TODO documentation

    @test(
        depends_on=[SetupEnvironment.prepare_slaves_3],
        groups=["untagged_networks_negative"],
        enabled=False)
    @log_snapshot_after_test
    def untagged_networks_negative(self):
        """Verify network verification fails with untagged network on eth0

        Scenario:
            1. Create cluster in ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Split networks on existing physical interfaces
            5. Remove VLAN tagging from networks which are on eth0
            6. Run network verification (assert it fails)
            7. Start cluster deployment (assert it fails)

        Duration 30m

        """
        self.env.revert_snapshot("ready_with_3_slaves")

        vlan_turn_off = {'vlan_start': None}
        interfaces = {
            'eth0': ["fixed"],
            'eth1': ["public"],
            'eth2': ["management", "storage"],
            'eth3': []
        }

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute']
            }
        )

        nets = self.fuel_web.client.get_networks(cluster_id)['networks']
        nailgun_nodes = self.fuel_web.client.list_cluster_nodes(cluster_id)
        for node in nailgun_nodes:
            self.fuel_web.update_node_networks(node['id'], interfaces)

        # select networks that will be untagged:
        [net.update(vlan_turn_off) for net in nets]

        # stop using VLANs:
        self.fuel_web.client.update_network(cluster_id, networks=nets)

        # run network check:
        self.fuel_web.verify_network(cluster_id, success=False)

        # deploy cluster:
        task = self.fuel_web.deploy_cluster(cluster_id)
        self.fuel_web.assert_task_failed(task)


@test(groups=["known_issues"])
class BackupRestoreHAOneController(TestBasic):
    """BackupRestoreHAOneController"""  # TODO documentation

    @test(depends_on=[HAOneControllerFlat.deploy_ha_one_controller_flat],
          groups=["ha_one_controller_backup_restore"])
    @log_snapshot_after_test
    def ha_one_controller_backup_restore(self):
        """Backup/restore master node with cluster in ha mode

        Scenario:
            1. Revert snapshot "deploy_ha_one_controller_flat"
            2. Backup master
            3. Check backup
            4. Run OSTF
            5. Add 1 node with compute role
            6. Restore master
            7. Check restore
            8. Run OSTF

        Duration 35m

        """
        self.env.revert_snapshot("deploy_ha_one_controller_flat")

        cluster_id = self.fuel_web.get_last_created_cluster()
        os_conn = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id),
            'novaSimpleFlat', 'novaSimpleFlat', 'novaSimpleFlat')
        self.fuel_web.assert_cluster_ready(
            os_conn, smiles_count=6, networks_count=1, timeout=300)
        # Execute master node backup
        self.fuel_web.backup_master(self.env.d_env.get_admin_remote())

        # Check created backup
        checkers.backup_check(self.env.d_env.get_admin_remote())

        self.fuel_web.update_nodes(
            cluster_id, {'slave-03': ['compute']}, True, False)

        assert_equal(
            3, len(self.fuel_web.client.list_cluster_nodes(cluster_id)))

        self.fuel_web.restore_master(self.env.d_env.get_admin_remote())
        checkers.restore_check_sum(self.env.d_env.get_admin_remote())
        self.fuel_web.restore_check_nailgun_api(
            self.env.d_env.get_admin_remote())
        checkers.iptables_check(self.env.d_env.get_admin_remote())

        assert_equal(
            2, len(self.fuel_web.client.list_cluster_nodes(cluster_id)))

        self.fuel_web.update_nodes(
            cluster_id, {'slave-03': ['compute']}, True, False)
        self.fuel_web.deploy_cluster_wait(cluster_id)

        self.fuel_web.run_ostf(
            cluster_id=cluster_id)

        self.env.make_snapshot("ha_one_controller_backup_restore")


@test(groups=["thread_usb"])
class HAOneControllerFlatUSB(HAOneControllerFlatBase):
    """HAOneControllerFlatUSB."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3])
    @log_snapshot_after_test
    def deploy_ha_one_controller_flat_usb(self):
        """Deploy cluster in HA mode (1 controller) with flat nova-network USB

        Scenario:
            1. Create cluster in HA mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Deploy the cluster
            5. Validate cluster was set up correctly, there are no dead
            services, there are no errors in logs
            6. Verify networks
            7. Verify network configuration on controller
            8. Run OSTF

        Duration 30m
        Snapshot: deploy_ha_one_controller_flat
        """

        super(self.__class__, self).deploy_ha_one_controller_flat_base()


@test(groups=["thread_usb", "classic_provisioning"])
class ProvisioningScripts(TestBasic):

    base_path = '/var/www/nailgun/ubuntu/x86_64/images/'
    filenames = ['initrd.gz', 'linux']

    def get_zero_length_files(self):
        remote = self.env.d_env.get_admin_remote()

        zero_length_files = []
        for f_name in self.filenames:
            file_size = checkers.get_file_size(remote, f_name, self.base_path)
            if not file_size:
                zero_length_files.append(f_name)
            full_path = os.path.join(self.base_path, f_name)
            logger.info("File %s has size %s", full_path, file_size)

        return zero_length_files

    @test(depends_on=[SetupEnvironment.prepare_release],
          groups=["check_fuel_provisioning_scripts"])
    @log_snapshot_after_test
    def check_fuel_provisioning_scripts(self):
        """Deploy cluster with controller node only

        Scenario:
            1. Deploy master node
            2. Check sizes of the files
            1. Create cluster
            2. Add 1 node with controller role
            3. Deploy the cluster
            4. Check sizes of the files again

        Duration 45m
        """
        if OPENSTACK_RELEASE_UBUNTU != OPENSTACK_RELEASE or \
                not CLASSIC_PROVISIONING:
            raise SkipTest()

        self.env.revert_snapshot("ready")

        zero_length_files = self.get_zero_length_files()
        non_zero_length_files = list(
            set(zero_length_files) - set(self.filenames))

        error_msg = "Non zero-length files: {0}".\
            format(', '.join(non_zero_length_files))
        assert_false(non_zero_length_files, error_msg)

        self.env.bootstrap_nodes(
            self.env.d_env.nodes().slaves[:1])
        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=DEPLOYMENT_MODE)
        logger.info('cluster is %s' % str(cluster_id))
        self.fuel_web.update_nodes(
            cluster_id,
            {'slave-01': ['controller']}
        )
        self.fuel_web.deploy_cluster_wait(cluster_id)

        zero_length_files = self.get_zero_length_files()
        assert_false(
            zero_length_files,
            "Files {0} have 0 length".format(', '.join(zero_length_files)))
