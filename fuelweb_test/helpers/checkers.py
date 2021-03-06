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
import hashlib
import json
import os
import re
import traceback

from ipaddr import IPAddress
from ipaddr import IPNetwork

from fuelweb_test import logger
from fuelweb_test import logwrap
from fuelweb_test.settings import EXTERNAL_DNS
from fuelweb_test.settings import EXTERNAL_NTP
from fuelweb_test.settings import OPENSTACK_RELEASE
from fuelweb_test.settings import OPENSTACK_RELEASE_UBUNTU
from fuelweb_test.settings import POOLS
from fuelweb_test.settings import PUBLIC_TEST_IP
from proboscis.asserts import assert_equal
from proboscis.asserts import assert_false
from proboscis.asserts import assert_true
from devops.error import TimeoutError
from devops.helpers.helpers import wait
from devops.helpers.helpers import _wait

from time import sleep


@logwrap
def check_cinder_status(remote):
    """Parse output and return False
       if any enabled service is down.
       'cinder service-list' stdout example:
    | cinder-scheduler | node-1.test.domain.local | nova | enabled |   up  |
    | cinder-scheduler | node-2.test.domain.local | nova | enabled |  down |
    """
    cmd = '. openrc; cinder service-list'
    result = remote.execute(cmd)
    cinder_services = ''.join(result['stdout'])
    logger.debug('>$ cinder service-list\n{}'.format(cinder_services))
    if result['exit_code'] == 0:
        return all(' up ' in x.split('enabled')[1]
                   for x in cinder_services.split('\n')
                   if 'cinder' in x and 'enabled' in x
                   and len(x.split('enabled')))
    return False


@logwrap
def check_image(image, md5, path):
    local_path = "{0}/{1}".format(path, image)
    logger.debug('Check md5 {0} of image {1}/{2}'.format(md5, path, image))
    if not os.path.isfile(local_path):
        logger.error('Image {0} not found in {1} directory'.format(
            image, path))
        return False
    with open(local_path, mode='rb') as fimage:
        digits = hashlib.md5()
        while True:
            buf = fimage.read(4096)
            if not buf:
                break
            digits.update(buf)
        md5_local = digits.hexdigest()
    if md5_local != md5:
        logger.error('MD5 of {0}/{1} is not correct, aborting'.format(
            path, image))
        return False
    return True


@logwrap
def get_interface_description(ctrl_ssh, interface_short_name):
    return ''.join(
        ctrl_ssh.execute(
            '/sbin/ip addr show dev %s' % interface_short_name
        )['stdout']
    )


def verify_network_configuration(remote, node):
    for interface in node['network_data']:
        if interface.get('vlan') is None:
            continue  # todo excess check fix interface json format
        interface_name = "{}.{}@{}".format(
            interface['dev'], interface['vlan'], interface['dev'])
        interface_short_name = "{}.{}".format(
            interface['dev'], interface['vlan'])
        interface_description = get_interface_description(
            remote, interface_short_name)
        assert_true(interface_name in interface_description)
        if interface.get('name') == 'floating':
            continue
        if interface.get('ip'):
            assert_true(
                "inet {}".format(interface.get('ip')) in
                interface_description)
        else:
            assert_false("inet " in interface_description)
        if interface.get('brd'):
            assert_true(
                "brd {}".format(interface['brd']) in interface_description)


@logwrap
def verify_service(remote, service_name, count=1):
    ps_output = remote.execute('ps ax')['stdout']
    api = filter(lambda x: service_name in x, ps_output)
    logger.debug("{} \\n: {}".format(service_name, str(api)))
    assert_equal(len(api), count,
                 "{0} count not equal to {1}".format(service_name, count))


@logwrap
def verify_service_list_api(os_conn, service_count):
    def _verify():
        ret = os_conn.get_nova_service_list()
        logger.debug('Service list {0}'.format(ret))
        assert_equal(service_count, len(ret),
                     'Expected service count is {0},'
                     ' but get {1} count, actual list {2}'.format(
                         service_count, len(ret), ret))
        for service in ret:
            logger.debug('service is {0}'.format(service))
            assert_equal(
                service.state, 'up',
                'Service {0} on host {1} has next '
                'state {2}'.format(service.binary,
                                   service.host,
                                   service.state))
    try:
        _verify()
    except AssertionError:
        logger.debug(
            "Services still not read. Sleeping for 60 seconds and retrying")
        sleep(60)
        _verify()


@logwrap
def verify_glance_image_api(os_conn):
    ret = os_conn.get_image_list()
    assert_equal(1, len([i for i in ret if i.name == 'TestVM']),
                 "TestVM not found in glance image-list")


@logwrap
def verify_network_list_api(os_conn, net_count=None):
    ret = os_conn.get_nova_network_list()
    assert_equal(net_count, len(ret),
                 'Unexpected count of networks detected, '
                 'expected: {0}, current {1} count,'
                 ' full list {2}'.format(net_count, len(ret), ret))


