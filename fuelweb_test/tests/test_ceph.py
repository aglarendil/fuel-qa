#    Copyright 2014 Mirantis, Inc.
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

import proboscis
import time

from proboscis.asserts import assert_true, assert_false, assert_equal
from proboscis import SkipTest
from proboscis import test
from devops.helpers.helpers import tcp_ping
from devops.helpers.helpers import wait

from fuelweb_test.helpers import os_actions
from fuelweb_test.helpers import checkers
from fuelweb_test.helpers.decorators import log_snapshot_after_test
from fuelweb_test import ostf_test_mapping as map_ostf
from fuelweb_test import settings
from fuelweb_test.settings import NEUTRON_ENABLE
from fuelweb_test import logger
from fuelweb_test.tests.base_test_case import SetupEnvironment
from fuelweb_test.tests.base_test_case import TestBasic


@test(groups=["ceph_ha_one_controller", "ceph"])
class CephCompact(TestBasic):
    """CephCompact."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["ceph_ha_one_controller_compact",
                  "ha_one_controller_nova_ceph",
                  "ceph_ha_one_controller_compact_neutron", "ceph",
                  "deployment"])
    @log_snapshot_after_test
    def ceph_ha_one_controller_compact(self):
        """Deploy ceph in HA mode with 1 controller

        Scenario:
            1. Create cluster
            2. Add 1 node with controller and ceph OSD roles
            3. Add 2 node with compute and ceph OSD roles
            4. Deploy the cluster
            5. Check ceph status

        Duration 35m
        Snapshot ceph_ha_one_controller_compact
        """
        self.env.revert_snapshot("ready_with_3_slaves")
        data = {
            'volumes_ceph': True,
            'images_ceph': True,
            'volumes_lvm': False,
            'tenant': 'ceph1',
            'user': 'ceph1',
            'password': 'ceph1'
        }
        if NEUTRON_ENABLE:
            data["net_provider"] = 'neutron'
            data["net_segment_type"] = 'vlan'

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=settings.DEPLOYMENT_MODE,
            settings=data)
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller', 'ceph-osd'],
                'slave-02': ['compute', 'ceph-osd'],
                'slave-03': ['compute', 'ceph-osd']
            }
        )
        # Cluster deploy
        self.fuel_web.deploy_cluster_wait(cluster_id)
        self.fuel_web.check_ceph_status(cluster_id)

        # Run ostf
        self.fuel_web.run_ostf(cluster_id=cluster_id)

        self.env.make_snapshot("ceph_ha_one_controller_compact")


@test(groups=["thread_3", "ceph"])
class CephCompactWithCinder(TestBasic):
    """CephCompactWithCinder."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_release],
          groups=["ceph_ha_one_controller_with_cinder"])
    @log_snapshot_after_test
    def ceph_ha_one_controller_with_cinder(self):
        """Deploy ceph with cinder in ha mode with 1 controller

        Scenario:
            1. Create cluster
            2. Add 1 node with controller role
            3. Add 1 node with compute role
            4. Add 2 nodes with cinder and ceph OSD roles
            5. Deploy the cluster
            6. Check ceph status
            7. Check partitions on controller node

        Duration 40m
        Snapshot ceph_ha_one_controller_with_cinder
        """
        try:
            self.check_run('ceph_ha_one_controller_with_cinder')
        except SkipTest:
            return

        self.env.revert_snapshot("ready")
        self.env.bootstrap_nodes(
            self.env.d_env.nodes().slaves[:4])

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=settings.DEPLOYMENT_MODE,
            settings={
                'volumes_ceph': False,
                'images_ceph': True,
                'volumes_lvm': True,
                'tenant': 'ceph2',
                'user': 'ceph2',
                'password': 'ceph2'
            }
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute'],
                'slave-03': ['cinder', 'ceph-osd'],
                'slave-04': ['cinder', 'ceph-osd']
            }
        )
        # Cluster deploy
        self.fuel_web.deploy_cluster_wait(cluster_id)
        self.fuel_web.check_ceph_status(cluster_id)

        disks = self.fuel_web.client.get_node_disks(
            self.fuel_web.get_nailgun_node_by_name('slave-01')['id'])

        logger.info("Current disk partitions are: \n{d}".format(d=disks))

        logger.info("Check unallocated space")
        # We expect failure here only for release 5.0 due to bug
        # https://bugs.launchpad.net/fuel/+bug/1306625, so it is
        # necessary to assert_true in the next release.
        assert_false(
            checkers.check_unallocated_space(disks, contr_img_ceph=True),
            "Check unallocated space on controller")

        # Run ostf
        self.fuel_web.run_ostf(cluster_id=cluster_id)

        self.env.make_snapshot("ceph_ha_one_controller_with_cinder",
                               is_make=True)


