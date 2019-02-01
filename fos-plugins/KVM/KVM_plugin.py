#!/usr/bin/env python3

# Copyright (c) 2014,2018 ADLINK Technology Inc.
#
# See the NOTICE file(s) distributed with this work for additional
# information regarding copyright ownership.
#
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# http://www.eclipse.org/legal/epl-2.0, or the Apache License, Version 2.0
# which is available at https://www.apache.org/licenses/LICENSE-2.0.
#
# SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
#
# Contributors: Gabriele Baldoni, ADLINK Technology Inc. - Base plugins set

import sys
import os
import uuid
import json
import signal
import random
import time
import re
import libvirt
import ipaddress
import threading
import base64
import binascii
from mvar import MVar
from fog05 import Yaks_Connector
from fog05.DLogger import DLogger
from fog05.interfaces.States import State
from fog05.interfaces.RuntimePluginFDU import *
from KVMFDU import KVMFDU
from jinja2 import Environment


class KVM(RuntimePluginFDU):

    def __init__(self, name, version, plugin_uuid, yaks_locator, nodeid,
                    manifest):
        super(KVM, self).__init__(version, plugin_uuid)
        self.name = name
        loc = yaks_locator.split('/')[1]
        self.connector = Yaks_Connector(loc)
        self.logger = DLogger(debug_flag=True)
        self.node = nodeid
        self.manifest = manifest
        self.configuration = manifest.get('configuration',{})
        self.pid = os.getpid()
        self.var = MVar()
        self.agent_conf = \
            self.connector.loc.actual.get_node_configuration(self.node)

        self.logger.info('__init__()', ' Hello from KVM Plugin')
        self.BASE_DIR = os.path.join(
            self.agent_conf.get('agent').get('path'),'kvm')
        self.DISK_DIR = 'disks'
        self.IMAGE_DIR = 'images'
        self.LOG_DIR = 'logs'
        file_dir = os.path.dirname(__file__)
        self.DIR = os.path.abspath(file_dir)
        self.conn = None
        self.images = {}
        self.flavors = {}
        self.lock = threading.Lock()
        signal.signal(signal.SIGINT, self.__catch_signal)

    def __catch_signal(self, signal, _):
        if signal == 2:
            self.var.put(signal)

    def start_runtime(self):
        self.logger.info('startRuntime()', ' KVM Plugin - Connecting to KVM')
        self.conn = libvirt.open('qemu:///system')
        self.logger.info('startRuntime()', '[ DONE ] KVM Plugin - Connecting to KVM')

        '''check if dirs exists if not exists create'''
        if self.call_os_plugin_function('dir_exists', {'dir_path': self.BASE_DIR}):
            if not self.call_os_plugin_function('dir_exists', {'dir_path': os.path.join(self.BASE_DIR, self.DISK_DIR)}):
                self.call_os_plugin_function(
                    'create_dir', {'dir_path': os.path.join(self.BASE_DIR, self.DISK_DIR)})
            if not self.call_os_plugin_function('dir_exists', {'dir_path': os.path.join(self.BASE_DIR, self.IMAGE_DIR)}):
                self.call_os_plugin_function(
                    'create_dir', {'dir_path': os.path.join(self.BASE_DIR, self.IMAGE_DIR)})
            if not self.call_os_plugin_function('dir_exists', {'dir_path': os.path.join(self.BASE_DIR, self.LOG_DIR)}):
                self.call_os_plugin_function(
                    'create_dir', {'dir_path': os.path.join(self.BASE_DIR, self.LOG_DIR)})
        else:
            self.call_os_plugin_function(
                'create_dir', {'dir_path': os.path.join(self.BASE_DIR)})
            self.call_os_plugin_function(
                    'create_dir', {'dir_path': os.path.join(self.BASE_DIR, self.DISK_DIR)})
            self.call_os_plugin_function(
                    'create_dir', {'dir_path': os.path.join(self.BASE_DIR, self.IMAGE_DIR)})
            self.call_os_plugin_function(
                    'create_dir', {'dir_path': os.path.join(self.BASE_DIR, self.LOG_DIR)})

        self.connector.loc.desired.observe_node_runtime_fdus(self.node, self.uuid, self.__fdu_observer)

        self.manifest.update({'pid': self.pid})
        self.manifest.update({'status': 'running'})
        self.connector.loc.actual.add_node_plugin(self.node, self.uuid, self.manifest)

        self.logger.info('start_runtime()', ' LXD Plugin - Started...')

        r = self.var.get()
        self.stop_runtime()
        self.connector.close()
        exit(r)

        return self.uuid

    def stop_runtime(self):
        self.logger.info('stopRuntime()', ' KVM Plugin - Destroying running domains')

        for k in list(self.current_fdus.keys()):
            fdu = self.current_fdus.get(k)
            self.__force_fdu_termination(k)
            if fdu.get_state() == State.DEFINED:
                self.undefine_fdu(k)

        for k in list(self.images.keys()):
            self.__remove_image(k)
        for k in list(self.flavors.keys()):
            self.__remove_flavor(k)

        try:
            self.conn.close()
        except libvirt.libvirtError as err:
            pass
        self.logger.info('stopRuntime()', '[ DONE ] KVM Plugin - Bye Bye')

    def get_fdus(self):
        return self.current_fdus

    def define_fdu(self, fdu_manifest):

        self.logger.info('define_fdu()', ' KVM Plugin - Defining a VM')

        entity = None
        img = None
        flavor = None

        fdu_uuid = fdu_manifest.get('uuid')
        edata = fdu_manifest.get('entity_data')
        base_image = edata.get('base_image')
        name = fdu_manifest.get('name')

        if self.is_uuid(base_image):
            self.lock.acquire()
            img = self.images.get(base_image, None)
            if img is None:
                self.logger.error('define_fdu()', '[ ERRO ] KVM Plugin - Cannot find image {}'.format(base_image))
                self.lock.release()
                return
            self.lock.release()
        else:
            self.logger.warning('define_fdu()', '[ WARN ] KVM Plugin - No image id specified defining from manifest information new image id uuid:{}'.format(fdu_uuid))
            img_info = {}
            img_info.update({'uuid': fdu_uuid})
            img_info.update({'name': '{}_img'.format(name)})
            img_info.update({'base_image': base_image})
            img_info.update({'type':'kvm'})
            img_info.update({'format': ''.join(base_image.split('.')[-1:])})
            self.__add_image(img_info)
            img = self.images.get(fdu_uuid, None)
            if img is None:
                self.logger.error('define_fdu()', '[ ERRO ] KVM Plugin - Cannot find image {}'.format(fdu_uuid))

        if edata.get('flavor_id', None) is None:
            self.logger.warning('define_fdu()', '[ WARN ] KVM Plugin - No flavor specified defining from manifest information new flavor uuid:{}'.format(fdu_uuid))
            cpu = edata.get('cpu')
            mem = edata.get('memory')
            disk_size = edata.get('disk_size')
            flavor_info = {}
            flavor_info.update({'name': '{}_flavor'.format(name)})
            flavor_info.update({'uuid': fdu_uuid})
            flavor_info.update({'cpu': cpu})
            flavor_info.update({'memory': mem})
            flavor_info.update({'disk_size': disk_size})
            flavor_info.update({'type':'kvm'})
            self.__add_flavor(flavor_info)
            flavor = self.flavors.get(fdu_uuid, None)
            if flavor is None:
                self.logger.error('define_fdu()', '[ ERRO ] KVM Plugin - Cannot find flavor {}'.format(fdu_uuid))
                self.__write_error_entity(fdu_uuid, 'Flavor not found!')

                return
        else:
            self.lock.acquire()
            flavor = self.flavors.get(edata.get('flavor_id'), None)
            if flavor is None:
                self.logger.error('define_fdu()', '[ ERRO ] KVM Plugin - Cannot find flavor {}'.format(edata.get('flavor_id')))
                self.__write_error_entity(fdu_uuid, 'Flavor not found!')
                self.lock.release()
                return

        entity = KVMFDU(fdu_uuid, name, img.get('uuid'), flavor.get('uuid'))
        entity.set_user_file(edata.get('user-data'))
        entity.set_ssh_key(edata.get('ssh-key'))
        entity.set_networks(edata.get('networks'))


        entity.on_defined()
        vm_info = fdu_manifest
        vm_info.update({'status': 'defined'})
        data = vm_info.get('entity_data')

        data.update({'flavor_id': flavor.get('uuid')})
        data.pop('cpu', None)
        data.pop('memory', None)
        data.pop('disk_size', None)
        data.update({'base_image': img.get('uuid')})

        vm_info.update({'entity_data': data})
        self.current_fdus.update({fdu_uuid: vm_info})
        self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)
        self.logger.info('define_fdu()', '[ DONE ] KVM Plugin - VM Defined uuid: {}'.format(fdu_uuid))

    def undefine_fdu(self, fdu_uuid):

        self.logger.info('undefine_fdu()', ' KVM Plugin - Undefine a VM uuid {}'.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('undefine_fdu()', 'KVM Plugin - FDU not exists')
            raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(fdu_uuid, self.uuid))

        elif fdu.get_state() != State.DEFINED:
            self.logger.error('undefine_fdu()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
            raise StateTransitionNotAllowedException('FDU is not in DEFINED state', 'FDU {} is not in DEFINED state'.format(fdu_uuid))
        else:
            if (self.current_fdus.pop(fdu_uuid, None)) is None:
                self.logger.warning('undefine_fdu()', 'KVM Plugin - pop from entities dict returned none')

            self.connector.loc.actual.remove_node_fdu(self.node, self.uuid, fdu_uuid)
            self.logger.info('undefine_fdu()', '[ DONE ] KVM Plugin - Undefine a VM uuid {} '.format(fdu_uuid))

            return True

    def configure_fdu(self, fdu_uuid):
        '''
        :param entity_uuid:
        :param instance_uuid:
        :return:
        '''

        self.logger.info('configure_fdu()', ' KVM Plugin - Configure a VM uuid {} '.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('configure_fdu()', 'KVM Plugin - FDU not exists')

            raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(fdu_uuid, self.uuid))
        elif fdu.get_state() != State.DEFINED:
            self.logger.error('configure_fdu()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
            raise StateTransitionNotAllowedException('FDU is not in DEFINED state', 'FDU {} is not in DEFINED state'.format(fdu_uuid))
        else:

            name = fdu.name
            flavor = self.flavors.get(fdu.flavor_id, None)
            img = self.images.get(fdu.image_id, None)
            if flavor is None:
                self.logger.error('configure_fdu()', '[ ERRO ] KVM Plugin - Cannot find flavor {}'.format(fdu.flavor_id))
                return

            if img is None:
                self.logger.error('configure_fdu()', '[ ERRO ] KVM Plugin - Cannot find image {}'.format(fdu.image_id))
                return

            disk_path = '{}.{}'.format(fdu_uuid, img.get('format'))
            cdrom_path = '{}_config.iso'.format(fdu_uuid)
            disk_path = os.path.join(self.BASE_DIR, self.DISK_DIR, disk_path)
            cdrom_path = os.path.join(self.BASE_DIR, self.DISK_DIR, cdrom_path)


            ### vm networking TODO: add support for SR-IOV
            if fdu.networks is not None:
                for i, n in enumerate(fdu.networks):
                    if n.get('type') in ['wifi']:

                        nw_ifaces = self.call_os_plugin_function('get_network_informations', {})
                        for iface in nw_ifaces:
                            if self.call_os_plugin_function('get_intf_type', {'name': iface.get('intf_name')})  == 'wireless' and iface.get('available') is True:
                                self.call_os_plugin_function('set_interface_unaviable', {'intf_name':iface.get('intf_name') })
                                n.update({'direct_intf': iface.get('intf_name')})
                        # TODO get available interface from os plugin
                    if n.get('network_uuid') is not None:
                        net = self.connector.loc.actual.find_node_network(self.node, n.get('network_uuid'))
                        if net is None:
                            self.logger.info('configure_fdu()', 'KVM Plugin - Network {} not found!!'.format(n.get('network_uuid')))
                            return
                        else:
                            br_name = net.get('virtual_device')
                            n.update({'br_name': br_name})
                    if n.get('intf_name') is None:
                        n.update({'intf_name': 'veth{0}'.format(i)})
            ######

            vm_xml = self.__generate_dom_xml(fdu, flavor, img)

            vendor_conf = self.__generate_vendor_data(fdu_uuid, self.node)
            vendor_filename = 'vendor_{}.yaml'.format(fdu)
            vendor_conf = binascii.hexlify(base64.b64encode(bytes(vendor_conf, 'utf-8'))).decode()
            self.call_os_plugin_function('store_file',{'content':vendor_conf, 'file_path':self.BASE_DIR, 'filename':vendor_filename})
            vendor_filename = os.path.join(self.BASE_DIR, vendor_filename)
            ### creating cloud-init initial drive TODO: check all the possibilities provided by OSM
            conf_cmd = '{} --hostname {} --uuid {} --vendor-data {}'.format(os.path.join(self.DIR, 'templates',
                                                                        'create_config_drive.sh'), fdu.name, fdu_uuid,
                                                                          vendor_filename)

            rm_temp_cmd = 'rm'
            if fdu.user_file is not None and fdu.user_file != '':
                data_filename = 'userdata_{}'.format(fdu_uuid)
                userdata = binascii.hexlify(base64.b64encode(bytes(fdu.user_file, 'utf-8'))).decode()
                self.call_os_plugin_function('store_file',{'content':userdata, 'file_path':self.BASE_DIR, 'filename':data_filename})
                data_filename = os.path.join(self.BASE_DIR, data_filename)
                conf_cmd = conf_cmd + ' --user-data {}'.format(data_filename)
            if fdu.ssh_key is not None and fdu.ssh_key != '':
                key_filename = 'key_{}.pub'.format(fdu_uuid)
                keydata = binascii.hexlify(base64.b64encode(bytes(fdu.ssh_key, 'utf-8'))).decode()
                self.call_os_plugin_function('store_file',{'content':keydata, 'file_path':self.BASE_DIR, 'filename':key_filename})
                key_filename = os.path.join(self.BASE_DIR, key_filename)
                conf_cmd = conf_cmd + ' --ssh-key {}'.format(key_filename)


            conf_cmd = conf_cmd + ' {}'.format(fdu.cdrom)
            #############

            qemu_cmd = 'qemu-img create -f {} {} {}G'.format(img.get('format'), fdu.disk, flavor.get('disk_size'))

            # As in the first example, but the output format will be qcow2 instead of a raw  disk:
            #
            # qemu-img create -f qcow2 -o preallocation=metadata newdisk.qcow2 15G
            # virt-resize --expand /dev/sda2 olddisk newdisk.qcow2

            dd_cmd = 'dd if={} of={}'.format(img.get('path'), fdu.disk)

            self.call_os_plugin_function('execute_command',{'command':qemu_cmd,'blocking':True, 'external':False})
            self.call_os_plugin_function('execute_command',{'command':conf_cmd,'blocking':True, 'external':False})
            self.call_os_plugin_function('execute_command',{'command':dd_cmd,'blocking':True, 'external':False})

            if fdu.ssh_key is not None and fdu.ssh_key != '':
                self.call_os_plugin_function('remove_file',{'file_path':key_filename})
            if fdu.user_file is not None and fdu.user_file != '':
                self.call_os_plugin_function('remove_file',{'file_path':data_filename})
            self.call_os_plugin_function('remove_file',{'file_path':vendor_filename})

            try:
                self.conn.defineXML(vm_xml)
            except libvirt.libvirtError as err:
                self.conn = libvirt.open('qemu:///system')
                self.conn.defineXML(vm_xml)

            fdu.on_configured(vm_xml)
            self.current_fdus.update({fdu_uuid: fdu})

            vm_info = self.connector.loc.actual.get_node_fdu(self.node, self.uuid, fdu_uuid)
            vm_info.update({'status': 'configured'})
            self.current_fdus.update({fdu_uuid: fdu})
            self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)
            self.logger.info('configure_fdu()', '[ DONE ] KVM Plugin - Configure a VM uuid {}'.format(fdu_uuid))


    def clean_fdu(self, fdu_uuid):

        self.logger.info('clean_fdu()', ' KVM Plugin - Clean a VM uuid {} '.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('clean_fdu()', 'KVM Plugin - FDU not exists')
            raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(fdu_uuid, self.uuid))
        elif fdu.get_state() != State.CONFIGURED:
            self.logger.error('clean_fdu()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
            raise StateTransitionNotAllowedException('FDU is not in CONFIGURED state', 'FDU {} is not in CONFIGURED state'.format(fdu_uuid))
        else:

            dom = self.__lookup_by_uuid(fdu_uuid)
            if dom is not None:
                dom.undefine()
            else:
                self.logger.error('clean_fdu()', 'KVM Plugin - Domain  not found!!')

            self.call_os_plugin_function('remove_file',{'file_path':fdu.cdrom})
            self.call_os_plugin_function('remove_file',{'file_path':fdu.disk})
            self.call_os_plugin_function('remove_file',{'file_path':os.path.join(self.BASE_DIR, self.LOG_DIR, fdu_uuid)})


            fdu.on_clean()
            self.current_fdus.update({fdu_uuid: fdu})
            vm_info = self.connector.loc.actual.get_node_fdu(self.node, self.uuid, fdu_uuid)
            vm_info.update({'status': 'defined'})
            # TODO: this should be an update when YAKS will implement update
            self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)
            self.logger.info('clean_fdu()', '[ DONE ] KVM Plugin - Clean a VM uuid {} '.format(fdu_uuid))


    def run_fdu(self, fdu_uuid):

        self.logger.info('run_entity()', 'KVM Plugin - Starting a VM uuid {}'.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('run_entity()', 'KVM Plugin - FDU not exists')
            raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(fdu_uuid, self.uuid))
        elif fdu.get_state() != State.CONFIGURED:
            self.logger.error('run_entity()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
            raise StateTransitionNotAllowedException('FDU is not in CONFIGURED state', 'FDU {} is not in CONFIGURED state'.format(fdu_uuid))
        else:
            vm_info = self.connector.loc.actual.get_node_fdu(self.node, self.uuid, fdu_uuid)
            vm_info.update({'status': 'starting'})
            self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)

            dom = self.__lookup_by_uuid(fdu_uuid)
            dom.create()
            while dom.state()[0] != 1:
               pass
            self.logger.info('run_entity()', ' KVM Plugin - VM {} Started!'.format(fdu))

            fdu.on_start()
            # log_filename = '{}/{}/{}_log.log'.format(self.BASE_DIR, self.LOG_DIR, instance_uuid)
            # if instance.user_file is not None and instance.user_file != '':
            #     self.__wait_boot(log_filename, True)
            # else:
            #     self.__wait_boot(log_filename)

            self.current_fdus.update({fdu_uuid: fdu})

            self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)
            vm_info.update({'status': 'run'})
            vm_info = self.connector.loc.actual.get_node_fdu(self.node, self.uuid, fdu_uuid)

            self.logger.info('run_entity()', '[ DONE ] KVM Plugin - Starting a VM uuid {}'.format(fdu_uuid))


    def stop_fdu(self, fdu_uuid):

        self.logger.info('stop_fdu()', ' KVM Plugin - Stop a VM uuid {}'.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('stop_fdu()', 'KVM Plugin - FDU not exists')
            raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(fdu_uuid, self.uuid))
        elif fdu.get_state() != State.RUNNING:
            self.logger.error('stop_fdu()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
            raise StateTransitionNotAllowedException('FDU is not in RUNNING state', 'FDU {} is not in RUNNING state'.format(fdu_uuid))
        else:

            dom = self.__lookup_by_uuid(fdu_uuid)
            dom.shutdown()
            retries = 100
            for i in range(0, retries):
                if dom.state()[0] != 5:
                    break
                else:
                    time.sleep(0.015)

            if dom.state()[0] != 5:
                dom.destroy()

            fdu.on_stop()
            self.current_fdus.update({fdu_uuid: fdu})

            vm_info = self.connector.loc.actual.get_node_fdu(self.node, self.uuid, fdu_uuid)
            vm_info.update({'status': 'stop'})
            self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)
            self.logger.info('stop_fdu()', '[ DONE ] KVM Plugin - Stop a VM uuid {}'.format(fdu_uuid))


    def pause_fdu(self, fdu_uuid):

        self.logger.info('pause_fdu()', ' KVM Plugin - Pause a VM uuid {}'.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('pause_fdu()', 'KVM Plugin - FDU not exists')
            raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(fdu_uuid, self.uuid))
        elif fdu.get_state() != State.RUNNING:
            self.logger.error('pause_fdu()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
            raise StateTransitionNotAllowedException('FDU is not in RUNNING state', 'FDU {} is not in RUNNING state'.format(fdu_uuid))
        else:
            self.__lookup_by_uuid(fdu_uuid).suspend()
            fdu.on_pause()
            self.current_fdus.update({fdu_uuid: fdu})
            vm_info = self.connector.loc.actual.get_node_fdu(self.node, self.uuid, fdu_uuid)
            vm_info.update({'status': 'pause'})
            self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)
            self.logger.info('pause_fdu()', '[ DONE ] KVM Plugin - Pause a VM uuid {}'.format(fdu_uuid))

    def resume_fdu(self, fdu_uuid):

        self.logger.info('resume_fdu()', ' KVM Plugin - Resume a VM uuid {}'.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('resume_fdu()', 'KVM Plugin - FDU not exists')
            raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(fdu_uuid, self.uuid))
        elif fdu.get_state() != State.PAUSED:
            self.logger.error('resume_fdu()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
            raise StateTransitionNotAllowedException('FDU is not in PAUSED state', 'FDU {} is not in PAUSED state'.format(fdu_uuid))
        else:
                    self.__lookup_by_uuid(fdu_uuid).resume()
                    fdu.on_resume()
                    self.current_fdus.update({fdu_uuid: fdu})
                    vm_info = self.connector.loc.actual.get_node_fdu(self.node, self.uuid, fdu_uuid)
                    vm_info.update({'status': 'run'})
                    self.connector.loc.actual.add_node_fdu(self.node, self.uuid, fdu_uuid, vm_info)
                    self.logger.info('resume_fdu()', '[ DONE ] KVM Plugin - Resume a VM uuid {}'.format(fdu_uuid))
                    return True

    # TODO rethink the migration workflow to be faster, copy the disk first and copy the base image only when migration ended
    def migrate_fdu(self, fdu_uuid, dst=False):
        # if type(entity_uuid) == dict:
        #     entity_uuid = entity_uuid.get('entity_uuid')
        # self.logger.info('migrate_entity()', ' KVM Plugin - Migrate a VM uuid {}'.format(entity_uuid))
        # entity = self.current_fdus.get(entity_uuid, None)
        # if entity is None or entity.get_instance(instance_uuid) is None:

        #     '''
        #     How migration works:

        #     Issue the migration by writing on the store of source and destination the correct states and set dst with uuid for destination node:
        #         source: migrating | destination: migrating

        #     ## BEFORE MIGRATING

        #     The source node send to the destination node the flavor, the image and the entity

        #     When flavor and image are defined the destination node create the disks and change status to LANDING
        #     The source node change status to TAKING_OFF

        #     ## MIGRATING

        #     Actual migration using libvirt API

        #     Destination node wait the VM to be defined and active on KVM
        #     Source Node issue the migration from libvirt

        #     ## AFTER MIGRATING


        #     Source node destroy all information about entity instance (so the entity remains defined, and flavor and image remains in the node)

        #     Destination node update status in RUNNING


        #     '''
        #     if dst is True:

        #         self.logger.info('migrate_entity()', ' KVM Plugin - I\'m the Destination Node')
        #         self.before_migrate_entity_actions(entity_uuid, True, instance_uuid)

        #         while True:  # wait for migration to be finished
        #             dom = self.__lookup_by_uuid(instance_uuid)
        #             if dom is None:
        #                 self.logger.info('migrate_entity()', ' KVM Plugin - Domain not already in this host')
        #             else:
        #                 if dom.isActive() == 1:
        #                     break
        #                 else:
        #                     self.logger.info('migrate_entity()', ' KVM Plugin - Domain in this host but not running')
        #             time.sleep(5)

        #         self.after_migrate_entity_actions(entity_uuid, True, instance_uuid)
        #         self.logger.info('migrate_entity()', '[ DONE ] KVM Plugin - Migrate a VM uuid {}'.format(entity_uuid))
        #         return True

        #     else:
        #         self.logger.error('migrate_entity()', 'KVM Plugin - FDU not exists')
        #         self.__write_error_entity(entity_uuid, 'FDU not exist')
        #         raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(entity_uuid, self.uuid))
        # elif entity.get_state() != State.DEFINED:
        #     self.logger.error('migrate_entity()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
        #     self.__write_error_entity(entity_uuid, 'FDU state transition not allowed')
        #     raise StateTransitionNotAllowedException('FDU is not in DEFINED state', 'FDU {} is not in DEFINED state'.format(entity_uuid))
        # else:
        #     instance = entity.get_instance(instance_uuid)
        #     if instance.get_state() not in [State.RUNNING, State.TAKING_OFF]:
        #         self.logger.error('clean_fdu()', 'KVM Plugin - Instance state is wrong, or transition not allowed')
        #         self.__write_error_instance(entity_uuid, instance_uuid, 'FDU Instance not exist')
        #         raise StateTransitionNotAllowedException('Instance is not in RUNNING state', 'Instance {} is not in RUNNING state'.format(entity_uuid))

        #     self.logger.info('migrate_entity()', ' KVM Plugin - I\'m the Source Node')
        #     res = self.before_migrate_entity_actions(entity_uuid, instance_uuid=instance_uuid)
        #     if not res:
        #         self.logger.error('migrate_entity()', ' KVM Plugin - Error source node before migration, aborting')
        #         self.__write_error_instance(entity_uuid, instance_uuid, 'FDU Instance migration error on source')
        #         return

        #     #### MIGRATION

        #     uri_instance = '{}/{}/{}/{}/{}'.format(self.agent.dhome, self.HOME_ENTITY, entity_uuid, self.INSTANCE, instance_uuid)
        #     instance_info = json.loads(self.agent.dstore.get(uri_instance))
        #     name = instance_info.get('entity_data').get('name')
        #     # destination node uuid
        #     destination_node_uuid = instance_info.get('dst')
        #     uri = '{}/{}'.format(self.agent.aroot, destination_node_uuid)

        #     while True:
        #         dst_node_info = self.agent.astore.get(uri)  # TODO: solve this ASAP
        #         if dst_node_info is not None:
        #             if isinstance(dst_node_info, tuple):
        #                 dst_node_info = dst_node_info[0]
        #             dst_node_info = dst_node_info.replace("'", '"')
        #             break
        #     # print(dst_node_info)
        #     dst_node_info = json.loads(dst_node_info)
        #     ## json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)
        #     # dst_node_info = json.loads(self.agent.astore.get(uri)[0])
        #     ##
        #     dom = self.__lookup_by_uuid(instance_uuid)
        #     nw = dst_node_info.get('network')

        #     dst_hostname = dst_node_info.get('name')

        #     dst_ip = [x for x in nw if x.get('default_gw') is True]
        #     # TODO: or x.get('inft_configuration').get('ipv6_gateway') for ip_v6
        #     if len(dst_ip) == 0:
        #         return False

        #     dst_ip = dst_ip[0].get('inft_configuration').get('ipv4_address')  # TODO: as on search should use ipv6

        #     # ## ADDING TO /etc/hosts otherwise migration can fail
        #     self.agent.get_os_plugin().add_know_host(dst_hostname, dst_ip)
        #     ###

        #     # ## ACTUAL MIGRATIION ##################
        #     dst_host = 'qemu+ssh://{}/system'.format(dst_ip)
        #     dest_conn = libvirt.open(dst_host)
        #     if dest_conn is None:
        #         self.logger.error('before_migrate_entity_actions()', 'KVM Plugin - Before Migration Source: Error on libvirt connection')
        #         self.__write_error_instance(entity_uuid, instance_uuid, 'Source Error on libvirt connection')
        #         return
        #     flags = libvirt.VIR_MIGRATE_LIVE | libvirt.VIR_MIGRATE_PERSIST_DEST
        #     new_dom = dom.migrate(dest_conn, flags, name, None, 0)
        #     # new_dom = dom.migrate(dest_conn, libvirt.VIR_MIGRATE_LIVE and libvirt.VIR_MIGRATE_PERSIST_DEST and libvirt.VIR_MIGRATE_NON_SHARED_DISK, name, None, 0)
        #     if new_dom is None:
        #         self.logger.error('before_migrate_entity_actions()', 'KVM Plugin - Before Migration Source: Migration failed')
        #         self.__write_error_instance(entity_uuid, instance_uuid, 'Source Error Migration failed')
        #         return
        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Before Migration Source: Migration succeeds')
        #     dest_conn.close()
        #     # #######################################

        #     # ## REMOVING AFTER MIGRATION
        #     self.agent.get_os_plugin().remove_know_host(dst_hostname)
        #     instance.on_stop()
        #     self.current_fdus.update({entity_uuid: entity})

        #     ####

        #     res = self.after_migrate_entity_actions(entity_uuid, instance_uuid=instance_uuid)
        #     if not res:
        #         self.logger.error('migrate_entity()', ' KVM Plugin - Error source node after migration, aborting')
        #         return
        pass

    def before_migrate_fdu_actions(self, fdu_uuid, dst=False):
        # if dst is True:

        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Before Migration Destination: Create Domain and destination files')
        #     uri = '{}/{}/{}/{}/{}'.format(self.agent.dhome, self.HOME_ENTITY, entity_uuid, self.INSTANCE, instance_uuid)
        #     instance_info = json.loads(self.agent.dstore.get(uri))
        #     vm_info = instance_info.get('entity_data')

        #     # waiting flavor
        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Waiting flavor')
        #     while True:
        #         flavor_id = vm_info.get('flavor_id')
        #         if flavor_id in self.flavors.keys():
        #             break

        #     # waiting image
        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Waiting image')
        #     while True:
        #         base_image = vm_info.get('base_image')
        #         if base_image in self.images.keys():
        #             break

        #     # waiting entity
        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Waiting entity')
        #     while True:
        #         if entity_uuid in self.current_fdus.keys():
        #             break
        #     self.logger.info('before_migrate_entity_actions()', ' FDU {} defined!!!'.format(entity_uuid))

        #     img_info = self.images.get(base_image)
        #     flavor_info = self.flavors.get(flavor_id)
        #     entity = self.current_fdus.get(entity_uuid)

        #     name = vm_info.get('name')
        #     disk_path = '{}.{}'.format(instance_uuid, img_info.get('format'))
        #     cdrom_path = '{}_config.iso'.format(instance_uuid)
        #     disk_path = os.path.join(self.BASE_DIR, self.DISK_DIR, disk_path)
        #     cdrom_path = os.path.join(self.BASE_DIR, self.DISK_DIR, cdrom_path)

        #     instance = KVMLibvirtFDUInstance(instance_uuid, name, disk_path, cdrom_path, entity.networks, entity.user_file,
        #                                         entity.ssh_key, entity_uuid, flavor_info.get('uuid'), img_info.get('uuid'))

        #     instance.state = State.LANDING
        #     vm_info.update({'name': name})
        #     vm_xml = self.__generate_dom_xml(instance, flavor_info, img_info)

        #     instance.xml = vm_xml
        #     qemu_cmd = 'qemu-img create -f {} {} {}G'.format(img_info.get('format'), instance.disk, flavor_info.get('disk_size'))
        #     self.agent.get_os_plugin().execute_command(qemu_cmd, True)
        #     self.agent.get_os_plugin().create_file(instance.cdrom)
        #     self.agent.get_os_plugin().create_file(os.path.join(self.BASE_DIR, self.LOG_DIR, '{}_log.log'.format(instance_uuid)))

        #     conf_cmd = '{} --hostname {} --uuid {}'.format(os.path.join(self.DIR, 'templates',
        #                                                                 'create_config_drive.sh'), instance.name, instance_uuid)
        #     rm_temp_cmd = 'rm'
        #     if instance.user_file is not None and instance.user_file != '':
        #         data_filename = 'userdata_{}'.format(instance_uuid)
        #         self.agent.get_os_plugin().store_file(instance.user_file, self.BASE_DIR, data_filename)
        #         data_filename = os.path.join(self.BASE_DIR, data_filename)
        #         conf_cmd = conf_cmd + ' --user-data {}'.format(data_filename)
        #         # rm_temp_cmd = rm_temp_cmd + ' {}'.format(data_filename)
        #     if instance.ssh_key is not None and instance.ssh_key != '':
        #         key_filename = 'key_{}.pub'.format(instance_uuid)
        #         self.agent.get_os_plugin().store_file(instance.ssh_key, self.BASE_DIR, key_filename)
        #         key_filename = os.path.join(self.BASE_DIR, key_filename)
        #         conf_cmd = conf_cmd + ' --ssh-key {}'.format(key_filename)
        #         # rm_temp_cmd = rm_temp_cmd + ' {}'.format(key_filename)

        #     conf_cmd = conf_cmd + ' {}'.format(instance.cdrom)

        #     self.agent.get_os_plugin().execute_command(conf_cmd, True)

        #     instance_info.update({'entity_data': vm_info})
        #     instance_info.update({'status': 'landing'})

        #     entity.add_instance(instance)
        #     self.current_fdus.update({entity_uuid: entity})

        #     self.__update_actual_store_instance(entity_uuid, instance_uuid, instance_info)

        #     return True
        # else:
        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Before Migration Source: get information about destination node')


        #     local_var = MVar()
        #     def cb(key, value, v):
        #         local_var.put(value)

        #     entity = self.current_fdus.get(entity_uuid, None)
        #     instance = entity.get_instance(instance_uuid)

        #     # reading entity info
        #     uri_entity = '{}/{}/{}'.format(self.agent.ahome, self.HOME_ENTITY, entity_uuid)
        #     entity_info = json.loads(self.agent.astore.get(uri_entity))
        #     entity_info.update({'status': 'define'})

        #     # reading instance info
        #     uri_instance = '{}/{}/{}/{}/{}'.format(self.agent.dhome, self.HOME_ENTITY, entity_uuid, self.INSTANCE, instance_uuid)
        #     instance_info = json.loads(self.agent.dstore.get(uri_instance))
        #     vm_info = instance_info.get('entity_data')
        #     # destination node uuid
        #     destination_node_uuid = instance_info.get('dst')

        #     # flavor and image information
        #     flavor_info = self.flavors.get(vm_info.get('flavor_id'))
        #     img_info = self.images.get(vm_info.get('base_image'))

        #     # getting same plugin in destination node
        #     uri = '{}/{}/plugins'.format(self.agent.aroot, destination_node_uuid)
        #     all_plugins = json.loads(self.agent.astore.get(uri)).get('plugins')  # TODO: solve this ASAP

        #     runtimes = [x for x in all_plugins if x.get('type') == 'runtime']
        #     search = [x for x in runtimes if 'KVMLibvirt' in x.get('name')]
        #     if len(search) == 0:
        #         self.logger.error('before_migrate_entity_actions()', 'KVM Plugin - Before Migration Source: No KVM Plugin, Aborting!!!')
        #         self.__write_error_instance(entity_uuid, instance_uuid, 'FDU Instance Migration error')
        #         return False
        #     else:
        #         kvm_uuid = search[0].get('uuid')

        #     self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - check if flavor is present on destination')
        #     uri_flavor = '{}/{}/runtime/{}/flavor/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, flavor_info.get('uuid'))
        #     if self.agent.astore.get(uri_flavor) is None:
        #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - sending flavor to destination')
        #         uri_flavor = '{}/{}/runtime/{}/flavor/{}'.format(self.agent.droot, destination_node_uuid, kvm_uuid, flavor_info.get('uuid'))
        #         self.agent.dstore.put(uri_flavor, json.dumps(flavor_info))
        #     # wait to be defined flavor
        #     # self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - waiting flavor in destination')
        #     # while True:
        #     #     time.sleep(0.1)
        #     #     uri_flavor = '{}/{}/runtime/{}/flavor/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, flavor_info.get('uuid'))
        #     #     f_i = self.agent.astore.get(uri_flavor)
        #     #     print('{}'.format(f_i))
        #     #     if f_i is not None:
        #     #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - Flavor in destination!')
        #     #         break

        #     self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - check if image is present on destination')
        #     uri_img = '{}/{}/runtime/{}/image/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, img_info.get('uuid'))
        #     if self.agent.astore.get(uri_img) is None:
        #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - sending image to destination')
        #         uri_img = '{}/{}/runtime/{}/image/{}'.format(self.agent.droot, destination_node_uuid, kvm_uuid, img_info.get('uuid'))
        #         self.agent.dstore.put(uri_img, json.dumps(img_info))

        #     # wait to be defined image
        #     # self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - Waiting image in destination')
        #     # while True:
        #     #     time.sleep(0.1)
        #     #     uri_img = '{}/{}/runtime/{}/image/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, img_info.get('uuid'))
        #     #     i_i = self.agent.astore.get(uri_img)
        #     #     if i_i is not None:
        #     #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - Image in destination!')
        #     #         break

        #     # send entity definition

        #     # uri = '{}/{}/runtime/{}/entity/*'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, entity_uuid, instance_uuid)
        #     # self.agent.astore.observe(uri, self.dummy_observer)
        #     # import colorama
        #     # colorama.init()
        #     # print(colorama.Fore.RED + '>>>>>> Registered observer for {} <<<<<<< '.format(uri) + colorama.Style.RESET_ALL)

        #     self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - check if image is present on destination')
        #     uri_entity = '{}/{}/runtime/{}/entity/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, entity_uuid)
        #     if self.agent.astore.get(uri_entity) is None:
        #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - sending entity to destination')
        #         uri_entity = '{}/{}/runtime/{}/entity/{}'.format(self.agent.droot, destination_node_uuid, kvm_uuid, entity_uuid)
        #         self.agent.dstore.put(uri_entity, json.dumps(entity_info))
        #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - Waiting entity in destination')


        #         uri_entity = '{}/{}/runtime/{}/entity/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, entity_uuid)
        #         subid = self.agent.astore.observe(uri_entity, cb)
        #         entity_info = json.loads(local_var.get())
        #         es = entity_info.get('status')
        #         while es not in ['defined','error']:
        #             entity_info = json.loads(local_var.get())
        #             es = entity_info.get('status')
        #         self.agent.astore.overlook(subid)

        #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - FDU in destination!')
        #         # while True:
        #         #     uri_entity = '{}/{}/runtime/{}/entity/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, entity_uuid)
        #         #     jdata = self.agent.astore.get(uri_entity)
        #         #     if jdata is not None:
        #         #         self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - FDU in destination!')
        #         #         entity_info = json.loads(jdata)
        #         #         if entity_info is not None and entity_info.get('status') == 'defined':
        #         #             break

        #         # waiting for destination node to be ready
        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Before Migration Source: Waiting destination to be ready')
        #     uri = '{}/{}/runtime/{}/entity/{}/instance/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, entity_uuid, instance_uuid)
        #     subid = self.agent.astore.observe(uri, cb)
        #     self.logger.info('before_migrate_entity_actions()', 'KVM Plugin - FDU in destination!')
        #     entity_info = json.loads(local_var.get())
        #     es = entity_info.get('status')
        #     while es not in ['landing','error']:
        #         entity_info = json.loads(local_var.get())
        #         es = entity_info.get('status')
        #     self.agent.astore.overlook(subid)
        #     # while True:
        #     #     # self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Before Migration Source: Waiting destination to be ready')
        #     #     uri = '{}/{}/runtime/{}/entity/{}/instance/{}'.format(self.agent.aroot, destination_node_uuid, kvm_uuid, entity_uuid, instance_uuid)
        #     #     vm_info = self.agent.astore.get(uri)
        #     #     if vm_info is not None:
        #     #         vm_info = json.loads(vm_info)
        #     #         if vm_info is not None and vm_info.get('status') == 'landing':
        #     #             break
        #     self.logger.info('before_migrate_entity_actions()', ' KVM Plugin - Before Migration Source: Destination is ready!')

        #     instance.state = State.TAKING_OFF
        #     instance_info.update({'status': 'taking_off'})
        #     self.__update_actual_store_instance(entity_uuid, instance_uuid, instance_info)
        #     self.current_fdus.update({entity_uuid: entity})
        #     return True
        pass

    def after_migrate_fdu_actions(self, fdu_uuid, dst=False):
        # if type(entity_uuid) == dict:
        #     entity_uuid = entity_uuid.get('entity_uuid')
        # entity = self.current_fdus.get(entity_uuid, None)
        # if entity is None:
        #     self.logger.error('after_migrate_entity_actions()', 'KVM Plugin - FDU not exists')
        #     self.__write_error_entity(entity_uuid, 'FDU not exist')
        #     raise FDUNotExistingException('FDU not existing', 'FDU {} not in runtime {}'.format(entity_uuid, self.uuid))
        # elif entity.get_state() != State.DEFINED:
        #     self.logger.error('after_migrate_entity_actions()', 'KVM Plugin - FDU state is wrong, or transition not allowed')
        #     self.__write_error_entity(entity_uuid, 'FDU state transition not allowed')
        #     raise StateTransitionNotAllowedException('FDU is not in correct state', 'FDU {} is not in correct state'.format(entity.get_state()))
        # else:
        #     if dst is True:

        #         instance = entity.get_instance(instance_uuid)
        #         '''
        #         Here the plugin also update to the current status, and remove unused keys
        #         '''
        #         self.logger.info('after_migrate_entity_actions()', ' KVM Plugin - After Migration Destination: Updating state')
        #         instance.on_start()
        #         self.current_fdus.update({entity_uuid: entity})

        #         uri = '{}/{}/{}/{}/{}'.format(self.agent.dhome, self.HOME_ENTITY, entity_uuid, self.INSTANCE, instance_uuid)
        #         vm_info = json.loads(self.agent.dstore.get(uri))
        #         vm_info.pop('dst')
        #         vm_info.update({'status': 'run'})

        #         self.__update_actual_store_instance(entity_uuid, instance_uuid, vm_info)
        #         self.current_fdus.update({entity_uuid: entity})

        #         return True
        #     else:
        #         '''
        #         Source node destroys all information about vm
        #         '''
        #         self.logger.info('after_migrate_entity_actions()', ' KVM Plugin - After Migration Source: Updating state, destroy vm')
        #         self.__force_entity_instance_termination(entity_uuid, instance_uuid)
        #         return True
        pass

    def __add_image(self, manifest):
        url = manifest.get('base_image')
        img_uuid = manifest.get('uuid')
        if url.startswith('http'):
            image_name = os.path.join(self.BASE_DIR, self.IMAGE_DIR, url.split('/')[-1])
            self.call_os_plugin_function('download_file',{'url':url,'file_path':image_name})
        elif url.startswith('file://'):
            image_name = os.path.join(self.BASE_DIR, self.IMAGE_DIR, url.split('/')[-1])
            cmd = 'cp {} {}'.format(url[len('file://'):], image_name)
            self.call_os_plugin_function('execute_command',{'command':cmd,'blocking':True, 'external':False})
        manifest.update({'path': image_name})
        self.images.update({img_uuid: manifest})
        self.connector.loc.actual.add_node_image(self.node, self.uuid, img_uuid, manifest)


    def __remove_image(self, image_uuid):
        self.lock.acquire()
        image = self.images.get(image_uuid, None)
        if image is None:
            self.logger.info('__remove_image()', ' KVM Plugin - Image not found!!')
            return
        self.call_os_plugin_function('remove_file',{'file_path':image.get('path')})
        self.images.pop(image_uuid)
        self.connector.loc.actual.remove_node_image(self.node, self.uuid,image_uuid)
        self.lock.release()

    def __add_flavor(self, manifest):
        # self.lock.acquire()
        fl_uuid  = manifest.get('uuid')
        self.flavors.update({fl_uuid: manifest})
        self.connector.loc.actual.add_node_flavor(self.node, self.uuid, fl_uuid, manifest)

        # self.lock.release()

    def __remove_flavor(self, flavor_uuid):
        self.flavors.pop(flavor_uuid)
        self.connector.loc.actual.remove_node_flavor(self.node, self.uuid,flavor_uuid)


    def __random_mac_generator(self):
        mac = [0x00, 0x16, 0x3e,
               random.randint(0x00, 0x7f),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff)]
        return ':'.join(map(lambda x: '%02x' % x, mac))

    def __lookup_by_uuid(self, uuid):
        try:
            domains = self.conn.listAllDomains(0)
        except libvirt.libvirtError as err:
            self.conn = libvirt.open('qemu:///system')
            domains = self.conn.listAllDomains(0)

        if len(domains) != 0:
            for domain in domains:
                if str(uuid) == domain.UUIDString():
                    return domain
        else:
            return None

    def __wait_boot(self, filename, configured=False):
        time.sleep(5)
        if configured:
            boot_regex = r"\[.+?\].+\[.+?\]:.+Cloud-init.+?v..+running.+'modules:final'.+Up.([0-9]*\.?[0-9]+).+seconds.\n"
        else:
            boot_regex = r".+?login:()"
        while True:
            file = open(filename, 'r')
            import os
            # Find the size of the file and move to the end
            st_results = os.stat(filename)
            st_size = st_results[6]
            file.seek(st_size)

            while 1:
                where = file.tell()
                line = file.readline()
                if not line:
                    time.sleep(1)
                    file.seek(where)
                else:
                    m = re.search(boot_regex, str(line))
                    if m:
                        found = m.group(1)
                        return found

    def __force_fdu_termination(self, fdu_uuid):
        self.logger.info('stop_fdu()', ' LXD Plugin - Stop a container uuid {}'.format(fdu_uuid))
        fdu = self.current_fdus.get(fdu_uuid, None)
        if fdu is None:
            self.logger.error('stop_fdu()', 'LXD Plugin - FDU not exists')
        else:
            if fdu.get_state() == State.PAUSED:
                self.resume_fdu(fdu_uuid)
                self.stop_fdu(fdu_uuid)
                self.clean_fdu(fdu_uuid)
                self.undefine_fdu(fdu_uuid)
            if fdu.get_state() == State.RUNNING:
                self.stop_fdu(fdu_uuid)
                self.clean_fdu(fdu_uuid)
                self.undefine_fdu(fdu_uuid)
            if fdu.get_state() == State.CONFIGURED:
                self.clean_fdu(fdu_uuid)
                self.undefine_fdu(fdu_uuid)
            if fdu.get_state() == State.DEFINED:
                self.undefine_fdu(fdu_uuid)

    def __generate_dom_xml(self, fdu, flavor, image):
        template_xml = self.call_os_plugin_function('read_file',{'file_path':os.path.join(self.DIR, 'templates', 'vm.xml'), 'root':False})
        vm_xml = Environment().from_string(template_xml)
        vm_xml = vm_xml.render(name=fdu.name, uuid=fdu.uuid, memory=flavor.get('memory'),
                               cpu=flavor.get('cpu'), disk_image=fdu.disk,
                               iso_image=fdu.cdrom, networks=fdu.networks, format=image.get('format'))
        return vm_xml

    def __generate_vendor_data(self, entityid, nodeid):
        vendor_yaml = self.call_os_plugin_function('read_file',{'file_path':os.path.join(self.DIR, 'templates', 'vendor_data.yaml'), 'root':False})
        vendor_conf = Environment().from_string(vendor_yaml)
        vendor_conf = vendor_conf.render(nodeid=nodeid, entityid=entityid)
        return vendor_conf


    def __netmask_to_cidr(self, netmask):
        return sum([bin(int(x)).count('1') for x in netmask.split('.')])


    def __fdu_observer(self, fdu_info):
        self.logger.info('__fdu_observer()', ' Native Plugin - New Action of a FDU - FDU Info: {}'.format(fdu_info))
        action = fdu_info.get('status')
        fdu_uuid = fdu_info.get('uuid')
        react_func = self.__react(action)
        if action == 'undefine':
            self.logger.info('__fdu_observer()', ' Native Plugin - This is a remove for : {}'.format(fdu_info))
            self.undefine_fdu(fdu_uuid)
        elif action == 'define':
            self.logger.info('__fdu_observer()', ' Native Plugin - This is a define for : {}'.format(fdu_info))
            self.define_fdu(fdu_info)
        elif react_func is not None:
            react_func(fdu_uuid)
        else:
            self.logger.info('__fdu_observer()', ' Native Plugin - Action not recognized : {}'.format(action))


    def __react(self, action):
        r = {
            'configure': self.configure_fdu,
            'stop': self.stop_fdu,
            'resume': self.resume_fdu,
            'run': self.run_fdu,
            'clean': self.clean_fdu
            # 'landing': self.migrate_entity,
            # 'taking_off': self.migrate_entity
        }
        return r.get(action, None)


def read_file(file_path):
    data = ''
    with open(file_path, 'r') as f:
        data = f.read()
    return data


if __name__ == '__main__':
    if len(sys.argv) < 3:
        exit(-1)
    yaksip = sys.argv[1]
    nodeid = sys.argv[2]
    print('ARGS {}'.format(sys.argv))
    file_dir = os.path.dirname(__file__)
    manifest = json.loads(
        read_file(os.path.join(file_dir, 'KVM_plugin.json')))
    vm = KVM(manifest.get('name'), manifest.get('version'), manifest.get(
        'uuid'), yaksip, nodeid, manifest)
    vm.start_runtime()