@logwrap
def get_ceph_partitions(remote, device, type="xfs"):
    ret = remote.check_call("parted {device} print | grep {type}".format(
                            device=device, type=type))['stdout']
    if not ret:
        logger.error("Partition not present! {partitions}: ".format(
                     remote.check_call("parted {device} print")))
        raise Exception
    logger.debug("Partitions: {part}".format(part=ret))
    return ret


@logwrap
def get_mongo_partitions(remote, device):
    ret = remote.check_call("lsblk | grep {device} | awk {size}".format(
                            device=device,
                            size=re.escape('{print $4}')))['stdout']
    if not ret:
        logger.error("Partition not present! {partitions}: ".format(
                     remote.check_call("parted {device} print")))
        raise Exception
    logger.debug("Partitions: {part}".format(part=ret))
    return ret


@logwrap
def check_unallocated_space(disks, contr_img_ceph=False):
    for disk in disks:
        # In case we have Ceph for images all space on controller
        # should be given to Base System space:
        if contr_img_ceph:
            logger.info("Check that all space on /dev/{d} is allocated for "
                        "Base System Space".format(d=disk['name']))
            if not bool(disk["volumes"][0]["size"] == disk["size"]):
                return False
        else:
            logger.info("Get overall size of volumes")
            sizes = [v['size'] for v in disk["volumes"]]
            logger.info("Space on disk: {s}".format(s=disk['size']))
            logger.info("Summary space of disks on /dev/{d}: {s}".format(
                d=disk["name"], s=sum(sizes)))
            if not bool(sum(sizes) == disk["size"]):
                return False
    return True


@logwrap
def check_upgraded_containers(remote, version_from, version_to):
    containers = remote.execute("docker ps | tail -n +2 |"
                                "awk '{ print $NF;}'")['stdout']
    symlink = remote.execute("readlink /etc/supervisord.d/current")['stdout']
    logger.debug('containers are {0}'.format(containers))
    logger.debug('symlinks are {0}'.format(symlink))
    components = [co.split('-') for x in containers for co in x.split(',')]

    for i in components:
        assert_true(version_from != i[2],
                    'There are {0} containers'.format(version_from))
    for i in components:
        assert_true(version_to == i[2],
                    'There are no {0} containers'.format(version_to))
    assert_true('/etc/supervisord.d/{0}'.format(version_to)
                in symlink[0],
                'Symlink is set not to {0}'.format(version_to))


@logwrap
def upload_tarball(node_ssh, tar_path, tar_target):
    assert_true(tar_path, "Source path for uploading 'tar_path' is empty, "
                "please check test settings!")
    check_archive_type(tar_path)
    try:
        logger.debug("Start to upload tar file")
        node_ssh.upload(tar_path, tar_target)
    except Exception:
        logger.error('Failed to upload file')
        logger.error(traceback.format_exc())


@logwrap
def check_archive_type(tar_path):
    if os.path.splitext(tar_path)[1] not in [".tar", ".lrz", ".fp", ".rpm"]:
        raise Exception("Wrong archive type!")


@logwrap
def check_tarball_exists(node_ssh, name, path):
    result = ''.join(node_ssh.execute(
        'ls -all {0} | grep {1}'.format(path, name))['stdout'])
    assert_true(name in result, 'Can not find tarball')


@logwrap
def untar(node_ssh, name, path):
    filename, ext = os.path.splitext(name)
    cmd = "tar -xpvf" if ext.endswith("tar") else "lrzuntar"
    result = ''.join(node_ssh.execute(
        'cd {0} && {2} {1}'.format(path, name, cmd))['stdout'])
    logger.debug('Result from tar command is {0}'.format(result))


@logwrap
def run_script(node_ssh, script_path, script_name, password='admin',
               rollback=False, exit_code=0):
    path = os.path.join(script_path, script_name)
    c_res = node_ssh.execute('chmod 755 {0}'.format(path))
    logger.debug("Result of cmod is {0}".format(c_res))
    if rollback:
        path = "UPGRADERS='host-system docker openstack" \
               " raise-error' {0}/{1}" \
               " --password {2}".format(script_path, script_name, password)
        chan, stdin, stderr, stdout = node_ssh.execute_async(path)
        logger.debug('Try to read status code from chain...')
        assert_equal(chan.recv_exit_status(), exit_code,
                     'Upgrade script fails with next message {0}'.format(
                         ''.join(stderr)))
    else:
        path = "{0}/{1} --no-rollback --password {2}".format(script_path,
                                                             script_name,
                                                             password)
        chan, stdin, stderr, stdout = node_ssh.execute_async(path)
        logger.debug('Try to read status code from chain...')
        assert_equal(chan.recv_exit_status(), exit_code,
                     'Upgrade script fails with next message {0}'.format(
                         ''.join(stderr)))