@test(groups=["thread_3", "ceph"])
class CephHA(TestBasic):
    """CephHA."""  # TODO documentation1

    @test(depends_on=[SetupEnvironment.prepare_release],
          groups=["ceph_ha", "classic_provisioning"])
    @log_snapshot_after_test
    def ceph_ha(self):
        """Deploy ceph with cinder in HA mode

        Scenario:
            1. Create cluster
            2. Add 3 nodes with controller and ceph OSD roles
            3. Add 1 node with ceph OSD roles
            4. Add 2 nodes with compute and ceph OSD roles
            5. Deploy the cluster

        Duration 90m
        Snapshot ceph_ha

        """
        try:
            self.check_run('ceph_ha')
        except SkipTest:
            return

        self.env.revert_snapshot("ready")
        self.env.bootstrap_nodes(
            self.env.d_env.nodes().slaves[:6])
        csettings = {}
        if settings.NEUTRON_ENABLE:
            csettings = {
                "net_provider": 'neutron',
                "net_segment_type": "vlan"
            }
        csettings.update(
            {
                'volumes_ceph': True,
                'images_ceph': True,
                'volumes_lvm': False,
                'tenant': 'cephHA',
                'user': 'cephHA',
                'password': 'cephHA'
            }
        )
        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=settings.DEPLOYMENT_MODE,
            settings=csettings
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller', 'ceph-osd'],
                'slave-02': ['controller', 'ceph-osd'],
                'slave-03': ['controller', 'ceph-osd'],
                'slave-04': ['compute', 'ceph-osd'],
                'slave-05': ['compute', 'ceph-osd'],
                'slave-06': ['ceph-osd']
            }
        )
        # Depoy cluster
        self.fuel_web.deploy_cluster_wait(cluster_id)

        self.env.make_snapshot("ceph_ha", is_make=True)

    @test(depends_on=[ceph_ha],
          groups=["ha_nova_ceph", "ha_neutron_ceph", "check_ceph_ha"])
    @log_snapshot_after_test
    def check_ceph_ha(self):
        """Check ceph with cinder in HA mode

        Scenario:
            1. Revert snapshot with ceph cluster in HA mode
            2. Check ceph status

        Duration 10m
        Snapshot check_ceph_ha

        """
        self.env.revert_snapshot("ceph_ha")
        cluster_id = self.fuel_web.get_last_created_cluster()

        self.fuel_web.check_ceph_status(cluster_id)

        # Run ostf
        self.fuel_web.run_ostf(
            cluster_id=cluster_id,
            test_sets=['ha', 'smoke', 'sanity'])

    @test(depends_on=[ceph_ha],
          groups=["openstack_stat"])
    @log_snapshot_after_test
    def check_openstack_stat(self):
        """Check openstack statistic on fuel and collector side

        Scenario:
            1. Revert ceph_ha env
            2. Create all openstack resources that are collected
            3. Check that all info was collected on fuel side
            4. Check that info was sent to collector
            5. Check that info is properly saved on collector side

        Duration 20m
        Snapshot check_openstack_stat

        """
        self.env.revert_snapshot("ceph_ha")
        cluster_id = self.fuel_web.get_last_created_cluster()
        os_conn = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id), 'cephHA', 'cephHA',
            'cephHA')

        # Check resources addition
        # create instance
        server = os_conn.create_instance(
            neutron_network=settings.NEUTRON_ENABLE)

        # create flavor
        flavor = os_conn.create_flavor('openstackstat', 1024, 1, 1)

        # create volume
        volume = os_conn.create_volume()

        # create image
        devops_node = self.fuel_web.get_nailgun_primary_node(
            self.env.d_env.nodes().slaves[0])
        slave = self.fuel_web.get_ssh_for_node(devops_node.name)
        if settings.OPENSTACK_RELEASE_CENTOS in settings.OPENSTACK_RELEASE:
            slave.execute(". openrc; glance image-create --name"
                          " 'custom-image' --disk-format qcow2"
                          " --container-format bare"
                          " --file /opt/vm/cirros-x86_64-disk.img")
        else:
            slave.execute(". openrc; glance image-create --name"
                          " 'custom-image' --disk-format qcow2"
                          " --container-format bare --file"
                          " /usr/share/cirros-testvm/cirros-x86_64-disk.img")

        image = os_conn.get_image_by_name('custom-image')

        # create tenant and user
        tenant = os_conn.create_tenant("openstack_tenant")
        user = os_conn.create_user('openstack_user', 'qwerty', tenant)

        self.env.nailgun_actions.force_oswl_collect()
        self.env.nailgun_actions.force_fuel_stats_sending()
        master_uid = self.env.get_masternode_uuid()
        checkers.check_oswl_stat(self.env.postgres_actions, self.env.collector,
                                 master_uid, operation='current',
                                 resources=['vm', 'flavor', 'volume', 'image',
                                            'tenant', 'keystone_user'])

        # Check resources modification
        # suspend instance
        server.suspend()
        # edit volume
        os_conn.extend_volume(volume, 2)
        # edit image
        os_conn.update_image(image, min_ram=333)
        # edit user
        os_conn.update_user_enabled(user, enabled=False)
        # edit tenant
        os_conn.update_tenant(tenant.id, enabled=False)

        self.env.nailgun_actions.force_oswl_collect()
        self.env.nailgun_actions.force_fuel_stats_sending()
        checkers.check_oswl_stat(self.env.postgres_actions, self.env.collector,
                                 master_uid, operation='modified',
                                 resources=['vm', 'volume', 'image',
                                            'tenant', 'keystone_user'])

        # Check resources deletion
        # delete instance
        server.delete()
        # delete flavor
        os_conn.delete_flavor(flavor)
        # delete volume
        os_conn.delete_volume_and_wait(volume, timeout=300)
        # delete image
        os_conn.delete_image(image.id)
        # delete tenant
        os_conn.delete_tenant(tenant)
        # delete user
        os_conn.delete_user(user)

        self.env.nailgun_actions.force_oswl_collect()
        self.env.nailgun_actions.force_fuel_stats_sending()
        checkers.check_oswl_stat(self.env.postgres_actions, self.env.collector,
                                 master_uid, operation='removed',
                                 resources=['vm', 'flavor', 'volume', 'image',
                                            'tenant', 'keystone_user'])


