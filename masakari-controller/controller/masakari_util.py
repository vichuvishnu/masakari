# !/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright(c) 2015 Nippon Telegraph and Telephone Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Management module of utility classes for VM recovery control
"""
import ConfigParser
import datetime
import json
import logging
import os
import paramiko
import re
import masakari_config as config
import socket
import subprocess
import sys
import syslog
import threading
import traceback
from eventlet import greenthread
import errno

from keystoneauth1 import loading
from keystoneauth1 import session
from keystoneclient import client as keystone_client
from novaclient import client as nova_client
from novaclient import exceptions
from sqlalchemy import exc

parentdir = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                         os.path.pardir))
# # rootdir = os.path.abspath(os.path.join(parentdir, os.path.pardir))
# # project root directory needs to be add at list head rather than tail
# # this file named 'masakari' conflicts to the directory name
if parentdir not in sys.path:
    sys.path = [parentdir] + sys.path

import db.api as dbapi
from db.models import NotificationList, VmList, ReserveList

class RecoveryControllerUtilDb(object):

    """
    DB-related utility classes for VM recovery control
    """

    def __init__(self, config_object):
        self.rc_config = config_object
        self.rc_util = RecoveryControllerUtil(self.rc_config)
        self.rc_util_ap = RecoveryControllerUtilApi(self.rc_config)

    def insert_vm_list_db(self, session, notification_id,
                          notification_uuid, retry_cnt):
        """
        VM list table registration
        :param :cursor: cursor object
        :param :notification_id: Notification ID
                (used as search criteria for notification list table)
        :param :notification_uuid:VM of uuid
                (used as the registered contents of the VM list table)
        :param :retry_cnt:Retry count
                (used as the registered contents of the VM list table)
        :return :primary_id: The value of LAST_INSERT_ID
        """

        try:
            res = dbapi.get_all_notification_list_by_notification_id(
                session,
                notification_id
            )
            # Todo(sampath): select first and only object from the list
            # log if many records
            notification_recover_to = res[0].recover_to
            notification_recover_by = res[0].recover_by
            vm_item = dbapi.add_vm_list(session,
                                        datetime.datetime.now(),
                                        "0",
                                        notification_uuid,
                                        "0",
                                        str(retry_cnt),
                                        notification_id,
                                        notification_recover_to,
                                        str(notification_recover_by)
                                        )

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0001",
                                      syslog.LOG_INFO)
            primary_id = vm_item.id

            return primary_id

        except KeyError:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0002",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : KeyError in insert_vm_list_db()."
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise KeyError

        except exc.SQLAlchemyError:
            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0003",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : sqlalchemy error in insert_vm_list_db()."
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise exc.SQLAlchemyError

        except:
            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0004",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)

            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : Exception in insert_vm_list_db()."
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise

    def insert_notification_list_db(self, jsonData, recover_by, session):
        """
           Insert into notification_list DB from notification JSON.
           :param :jsonData: notifocation json data.
           :param :recover_by:node recover(0)/VM recover(1)/process error(2)
           :param :cursor: cursor object
           :return :ret_dic:and return the information that was registered to
                       notification_list table in the dictionary type

        """

        # NOTE: The notification item 'endTime' may have a NULL value.
        #       reference : The Notification Spec for RecoveryController.
        # JSON decoder perform null -> None translation
        try:
            if not jsonData.get("endTime"):
                j_endTime = None
            else:
                j_endTime = datetime.datetime.strptime(
                    jsonData.get("endTime"), '%Y%m%d%H%M%S')
            # update and deleted :not yet
            create_at = datetime.datetime.now()
            update_at = None
            delete_at = None
            deleted = 0
            # progress 0:not yet
            progress = 0
            # From /etc/hosts
            # NOTE: Hosts hostname suffix is
            # undetermined("_data_line","_control_line")
            iscsi_ip = None
            controle_ip = socket.gethostbyname(jsonData.get("hostname"))
            recover_to = None
            if recover_by == 0:
                recover_to = self._get_reserve_node_from_reserve_list_db(
                    jsonData.get("cluster_port"),
                    jsonData.get("hostname"),
                    session)
                # If reserve node is None, set progress 3.
                if recover_to is None:
                    progress = 3

            def strp_time(u_time):
                """
                Convert unicode time with format '%Y%m%d%H%M%S' to
                datetime format.
                """
                try:
                    d = datetime.datetime.strptime(u_time, '%Y%m%d%H%M%S')
                except (ValueError, TypeError) as e:
                    self.rc_util.syslogout(e, syslog.LOG_WARNING)
                    d = None
                return d

            notification_time = strp_time(jsonData.get("time"))
            notification_startTime = strp_time(jsonData.get("startTime"))
        except Exception as e:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0005",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            self.rc_util.syslogout(e.message, syslog.LOG_ERR)

            raise e
        # Todo: (sampath) correct the exceptions catching
        # Insert to notification_list DB.

        try:
            result = dbapi.add_notification_list(
                session,
                create_at=create_at,
                update_at=update_at,
                delete_at=delete_at,
                deleted=deleted,
                notification_id=jsonData.get("id"),
                notification_type=jsonData.get("type"),
                notification_regionID=jsonData.get("regionID"),
                notification_hostname=jsonData.get("hostname"),
                notification_uuid=jsonData.get("uuid"),
                notification_time=notification_time,
                notification_eventID=jsonData.get("eventID"),
                notification_eventType=jsonData.get("eventType"),
                notification_detail=jsonData.get("detail"),
                notification_startTime=notification_startTime,
                notification_endTime=j_endTime,
                notification_tzname=jsonData.get("tzname"),
                notification_daylight=jsonData.get("daylight"),
                notification_cluster_port=jsonData.get("cluster_port"),
                progress=progress,
                recover_by=recover_by,
                iscsi_ip=iscsi_ip,
                controle_ip=controle_ip,
                recover_to=recover_to
            )

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0006",
                                      syslog.LOG_INFO)

            cnt = dbapi.get_all_reserve_list_by_hostname_not_deleted(
                session,
                jsonData.get("hostname")
            )
            if len(cnt) > 0:
                dbapi.update_reserve_list_by_hostname_as_deleted(
                    session,
                    jsonData.get("hostname"),
                    datetime.datetime.now()
                )

            ret_dic = {
                "create_at": create_at,
                "update_at": update_at,
                "delete_at": delete_at,
                "deleted": deleted,
                "notification_id": jsonData.get("id"),
                "notification_type": jsonData.get("type"),
                "notification_regionID": jsonData.get("regionID"),
                "notification_hostname": jsonData.get("hostname"),
                "notification_uuid": jsonData.get("uuid"),
                "notification_time": jsonData.get("time"),
                "notification_eventID": jsonData.get("eventID"),
                "notification_eventType": jsonData.get("eventType"),
                "notification_detail": jsonData.get("detail"),
                "notification_startTime": jsonData.get("startTime"),
                "notification_endTime": j_endTime,
                "notification_tzname": jsonData.get("tzname"),
                "notification_daylight": jsonData.get("daylight"),
                "notification_cluster_port": jsonData.get("cluster_port"),
                "progress": progress,
                "recover_by": recover_by,
                "iscsi_ip": iscsi_ip,
                "controle_ip": controle_ip,
                "recover_to": recover_to
            }

            return ret_dic

        except Exception as e:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0007",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            self.rc_util.syslogout(e.message, syslog.LOG_ERR)

            raise e

    def _get_reserve_node_from_reserve_list_db(self,
                                               cluster_port,
                                               notification_hostname,
                                               session):
        """
        Get reserve node, check it in use and change to 'enable'.
        :param: con_args: args database connection.
        :param: cluster_port: select keys, cluster port number.
        :param :cursor: cursor object
        :return: hostname: Host name of the spare node machine
                            (obtained from the spare node list table)

        """

        try:
            # Todo(sampath): write the test codes
            #                Check it
            cnt = dbapi.get_one_reserve_list_by_cluster_port_for_update(
                session,
                cluster_port,
                notification_hostname
            )
            if not cnt:
                self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0008",
                                          syslog.LOG_WARNING)
                msg = "The reserve node not exist in reserve_list DB."
                self.rc_util.syslogout(msg, syslog.LOG_WARNING)
                hostname = None
            if not isinstance(cnt, (list, tuple)):
                hostname = cnt.hostname

        except Exception as e:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0010",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            self.rc_util.syslogout(e.message, syslog.LOG_ERR)

            raise e

        return hostname

    def update_notification_list_db(self, session, key, value,
                                    notification_id):
        """
        Notification list table update
        :param :key: Update column name
        :param :value: Updated value
        :param :notification_id: Notification ID
                (updated narrowing condition of notification list table)
        """
        try:
            # Update progress with update_at and delete_at
            now = datetime.datetime.now()
            update_val = {'update_at': now}
            if key == 'progress':
                update_val['progress'] = value
                update_val['delete_at'] = now
            # Updated than progress
            else:
                if hasattr(NotificationList, key):
                    update_val[key] = value
                else:
                    raise AttributeError
            dbapi.update_notification_list_dict(
                session, notification_id, update_val)

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0011",
                                      syslog.LOG_INFO)
        except AttributeError:
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : %s is not in attribute of \
            NotificationList" % (key)
            self.rc_util.syslogout(msg, syslog.LOG_ERR)
            raise AttributeError

        except exc.SQLAlchemyError:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0014",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : SQLAlchemy.Error in \
            update_notification_list_db()."
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise exc.SQLAlchemyError

        except KeyError:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0013",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : KeyError in update_notification_list_db()."
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise KeyError

    def update_vm_list_db(self,  session, key, value, primary_id):
        """
        VM list table update
        :param :key: Update column name
        :param :value: Updated value
        :param :uuid: VM of uuid (updated narrowing condition of VM list table)
        """

        try:
            # Updated progress to start
            now = datetime.datetime.now()
            update_val = {}
            if key == 'progress' and value == 1:
                update_val['update_at'] = now
                update_val['progress'] = value
            # End the progress([success:2][error:3][skipped old:4])
            elif key == 'progress':
                update_val['update_at'] = now
                update_val['progress'] = value
                update_val['delete_at'] = now
            # Update than progress
            else:
                if hasattr(VmList, key):
                    update_val[key] = value
                else:
                    raise AttributeError
            dbapi.update_vm_list_by_id_dict(session, primary_id, update_val)

        except AttributeError:
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : %s is not in attribute of \
            VmList" % (key)
            self.rc_util.syslogout(msg, syslog.LOG_ERR)
            raise AttributeError

        except exc.SQLAlchemyError:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0014",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : SQLAlchemy.Error in \
            update_vm_list_by_id_dict()."
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise exc.SQLAlchemyError

        except KeyError:

            self.rc_util.syslogout_ex("RecoveryControllerUtilDb_0017",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "Exception : KeyError in update_notification_list_db()."
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise KeyError


class RecoveryControllerUtilApi(object):

    """
    API-related utility classes related to VM recovery control
    """

    KEYSTONE_API_VERSION = '3'
    NOVA_API_VERSION = '2'

    def __init__(self, config_object):
        self.rc_config = config_object
        self.rc_util = RecoveryControllerUtil(self.rc_config)

        project_id = self._fetch_project_id()
        auth_args = {
            'auth_url': self.rc_config.conf_nova['auth_url'],
            'username': self.rc_config.conf_nova['admin_user'],
            'password': self.rc_config.conf_nova['admin_password'],
            'project_id': project_id,
            'user_domain_name': self.rc_config.conf_nova['domain'],
            'project_domain_name': self.rc_config.conf_nova['domain'],
            }

        self.auth_session = self._get_session(auth_args)

        conf_dic = self.rc_config.get_value('recover_starter')
        api_retries = conf_dic.get('api_max_retry_cnt')

        self.nova_client = nova_client.Client(self.NOVA_API_VERSION,
                                              session=self.auth_session,
                                              connect_retries=api_retries)

    def _get_session(self, auth_args):
        """ Return Keystone API session object."""
        loader = loading.get_plugin_loader('password')
        auth = loader.load_from_options(**auth_args)
        sess = session.Session(auth=auth)

        return sess

    def _fetch_project_id(self):
        auth_args = {
            'auth_url': self.rc_config.conf_nova['auth_url'],
            'username': self.rc_config.conf_nova['admin_user'],
            'password': self.rc_config.conf_nova['admin_password'],
            'project_name': self.rc_config.conf_nova['project_name'],
            'project_domain_name': self.rc_config.conf_nova['domain'],
            'user_domain_name': self.rc_config.conf_nova['domain'],
            }
        sess = self._get_session(auth_args)

        ks_client = keystone_client.Client(self.KEYSTONE_API_VERSION,
                                           session=sess)
        project_name = self.rc_config.conf_nova['project_name']
        projects = filter(lambda x: (x.name == project_name),
                         ks_client.projects.list())

        msg = ("Project name: %s doesn't exist in project list."
               % self.rc_config.conf_nova['project_name'])
        assert len(projects) == 1, msg

        return projects[0].id

    def do_instance_show(self, uuid):
        """Returns Server Intance.

        :uuid : Instance id
        :return : Server instance
        """
        try:
            self.rc_util.syslogout('Call Server Details API with %s' % uuid,
                                   syslog.LOG_INFO)
            server = self.nova_client.servers.get(uuid)

        except exceptions.ClientException as e:
            error_code = "[RecoveryControllerUtilApi_0001]"
            msg = 'Fails to call Nova get Server Details API: %s' % e
            self.rc_util.syslogout(error_code + msg, syslog.LOG_ERR)

            raise

        return server

    def do_instance_stop(self, uuid):
        """Call Nova instance stop API.

        :param :uuid : Instance id
        :return : None if succeed
        """
        try:
            self.rc_util.syslogout('Call Stop API with %s' % uuid,
                                   syslog.LOG_INFO)
            self.nova_client.servers.stop(uuid)

        except exceptions.Conflict as e:
            msg = "Server instance %s is already in stopped." % uuid
            error_msg = "Original Nova client's error: %e" % e
            self.rc_util.syslogout(msg + error_msg, syslog.LOG_ERR)
            raise EnvironmentError(msg)

        except exceptions.ClientException as e:
            error_code = "[RecoveryControllerUtilApi_0002]"
            msg = 'Fails to call Nova Server Stop API: %s' % e
            self.rc_util.syslogout(error_code + msg, syslog.LOG_ERR)
            raise

    def do_instance_start(self, uuid):
        """Call Nova instance start API.

        :uuid : Instance id
        :return : None if succeed
        """
        try:
            self.rc_util.syslogout('Call Start API with %s' % uuid,
                                   syslog.LOG_INFO)
            self.nova_client.servers.start(uuid)

        except exceptions.Conflict as e:
            msg = "Server instance %s is already in active." % uuid
            error_msg = "Original Nova client's error: %e" % e
            self.rc_util.syslogout(msg + error_msg, syslog.LOG_ERR)
            raise EnvironmentError(msg)

        except exceptions.ClientException as e:
            error_code = "[RecoveryControllerUtilApi_0003]"
            msg = 'Fails to call Nova Server Start API: %s' % e
            self.rc_util.syslogout(error_code + msg, syslog.LOG_ERR)
            raise

    def do_instance_reset(self, uuid, status):
        """ Call Nova reset state API.

        :uuid : Instance id
        :status : Status reset to
        """
        try:
            self.rc_util.syslogout('Call Reset State API with %s to %s' %
                                   (uuid, status), syslog.LOG_INFO)
            self.nova_client.servers.reset_state(uuid, status)

        except exceptions.ClientException as e:
            error_code = "[RecoveryControllerUtilApi_0004]"
            msg = 'Fails to call Nova Server Reset State API: %s' % e
            self.rc_util.syslogout(error_code + msg, syslog.LOG_ERR)
            raise EnvironmentError(msg)

    def fetch_servers_on_hypervisor(self, hypervisor):
        """Fetch server instance list on the hypervisor.

        :hypervisor : hypervisor's hostname
        :return : A list of servers
        """
        opts = {
            'host': hypervisor,
            'all_tenants': True,
            }
        try:
            self.rc_util.syslogout('Fetch Server list on %s' % hypervisor,
                                   syslog.LOG_INFO)
            servers = self.nova_client.servers.list(detailed=False,
                                                    search_opts=opts)
            return [s.id for s in servers]

        except exceptions.ClientException as e:
            error_code = "[RecoveryControllerUtilApi_0005]"
            msg = 'Fails to call Nova Servers List API: %s' % e
            self.rc_util.syslogout(error_code + msg, syslog.LOG_ERR)
            raise

    def do_host_maintenance_mode(self, hostname, mode):
        """
        API_host_maintenance_mode.
        Edit the body of the curl is performed using the nova client.
        :hostname: Target host name
        :mode: change to 'enable'/'disable'
        :return :response_code :response code
        :return :rbody :response body(json)
        """

        nova_variable_url = ""
        nova_body = ""

        try:

            # Set nova_curl_method
            nova_curl_method = "PUT"

            # Set nova_variable_url
            if mode == "enable" or mode == "disable":
                nova_variable_url = "/os-services/" + mode
            else:
                e_msg = "mode is invalid.(mode=%s)" % (mode)
                raise Exception(e_msg)

            # Set nova_body
            nova_body = "{\"host\":\"" + hostname + \
                "\",\"binary\":\"nova-compute\"}"

            response_code, rbody = self._nova_curl_client(nova_curl_method,
                                                          nova_variable_url,
                                                          nova_body)

        except:

            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0006",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "[ nova_curl_method=" + nova_curl_method + " ]"
            self.rc_util.syslogout(msg, syslog.LOG_ERR)
            msg = "[ nova_variable_url=" + nova_variable_url + " ]"
            self.rc_util.syslogout(msg, syslog.LOG_ERR)
            msg = "[ nova_body=" + nova_body + " ]"
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise

        return response_code, rbody

    def do_instance_evacuate(self, uuid, targethost):
        """
        API-evacuate. Edit the body of the curl is performed
        using the nova client.
        :uuid : Instance id to be used in nova cliant curl.
        :targethost: The name or ID of the host where the server is evacuated.
        :return :response_code :response code
        :return :rbody :response body(json)
        """
        try:

            # Set nova_curl_method
            nova_curl_method = "POST"
            # Set nova_variable_url
            nova_variable_url = "/servers/" + uuid + "/action"
            # Set nova_body
            nova_body = "{\"evacuate\":{\"host\":\"" + \
                targethost + "\",\"onSharedStorage\":\"True\"}}"

            response_code, rbody = self._nova_curl_client(nova_curl_method,
                                                          nova_variable_url,
                                                          nova_body)

        except:

            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0007",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = "[ nova_curl_method=" + nova_curl_method + " ]"
            self.rc_util.syslogout(msg, syslog.LOG_ERR)
            msg = "[ nova_variable_url=" + nova_variable_url + " ]"
            self.rc_util.syslogout(msg, syslog.LOG_ERR)
            msg = "[ nova_body=" + nova_body + " ]"
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            raise

        return response_code, rbody

    # TODO(sampath):
    # Use novaclient and omit this code
    # For now, imported this code from current release
    def _get_x_subject_token(self, curl_response):

        x_subject_token = None
        for line in curl_response:
            obj = re.match("(X-Subject-Token:\s*)([\w|-]+)", line,
                           re.IGNORECASE)
            if obj is not None:
                x_subject_token = obj.group(2)
                break

        return x_subject_token

    def _get_body(self, curl_response):
        return curl_response[-1]

    # TODO(sampath):
    # Use novaclient and omit this code
    # For now, imported this code from current release
    def _exe_curl(self, curl):

        conf_dic = self.rc_config.get_value('recover_starter')
        api_max_retry_cnt = conf_dic.get('api_max_retry_cnt')
        api_retry_interval = conf_dic.get('api_retry_interval')

        for cnt in range(0, int(api_max_retry_cnt) + 1):
            line_list = []
            p = subprocess.Popen(curl,
                                 shell=True,
                                 cwd='./',
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)

            out, err = p.communicate()
            rc = p.returncode

            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0008",
                                      syslog.LOG_INFO)
            self.rc_util.syslogout("curl request:" + curl, syslog.LOG_INFO)
            self.rc_util.syslogout("curl response:" + out, syslog.LOG_INFO)
            self.rc_util.syslogout("curl return code:" + str(rc),
                                   syslog.LOG_INFO)

            if rc == 0:
                line_list = out.splitlines()
                # If HTTP status code is 5xx, do retry.
                if re.match("HTTP/1.\d 5\d\d ", line_list[0]) is not None:
                    greenthread.sleep(int(api_retry_interval))
                    continue
                break
            # If curl response code is error, do retry.
            elif rc == 28 or rc == 52 or rc == 55 or rc == 56 or rc == 89:
                greenthread.sleep(int(api_retry_interval))
                continue
            else:
                break

        return line_list

    def _nova_curl_client(self,
                          nova_curl_method=None,
                          nova_variable_url=None,
                          nova_body=None,
                          auth_url=None,
                          admin_user=None,
                          admin_password=None,
                          domain=None,
                          project_id=None,
                          project_name=None):

        nova_client_url = None
        token = None
        response_code = None
        rbody = None

        # Check Required.
        try:
            if nova_curl_method is None:
                raise Exception("Need a nova_curl_method.")
            if nova_curl_method == "POST" \
               or nova_curl_method == "PUT" \
               or nova_curl_method == "PATCH":
                if nova_body is None:
                    e_msg = "method is %s. Need a nova_body." % (
                        nova_curl_method)
                    raise Exception(e_msg)
        except Exception, e:

            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0009",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = e
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

            return None, None

        # Set default value for optional args.
        optinal_arg = self.rc_config.get_value('nova')
        if auth_url is None:
            auth_url = optinal_arg.get("auth_url")
        if admin_user is None:
            admin_user = optinal_arg.get("admin_user")
        if admin_password is None:
            admin_password = optinal_arg.get("admin_password")
        if domain is None:
            domain = optinal_arg.get("domain")
        if project_name is None:
            project_name = optinal_arg.get("project_name")

        api_max_retry_cnt = int(self.rc_config.get_value(
            "recover_starter").get("api_max_retry_cnt"))
        api_retry_cnt_get_detail = 0
        api_retry_cnt_exec_api = 0

        while api_retry_cnt_get_detail <= api_max_retry_cnt and \
                api_retry_cnt_exec_api <= api_max_retry_cnt:

            # I get a token of admin.
            nova_client_url, token, project_id, response_code\
                = self._get_token_admin(auth_url,
                                        domain,
                                        admin_user,
                                        admin_password,
                                        project_name)

            # Get the admintoken by the project_id in scope in the case of
            # non-GET
            if nova_curl_method != "GET":
                nova_client_url, response_code, rbody\
                    = self._get_detail(nova_client_url,
                                       nova_variable_url,
                                       token)

                # The re-implementation in the case of authentication error
                if response_code == "401":
                    api_retry_cnt_get_detail += 1
                    if api_retry_cnt_get_detail > api_max_retry_cnt:
                        error_msg = "detail acquisition failure"
                        raise Exception(error_msg)
                    else:
                        continue

                nova_client_url, token, project_id, response_code\
                    = self._get_token_project_scope(auth_url,
                                                    domain,
                                                    admin_user,
                                                    admin_password,
                                                    project_id)

            # Run the Objective curl
            response_code, rbody\
                = self._run_curl_objective(nova_curl_method,
                                           nova_client_url,
                                           nova_variable_url,
                                           nova_body,
                                           token)

            # The re-implementation in the case of authentication error
            if response_code == "401":
                api_retry_cnt_exec_api += 1
                api_retry_cnt_get_detail = 0
            else:
                break

        return response_code, rbody

    def _get_token_admin(self,
                         auth_url,
                         domain,
                         admin_user,
                         admin_password,
                         project_name):

        response_code = None

        # Make curl for get token.
        token_url = "%s/v3/auth/tokens" % (auth_url)
        token_body = "{ \"auth\": { \"identity\": { \"methods\": " \
            "[ \"password\" ], \"password\": { \"user\":" \
            "{ \"domain\": { \"name\": \"%s\" }, \"name\": " \
            "\"%s\", \"password\": \"%s\" } } }, \"scope\": " \
            "{ \"project\": { \"domain\": { \"name\": \"%s\" }, " \
            "\"name\": \"%s\"} } } }" \
            % (domain, admin_user, admin_password, domain,
               project_name)

        token_curl = "curl " \
            "-i '%s' -X POST -H \"Accept: application/json\" " \
            "-H \"Content-Type: application/json\" -d '%s'" \
            % (token_url,
               token_body)

        # Get token id.
        token_get_res = self._exe_curl(token_curl)

        if len(token_get_res) == 0:
            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0016",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("exec curl command failure", syslog.LOG_ERR)
            raise Exception("exec curl command failure")

        # Token acquisition
        token = self._get_x_subject_token(token_get_res)

        response_code = token_get_res[0].split(" ")[1]

        if response_code != "201":

            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0010",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("token acquisition failure", syslog.LOG_ERR)
            raise Exception("token acquisition failure")

        # Response body acquisition
        res_json = json.loads(token_get_res[-1])
        project_id = res_json.get("token").get("project").get("id")

        catalog_list = res_json.get("token").get("catalog")

        for catalog in catalog_list:
            name = catalog.get("name")
            if name == "nova":
                endpoints = catalog.get("endpoints")
                for endpoint in endpoints:
                    interface = endpoint.get("interface")
                    if interface == "admin":
                        nova_client_url = endpoint.get("url")

        return nova_client_url, token, project_id, response_code

    def _get_detail(self,
                    nova_client_url,
                    nova_variable_url,
                    token):

        rbody = None
        response_code = None

        # Join variable url.
        if nova_variable_url is not None:
            nova_client_url = "%s%s" % (nova_client_url, "/servers/detail")

        nova_client_curl = "curl " \
            "-i \"%s\" -X GET " \
            "-H \"Accept: application/json\" " \
            "-H \"Content-Type: application/json\" " \
            "-H \"X-Auth-Token: %s\"" \
            % (nova_client_url, token)
        nova_exe_res = self._exe_curl(nova_client_curl)

        if len(nova_exe_res) == 0:
            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0017",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("exec curl command failure", syslog.LOG_ERR)
            raise Exception("exec curl command failure")

        response_code = nova_exe_res[0].split(" ")[1]

        if response_code != "200" and response_code != "401":
            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0011",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("detail acquisition failure",
                                   syslog.LOG_ERR)
            raise Exception("detail acquisition failure")
        else:
            try:
                rbody = self._get_body(nova_exe_res)

            except Exception, e:

                self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0012",
                                          syslog.LOG_ERR)
                error_type, error_value, traceback_ = sys.exc_info()
                tb_list = traceback.format_tb(traceback_)
                self.rc_util.syslogout(error_type, syslog.LOG_ERR)
                self.rc_util.syslogout(error_value, syslog.LOG_ERR)
                for tb in tb_list:
                    self.rc_util.syslogout(tb, syslog.LOG_ERR)

                msg = e
                self.rc_util.syslogout(msg, syslog.LOG_ERR)

        return nova_client_url, response_code, rbody

    def _get_token_project_scope(self,
                                 auth_url,
                                 domain,
                                 admin_user,
                                 admin_password,
                                 project_id):

        response_code = None

        # Make curl for get token.
        token_url = "%s/v3/auth/tokens" % (auth_url)
        token_body = "{ \"auth\": { \"identity\": { \"methods\": " \
            "[ \"password\" ], \"password\": { \"user\": " \
            "{ \"domain\": { \"name\": \"%s\" }, \"name\": \"%s\", " \
            "\"password\": \"%s\" } } }, \"scope\": { \"project\": " \
            "{ \"id\": \"%s\"} } } }" \
            % (domain, admin_user, admin_password, project_id)

        token_curl = "curl " \
            "-i '%s' -X POST -H \"Accept: application/json\" " \
            "-H \"Content-Type: application/json\" -d '%s'" \
            % (token_url, token_body)

        # Get token id.
        token_get_res = self._exe_curl(token_curl)

        if len(token_get_res) == 0:
            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0018",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("exec curl command failure", syslog.LOG_ERR)
            raise Exception("exec curl command failure")

        response_code = token_get_res[0].split(" ")[1]

        if response_code != "201":
            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0013",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("token acquisition failure", syslog.LOG_ERR)
            raise Exception("token acquisition failure")

        # Token acquisition
        token = self._get_x_subject_token(token_get_res)

        res_json = json.loads(token_get_res[-1])

        project_id = res_json.get("token").get("project").get("id")
        catalog_list = res_json.get("token").get("catalog")

        for catalog in catalog_list:
            name = catalog.get("name")
            if name == "nova":
                endpoints = catalog.get("endpoints")
                for endpoint in endpoints:
                    interface = endpoint.get("interface")
                    if interface == "admin":
                        nova_client_url = endpoint.get("url")

        return nova_client_url, token, project_id, response_code

    def _run_curl_objective(self,
                            nova_curl_method,
                            nova_client_url,
                            nova_variable_url,
                            nova_body,
                            token):

        rbody = None
        response_code = None

        # Join variable url.
        if nova_variable_url is not None:
            nova_client_url = "%s%s" % (nova_client_url, nova_variable_url)

        nova_client_curl = "curl " \
            "-i \"%s\" -X %s -H \"Content-Type: " \
            "application/json\" -H \"X-Auth-Token: %s\"" \
            % (nova_client_url, nova_curl_method, token)

        if nova_body is not None:
            nova_client_curl = "%s -d '%s'" % (nova_client_curl, nova_body)

        nova_exe_res = self._exe_curl(nova_client_curl)

        if len(nova_exe_res) == 0:
            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0019",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("exec curl command failure", syslog.LOG_ERR)
            raise Exception("exec curl command failure")

        response_code = nova_exe_res[0].split(" ")[1]

        if response_code != "200" and response_code != "202":
            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0014",
                                      syslog.LOG_ERR)
            self.rc_util.syslogout("exec curl command failure", syslog.LOG_ERR)

        try:
            rbody = self._get_body(nova_exe_res)

        except Exception, e:

            self.rc_util.syslogout_ex("RecoveryControllerUtilApi_0015",
                                      syslog.LOG_ERR)
            error_type, error_value, traceback_ = sys.exc_info()
            tb_list = traceback.format_tb(traceback_)
            self.rc_util.syslogout(error_type, syslog.LOG_ERR)
            self.rc_util.syslogout(error_value, syslog.LOG_ERR)
            for tb in tb_list:
                self.rc_util.syslogout(tb, syslog.LOG_ERR)

            msg = e
            self.rc_util.syslogout(msg, syslog.LOG_ERR)

        return response_code, rbody


class RecoveryControllerUtil(object):

    """
    Other utility classes for VM recovery control
    """

    def __init__(self, config_object):
        self.rc_config = config_object

    def syslogout_ex(self, msgid, logOutLevel):
        """
        I output the log to a given log file
        :msgid : Log output message ID(Monitoring message)
        :logOutLevel: Log output level
        """
        monitoring_message = str(threading.current_thread())\
            + " --MonitoringMessage--ID:[%s]" % (msgid)
        self.syslogout(monitoring_message, logOutLevel)

    def syslogout(self, rawmsg, logOutLevel):
        """
        I output the log to a given log file
        :msg : Log output messages
        :logOutLevel: Log output level
        """
        msg = str(threading.current_thread()) + " " + str(rawmsg)

        config_log_dic = self.rc_config.get_value('log')
        logLevel = config_log_dic.get("log_level")

        # Output log
        host = socket.gethostname()

        logger = logging.getLogger()

        wk_setLevel = ""
        if logLevel == syslog.LOG_DEBUG:
            wk_setLevel = logging.DEBUG
        elif logLevel == syslog.LOG_INFO or logLevel == syslog.LOG_NOTICE:
            wk_setLevel = logging.INFO
        elif logLevel == syslog.LOG_WARNING:
            wk_setLevel = logging.WARNING
        elif logLevel == syslog.LOG_ERR:
            wk_setLevel = logging.ERROR
        elif logLevel == syslog.LOG_CRIT or logLevel == syslog.LOG_ALERT or \
                logLevel == syslog.LOG_EMERG:
            wk_setLevel = logging.CRITICAL
        else:
            wk_setLevel = logging.ERROR

        logger.setLevel(wk_setLevel)
        f = "%(asctime)s " + host + \
            " masakari-controller(%(process)d): %(levelname)s: %(message)s'"
        formatter = logging.Formatter(fmt=f, datefmt='%b %d %H:%M:%S')
        log_dir = '/var/log/masakari/'

        # create log dir if not created
        try:
            os.makedirs(log_dir)
        except OSError as exc:
            if exc.errno == errno.EEXIST and os.path.isdir(log_dir):
                pass
            else:
                raise

        fh = logging.FileHandler(
            filename='/var/log/masakari/masakari-controller.log')

        fh.setLevel(wk_setLevel)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        if logOutLevel == syslog.LOG_DEBUG:
            logger.debug(msg)
        elif logOutLevel == syslog.LOG_INFO or \
                logOutLevel == syslog.LOG_NOTICE:
            logger.info(msg)
        elif logOutLevel == syslog.LOG_WARNING:
            logger.warn(msg)
        elif logOutLevel == syslog.LOG_ERR:
            logger.error(msg)
        elif logOutLevel == syslog.LOG_CRIT or \
                logOutLevel == syslog.LOG_ALERT or \
                logOutLevel == syslog.LOG_EMERG:
            logger.critical(msg)
        else:
            logger.debug(msg)

        logger.removeHandler(fh)