@logwrap
def wait_upgrade_is_done(node_ssh, timeout, phrase):
    cmd = "grep '{0}' /var/log/fuel_upgrade.log".format(phrase)
    try:
        wait(
            lambda: not node_ssh.execute(cmd)['exit_code'], timeout=timeout)
    except Exception as e:
        a = node_ssh.execute(cmd)
        logger.error(e)
        assert_equal(0, a['exit_code'], a['stderr'])


@logwrap
def wait_rollback_is_done(node_ssh, timeout):
    logger.debug('start waiting for rollback done')
    wait(
        lambda: not node_ssh.execute(
            "grep 'UPGRADE FAILED' /var/log/fuel_upgrade.log"
        )['exit_code'], timeout=timeout)


@logwrap
def get_package_versions_from_node(remote, name, os_type):
    if os_type and 'Ubuntu' in os_type:
        cmd = "dpkg-query -W -f='${Version}' %s" % name
    else:
        cmd = "rpm -q {0}".format(name)
    try:
        result = ''.join(remote.execute(cmd)['stdout'])
        return result.strip()
    except Exception:
        logger.error(traceback.format_exc())
        raise


@logwrap
def check_enable_experimental_mode(remote, path):
        cmd = "sed '/feature_groups:" \
              "/a \ \ \ \ - experimental' -i {0}".format(path)
        result = remote.execute(cmd)
        assert_equal(0, result['exit_code'], result['stderr'])


@logwrap
def restart_nailgun(remote):
    cmd = 'dockerctl shell nailgun supervisorctl restart nailgun'
    result = remote.execute(cmd)
    assert_equal(0, result['exit_code'], result['stderr'])


def find_backup(remote):
    try:
        arch_dir = ''.join(
            remote.execute("ls -1u /var/backup/fuel/ | sed -n 1p")['stdout'])
        arch_path = ''.join(
            remote.execute("ls -1u /var/backup/fuel/{0}/*.lrz".
                           format(arch_dir.strip()))["stdout"])
        logger.debug('arch_path is {0}'.format(arch_path))
        return arch_path
    except Exception as e:
        logger.error('exception is {0}'.format(e))
        raise e


@logwrap
def backup_check(remote):
    logger.info("Backup check archive status")
    path = find_backup(remote)
    assert_true(path, "Can not find backup. Path value {0}".format(path))
    arch_result = ''.join(
        remote.execute(("if [ -e {0} ]; "
                        "then echo  Archive exists;"
                        " fi").format(path.rstrip()))["stdout"])
    assert_true("Archive exists" in arch_result, "Archive does not exist")


@logwrap
def restore_check_sum(remote):
    logger.debug('Check if removed file /etc/fuel/data was restored')
    res = remote.execute("if [ -e /etc/fuel/data ]; "
                         "then echo Restored!!;"
                         " fi")
    assert_true("Restored!!" in ''.join(res['stdout']).strip(),
                'Test file /etc/fuel/data '
                'was not restored!!! {0}'.format(res['stderr']))
    logger.info("Restore check md5sum")
    md5sum_backup = remote.execute("cat /etc/fuel/sum")
    assert_true(''.join(md5sum_backup['stdout']).strip(),
                'Command cat /etc/fuel/sum '
                'failed with {0}'.format(md5sum_backup['stderr']))
    md5sum_restore = remote.execute("md5sum /etc/fuel/data | sed -n 1p "
                                    " | awk '{print $1}'")
    assert_equal(md5sum_backup, md5sum_restore,
                 "md5sums not equal: backup{0}, restore{1}".
                 format(md5sum_backup, md5sum_restore))


@logwrap
def iptables_check(remote):
    logger.info("Iptables check")
    remote.execute("iptables-save > /etc/fuel/iptables-restore")
    iptables_backup = remote.execute("sed -e '/^:/d; /^#/d' "
                                     " /etc/fuel/iptables-backup")
    iptables_restore = remote.execute("sed -e '/^:/d; /^#/d' "
                                      " /etc/fuel/iptables-restore")
    assert_equal(iptables_backup, iptables_restore,
                 "list of iptables rules are not equal")


@logwrap
def check_mysql(remote, node_name):
    check_cmd = 'pkill -0 -x mysqld'
    check_crm_cmd = ('crm resource status clone_p_mysql |'
                     ' grep -q "is running on: $HOSTNAME"')
    check_galera_cmd = ("mysql --connect_timeout=5 -sse \"SELECT"
                        " VARIABLE_VALUE FROM"
                        " information_schema.GLOBAL_STATUS"
                        " WHERE VARIABLE_NAME"
                        " = 'wsrep_local_state_comment';\"")
    try:
        wait(lambda: remote.execute(check_cmd)['exit_code'] == 0,
             timeout=300)
        logger.info('MySQL daemon is started on {0}'.format(node_name))
    except TimeoutError:
        logger.error('MySQL daemon is down on {0}'.format(node_name))
        raise
    _wait(lambda: assert_equal(remote.execute(check_crm_cmd)['exit_code'], 0,
                               'MySQL resource is NOT running on {0}'.format(
                                   node_name)), timeout=60)
    try:
        wait(lambda: ''.join(remote.execute(
            check_galera_cmd)['stdout']).rstrip() == 'Synced', timeout=600)
    except TimeoutError:
        logger.error('galera status is {0}'.format(''.join(remote.execute(
            check_galera_cmd)['stdout']).rstrip()))
        raise