@test(groups=["thread_4", "ceph"])
class CephRadosGW(TestBasic):
    """CephRadosGW."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_release],
          groups=["ceph_rados_gw", "bvt_2"])
    @log_snapshot_after_test
    def ceph_rados_gw(self):
        """Deploy ceph HA with RadosGW for objects

        Scenario:
            1. Create cluster with Neurton/VLAN
            2. Add 3 node with controller role
            3. Add 3 node with compute and ceph-osd role
            4. Deploy the cluster
            5. Check ceph status
            6. Run OSTF tests
            7. Check the radosqw daemon is started

        Duration 90m
        Snapshot ceph_rados_gw

        """
        self.env.revert_snapshot("ready")
        self.env.bootstrap_nodes(
            self.env.d_env.nodes().slaves[:6])

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=settings.DEPLOYMENT_MODE,
            settings={
                'volumes_lvm': False,
                'volumes_ceph': True,
                'images_ceph': True,
                'objects_ceph': True,
                'net_provider': 'neutron',
                'net_segment_type': 'vlan',
                'tenant': 'rados',
                'user': 'rados',
                'password': 'rados'
            }
        )
        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['controller'],
                'slave-03': ['controller'],
                'slave-04': ['compute', 'ceph-osd'],
                'slave-05': ['compute', 'ceph-osd'],
                'slave-06': ['compute', 'ceph-osd']
            }
        )
        self.fuel_web.verify_network(cluster_id)
        # Deploy cluster
        self.fuel_web.deploy_cluster_wait(cluster_id)

        # Network verification
        self.fuel_web.verify_network(cluster_id)

        # HAProxy backend checking
        controller_nodes = self.fuel_web.get_nailgun_cluster_nodes_by_roles(
            cluster_id, ['controller'])

        for node in controller_nodes:
            remote = self.env.d_env.get_ssh_to_remote(node['ip'])
            logger.info("Check all HAProxy backends on {}".format(
                node['meta']['system']['fqdn']))
            haproxy_status = checkers.check_haproxy_backend(remote)
            assert_equal(haproxy_status['exit_code'], 1,
                         "HAProxy backends are DOWN. {0}".format(
                             haproxy_status))
            remote.clear()

        self.fuel_web.check_ceph_status(cluster_id)

        # Run ostf
        self.fuel_web.run_ostf(cluster_id=cluster_id,
                               test_sets=['ha', 'smoke', 'sanity'])

        # Check the radosqw daemon is started
        remote = self.fuel_web.get_ssh_for_node('slave-01')
        radosgw_started = lambda: len(remote.check_call(
            'ps aux | grep "/usr/bin/radosgw -n '
            'client.radosgw.gateway"')['stdout']) == 3
        assert_true(radosgw_started(), 'radosgw daemon started')
        remote.clear()

        self.env.make_snapshot("ceph_rados_gw")


@test(groups=["ceph_ha_one_controller", "ceph_migration"])
class VmBackedWithCephMigrationBasic(TestBasic):
    """VmBackedWithCephMigrationBasic."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["ceph_migration"])
    @log_snapshot_after_test
    def migrate_vm_backed_with_ceph(self):
        """Check VM backed with ceph migration in ha mode with 1 controller

        Scenario:
            1. Create cluster
            2. Add 1 node with controller and ceph OSD roles
            3. Add 2 node with compute and ceph OSD roles
            4. Deploy the cluster
            5. Check ceph status
            6. Run OSTF
            7. Create a new VM, assign floating ip
            8. Migrate VM
            9. Check cluster and server state after migration
            10. Terminate VM

        Duration 35m
        Snapshot vm_backed_with_ceph_live_migration

        """
        if settings.OPENSTACK_RELEASE == settings.OPENSTACK_RELEASE_REDHAT:
            raise proboscis.SkipTest()

        self.env.revert_snapshot("ready_with_3_slaves")

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=settings.DEPLOYMENT_MODE,
            settings={
                'volumes_ceph': True,
                'images_ceph': True,
                'ephemeral_ceph': True,
                'volumes_lvm': False
            }
        )

        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller', 'ceph-osd'],
                'slave-02': ['compute', 'ceph-osd'],
                'slave-03': ['compute', 'ceph-osd']
            }
        )
        creds = ("cirros", "test")

        # Cluster deploy
        self.fuel_web.deploy_cluster_wait(cluster_id)

        def _check():
            # Run volume test several times with hope that it pass
            test_path = map_ostf.OSTF_TEST_MAPPING.get(
                'Create volume and attach it to instance')
            logger.debug('Start to run test {0}'.format(test_path))
            self.fuel_web.run_single_ostf_test(
                cluster_id, test_sets=['smoke'],
                test_name=test_path)
        try:
            _check()
        except AssertionError:
            logger.debug(AssertionError)
            logger.debug("Test failed from first probe,"
                         " we sleep 60 second try one more time "
                         "and if it fails again - test will fails ")
            time.sleep(60)
            _check()

        # Run ostf
        self.fuel_web.run_ostf(cluster_id)

        # Create new server
        os = os_actions.OpenStackActions(
            self.fuel_web.get_public_vip(cluster_id))

        logger.info("Create new server")
        srv = os.create_server_for_migration(
            scenario='./fuelweb_test/helpers/instance_initial_scenario')
        logger.info("Srv is currently in status: %s" % srv.status)

        srv_remote_node = self.fuel_web.get_ssh_for_node(
            self.fuel_web.find_devops_node_by_nailgun_fqdn(
                os.get_srv_hypervisor_name(srv),
                self.env.d_env.nodes().slaves[:3]).name)
        srv_instance_ip = os.get_nova_instance_ip(srv)
        srv_instance_mac = os.get_instance_mac(srv_remote_node, srv)
        res = ''.join(srv_remote_node.execute('ip r | fgrep br100')['stdout'])
        srv_node_dhcp_ip = res.split()[-1]

        logger.info("Assigning floating ip to server")
        floating_ip = os.assign_floating_ip(srv)
        srv_host = os.get_srv_host_name(srv)
        logger.info("Server is on host %s" % srv_host)

        wait(lambda: tcp_ping(floating_ip.ip, 22), timeout=120)

        md5before = os.get_md5sum(
            "/home/test_file",
            self.fuel_web.get_ssh_for_node("slave-01"),
            floating_ip.ip, creds)

        logger.info("Get available computes")
        avail_hosts = os.get_hosts_for_migr(srv_host)

        logger.info("Migrating server")
        new_srv = os.migrate_server(srv, avail_hosts[0], timeout=200)
        logger.info("Check cluster and server state after migration")

        wait(lambda: tcp_ping(floating_ip.ip, 22), timeout=120)

        md5after = os.get_md5sum(
            "/home/test_file",
            self.fuel_web.get_ssh_for_node("slave-01"),
            floating_ip.ip, creds)

        assert_true(
            md5after in md5before,
            "Md5 checksums don`t match."
            "Before migration md5 was equal to: {bef}"
            "Now it eqals: {aft}".format(bef=md5before, aft=md5after))

        res = os.execute_through_host(
            self.fuel_web.get_ssh_for_node("slave-01"),
            floating_ip.ip, "ping -q -c3 -w10 {0} | grep 'received' |"
            " grep -v '0 packets received'"
            .format(settings.PUBLIC_TEST_IP), creds)
        logger.info("Ping {0} result on vm is: {1}"
                    .format(settings.PUBLIC_TEST_IP, res))

        logger.info("Check Ceph health is ok after migration")
        self.fuel_web.check_ceph_status(cluster_id)

        logger.info("Server is now on host %s" %
                    os.get_srv_host_name(new_srv))

        logger.info("Terminate migrated server")
        os.delete_instance(new_srv)
        assert_true(os.verify_srv_deleted(new_srv),
                    "Verify server was deleted")

        # Check if the dhcp lease for instance still remains
        # on the previous compute node. Related Bug: #1391010
        assert_false(checkers.check_nova_dhcp_lease(srv_remote_node,
                                                    srv_instance_ip,
                                                    srv_instance_mac,
                                                    srv_node_dhcp_ip),
                     "Instance has been deleted, but it\'s DHCP lease "
                     "for IP:{0} with MAC:{1} still remains on the "
                     "compute node {2}".format(srv_instance_ip,
                                               srv_instance_mac,
                                               srv_host))
        # Create a new server
        logger.info("Create new server")
        srv = os.create_server_for_migration(
            scenario='./fuelweb_test/helpers/instance_initial_scenario')
        logger.info("Srv is currently in status: %s" % srv.status)

        logger.info("Assigning floating ip to server")
        floating_ip = os.assign_floating_ip(srv)
        srv_host = os.get_srv_host_name(srv)
        logger.info("Server is on host %s" % srv_host)

        logger.info("Create volume")
        vol = os.create_volume()
        logger.info("Attach volume to server")
        os.attach_volume(vol, srv)

        wait(lambda: tcp_ping(floating_ip.ip, 22), timeout=120)
        logger.info("Create filesystem and mount volume")
        os.execute_through_host(
            self.fuel_web.get_ssh_for_node("slave-01"),
            floating_ip.ip, 'sudo sh /home/mount_volume.sh', creds)

        os.execute_through_host(
            self.fuel_web.get_ssh_for_node("slave-01"),
            floating_ip.ip, 'sudo touch /mnt/file-on-volume', creds)

        logger.info("Get available computes")
        avail_hosts = os.get_hosts_for_migr(srv_host)

        logger.info("Migrating server")
        new_srv = os.migrate_server(srv, avail_hosts[0], timeout=120)
        logger.info("Check cluster and server state after migration")

        wait(lambda: tcp_ping(floating_ip.ip, 22), timeout=120)
        logger.info("Mount volume after migration")
        out = os.execute_through_host(
            self.fuel_web.get_ssh_for_node("slave-01"),
            floating_ip.ip, 'sudo mount /dev/vdb /mnt', creds)

        logger.info("out of mounting volume is: %s" % out)

        assert_true("file-on-volume" in os.execute_through_host(
                    self.fuel_web.get_ssh_for_node("slave-01"),
                    floating_ip.ip, "sudo ls /mnt", creds),
                    "File is abscent in /mnt")

        logger.info("Check Ceph health is ok after migration")
        self.fuel_web.check_ceph_status(cluster_id)

        logger.info("Server is now on host %s" %
                    os.get_srv_host_name(new_srv))

        logger.info("Terminate migrated server")
        os.delete_instance(new_srv)
        assert_true(os.verify_srv_deleted(new_srv),
                    "Verify server was deleted")

        self.env.make_snapshot(
            "vm_backed_with_ceph_live_migration")