@logwrap
def install_plugin_check_code(
        remote, plugin, exit_code=0):
    cmd = "cd /var && fuel plugins --install {0} ".format(plugin)
    chan, stdin, stderr, stdout = remote.execute_async(cmd)
    logger.debug('Try to read status code from chain...')
    assert_equal(
        chan.recv_exit_status(), exit_code,
        'Install script fails with next message {0}'.format(''.join(stderr)))


@logwrap
def check_action_logs(scenario, postgres_actions):
    def _check(_action, _group=False):
        assert_true(postgres_actions.action_logs_contain(_action, _group),
                    "Action logs are missed for '{0}'!".format(
                        _action))

    actions = [
        {
            'desc': [''],
            'name': ['master_node_settings'],
            'group': [],
            'regex': False,
        },
        {
            'desc': [r'create\s+.*(cluster|environment|cloud)'],
            'name': ['cluster_collection'],
            'group': ['cluster_attributes', 'network_configuration'],
            'regex': True,
        },
        {
            'desc': ['deploy'],
            'name': ['deploy_changes', 'provision', 'deployment',
                     'cluster_collection', 'check_before_deployment'],
            'group': ['cluster_changes', 'cluster_checking'],
            'regex': True,
        },
        {
            'desc': [r'verif.*\s+.*network|network.*\s+.*verif'],
            'name': ['check_networks', 'verify_networks'],
            'group': ['network_verification'],
            'regex': True,
        },
        {
            'desc': [r'(stop|abort).*(deployment|provision)'],
            'name': ['stop_deployment'],
            'group': ['cluster_changes'],
            'regex': True,
        },
        {
            'desc': [r'reset.*(deployment|provision)'],
            'name': ['reset'],
            'group': ['cluster_changes'],
            'regex': True,
        },
        {
            'desc': [r'rename.*(cluster|environment|cloud)'],
            'name': ['cluster_instance'],
            'group': ['cluster_changes'],
            'regex': True,
        },
        {
            'desc': [r'upgrade'],
            'name': ['releases_collection'],
            'group': ['release_changes'],
            'regex': True,
        },
        {
            'desc': [r'update.*(cluster|environment|cloud)'],
            'name': ['update'],
            'group': ['cluster_changes'],
            'regex': True,
        },
        {
            'desc': [r'upload.*deployment'],
            'name': ['deployment_info'],
            'group': ['orchestrator'],
            'regex': True,
        },
        {
            'desc': [r'upload.*provisioning'],
            'name': ['provisioning_info'],
            'group': ['orchestrator'],
            'regex': True,
        },
        # Logging of OSTF isn't implemented yet, so actions list is
        # empty
        {
            'desc': ['OSTF', 'Health'],
            'name': [],
            'group': [],
            'regex': False,
        },
    ]

    # Check logs in Nailgun database
    for action in actions:
        if action['regex']:
            if not any(re.search(regex, scenario, re.IGNORECASE)
                       for regex in action['desc']):
                continue
        elif not any(action in scenario for action in action['desc']):
            logger.info(action['desc'])
            continue
        for action_name in action['name']:
            _check(action_name, _group=False)
        for action_group in action['group']:
            _check(action_group, _group=True)


def execute_query_on_collector(collector_remote, master_uuid, query,
                               collector_db='collector',
                               collector_db_user='collector',
                               collector_db_pass='collector'):
    if master_uuid is not None:
        query = "{0} where master_node_uid = '{1}';".format(query, master_uuid)
    cmd = 'PGPASSWORD={0} psql -qt -h 127.0.0.1 -U {1} -d {2} -c "{3}"'.\
        format(collector_db_pass, collector_db_user, collector_db, query)
    logger.debug('query collector is {0}'.format(cmd))
    return ''.join(collector_remote.execute(cmd)['stdout']).strip()


def count_stats_on_collector(collector_remote, master_uuid):
    return execute_query_on_collector(collector_remote, master_uuid=None,
                                      query="select (select count(*) from "
                                            "action_logs where master_node_uid"
                                            " = \'{0}\') + (select count(*) "
                                            "from installation_structures "
                                            "where master_node_uid = \'{0}\')".
                                      format(master_uuid))


@logwrap
def check_stats_on_collector(collector_remote, postgres_actions, master_uuid):
    sent_logs_count = postgres_actions.count_sent_action_logs()
    logger.info("Number of logs that were sent to collector: {}".format(
        sent_logs_count
    ))
    logs = collector_remote.get_action_logs_count(master_uuid)
    logger.info("Number of logs that were saved on collector: {}".format(logs))
    assert_true(sent_logs_count <= int(logs),
                ("Count of action logs in Nailgun DB ({0}) is bigger than on "
                 "Collector ({1}), but should be less or equal").format(
                    sent_logs_count, logs))

    sum_stats_count = len(
        [collector_remote.get_installation_info(master_uuid)['id']])
    assert_equal(int(sum_stats_count), 1,
                 "Installation structure wasn't saved on Collector side proper"
                 "ly: found: {0}, expected: 1 record.".format(sum_stats_count))

    summ_stats = collector_remote.get_installation_info_data(master_uuid)
    general_stats = {
        'clusters_num': int,
        'allocated_nodes_num': int,
        'unallocated_nodes_num': int,
        'fuel_release': dict,
        'clusters': list,
        'user_information': dict,
    }

    # Check that important data (clusters number, nodes number, nodes roles,
    # user's email, used operation system, OpenStack stats) is saved correctly
    for stat_type in general_stats.keys():
        assert_true(type(summ_stats[stat_type]) == general_stats[stat_type],
                    "Installation structure in Collector's DB doesn't contain"
                    "the following stats: {0}".format(stat_type))

    real_clusters_number = int(postgres_actions.run_query(
        db='nailgun', query='select count(*) from clusters;'))
    assert_equal(real_clusters_number, summ_stats['clusters_num'],
                 'Real clusters number is {0}, but usage statistics says '
                 'that clusters number is {1}'.format(
                     real_clusters_number, summ_stats['clusters_num']))

    real_allocated_nodes_num = int(postgres_actions.run_query(
        db='nailgun',
        query='select count(id) from nodes where cluster_id is not Null;'))
    assert_equal(real_allocated_nodes_num, summ_stats['allocated_nodes_num'],
                 'Real allocated nodes number is {0}, but usage statistics '
                 'says that allocated nodes number is {1}'.format(
                     real_allocated_nodes_num,
                     summ_stats['allocated_nodes_num']))

    real_user_email = json.loads(postgres_actions.run_query(
        db='nailgun', query='select settings from master_node_settings;')
    )['statistics']['email']['value']
    assert_equal(real_user_email, summ_stats['user_information']['email'],
                 "Usage statistics contains incorrect user's email address: "
                 "'{0}', but should be {1}".format(
                     summ_stats['user_information']['email'],
                     real_user_email))

    for cluster in summ_stats['clusters']:
        for node in cluster['nodes']:
            assert_true(len(node['roles']) > 0,
                        "Usage statistics contains nodes without roles: node-"
                        "{0} roles: {1}".format(node['id'], node['roles']))
        assert_equal(len(cluster['nodes']), cluster['nodes_num'],
                     "Usage statistics contains incorrect number of nodes"
                     "assigned to cluster!")
        real_cluster_os = postgres_actions.run_query(
            db="nailgun", query="select operating_system from releases where "
                                "id = (select release_id from clusters where "
                                "id  = {0});".format(cluster['id']))
        assert_equal(real_cluster_os, cluster['release']['os'],
                     "Usage statistics contains incorrect operation system "
                     "that is used for environment with ID '{0}'. Expected: "
                     "'{1}', reported: '{2}'.".format(
                         cluster['id'], real_cluster_os,
                         cluster['release']['os']))

    logger.info("Usage stats were properly saved to collector's database.")