@test(groups=["ceph_ha_one_controller", "ceph_partitions"])
class CheckCephPartitionsAfterReboot(TestBasic):
    """CheckCephPartitionsAfterReboot."""  # TODO documentation

    @test(depends_on=[SetupEnvironment.prepare_slaves_3],
          groups=["ceph_partitions"])
    @log_snapshot_after_test
    def check_ceph_partitions_after_reboot(self):
        """Check that Ceph OSD partitions are remounted after reboot

        Scenario:
            1. Create cluster in Ha mode with 1 controller
            2. Add 1 node with controller role
            3. Add 1 node with compute and Ceph OSD roles
            4. Add 1 node with Ceph OSD role
            5. Deploy the cluster
            7. Check Ceph status
            8. Read current partitions
            9. Warm-reboot Ceph nodes
            10. Read partitions again
            11. Check Ceph health
            12. Cold-reboot Ceph nodes
            13. Read partitions again
            14. Check Ceph health

        Duration 40m
        Snapshot check_ceph_partitions_after_reboot

        """
        if settings.OPENSTACK_RELEASE == settings.OPENSTACK_RELEASE_REDHAT:
            raise proboscis.SkipTest()

        self.env.revert_snapshot("ready_with_3_slaves")

        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=settings.DEPLOYMENT_MODE,
            settings={
                'volumes_ceph': True,
                'images_ceph': True,
                'ephemeral_ceph': True,
                'volumes_lvm': False
            }
        )

        self.fuel_web.update_nodes(
            cluster_id,
            {
                'slave-01': ['controller'],
                'slave-02': ['compute', 'ceph-osd'],
                'slave-03': ['ceph-osd']
            }
        )
        # Deploy cluster
        self.fuel_web.deploy_cluster_wait(cluster_id)
        for node in ["slave-02", "slave-03"]:
            logger.info("Get partitions for {node}".format(node=node))
            _ip = self.fuel_web.get_nailgun_node_by_name(node)['ip']
            before_reboot_partitions = [checkers.get_ceph_partitions(
                self.env.d_env.get_ssh_to_remote(_ip),
                "/dev/vd{p}".format(p=part)) for part in ["b", "c"]]

            logger.info("Warm-restart nodes")
            self.fuel_web.warm_restart_nodes(
                [self.fuel_web.environment.d_env.get_node(name=node)])

            logger.info("Get partitions for {node} once again".format(
                node=node
            ))
            _ip = self.fuel_web.get_nailgun_node_by_name(node)['ip']
            after_reboot_partitions = [checkers.get_ceph_partitions(
                self.env.d_env.get_ssh_to_remote(_ip),
                "/dev/vd{p}".format(p=part)) for part in ["b", "c"]]

            if before_reboot_partitions != after_reboot_partitions:
                logger.info("Partitions don`t match")
                logger.info("Before reboot: %s" % before_reboot_partitions)
                logger.info("After reboot: %s" % after_reboot_partitions)
                raise Exception()

            logger.info("Check Ceph health is ok after reboot")
            self.fuel_web.check_ceph_status(cluster_id)

            logger.info("Cold-restart nodes")
            self.fuel_web.cold_restart_nodes(
                [self.fuel_web.environment.d_env.get_node(name=node)])

            _ip = self.fuel_web.get_nailgun_node_by_name(node)['ip']
            after_reboot_partitions = [checkers.get_ceph_partitions(
                self.env.d_env.get_ssh_to_remote(_ip),
                "/dev/vd{p}".format(p=part)) for part in ["b", "c"]]

            if before_reboot_partitions != after_reboot_partitions:
                logger.info("Partitions don`t match")
                logger.info("Before reboot: %s" % before_reboot_partitions)
                logger.info("After reboot: %s" % after_reboot_partitions)
                raise Exception()

            logger.info("Check Ceph health is ok after reboot")
            self.fuel_web.check_ceph_status(cluster_id)