@logwrap
def check_stats_private_info(collector_remote, postgres_actions,
                             master_uuid, _settings):
    def _contain_secret_data(data):
        _has_private_data = False
        # Check that stats doesn't contain private data (e.g.
        # specific passwords, settings, emails)
        for _private in private_data.keys():
            _regex = r'(?P<key>"\S+"): (?P<value>[^:]*"{0}"[^:]*)'.format(
                private_data[_private])
            for _match in re.finditer(_regex, data):
                logger.warning('Found private info in usage statistics using '
                               'pattern: {0}'. format(_regex))
                logger.debug('Usage statistics with private data:\n {0}'.
                             format(data))
                logger.error("Usage statistics contains private info: '{type}:"
                             " {value}'. Part of the stats: {match}".format(
                                 type=_private,
                                 value=private_data[_private],
                                 match=_match.group('key', 'value')))
                _has_private_data = True
        # Check that stats doesn't contain private types of data (e.g. any kind
        # of passwords)
        for _data_type in secret_data_types.keys():
            _regex = (r'(?P<secret>"[^"]*{0}[^"]*": (\{{[^\}}]+\}}|\[[^\]+]\]|'
                      r'"[^"]+"))').format(secret_data_types[_data_type])

            for _match in re.finditer(_regex, data, re.IGNORECASE):
                logger.warning('Found private info in usage statistics using '
                               'pattern: {0}'. format(_regex))
                logger.debug('Usage statistics with private data:\n {0}'.
                             format(data))
                logger.error("Usage statistics contains private info: '{type}:"
                             " {value}'. Part of the stats: {match}".format(
                                 type=_data_type,
                                 value=secret_data_types[_data_type],
                                 match=_match.group('secret')))
                _has_private_data = True
        return _has_private_data

    def _contain_public_ip(data, _used_networks):
        _has_puplic_ip = False
        _ip_regex = (r'\b((\d|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])\.){3}'
                     r'(\d|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])\b')
        _not_public_regex = [
            r'\b10(\.\d{1,3}){3}',
            r'\b127(\.\d{1,3}){3}',
            r'\b169\.254(\.\d{1,3}){2}',
            r'172\.(1[6-9]|2[0-9]|3[0-1])(\.\d{1,3}){2}',
            r'192\.168(\.\d{1,3}){2}',
            r'2(2[4-9]|[3-5][0-9])(\.\d{1,3}){3}'
        ]
        for _match in re.finditer(_ip_regex, data):
            # If IP address isn't public and doesn't belong to defined for
            # deployment pools (e.g. admin, public, storage), then skip it
            if any(re.search(_r, _match.group()) for _r in _not_public_regex) \
                    and not any(IPAddress(_match.group()) in IPNetwork(net) for
                                net in _used_networks):
                continue
            logger.debug('Usage statistics with piblic IP(s):\n {0}'.
                         format(data))
            logger.error('Found public IP in usage statistics: "{0}"'.format(
                _match.group()))
            _has_puplic_ip = True
        return _has_puplic_ip

    private_data = {
        'hostname': _settings['HOSTNAME'],
        'dns_domain': _settings['DNS_DOMAIN'],
        'dns_search': _settings['DNS_SEARCH'],
        'dns_upstream': _settings['DNS_UPSTREAM'],
        'fuel_password': _settings['FUEL_ACCESS']['password'] if
        _settings['FUEL_ACCESS']['password'] != 'admin'
        else 'DefaultPasswordIsNotAcceptableForSearch',
        'nailgun_password': _settings['postgres']['nailgun_password'],
        'keystone_password': _settings['postgres']['keystone_password'],
        'ostf_password': _settings['postgres']['ostf_password'],
        'cobbler_password': _settings['cobbler']['password'],
        'astute_password': _settings['astute']['password'],
        'mcollective_password': _settings['mcollective']['password'],
        'keystone_admin_token': _settings['keystone']['admin_token'],
        'keystone_nailgun_password': _settings['keystone']['nailgun_password'],
        'kesytone_ostf_password': _settings['keystone']['ostf_password'],
    }

    secret_data_types = {
        'some_password': 'password',
        'some_login': 'login',
        'some_tenant': 'tenant',
        'some_token': 'token',
        'some_ip': '\bip\b',
        'some_netmask': 'netmask',
        'some_network': 'network\b',
    }

    action_logs = [l.strip() for l in postgres_actions.run_query(
        'nailgun', 'select id from action_logs;').split('\n')]
    sent_stats = str(collector_remote.get_installation_info_data(master_uuid))
    logger.debug('installation structure is {0}'.format(sent_stats))
    used_networks = [POOLS[net_name][0] for net_name in POOLS.keys()]
    has_no_private_data = True

    logger.debug("Looking for private data in the installation structure, "
                 "that was sent to collector")

    if _contain_secret_data(sent_stats) or _contain_public_ip(sent_stats,
                                                              used_networks):
        has_no_private_data = False

    for log_id in action_logs:
        log_data = postgres_actions.run_query(
            'nailgun',
            "select additional_info from action_logs where id = '{0}';".format(
                log_id
            ))
        logger.debug("Looking for private data in action log with ID={0}".
                     format(log_id))
        if _contain_secret_data(log_data) or _contain_public_ip(log_data,
                                                                used_networks):
            has_no_private_data = False

    assert_true(has_no_private_data, 'Found private data in stats, check test '
                                     'output and logs for details.')
    logger.info('Found no private data in logs')


def check_kernel(kernel, expected_kernel):
    assert_equal(kernel, expected_kernel,
                 "kernel version is wrong, it is {0}".format(kernel))


@logwrap
def external_dns_check(remote_slave):
    logger.info("External dns check")
    ext_dns_ip = ''.join(
        remote_slave.execute("grep {0} /etc/resolv.dnsmasq.conf | "
                             "awk {{'print $2'}}".
                             format(EXTERNAL_DNS))["stdout"]).rstrip()
    assert_equal(ext_dns_ip, EXTERNAL_DNS,
                 "/etc/resolv.dnsmasq.conf does not contain external dns ip")
    command_hostname = ''.join(
        remote_slave.execute("host {0} | awk {{'print $5'}}"
                             .format(PUBLIC_TEST_IP))
        ["stdout"]).rstrip()
    hostname = 'google-public-dns-a.google.com.'
    assert_equal(command_hostname, hostname,
                 "Can't resolve hostname")


@logwrap
def external_ntp_check(remote_slave, vrouter_vip):
    logger.info("External ntp check")
    ext_ntp_ip = ''.join(
        remote_slave.execute("awk '/^server +{0}/{{print $2}}' "
                             "/etc/ntp.conf".
                             format(EXTERNAL_NTP))["stdout"]).rstrip()
    assert_equal(ext_ntp_ip, EXTERNAL_NTP,
                 "/etc/ntp.conf does not contain external ntp ip")
    try:
        wait(
            lambda: not is_ntpd_active(remote_slave, vrouter_vip), timeout=120)
    except Exception as e:
        logger.error(e)
        status = is_ntpd_active(remote_slave, vrouter_vip)
        assert_equal(
            status, 1, "Failed updated ntp. "
                       "Exit code is {0}".format(status))


def check_swift_ring(remote):
    for ring in ['object', 'account', 'container']:
        res = ''.join(remote.execute(
            "swift-ring-builder /etc/swift/{0}.builder".format(
                ring))['stdout'])
        logger.debug("swift ring builder information is {0}".format(res))
        balance = re.search('(\d+.\d+) balance', res).group(1)
        assert_true(float(balance) < 10,
                    "swift ring builder {1} is not ok,"
                    " balance is {0}".format(balance, ring))


def check_oswl_stat(postgres_actions, remote_collector, master_uid,
                    operation='current',
                    resources=['vm', 'flavor', 'volume', 'image',
                               'tenant', 'keystone_user']):
    logger.info("Checking that all resources were collected...")
    expected_resource_count = {
        'current':
        {'vm': 0,
         'flavor': 6,
         'volume': 0,
         'image': 0,
         'tenant': 2,
         'keystone_user': 8
         },
        'modified':
        {'vm': 0,
         'flavor': 0,
         'volume': 0,
         'image': 0,
         'tenant': 0,
         'keystone_user': 0
         },
        'removed':
        {'vm': 0,
         'flavor': 0,
         'volume': 0,
         'image': 0,
         'tenant': 0,
         'keystone_user': 0
         }
    }
    for resource in resources:
        q = "select resource_data from oswl_stats where" \
            " resource_type = '\"'\"'{0}'\"'\"';".format(resource)
        resource_data = json.loads(postgres_actions.run_query('nailgun', q))

        logger.debug('db return {0}'.format(resource_data))
        assert_true(len(resource_data['added']) >
                    expected_resource_count[operation][resource],
                    "resource {0} wasn't added,"
                    " added is {1}".format(resource, resource_data['added']))
        assert_true(len(resource_data[operation]) >
                    expected_resource_count[operation][resource],
                    "number of resources in current {0},"
                    " expected is {1}".format(len(resource_data[operation]),
                                              expected_resource_count[
                                                  operation][resource]))

    # check stat on collector side
    sent_logs_count = postgres_actions.count_sent_action_logs(
        table='oswl_stats')
    logger.info("Number of logs that were sent to collector: {}".format(
        sent_logs_count
    ))
    logger.debug('oswls are {}'.format(remote_collector.get_oswls(master_uid)))
    logs = len(remote_collector.get_oswls(master_uid))
    logger.info("Number of logs that were saved"
                " on collector: {}".format(logs))
    assert_true(sent_logs_count <= int(logs),
                ("Count of action logs in Nailgun DB ({0}) is bigger than on "
                 "Collector ({1}), but should be less or equal").format(
                    sent_logs_count, logs))
    for resource in resources:
        resource_data = remote_collector.get_oswls_by_resource_data(
            master_uid, resource)

        logger.debug('resource data on'
                     ' collector is {0}'.format(resource_data))
        assert_true(len(resource_data['added']) >
                    expected_resource_count[operation][resource],
                    "resource {0} wasn't added,"
                    " added is {1}".format(resource, resource_data['added']))
        assert_true(len(resource_data[operation]) >
                    expected_resource_count[operation][resource],
                    "number of resources in current {0},"
                    " expected is {1}".format(len(resource_data[operation]),
                                              expected_resource_count[
                                                  operation][resource]))

    logger.info("OSWL stats were properly saved to collector's database.")


@logwrap
def get_file_size(remote, file_name, file_path):
    file_size = remote.execute(
        'stat -c "%s" {0}/{1}'.format(file_path, file_name))
    assert_equal(
        int(file_size['exit_code']), 0, "Failed to get '{0}/{1}' file stats on"
                                        " remote node".format(file_path,
                                                              file_name))
    return int(file_size['stdout'][0].rstrip())


@logwrap
def check_ping(remote, host, deadline=10, size=56, timeout=1, interval=1):
    """Check network connectivity from
     remote to host using ICMP (ping)
    :param remote: SSHClient
    :param host: string IP address or host/domain name
    :param deadline: time in seconds before ping exits
    :param size: size of data to be sent
    :param timeout: time to wait for a response, in seconds
    :param interval: wait interval seconds between sending each packet
    :return: bool: True if ping command
    """
    cmd = ("ping -W {timeout} -i {interval} -s {size} -c 1 -w {deadline} "
           "{host}".format(host=host,
                           size=size,
                           timeout=timeout,
                           interval=interval,
                           deadline=deadline))
    return int(remote.execute(cmd)['exit_code']) == 0


@logwrap
def check_nova_dhcp_lease(remote, instance_ip, instance_mac, node_dhcp_ip):
    logger.debug("Checking DHCP server {0} for lease {1} with MAC address {2}"
                 .format(node_dhcp_ip, instance_ip, instance_mac))
    res = remote.execute('ip link add dhcptest0 type veth peer name dhcptest1;'
                         'brctl addif br100 dhcptest0;'
                         'ifconfig dhcptest0 up;'
                         'ifconfig dhcptest1 hw ether {1};'
                         'ifconfig dhcptest1 up;'
                         'dhcpcheck request dhcptest1 {2} --range_start {0} '
                         '--range_end 255.255.255.255 | fgrep \" {2} \";'
                         'ifconfig dhcptest1 down;'
                         'ifconfig dhcptest0 down;'
                         'brctl delif br100 dhcptest0;'
                         'ip link delete dhcptest0;'
                         .format(instance_ip, instance_mac, node_dhcp_ip))
    res_str = ''.join(res['stdout'])
    logger.debug("DHCP server answer: {}".format(res_str))
    return ' ack ' in res_str


def check_available_mode(remote):
    command = ('umm status | grep runlevel &>/dev/null && echo "True" '
               '|| echo "False"')
    if remote.execute(command)['exit_code'] == 0:
        return ''.join(remote.execute(command)['stdout']).strip()
    else:
        return ''.join(remote.execute(command)['stderr']).strip()


def check_auto_mode(remote):
    command = ('umm status | grep umm &>/dev/null && echo "True" '
               '|| echo "False"')
    if remote.execute(command)['exit_code'] == 0:
        return ''.join(remote.execute(command)['stdout']).strip()
    else:
        return ''.join(remote.execute(command)['stderr']).strip()


def is_ntpd_active(remote, ntpd_ip):
    cmd = 'ntpdate -d -p 4 -t 0.2 -u {0}'.format(ntpd_ip)
    return (not remote.execute(cmd)['exit_code'])


def check_repo_managment(remote):
    """Check repo managment

    run 'yum -y clean all && yum check-update' or
        'apt-get clean all && apt-get update' exit code should be 0

    :type devops_node: Node
        :rtype True or False
    """
    if OPENSTACK_RELEASE == OPENSTACK_RELEASE_UBUNTU:
        cmd = "apt-get clean all && apt-get update > /dev/null 2>&1"
    else:
        cmd = "yum -y clean all && yum check-update > /dev/null 2>&1"
    remote.check_call(cmd)


def check_public_ping(remotes):
    """ Check if ping public vip
    :type remotes: list
    """
    cmd = ('ruby /etc/puppet/modules/osnailyfacter/'
           'modular/virtual_ips/public_vip_ping_post.rb')
    for remote in remotes:
        res = remote.execute(cmd)
        assert_equal(0, res['exit_code'],
                     'Public ping check failed:'
                     ' {0}'.format(res))


def check_cobbler_node_exists(remote, node_id):
    """Check node with following node_id
    is present in the cobbler node list
    :param remote: SSHClient
    :param node_id: fuel node id
    :return: bool: True if exit code of command (node) == 0
    """
    logger.debug("Check that cluster contains node with ID:{0} ".
                 format(node_id))
    node = remote.execute(
        'dockerctl shell cobbler bash -c "cobbler system list" | grep '
        '-w "node-{0}"'.format(node_id))
    return int(node['exit_code']) == 0


def check_cluster_presence(cluster_id, postgres_actions):
    logger.debug("Check cluster presence")
    query_result = postgres_actions.run_query(
        db='nailgun',
        query="select id from clusters where id={0}".format(cluster_id))
    return str(cluster_id) in query_result


def check_haproxy_backend(remote,
                          services=None, nodes=None,
                          ignore_services=None, ignore_nodes=None):
    """Check DOWN state of HAProxy backends. Define names of service or nodes
    if need check some specific service or node. Use ignore_services for ignore
    service status on all nodes. Use ignore_nodes for ignore all services on
    all nodes. Ignoring has a bigger priority.

    :type remote: SSHClient
    :type service: List
    :type nodes: List
    :type ignore_services: List
    :type ignore_nodes: List
        :rtype: Dict
    """
    cmd = 'haproxy-status | egrep -v "BACKEND|FRONTEND" | grep "DOWN"'

    positive_filter = (services, nodes)
    negativ_filter = (ignore_services, ignore_nodes)
    grep = ['|egrep "{}"'.format('|'.join(n)) for n in positive_filter if n]
    grep.extend(
        ['|egrep -v "{}"'.format('|'.join(n)) for n in negativ_filter if n])

    return remote.execute("{}{}".format(cmd, ''.join(grep)))
